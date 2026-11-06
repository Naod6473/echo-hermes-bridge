"""End-to-end test of bridge.py with a fake Echo (minimal ESPHome plaintext API server), fake Hermes,
and stubbed Whisper/Piper. Checks the event sequence the Echo would see and that the reply WAV is fetchable."""
import asyncio, io, math, struct, sys, wave
import os; sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import aiohttp
from aiohttp import web
from aioesphomeapi import api_pb2 as pb
from aioesphomeapi.core import MESSAGE_TYPE_TO_PROTO
import bridge

TYPE_OF = {v: k for k, v in MESSAGE_TYPE_TO_PROTO.items()}
events, got_config = [], []


def varint(n):
    out = b""
    while True:
        b = n & 0x7F; n >>= 7
        if n: out += bytes([b | 0x80])
        else: return out + bytes([b])


async def read_varint(r):
    n = shift = 0
    while True:
        b = (await r.readexactly(1))[0]; n |= (b & 0x7F) << shift; shift += 7
        if not b & 0x80: return n


def frame(msg):
    data = msg.SerializeToString()
    return b"\x00" + varint(len(data)) + varint(TYPE_OF[type(msg)]) + data


def speech_pcm():
    out = bytearray(b"\x00" * 800 * 32)                      # Echo lead-in silence
    for i in range(int(0.4 * 16000)): out += struct.pack("<h", int(50 * math.sin(i)))       # room noise
    for i in range(int(1.5 * 16000)): out += struct.pack("<h", int(8000 * math.sin(2 * math.pi * 300 * i / 16000)))
    for i in range(int(3 * 16000)): out += struct.pack("<h", int(50 * math.sin(i)))         # silence after
    return bytes(out)


async def echo_server(reader, writer):
    send = lambda m: writer.write(frame(m))
    while True:
        try:
            assert (await reader.readexactly(1)) == b"\x00"
        except asyncio.IncompleteReadError:
            return
        ln = await read_varint(reader); t = await read_varint(reader); data = await reader.readexactly(ln)
        msg = MESSAGE_TYPE_TO_PROTO[t](); msg.ParseFromString(data); name = type(msg).__name__
        if name == "HelloRequest": send(pb.HelloResponse(api_version_major=1, api_version_minor=10, name="Echo Dot"))
        elif name in ("ConnectRequest", "AuthenticationRequest"): send(MESSAGE_TYPE_TO_PROTO[t + 1]())
        elif name == "DeviceInfoRequest":
            send(pb.DeviceInfoResponse(name="echo-dot", model="Echo Dot", voice_assistant_feature_flags=1 | 2 | 4 | 8))
        elif name == "PingRequest": send(pb.PingResponse())
        elif name == "VoiceAssistantConfigurationRequest":
            send(pb.VoiceAssistantConfigurationResponse(
                available_wake_words=[pb.VoiceAssistantWakeWord(id="alexa", wake_word="Alexa"),
                                      pb.VoiceAssistantWakeWord(id="hey_hermes", wake_word="Hey Hermes")],
                active_wake_words=["alexa"], max_active_wake_words=2))
        elif name == "VoiceAssistantSetConfiguration": got_config.append(list(msg.active_wake_words))
        elif name == "SubscribeVoiceAssistantRequest":
            asyncio.get_running_loop().call_later(0.5, lambda: asyncio.create_task(talk(send)))
        elif name == "VoiceAssistantEventResponse":
            d = {x.name: x.value for x in msg.data}; events.append((msg.event_type, d))
        elif name == "DisconnectRequest": send(pb.DisconnectResponse()); writer.close(); return
        await writer.drain()


async def talk(send):
    send(pb.VoiceAssistantRequest(start=True, conversation_id="", flags=1, wake_word_phrase="Hey Hermes"))
    pcm = speech_pcm()
    for i in range(0, len(pcm), 1600):                     # 50 ms chunks, real time /4
        if any(e[0] == pb.VOICE_ASSISTANT_STT_VAD_END for e in events): break
        send(pb.VoiceAssistantAudio(data=pcm[i:i + 1600])); await asyncio.sleep(0.0125)


async def fake_hermes():
    seen = {}
    async def models(r): return web.json_response({"data": [{"id": "hermes-agent"}]})
    async def chat(r):
        body = await r.json(); seen["sid"] = r.headers.get("X-Hermes-Session-Id"); seen["auth"] = r.headers.get("Authorization")
        seen["msgs"] = body["messages"]
        return web.json_response({"choices": [{"message": {"content": "**Bonjour** Sam ! Voir https://x.y 😀"}, "finish_reason": "stop"}]})
    app = web.Application(); app.router.add_get("/v1/models", models); app.router.add_post("/v1/chat/completions", chat)
    run = web.AppRunner(app); await run.setup(); await web.TCPSite(run, "127.0.0.1", 18642).start()
    return seen


class FakeSTT:
    def transcribe(self, pcm): FakeSTT.n = len(pcm); return "quelle heure est-il"


class FakeTTS:
    def wav(self, text):
        FakeTTS.text = text; b = io.BytesIO()
        with wave.open(b, "wb") as w: w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050); w.writeframes(b"\x00\x00" * 2205)
        return b.getvalue()


async def main():
    srv = await asyncio.start_server(echo_server, "127.0.0.1", 26053)
    seen = await fake_hermes()
    sys.argv = ["bridge", "--echo-host", "127.0.0.1", "--hermes-url", "http://127.0.0.1:18642", "--hermes-key", "k",
                "--public-ip", "127.0.0.1", "--http-port", "18765", "--wake-word", "hermes", "--language", "fr"]
    args = bridge.parse_args()
    async with aiohttp.ClientSession() as http:
        h = bridge.Hermes(http, args.hermes_url, args.hermes_key, None, args.instruction, 10)
        b = bridge.Bridge(args, FakeSTT(), FakeTTS(), h)
        task = asyncio.create_task(b.main())
        for _ in range(200):
            if any(e[0] == pb.VOICE_ASSISTANT_RUN_END for e in events): break
            await asyncio.sleep(0.05)
        names = [pb.VoiceAssistantEvent.Name(e[0]).replace("VOICE_ASSISTANT_", "") for e in events]
        print("events:", names)
        url = next((e[1]["url"] for e in events if e[0] == pb.VOICE_ASSISTANT_TTS_END), None)
        async with http.get(url) as r: wav = await r.read(); ok_wav = r.status == 200 and wav[:4] == b"RIFF"
        audio_s = FakeSTT.n / 32000
        checks = {
            "event order": names == ["RUN_START", "STT_START", "STT_VAD_END", "STT_END", "INTENT_START", "INTENT_END", "TTS_START", "TTS_END", "RUN_END"],
            "wake word set to hey_hermes": got_config == [["hey_hermes"]],
            "end of speech ~0.8s after talking": 2.9 <= audio_s <= 3.6,
            "session header sent": (seen.get("sid") or "").startswith("echo-"),
            "auth header": seen.get("auth") == "Bearer k",
            "system instruction + user text": [m["role"] for m in seen["msgs"]] == ["system", "user"] and seen["msgs"][1]["content"] == "quelle heure est-il",
            "reply cleaned for speech": FakeTTS.text == "Bonjour Sam! Voir link",
            "reply WAV served": ok_wav,
        }
        print(f"audio kept: {audio_s:.2f}s, spoken: {FakeTTS.text!r}")
        for k, v in checks.items(): print("ok  " if v else "FAIL", k)
        task.cancel(); srv.close()
        sys.exit(0 if all(checks.values()) else 1)

asyncio.run(main())
