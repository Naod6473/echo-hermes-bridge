"""TV side of bridge.py, without an Echo: /screen/, the live feed (/events), /ask and /talk with
TV_TOKEN, with a fake Hermes and stubbed Whisper/Piper."""
import asyncio, io, json, os, struct, sys, wave
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import aiohttp
from aiohttp import web
import bridge


async def fake_hermes():
    async def models(r): return web.json_response({"data": [{"id": "hermes-agent"}]})
    async def chat(r):
        body = await r.json()
        return web.json_response({"choices": [{"message": {"content": f"Tu as dit, {body['messages'][1]['content']}"}, "finish_reason": "stop"}]})
    app = web.Application(); app.router.add_get("/v1/models", models); app.router.add_post("/v1/chat/completions", chat)
    run = web.AppRunner(app); await run.setup(); await web.TCPSite(run, "127.0.0.1", 18643).start()


class FakeSTT:
    def transcribe(self, pcm): return "allume la lumière" if any(pcm) else ""


class FakeTTS:
    def wav(self, text):
        b = io.BytesIO()
        with wave.open(b, "wb") as w: w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050); w.writeframes(b"\x00\x00" * 22050)
        return b.getvalue()


async def main():
    await fake_hermes()
    sys.argv = ["bridge", "--hermes-url", "http://127.0.0.1:18643", "--hermes-key", "k", "--public-ip", "127.0.0.1",
                "--http-port", "18766", "--tv-token", "s3cret", "--language", "fr"]
    args = bridge.parse_args()
    base = "http://127.0.0.1:18766"
    async with aiohttp.ClientSession() as http:
        h = bridge.Hermes(http, args.hermes_url, args.hermes_key, None, args.instruction, 10)
        b = bridge.Bridge(args, FakeSTT(), FakeTTS(), h)
        task = asyncio.create_task(b.main())
        await asyncio.sleep(0.5)

        feed = []
        async def listen():
            async with http.get(base + "/events") as r:
                async for line in r.content:
                    if line.startswith(b"data: "): feed.append(json.loads(line[6:]))
        lt = asyncio.create_task(listen()); await asyncio.sleep(0.3)

        auth = {"Authorization": "Bearer s3cret"}
        async with http.get(base + "/screen/") as r: screen_ok = r.status == 200 and "HermesScreen" in await r.text()
        async with http.get(base + "/health") as r: health = await r.json()
        async with http.post(base + "/ask", json={"text": "salut"}) as r: no_token = r.status
        async with http.post(base + "/ask", json={"text": "salut"}, headers={"Authorization": "Bearer nope"}) as r: bad_token = r.status
        async with http.post(base + "/ask", json={}, headers=auth) as r: empty = r.status
        async with http.post(base + "/ask", json={"text": "salut"}, headers=auth) as r: ask = await r.json()
        async with http.get(base + ask["audio"]) as r: wav_ok = r.status == 200 and (await r.read())[:4] == b"RIFF"
        pcm = struct.pack("<1600h", *([3000, -3000] * 800))
        async with http.post(base + "/talk", data=pcm, headers=auth) as r: talk = await r.json()
        async with http.post(base + "/talk", data=b"\x00" * 3200, headers=auth) as r: silent = r.status
        await asyncio.sleep(3.5)                                 # the error clears after 2.5 s
        lt.cancel()

        kinds = [e["type"] for e in feed]
        print("feed:", kinds)
        checks = {
            "screen page served": screen_ok,
            "health": health == {"ok": True, "echo": False, "tv": True, "language": "fr"},
            "no token -> 401": no_token == 401,
            "wrong token -> 401": bad_token == 401,
            "empty question -> 400": empty == 400,
            "/ask answers": ask.get("reply") == "Tu as dit, salut" and ask.get("duration") == 1.0,
            "reply audio served": wav_ok,
            "/talk transcribes and answers": talk.get("heard") == "allume la lumière" and talk.get("reply") == "Tu as dit, allume la lumière",
            "/talk with silence -> 422": silent == 422,
            "feed starts with hello": kinds[:1] == ["hello"],
            "feed has thinking, reply, error, idle": all(k in kinds for k in ("thinking", "reply", "error", "idle")),
            "replies carry the source": all(e.get("source") == "tv" for e in feed if e["type"] == "reply"),
        }
        for k, v in checks.items(): print("ok  " if v else "FAIL", k)
        task.cancel()
        sys.exit(0 if all(checks.values()) else 1)

asyncio.run(main())
