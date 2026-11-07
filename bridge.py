#!/usr/bin/env python3
"""Echo Hermes Bridge: talk to a Hermes Agent profile through an Echo running echo-dot-assist, without
Home Assistant.

The Echo (echo-dot-assist) is an ESPHome voice satellite. Normally Home Assistant connects to it; this
script takes Home Assistant's place on the ESPHome native API (same library Home Assistant uses,
aioesphomeapi):

    Echo: wake word + your request (16 kHz audio)
      -> this bridge: end-of-speech detection, faster-whisper (speech to text)
      -> Hermes /v1/chat/completions with X-Hermes-Session-Id (Hermes keeps the history)
      -> Piper (text to speech) -> WAV served over HTTP by this bridge
      -> the Echo downloads it and plays it

It also feeds TV screens: /screen/ is a live full-screen page of the conversation (open it in any TV
browser), /events streams the same thing (Server-Sent Events), and /talk + /ask let the Android TV app
(android-tv/) talk to Hermes with the remote's microphone. /talk and /ask need TV_TOKEN.

Only one client may hold the Echo's voice assistant: do not run Home Assistant against the same Echo.
"""
from __future__ import annotations

import argparse
import asyncio
import hmac
import io
import json
import logging
import os
import re
import socket
import struct
import time
import uuid
import wave

import aiohttp
from aiohttp import web
from aioesphomeapi import APIClient, ReconnectLogic, VoiceAssistantEventType as Ev

from cleanup import clean_for_voice

log = logging.getLogger("hermes-echo")

HERE = os.path.dirname(os.path.abspath(__file__))
MIC_RATE = 16000                        # what the Echo streams: 16 kHz, 16 bit, mono
BYTES_PER_SEC = MIC_RATE * 2

# Per language: (voice instruction sent to Hermes, fallback spoken when Hermes fails). Others fall back to English.
DEFAULTS = {
    "en": ("You are heard through a voice speaker: answer in one to three short spoken sentences, "
           "without lists, markdown, links or emoji.",
           "I can't reach Hermes right now, try again in a minute."),
    "fr": ("Tu es entendu à travers une enceinte vocale : réponds en une à trois phrases courtes, "
           "faites pour être dites à voix haute, sans listes, markdown, liens ni emoji.",
           "Je n'arrive pas à joindre Hermès pour le moment, réessaie dans une minute."),
}


# ----------------------------------------------------------------------------------------------- config
def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def load_dotenv(path: str) -> None:
    """KEY=VALUE lines, no dependency needed; real environment variables win."""
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if not v.startswith(("'", '"')):
            v = re.split(r"\s+#", v, maxsplit=1)[0]   # inline comment: VALUE   # note
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def local_ip_towards(host: str | None) -> str:
    """The address of this machine that the Echo can reach (no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host or "192.0.2.1", 9))           # no Echo: the default route's address
        return s.getsockname()[0]
    finally:
        s.close()


def wav_seconds(wav: bytes) -> float:
    with wave.open(io.BytesIO(wav)) as w:
        return w.getnframes() / float(w.getframerate() or 1)


def pcm_from_upload(body: bytes) -> bytes:
    """Raw 16 kHz s16 mono PCM, or a WAV file (any rate, 16 bit) converted to that."""
    if body[:4] != b"RIFF":
        return body[: len(body) // 2 * 2]
    import numpy as np
    with wave.open(io.BytesIO(body)) as w:
        if w.getsampwidth() != 2:
            raise ValueError("WAV must be 16 bit")
        rate, ch = w.getframerate(), w.getnchannels()
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    a = a.reshape(-1, ch)[:, 0].astype(np.float32)
    if rate != MIC_RATE and len(a):
        a = np.interp(np.arange(0, len(a), rate / MIC_RATE), np.arange(len(a)), a)
    return a.astype(np.int16).tobytes()


# ------------------------------------------------------------------------------------------- screens
class Screen:
    """What TV screens show: the live state of the conversation and the last few turns.

    Events (JSON, one per Server-Sent Event): listening, thinking (heard), reply (heard, text, audio,
    duration), idle, error (message). Each carries the turn `id` and its `source` (echo or tv).
    """

    def __init__(self, keep: int = 12):
        self.subs: set[asyncio.Queue] = set()
        self.history: list[dict] = []
        self.keep = keep
        self.turn: str | None = None

    def new_turn(self) -> str:
        self.turn = uuid.uuid4().hex[:8]
        return self.turn

    def emit(self, kind: str, **data) -> None:
        ev = {"type": kind, "ts": round(time.time(), 3), **data}
        if kind == "reply":
            self.history.append({k: ev.get(k) for k in ("id", "source", "heard", "text", "ts")})
            del self.history[:-self.keep]
        for q in list(self.subs):
            if q.qsize() < 200:
                q.put_nowait(ev)

    def idle_later(self, turn: str, delay: float, source: str) -> None:
        """Back to idle once the reply has been spoken, unless another turn started meanwhile."""
        def done():
            if self.turn == turn:
                self.emit("idle", id=turn, source=source)
        asyncio.get_running_loop().call_later(max(delay, 0.5), done)


# ------------------------------------------------------------------------------------- end of speech
class EndOfSpeech:
    """Energy-based end-of-speech detection on 16 kHz s16 audio.

    Calibrates on the first 0.3 s after the Echo's lead-in silence, then waits for speech and ends
    `silence_s` after the last loud frame. Gives up after `max_s`. Simple and dependency-free; the
    Echo's own "End of request" setting (settings page) is better and also supported: when it is
    on, the Echo ends the request itself.
    """

    FRAME = 640  # 20 ms

    def __init__(self, silence_s: float = 0.8, max_s: float = 12.0, no_speech_s: float = 5.0):
        self.silence_s, self.max_s, self.no_speech_s = silence_s, max_s, no_speech_s
        self.buf = bytearray()
        self._pos = 0
        self._noise: list[float] = []
        self._floor = None
        self._speech_started = False
        self._last_loud = 0.0

    @staticmethod
    def _rms(frame: bytes) -> float:
        n = len(frame) // 2
        if not n:
            return 0.0
        vals = struct.unpack(f"<{n}h", frame[: n * 2])
        return (sum(v * v for v in vals) / n) ** 0.5

    def feed(self, data: bytes) -> bool:
        """Add audio; True once the request is over."""
        self.buf.extend(data)
        while self._pos + self.FRAME <= len(self.buf):
            frame = bytes(self.buf[self._pos:self._pos + self.FRAME])
            self._pos += self.FRAME
            t = self._pos / BYTES_PER_SEC
            rms = self._rms(frame)
            if rms == 0.0 and not self._speech_started:
                continue                                    # the Echo's 800 ms digital-silence lead-in
            if self._floor is None:
                self._noise.append(rms)
                if len(self._noise) >= 15:                  # 0.3 s of room noise
                    self._floor = sorted(self._noise)[len(self._noise) // 2]
                continue
            threshold = max(self._floor * 3.0, 300.0)
            if rms > threshold:
                self._speech_started = True
                self._last_loud = t
            if self._speech_started and t - self._last_loud >= self.silence_s:
                return True
            if not self._speech_started and t >= self.no_speech_s:
                return True
            if t >= self.max_s:
                return True
        return False


# ------------------------------------------------------------------------------------------- engines
class SpeechToText:
    def __init__(self, model: str, language: str, device: str, compute_type: str):
        from faster_whisper import WhisperModel
        log.info("loading Whisper model %s (first run downloads it)", model)
        self.model = WhisperModel(model, device=device, compute_type=compute_type)
        self.language = language

    def transcribe(self, pcm: bytes) -> str:
        import numpy as np
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        segments, _ = self.model.transcribe(audio, language=self.language, beam_size=1, vad_filter=True)
        return " ".join(s.text.strip() for s in segments).strip()


class TextToSpeech:
    def __init__(self, voice_path: str):
        from piper import PiperVoice
        log.info("loading Piper voice %s", voice_path)
        self.voice = PiperVoice.load(voice_path)

    def wav(self, text: str) -> bytes:
        b = io.BytesIO()
        with wave.open(b, "wb") as w:
            if hasattr(self.voice, "synthesize_wav"):       # piper-tts >= 1.3
                self.voice.synthesize_wav(text, w)
            else:                                           # older piper-tts
                self.voice.synthesize(text, w)
        return b.getvalue()


class Hermes:
    """Hermes Agent's OpenAI-compatible API server, one profile per URL."""

    def __init__(self, http: aiohttp.ClientSession, base_url: str, api_key: str, model: str | None,
                 instruction: str, timeout: float):
        base_url = base_url.strip().rstrip("/")
        self.base = base_url if base_url.endswith("/v1") else base_url + "/v1"
        self.http, self.key, self.model = http, api_key, model
        self.instruction, self.timeout = instruction, timeout

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key}"}

    async def resolve_model(self) -> str:
        if not self.model:
            async with self.http.get(f"{self.base}/models", headers=self.headers,
                                     timeout=aiohttp.ClientTimeout(total=10)) as r:
                r.raise_for_status()
                data = await r.json(content_type=None)
            self.model = data["data"][0]["id"]
        return self.model

    async def ask(self, text: str, session_id: str) -> str:
        body = {"model": await self.resolve_model(), "stream": False,
                "messages": [{"role": "system", "content": self.instruction},
                             {"role": "user", "content": text}]}
        async with self.http.post(f"{self.base}/chat/completions", json=body,
                                  headers={**self.headers, "X-Hermes-Session-Id": session_id},
                                  timeout=aiohttp.ClientTimeout(total=self.timeout)) as r:
            data = await r.json(content_type=None)
            if r.status != 200:
                raise RuntimeError(f"Hermes HTTP {r.status}: {data}")
            if r.headers.get("X-Hermes-Completed", "").lower() == "false":
                raise RuntimeError(f"Hermes run incomplete: {r.headers.get('X-Hermes-Error')}")
        choice = (data.get("choices") or [{}])[0]
        if choice.get("finish_reason") == "error" or (data.get("hermes") or {}).get("failed"):
            raise RuntimeError(f"Hermes run failed: {data}")
        reply = ((choice.get("message") or {}).get("content") or "").strip()
        low = reply.lower()
        if not reply or low.startswith("provider rate-limited") or low.startswith("provider authentication failed"):
            raise RuntimeError(f"Hermes gave no usable answer: {reply[:120]!r}")
        return reply


# -------------------------------------------------------------------------------------------- bridge
class Sessions:
    """One Hermes session per idle window: a new one after `idle_min` minutes without a request."""

    def __init__(self, idle_min: float):
        self.idle = idle_min * 60
        self.sid, self.last = None, 0.0

    def current(self) -> str:
        now = time.time()
        if self.sid is None or now - self.last > self.idle:
            self.sid = f"echo-{time.strftime('%Y%m%d-%H%M%S')}"
            log.info("new Hermes session %s", self.sid)
        self.last = now
        return self.sid


class Bridge:
    def __init__(self, args, stt: SpeechToText, tts: TextToSpeech, hermes: Hermes):
        self.a, self.stt, self.tts, self.hermes = args, stt, tts, hermes
        self.sessions = Sessions(args.idle_minutes)
        self.cli = APIClient(args.echo_host, args.echo_port, None, noise_psk=args.echo_key or None,
                             client_info="hermes-echo-bridge") if args.echo_host else None
        self.media: dict[str, bytes] = {}
        self.base_url = f"http://{args.public_ip or local_ip_towards(args.echo_host)}:{args.http_port}"
        self.run: asyncio.Task | None = None
        self.eos: EndOfSpeech | None = None
        self.audio_done = asyncio.Event()
        self.screen = Screen()

    # ---- shared steps (Echo and TV)
    async def answer(self, text: str) -> str:
        t0 = time.monotonic()
        try:
            reply = clean_for_voice(await self.hermes.ask(text, self.sessions.current())) or self.a.fallback
        except Exception as err:                           # noqa: BLE001 - anything: speak the fallback
            log.warning("Hermes failed: %s", err)
            reply = self.a.fallback
        log.info("Hermes (%.1fs): %r", time.monotonic() - t0, reply)
        return reply

    async def speak(self, reply: str) -> tuple[str, float]:
        """Synthesize, publish; returns (path under this server, seconds)."""
        wav = await asyncio.get_running_loop().run_in_executor(None, self.tts.wav, reply)
        return self.publish(wav), wav_seconds(wav)

    # ---- HTTP server for the replies
    async def serve_media(self, request: web.Request) -> web.Response:
        body = self.media.get(request.match_info["name"])
        if body is None:
            raise web.HTTPNotFound()
        return web.Response(body=body, content_type="audio/wav")

    def publish(self, wav: bytes) -> str:
        name = f"{uuid.uuid4().hex}.wav"
        self.media[name] = wav
        while len(self.media) > 20:                          # keep the last few replies only
            self.media.pop(next(iter(self.media)))
        return f"/tts/{name}"

    # ---- HTTP API for TV screens and the Android TV app
    def authorized(self, request: web.Request) -> None:
        token = self.a.tv_token
        if not token:
            raise web.HTTPForbidden(text=json.dumps({"error": "set TV_TOKEN in the bridge's .env to use the TV app"}),
                                    content_type="application/json")
        given = request.headers.get("Authorization", "").removeprefix("Bearer ").strip() or request.query.get("token", "")
        if not hmac.compare_digest(given.encode(), token.encode()):
            raise web.HTTPUnauthorized(text=json.dumps({"error": "wrong TV token"}), content_type="application/json")

    async def events(self, request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                                           "Access-Control-Allow-Origin": "*", "X-Accel-Buffering": "no"})
        await resp.prepare(request)
        q: asyncio.Queue = asyncio.Queue()
        self.screen.subs.add(q)
        send = lambda ev: resp.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())  # noqa: E731
        try:
            await send({"type": "hello", "history": self.screen.history, "language": self.a.language,
                        "echo": bool(self.cli), "tv": bool(self.a.tv_token),
                        "avatar": self.a.screen_avatar, "ink": self.a.screen_ink})
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), 15)
                except asyncio.TimeoutError:
                    await resp.write(b": ping\n\n")          # keeps proxies and TV browsers from timing out
                    continue
                await send(ev)
        except (ConnectionResetError, RuntimeError):
            pass
        finally:
            self.screen.subs.discard(q)
        return resp

    async def tv_turn(self, text: str, turn: str) -> web.Response:
        self.screen.emit("thinking", id=turn, source="tv", heard=text)
        reply = await self.answer(text)
        path, secs = await self.speak(reply)
        self.screen.emit("reply", id=turn, source="tv", heard=text, text=reply, audio=path, duration=round(secs, 2))
        self.screen.idle_later(turn, secs + 0.8, "tv")
        return web.json_response({"id": turn, "heard": text, "reply": reply, "audio": path, "duration": round(secs, 2)})

    async def talk(self, request: web.Request) -> web.Response:
        """POST audio (raw 16 kHz s16 mono PCM, or WAV) -> {heard, reply, audio}."""
        self.authorized(request)
        try:
            pcm = pcm_from_upload(await request.read())
        except Exception as err:                           # noqa: BLE001
            return web.json_response({"error": f"bad audio: {err}"}, status=400)
        turn = self.screen.new_turn()
        self.screen.emit("thinking", id=turn, source="tv", heard="")
        t0 = time.monotonic()
        text = await asyncio.get_running_loop().run_in_executor(None, self.stt.transcribe, pcm)
        log.info("TV heard (%.1fs audio, %.1fs STT): %r", len(pcm) / BYTES_PER_SEC, time.monotonic() - t0, text)
        if not text:
            self.screen.emit("error", id=turn, source="tv", message="nothing heard")
            self.screen.idle_later(turn, 2.5, "tv")
            return web.json_response({"id": turn, "heard": "", "error": "nothing heard"}, status=422)
        return await self.tv_turn(text, turn)

    async def ask_text(self, request: web.Request) -> web.Response:
        """POST {"text": "..."} (speech recognised on the TV itself) -> {reply, audio}."""
        self.authorized(request)
        try:
            text = str((await request.json()).get("text", "")).strip()
        except Exception:                                  # noqa: BLE001
            text = ""
        if not text:
            return web.json_response({"error": "no text"}, status=400)
        log.info("TV asked: %r", text)
        return await self.tv_turn(text, self.screen.new_turn())

    async def to_screen(self, request: web.Request) -> web.Response:
        raise web.HTTPFound("/screen/" + (f"?{request.query_string}" if request.query_string else ""))

    async def screen_page(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(os.path.join(HERE, "screen", "index.html"), headers={"Cache-Control": "no-cache"})

    async def avatar_file(self, request: web.Request) -> web.FileResponse:
        """The animated portrait made by tools/make_avatar.py, if there is one."""
        path = self.a.avatar_file or os.path.join(HERE, "avatar.json")
        if not os.path.isfile(path):
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"})

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "echo": bool(self.cli), "tv": bool(self.a.tv_token),
                                  "language": self.a.language})

    # ---- ESPHome voice assistant callbacks
    async def handle_start(self, conversation_id: str, flags: int, audio_settings, wake_word_phrase: str | None):
        log.info("wake word heard (%s)", wake_word_phrase or "?")
        self.screen.emit("listening", id=self.screen.new_turn(), source="echo")
        if self.run and not self.run.done():
            self.run.cancel()
        self.eos = EndOfSpeech(self.a.silence, self.a.max_request)
        self.audio_done = asyncio.Event()
        self.run = asyncio.create_task(self.pipeline())
        return 0                                             # 0 = audio comes over this API connection

    async def handle_audio(self, data: bytes, *_) -> None:
        if self.eos and not self.audio_done.is_set() and self.eos.feed(data):
            self.audio_done.set()

    async def handle_stop(self, abort: bool) -> None:          # the Echo ended the request itself
        if abort and self.run and not self.run.done():
            log.info("request aborted on the Echo")
            self.run.cancel()
        self.audio_done.set()

    async def handle_announcement_finished(self, msg) -> None:
        pass

    def event(self, ev, data: dict[str, str] | None = None) -> None:
        self.cli.send_voice_assistant_event(ev, data)

    async def pipeline(self) -> None:
        loop = asyncio.get_running_loop()
        turn = self.screen.turn or self.screen.new_turn()
        try:
            self.event(Ev.VOICE_ASSISTANT_RUN_START, None)
            self.event(Ev.VOICE_ASSISTANT_STT_START, None)
            try:
                await asyncio.wait_for(self.audio_done.wait(), self.a.max_request + 3)
            except asyncio.TimeoutError:
                pass
            self.event(Ev.VOICE_ASSISTANT_STT_VAD_END, None)      # the Echo stops streaming the mic
            pcm = bytes(self.eos.buf)
            t0 = time.monotonic()
            text = await loop.run_in_executor(None, self.stt.transcribe, pcm)
            log.info("heard (%.1fs audio, %.1fs STT): %r", len(pcm) / BYTES_PER_SEC, time.monotonic() - t0, text)
            if not text:
                self.screen.emit("error", id=turn, source="echo", message="nothing heard")
                self.screen.idle_later(turn, 2.5, "echo")
                self.event(Ev.VOICE_ASSISTANT_ERROR, {"code": "stt-no-text-recognized", "message": "nothing heard"})
                self.event(Ev.VOICE_ASSISTANT_RUN_END, None)
                return
            self.event(Ev.VOICE_ASSISTANT_STT_END, {"text": text})
            self.screen.emit("thinking", id=turn, source="echo", heard=text)
            self.event(Ev.VOICE_ASSISTANT_INTENT_START, None)
            reply = await self.answer(text)
            self.event(Ev.VOICE_ASSISTANT_INTENT_END, {"conversation_id": self.sessions.sid or "", "continue_conversation": "0"})
            self.event(Ev.VOICE_ASSISTANT_TTS_START, {"text": reply})
            path, secs = await self.speak(reply)
            self.event(Ev.VOICE_ASSISTANT_TTS_END, {"url": self.base_url + path})
            self.event(Ev.VOICE_ASSISTANT_RUN_END, None)
            self.screen.emit("reply", id=turn, source="echo", heard=text, text=reply, audio=path, duration=round(secs, 2))
            self.screen.idle_later(turn, secs + 1.5, "echo")
        except asyncio.CancelledError:
            self.event(Ev.VOICE_ASSISTANT_RUN_END, None)
            raise
        except Exception:                                  # noqa: BLE001
            log.exception("pipeline failed")
            self.screen.emit("error", id=turn, source="echo", message="bridge error")
            self.screen.idle_later(turn, 2.5, "echo")
            self.event(Ev.VOICE_ASSISTANT_ERROR, {"code": "bridge-error", "message": "bridge error"})
            self.event(Ev.VOICE_ASSISTANT_RUN_END, None)

    # ---- connection
    async def on_connect(self) -> None:
        info = await self.cli.device_info()
        log.info("connected to %s (%s)", info.name, info.model or "Echo")
        self.cli.subscribe_voice_assistant(handle_start=self.handle_start, handle_stop=self.handle_stop,
                                           handle_audio=self.handle_audio,
                                           handle_announcement_finished=self.handle_announcement_finished)
        if self.a.wake_word:
            await self.pick_wake_word(self.a.wake_word)
        log.info("ready: say the wake word")

    async def pick_wake_word(self, wanted: str) -> None:
        try:
            cfg = await self.cli.get_voice_assistant_configuration(10)
        except Exception as err:                           # noqa: BLE001
            log.warning("could not read the Echo's wake words: %s", err)
            return
        words = {w.id: w.wake_word for w in cfg.available_wake_words}
        log.info("wake words on the Echo: %s (active: %s)", words, list(cfg.active_wake_words))
        match = next((i for i, name in words.items() if wanted.lower() in (i + " " + name).lower()), None)
        if match is None:
            log.warning("wake word %r not installed on the Echo; add it on its settings page (see docs/wake-word.md)", wanted)
        elif list(cfg.active_wake_words) != [match]:
            await self.cli.set_voice_assistant_configuration([match])
            log.info("wake word set to %s", words[match])

    async def on_disconnect(self, expected: bool) -> None:
        log.warning("Echo disconnected, reconnecting...")
        if self.run and not self.run.done():
            self.run.cancel()

    async def main(self) -> None:
        app = web.Application(client_max_size=8 * 1024 * 1024)       # /talk: up to ~4 min of 16 kHz audio
        app.router.add_get("/tts/{name}", self.serve_media)
        app.router.add_get("/events", self.events)
        app.router.add_post("/talk", self.talk)
        app.router.add_post("/ask", self.ask_text)
        app.router.add_get("/health", self.health)
        app.router.add_get("/", self.to_screen)
        app.router.add_get("/screen", self.to_screen)
        app.router.add_get("/screen/", self.screen_page)
        app.router.add_get("/screen/avatar.json", self.avatar_file)
        app.router.add_static("/screen/", os.path.join(HERE, "screen"))
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", self.a.http_port).start()
        log.info("serving replies at %s/tts/, TV screen at %s/screen/", self.base_url, self.base_url)
        if not self.a.tv_token:
            log.info("TV app disabled: set TV_TOKEN to enable /talk and /ask")
        if not self.cli:
            log.info("no ECHO_HOST: running for TV screens and the TV app only")
            await asyncio.Event().wait()
        reconnect = ReconnectLogic(client=self.cli, on_connect=self.on_connect, on_disconnect=self.on_disconnect,
                                   zeroconf_instance=None, name=None, zeroconf_wake=False)
        await reconnect.start()
        log.info("connecting to the Echo at %s:%d", self.a.echo_host, self.a.echo_port)
        await asyncio.Event().wait()


async def amain(args) -> None:
    async with aiohttp.ClientSession() as http:
        hermes = Hermes(http, args.hermes_url, args.hermes_key, args.hermes_model, args.instruction, args.timeout)
        try:
            log.info("Hermes model: %s", await hermes.resolve_model())
        except Exception as err:                           # noqa: BLE001
            log.error("cannot reach Hermes at %s: %s", hermes.base, err)
            raise SystemExit(2)
        if args.ask:                                       # text-only test, no Echo, no audio
            print(clean_for_voice(await hermes.ask(args.ask, "echo-test")))
            return
        stt = SpeechToText(args.whisper_model, args.language, args.whisper_device, args.whisper_compute)
        tts = TextToSpeech(args.piper_voice)
        await Bridge(args, stt, tts, hermes).main()


def parse_args() -> argparse.Namespace:
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--echo-host", default=env("ECHO_HOST"), help="the Echo's IP address")
    p.add_argument("--echo-port", type=int, default=int(env("ECHO_PORT", "26053")))
    p.add_argument("--echo-key", default=env("ECHO_KEY"), help="ESPHome encryption key, if one was ever set")
    p.add_argument("--hermes-url", default=env("HERMES_URL", "http://127.0.0.1:8642/v1"))
    p.add_argument("--hermes-key", default=env("HERMES_API_KEY", ""), help="the profile's API_SERVER_KEY")
    p.add_argument("--hermes-model", default=env("HERMES_MODEL"), help="default: first model the gateway lists")
    p.add_argument("--instruction", default=env("VOICE_INSTRUCTION"), help="default: per --language")
    p.add_argument("--fallback", default=env("FALLBACK_MESSAGE"), help="default: per --language")
    p.add_argument("--timeout", type=float, default=float(env("HERMES_TIMEOUT", "45")))
    p.add_argument("--idle-minutes", type=float, default=float(env("IDLE_MINUTES", "30")),
                   help="start a new Hermes conversation after this much silence")
    p.add_argument("--language", default=env("LANGUAGE", "en"), help="speech-to-text language (en, fr, de...)")
    p.add_argument("--whisper-model", default=env("WHISPER_MODEL", "small"))
    p.add_argument("--whisper-device", default=env("WHISPER_DEVICE", "cpu"))
    p.add_argument("--whisper-compute", default=env("WHISPER_COMPUTE", "int8"))
    p.add_argument("--piper-voice", default=env("PIPER_VOICE", "voices/en_US-lessac-medium.onnx"))
    p.add_argument("--wake-word", default=env("WAKE_WORD", ""), help="pick this wake word on the Echo ('' = leave as is)")
    p.add_argument("--silence", type=float, default=float(env("END_SILENCE", "0.8")), help="seconds of silence that end a request")
    p.add_argument("--max-request", type=float, default=float(env("MAX_REQUEST", "12")))
    p.add_argument("--http-port", type=int, default=int(env("HTTP_PORT", "8765")))
    p.add_argument("--public-ip", default=env("PUBLIC_IP"), help="this machine's LAN IP as the Echo sees it (auto)")
    p.add_argument("--screen-avatar", default=env("SCREEN_AVATAR", "orb"),
                   help="TV screen default: orb, medallion, spectrum or hologram (the last three need avatar.json)")
    p.add_argument("--screen-ink", default=env("SCREEN_INK", "white"), help="avatar drawing: white or black ink")
    p.add_argument("--avatar-file", default=env("AVATAR_FILE"), help="avatar.json from tools/make_avatar.py (default: next to bridge.py)")
    p.add_argument("--tv-token", default=env("TV_TOKEN", ""), help="shared secret for the Android TV app ('' = TV app off)")
    p.add_argument("--ask", help="text-only test: send this sentence to Hermes, print the answer, exit")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args()
    instruction, fallback = DEFAULTS.get(a.language.split("-")[0].lower(), DEFAULTS["en"])
    a.instruction = a.instruction or instruction
    a.fallback = a.fallback or fallback
    if not a.ask and not a.echo_host and not a.tv_token:
        p.error("--echo-host (or ECHO_HOST in .env) is required, unless TV_TOKEN is set for a TV-only bridge")
    return a


if __name__ == "__main__":
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass
