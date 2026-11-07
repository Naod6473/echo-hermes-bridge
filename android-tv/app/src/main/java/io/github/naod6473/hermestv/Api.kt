package io.github.naod6473.hermestv

import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/** The bridge's small HTTP API (see bridge.py): /health, /talk (audio) and /ask (text). Blocking: call off the main thread. */
class Api(base: String, private val token: String) {
    val base: String = base.trim().trimEnd('/')

    /** 16 kHz, 16 bit, mono PCM -> {"heard", "reply", "audio", "duration"} (+ "status"). */
    fun talk(pcm: ByteArray): JSONObject = post("/talk", pcm, "audio/L16; rate=16000; channels=1")

    /** Text recognised on the TV -> {"reply", "audio", "duration"} (+ "status"). */
    fun ask(text: String): JSONObject =
        post("/ask", JSONObject().put("text", text).toString().toByteArray(Charsets.UTF_8), "application/json")

    /** Checks the URL and the token: {"ok": bool, "reason": "..."}. */
    fun check(): JSONObject {
        val result = JSONObject()
        val health = try {
            get("/health")
        } catch (e: Exception) {
            return result.put("ok", false).put("reason", "unreachable").put("detail", e.message ?: e.javaClass.simpleName)
        }
        if (health.optInt("status") != 200) {
            return result.put("ok", false).put("reason", "not-a-bridge").put("detail", "HTTP ${health.optInt("status")}")
        }
        // An empty question: 400 means the token was accepted.
        val probe = try {
            post("/ask", "{}".toByteArray(), "application/json")
        } catch (e: Exception) {
            return result.put("ok", false).put("reason", "unreachable").put("detail", e.message ?: "")
        }
        return when (probe.optInt("status")) {
            400 -> result.put("ok", true).put("echo", health.optBoolean("echo")).put("language", health.optString("language"))
            401 -> result.put("ok", false).put("reason", "wrong-token")
            403 -> result.put("ok", false).put("reason", "tv-disabled")
            else -> result.put("ok", false).put("reason", "unexpected").put("detail", "HTTP ${probe.optInt("status")}")
        }
    }

    private fun get(path: String): JSONObject {
        val c = open(path)
        c.requestMethod = "GET"
        return read(c)
    }

    private fun post(path: String, body: ByteArray, type: String): JSONObject {
        val c = open(path)
        c.requestMethod = "POST"
        c.doOutput = true
        c.setRequestProperty("Content-Type", type)
        c.setFixedLengthStreamingMode(body.size)
        c.outputStream.use { it.write(body) }
        return read(c)
    }

    private fun open(path: String): HttpURLConnection {
        val c = URL(base + path).openConnection() as HttpURLConnection
        c.connectTimeout = 5_000
        c.readTimeout = 120_000            // Whisper + Hermes + Piper can take a while on a small server
        c.useCaches = false
        if (token.isNotEmpty()) c.setRequestProperty("Authorization", "Bearer $token")
        return c
    }

    private fun read(c: HttpURLConnection): JSONObject {
        try {
            val code = c.responseCode
            val stream = if (code >= 400) c.errorStream else c.inputStream
            val text = stream?.bufferedReader(Charsets.UTF_8)?.use { it.readText() } ?: ""
            val json = try {
                JSONObject(text)
            } catch (e: Exception) {
                JSONObject().put("error", text.take(200))
            }
            return json.put("status", code)
        } finally {
            c.disconnect()
        }
    }
}
