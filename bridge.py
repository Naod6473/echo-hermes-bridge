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

Only one client may hold the Echo's voice assistant: do not run Home Assistant against the same Echo.
"""
from __future__ import annotations

import argparse
import asyncio
import io
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


def local_ip_towards(host: str) -> str:
    """The address of this machine that the Echo can reach (no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 9))
        return s.getsockname()[0]
    finally:
        s.close()


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
                             client_info="hermes-echo-bridge")
        self.media: dict[str, bytes] = {}
        self.base_url = f"http://{args.public_ip or local_ip_towards(args.echo_host)}:{args.http_port}"
        self.run: asyncio.Task | None = None
        self.eos: EndOfSpeech | None = None
        self.audio_done = asyncio.Event()

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
        return f"{self.base_url}/tts/{name}"

    # ---- ESPHome voice assistant callbacks
    async def handle_start(self, conversation_id: str, flags: int, audio_settings, wake_word_phrase: str | None):
        log.info("wake word heard (%s)", wake_word_phrase or "?")
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
                self.event(Ev.VOICE_ASSISTANT_ERROR, {"code": "stt-no-text-recognized", "message": "nothing heard"})
                self.event(Ev.VOICE_ASSISTANT_RUN_END, None)
                return
            self.event(Ev.VOICE_ASSISTANT_STT_END, {"text": text})
            self.event(Ev.VOICE_ASSISTANT_INTENT_START, None)
            t0 = time.monotonic()
            try:
                reply = clean_for_voice(await self.hermes.ask(text, self.sessions.current())) or self.a.fallback
            except Exception as err:                       # noqa: BLE001 - anything: speak the fallback
                log.warning("Hermes failed: %s", err)
                reply = self.a.fallback
            log.info("Hermes (%.1fs): %r", time.monotonic() - t0, reply)
            self.event(Ev.VOICE_ASSISTANT_INTENT_END, {"conversation_id": self.sessions.sid or "", "continue_conversation": "0"})
            self.event(Ev.VOICE_ASSISTANT_TTS_START, {"text": reply})
            wav = await loop.run_in_executor(None, self.tts.wav, reply)
            url = self.publish(wav)
            self.event(Ev.VOICE_ASSISTANT_TTS_END, {"url": url})
            self.event(Ev.VOICE_ASSISTANT_RUN_END, None)
        except asyncio.CancelledError:
            self.event(Ev.VOICE_ASSISTANT_RUN_END, None)
            raise
        except Exception:                                  # noqa: BLE001
            log.exception("pipeline failed")
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
        app = web.Application()
        app.router.add_get("/tts/{name}", self.serve_media)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", self.a.http_port).start()
        log.info("serving replies at %s/tts/", self.base_url)
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
    p.add_argument("--ask", help="text-only test: send this sentence to Hermes, print the answer, exit")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args()
    instruction, fallback = DEFAULTS.get(a.language.split("-")[0].lower(), DEFAULTS["en"])
    a.instruction = a.instruction or instruction
    a.fallback = a.fallback or fallback
    if not a.ask and not a.echo_host:
        p.error("--echo-host (or ECHO_HOST in .env) is required")
    return a


if __name__ == "__main__":
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass
