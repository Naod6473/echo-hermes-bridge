package io.github.naod6473.hermestv

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.view.KeyEvent
import android.view.View
import android.view.WindowManager
import android.webkit.JavascriptInterface
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import org.json.JSONObject
import java.util.Locale

/**
 * Hermes on the TV: a full-screen view of the bridge's /screen/ page (the same design the TV browser
 * shows), plus push-to-talk with the remote:
 *
 *  - press OK (or the mic / search button when the TV lets apps have it), talk, it stops by itself
 *    when you pause; or hold the button while talking and release it to send;
 *  - Back cancels; D-pad down opens the settings.
 *
 * The page shows what the bridge broadcasts (Echo and TV conversations). This activity only adds
 * what happens on the TV itself: listening, the voice level, and playing the spoken reply.
 */
class MainActivity : Activity(), Talker.Listener {

    private lateinit var web: WebView
    private lateinit var prefs: Prefs
    private val main = Handler(Looper.getMainLooper())
    private var talker: Talker? = null
    private var onSetup = false
    private var pageReady = false
    private var listenWhenReady = false
    private var talkStartedAt = 0L
    private var lastLevelAt = 0L
    private var pendingTalk = false

    private val pushToTalkKeys = setOf(
        KeyEvent.KEYCODE_DPAD_CENTER, KeyEvent.KEYCODE_ENTER, KeyEvent.KEYCODE_NUMPAD_ENTER,
        KeyEvent.KEYCODE_SEARCH, KeyEvent.KEYCODE_VOICE_ASSIST, KeyEvent.KEYCODE_ASSIST,
        KeyEvent.KEYCODE_BUTTON_A, KeyEvent.KEYCODE_MEDIA_RECORD, KeyEvent.KEYCODE_HEADSETHOOK,
    )

    private val fr get() = Locale.getDefault().language == "fr"

    // ----------------------------------------------------------------------------------- lifecycle
    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        web = WebView(this).apply {
            setBackgroundColor(0xFF060816.toInt())
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.mediaPlaybackRequiresUserGesture = false
            settings.cacheMode = WebSettings.LOAD_NO_CACHE
            isFocusable = true
            isFocusableInTouchMode = true
            addJavascriptInterface(JsApi(), "HermesApp")
            webChromeClient = WebChromeClient()
            webViewClient = object : WebViewClient() {
                override fun onPageFinished(view: WebView, url: String) {
                    pageReady = true
                    if (listenWhenReady && !onSetup) {
                        listenWhenReady = false
                        main.postDelayed({ startTalking() }, 400)
                    }
                }

                override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                    if (request.isForMainFrame && !onSetup) showSetup(error.description?.toString() ?: "error")
                }

                override fun onReceivedHttpError(view: WebView, request: WebResourceRequest, response: WebResourceResponse) {
                    if (request.isForMainFrame && !onSetup) showSetup("HTTP ${response.statusCode}")
                }
            }
        }
        setContentView(web)
        immersive()

        applyIntentConfig(intent)
        if (prefs.bridgeUrl.isBlank()) showSetup(null) else showScreen()
        if (wantsToListen(intent)) listenWhenReady = true
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        if (applyIntentConfig(intent)) showScreen()
        if (wantsToListen(intent)) {
            if (pageReady && !onSetup) startTalking() else listenWhenReady = true
        }
    }

    override fun onResume() {
        super.onResume()
        immersive()
        web.onResume()
    }

    override fun onPause() {
        talker?.cancel()
        web.onPause()
        super.onPause()
    }

    override fun onDestroy() {
        talker?.cancel()
        web.destroy()
        super.onDestroy()
    }

    @Suppress("DEPRECATION")
    private fun immersive() {
        web.systemUiVisibility = (View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY or View.SYSTEM_UI_FLAG_FULLSCREEN
            or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION or View.SYSTEM_UI_FLAG_LAYOUT_STABLE
            or View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION or View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN)
    }

    /** adb shell am start -n io.github.naod6473.hermestv/.MainActivity --es bridge 192.168.1.64 --es token XXX */
    private fun applyIntentConfig(intent: Intent?): Boolean {
        val extras = intent?.extras ?: return false
        var changed = false
        (extras.getString("bridge") ?: extras.getString("url"))?.let { prefs.bridgeUrl = it; changed = true }
        extras.getString("token")?.let { prefs.token = it; changed = true }
        extras.getString("mode")?.let { prefs.mode = it; changed = true }
        extras.getString("avatar")?.let { prefs.avatar = it; changed = true }
        extras.getString("ink")?.let { prefs.ink = it; changed = true }
        return changed
    }

    private fun wantsToListen(intent: Intent?): Boolean = when (intent?.action) {
        Intent.ACTION_ASSIST, Intent.ACTION_VOICE_COMMAND, Intent.ACTION_SEARCH_LONG_PRESS -> true
        else -> intent?.getBooleanExtra("listen", false) == true
    }

    // ----------------------------------------------------------------------------------- pages
    private fun showScreen() {
        talker?.cancel()
        onSetup = false
        pageReady = false
        val lang = if (fr) "fr" else "en"
        val look = (if (prefs.avatar.isNotEmpty()) "&avatar=${prefs.avatar}" else "") +
            (if (prefs.ink.isNotEmpty()) "&ink=${prefs.ink}" else "")
        web.loadUrl("${prefs.bridgeUrl}/screen/?app=1&lang=$lang$look")
        web.requestFocus()
    }

    private fun showSetup(error: String?) {
        talker?.cancel()
        onSetup = true
        pageReady = false
        val q = if (error != null) "?error=" + Uri.encode(error) else ""
        web.loadUrl("file:///android_asset/setup.html$q")
        web.requestFocus()
    }

    private fun js(code: String) {
        if (!onSetup && pageReady) web.evaluateJavascript("window.HermesScreen && ($code)", null)
    }

    private fun screenEvent(json: JSONObject) = js("HermesScreen.event(${json})")

    /** Called from the pages' JavaScript (on a background thread: hop to the main one). */
    inner class JsApi {
        @JavascriptInterface
        fun config(): String = JSONObject()
            .put("bridge", prefs.bridgeUrl).put("token", prefs.token).put("mode", prefs.mode)
            .put("avatar", prefs.avatar).put("ink", prefs.ink)
            .put("lang", if (fr) "fr" else "en").put("version", BuildConfigVersion.NAME).toString()

        @JavascriptInterface
        fun save(bridge: String, token: String, mode: String, avatar: String, ink: String) {
            prefs.bridgeUrl = bridge
            prefs.token = token
            prefs.mode = mode
            prefs.avatar = avatar
            prefs.ink = ink
            main.post { showScreen() }
        }

        @JavascriptInterface
        fun test(bridge: String, token: String) {
            Thread({
                val result = try {
                    Api(Prefs.normalizeUrl(bridge), token.trim()).check()
                } catch (e: Exception) {
                    JSONObject().put("ok", false).put("reason", "unreachable").put("detail", e.message ?: "")
                }
                result.put("url", Prefs.normalizeUrl(bridge))
                main.post { web.evaluateJavascript("window.onTest && window.onTest($result)", null) }
            }, "hermes-test").start()
        }

        @JavascriptInterface
        fun retry() {
            main.post { if (prefs.bridgeUrl.isNotBlank()) showScreen() }
        }

        @JavascriptInterface
        fun openSettings() {
            main.post { showSetup(null) }
        }

        @JavascriptInterface
        fun onPlaybackDone() {}
    }

    // ----------------------------------------------------------------------------------- keys
    override fun dispatchKeyEvent(event: KeyEvent): Boolean {
        if (onSetup) return super.dispatchKeyEvent(event)
        val code = event.keyCode
        val now = SystemClock.uptimeMillis()
        if (code in pushToTalkKeys) {
            if (event.action == KeyEvent.ACTION_DOWN && event.repeatCount == 0) {
                if (talker?.active == true) talker?.stop() else startTalking()
            } else if (event.action == KeyEvent.ACTION_UP && talker?.active == true && now - talkStartedAt > HOLD_MS) {
                talker?.stop()                               // held while talking: release sends
            }
            return true
        }
        if (event.action == KeyEvent.ACTION_DOWN) {
            when (code) {
                KeyEvent.KEYCODE_BACK -> if (talker?.active == true) {
                    talker?.cancel()
                    screenEvent(JSONObject().put("type", "idle"))
                    return true
                }
                KeyEvent.KEYCODE_DPAD_DOWN, KeyEvent.KEYCODE_MENU, KeyEvent.KEYCODE_SETTINGS, KeyEvent.KEYCODE_INFO -> {
                    if (talker?.active != true) showSetup(null)
                    return true
                }
            }
        }
        return super.dispatchKeyEvent(event)
    }

    // ----------------------------------------------------------------------------------- talking
    private fun startTalking() {
        if (onSetup || prefs.bridgeUrl.isBlank()) return
        if (Build.VERSION.SDK_INT >= 23 && checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            pendingTalk = true
            requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), REQ_MIC)
            return
        }
        talker?.cancel()
        talkStartedAt = SystemClock.uptimeMillis()
        val locale = Locale.getDefault().toLanguageTag()
        talker = Talker(this, Api(prefs.bridgeUrl, prefs.token), locale, this).also { it.start(prefs.mode) }
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != REQ_MIC || !pendingTalk) return
        pendingTalk = false
        if (grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED) startTalking()
        else onFailed("no-permission")
    }

    override fun onListening() {
        screenEvent(JSONObject().put("type", "listening").put("id", "local").put("source", "tv"))
    }

    override fun onLevel(level: Float) {
        val now = SystemClock.uptimeMillis()
        if (now - lastLevelAt < 40) return                 // ~25 updates a second is plenty
        lastLevelAt = now
        js("HermesScreen.level(${"%.3f".format(Locale.ROOT, level)})")
    }

    override fun onPartial(text: String) {
        screenEvent(JSONObject().put("type", "listening").put("id", "local").put("source", "tv").put("partial", text))
    }

    override fun onSending(heard: String?) {
        js("HermesScreen.level(0)")
        screenEvent(JSONObject().put("type", "thinking").put("id", "local").put("source", "tv").put("heard", heard ?: ""))
    }

    override fun onReply(result: JSONObject) {
        val status = result.optInt("status")
        val audio = result.optString("audio")
        when {
            status == 200 && audio.isNotEmpty() -> {
                // The page also gets this reply from the bridge's live feed; make sure it shows even if
                // that feed is late, then speak it.
                screenEvent(JSONObject().put("type", "reply").put("id", result.optString("id", "local"))
                    .put("source", "tv").put("heard", result.optString("heard"))
                    .put("text", result.optString("reply")).put("duration", result.optDouble("duration", 4.0))
                    .put("ts", System.currentTimeMillis() / 1000.0))
                js("HermesScreen.play(${JSONObject.quote(audio)})")
            }
            status == 422 -> onFailed("nothing heard")
            status == 401 -> onFailed(if (fr) "Jeton TV refusé : vérifiez les réglages" else "TV token refused: check the settings")
            status == 403 -> onFailed(if (fr) "Appli TV désactivée sur le bridge (TV_TOKEN)" else "TV app disabled on the bridge (TV_TOKEN)")
            status == 0 -> onFailed(if (fr) "Bridge injoignable" else "Bridge unreachable")
            else -> onFailed(result.optString("error").ifEmpty { "HTTP $status" })
        }
    }

    override fun onFailed(reason: String) {
        js("HermesScreen.level(0)")
        val message = when (reason) {
            "no-mic" -> if (fr) "Micro indisponible" else "Microphone unavailable"
            "no-permission" -> if (fr) "Autorisez le micro pour Hermes" else "Allow the microphone for Hermes"
            "no-recognizer" -> if (fr) "Pas de reconnaissance vocale sur cette TV : passez en mode Whisper" else "No speech recogniser on this TV: use Whisper mode"
            else -> reason
        }
        screenEvent(JSONObject().put("type", "error").put("id", "local").put("source", "tv").put("message", message))
    }

    companion object {
        private const val REQ_MIC = 7
        private const val HOLD_MS = 700L
    }
}

/** Shown on the settings page. Kept by hand to avoid enabling BuildConfig generation. */
object BuildConfigVersion {
    const val NAME = "0.1.0"
}
