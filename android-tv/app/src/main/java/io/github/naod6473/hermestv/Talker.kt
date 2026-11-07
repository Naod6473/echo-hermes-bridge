package io.github.naod6473.hermestv

import android.content.Context
import android.content.Intent
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sqrt

/**
 * One push-to-talk turn: listen with the TV's microphone (the remote's, when the TV routes it to
 * apps), find the end of the request, and send it to the bridge.
 *
 * Two ways to hear:
 *  - Whisper (private): record 16 kHz audio here and POST it to the bridge's /talk.
 *  - Android: the TV's own speech recogniser (Google on Google TV), then POST the text to /ask.
 *    Some TVs only open the remote's microphone for the recogniser; "auto" mode notices a silent
 *    recording after ~1 s and switches to it.
 *
 * Every Listener callback runs on the main thread.
 */
class Talker(
    private val context: Context,
    private val api: Api,
    private val language: String,
    private val listener: Listener,
) {
    interface Listener {
        fun onListening()
        fun onLevel(level: Float)
        fun onPartial(text: String)
        fun onSending(heard: String?)
        fun onReply(result: JSONObject)
        fun onFailed(reason: String)
    }

    private val main = Handler(Looper.getMainLooper())
    @Volatile private var stopRequested = false
    @Volatile private var cancelled = false
    private var recognizer: SpeechRecognizer? = null

    @Volatile var active = false
        private set

    fun start(mode: String) {
        active = true
        stopRequested = false
        cancelled = false
        if (mode == "android") startRecognizer()
        else Thread({ record(fallback = mode == "auto") }, "hermes-mic").start()
    }

    /** Stop listening and send what was heard. */
    fun stop() {
        stopRequested = true
        recognizer?.stopListening()
    }

    /** Stop listening and drop it. */
    fun cancel() {
        cancelled = true
        stopRequested = true
        recognizer?.cancel()
        recognizer?.destroy()
        recognizer = null
        active = false
    }

    private fun post(block: () -> Unit) = main.post { if (!cancelled) block() }

    private fun finish(block: () -> Unit) = main.post {
        active = false
        if (!cancelled) block()
    }

    // ------------------------------------------------------------------------- Whisper (bridge)
    private fun record(fallback: Boolean) {
        val bufSize = max(AudioRecord.getMinBufferSize(RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT), RATE)
        val rec = try {
            AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, RATE, AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT, bufSize)
        } catch (e: Exception) {
            null
        }
        if (rec == null || rec.state != AudioRecord.STATE_INITIALIZED) {
            rec?.release()
            if (fallback) main.post { if (!cancelled) startRecognizer() } else finish { listener.onFailed("no-mic") }
            return
        }

        val pcm = ByteArrayOutputStream(RATE * 2 * 8)
        val frame = ShortArray(FRAME)
        val noise = ArrayList<Double>()
        var floor = -1.0
        var speech = false
        var lastLoud = 0.0
        var t = 0.0
        var peak = 0
        var silentMic = false
        var gaveUp = false

        try {
            rec.startRecording()
            post { listener.onListening() }
            while (!stopRequested) {
                val n = rec.read(frame, 0, FRAME)
                if (n <= 0) break
                var sum = 0.0
                for (i in 0 until n) {
                    val s = frame[i].toInt()
                    pcm.write(s and 0xff)
                    pcm.write((s shr 8) and 0xff)
                    sum += (s * s).toDouble()
                    peak = max(peak, kotlin.math.abs(s))
                }
                val rms = sqrt(sum / n)
                t += n.toDouble() / RATE
                val level = min(1.0, rms / 3000.0).toFloat()
                post { listener.onLevel(level) }

                if (fallback && t >= 1.0 && peak < 3) { silentMic = true; break }   // digital silence: no mic for us
                if (floor < 0) {
                    noise.add(rms)
                    if (noise.size >= 15) floor = noise.sorted()[noise.size / 2]
                    continue
                }
                if (rms > max(floor * 3.0, 300.0)) { speech = true; lastLoud = t }
                if (speech && t - lastLoud >= END_SILENCE) break
                if (!speech && t >= NO_SPEECH) { gaveUp = true; break }
                if (t >= MAX_REQUEST) break
            }
        } finally {
            try { rec.stop() } catch (_: Exception) {}
            rec.release()
        }
        if (cancelled) return
        if (silentMic) {
            main.post { if (!cancelled) startRecognizer() }
            return
        }
        if (gaveUp || pcm.size() < RATE / 2) {           // nothing said, or under 1/4 s of audio
            finish { listener.onFailed("nothing heard") }
            return
        }
        post { listener.onSending(null) }
        val result = try {
            api.talk(pcm.toByteArray())
        } catch (e: Exception) {
            JSONObject().put("status", 0).put("error", e.message ?: "network")
        }
        finish { listener.onReply(result) }
    }

    // ------------------------------------------------------------------------- Android recogniser
    private fun startRecognizer() {
        if (!SpeechRecognizer.isRecognitionAvailable(context)) {
            finish { listener.onFailed("no-recognizer") }
            return
        }
        val r = SpeechRecognizer.createSpeechRecognizer(context)
        recognizer = r
        r.setRecognitionListener(object : RecognitionListener {
            override fun onReadyForSpeech(params: Bundle?) = listener.onListening()
            override fun onBeginningOfSpeech() {}
            override fun onRmsChanged(rmsdB: Float) = listener.onLevel(((rmsdB + 2f) / 12f).coerceIn(0f, 1f))
            override fun onBufferReceived(buffer: ByteArray?) {}
            override fun onEndOfSpeech() = listener.onLevel(0f)
            override fun onEvent(eventType: Int, params: Bundle?) {}

            override fun onPartialResults(partialResults: Bundle?) {
                val text = partialResults?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull()
                if (!text.isNullOrBlank()) listener.onPartial(text)
            }

            override fun onResults(results: Bundle?) {
                release()
                val text = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull()?.trim()
                if (text.isNullOrEmpty()) {
                    finish { listener.onFailed("nothing heard") }
                    return
                }
                listener.onSending(text)
                Thread({
                    val result = try {
                        api.ask(text)
                    } catch (e: Exception) {
                        JSONObject().put("status", 0).put("error", e.message ?: "network")
                    }
                    finish { listener.onReply(result) }
                }, "hermes-ask").start()
            }

            override fun onError(error: Int) {
                release()
                val reason = when (error) {
                    SpeechRecognizer.ERROR_NO_MATCH, SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> "nothing heard"
                    SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS -> "no-permission"
                    else -> "recognizer-$error"
                }
                finish { listener.onFailed(reason) }
            }

            private fun release() {
                r.destroy()
                if (recognizer === r) recognizer = null
            }
        })
        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_LANGUAGE, language)
            putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
            putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
            putExtra(RecognizerIntent.EXTRA_CALLING_PACKAGE, context.packageName)
        }
        r.startListening(intent)
    }

    companion object {
        const val RATE = 16_000
        const val FRAME = 320              // 20 ms
        const val END_SILENCE = 0.9        // seconds of silence that end the request
        const val NO_SPEECH = 6.0
        const val MAX_REQUEST = 15.0
    }
}
