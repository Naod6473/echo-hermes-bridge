package io.github.naod6473.hermestv

import android.content.Context

/** The app's three settings. */
class Prefs(context: Context) {
    private val p = context.getSharedPreferences("hermes", Context.MODE_PRIVATE)

    var bridgeUrl: String
        get() = p.getString("bridge", "") ?: ""
        set(v) = p.edit().putString("bridge", normalizeUrl(v)).apply()

    var token: String
        get() = p.getString("token", "") ?: ""
        set(v) = p.edit().putString("token", v.trim()).apply()

    /** "auto": the bridge's Whisper, or Android's recogniser if the TV gives the app a silent mic.
     *  "whisper": always the bridge. "android": always the TV's own recogniser. */
    var mode: String
        get() = p.getString("mode", "auto") ?: "auto"
        set(v) = p.edit().putString("mode", if (v in MODES) v else "auto").apply()

    /** Look of the screen: "" = the bridge's default (SCREEN_AVATAR), or orb / medallion / spectrum / hologram. */
    var avatar: String
        get() = p.getString("avatar", "") ?: ""
        set(v) = p.edit().putString("avatar", if (v in AVATARS) v else "").apply()

    /** Avatar drawing: "" = the bridge's default, white or black ink. */
    var ink: String
        get() = p.getString("ink", "") ?: ""
        set(v) = p.edit().putString("ink", if (v in INKS) v else "").apply()

    companion object {
        val MODES = setOf("auto", "whisper", "android")
        val AVATARS = setOf("orb", "medallion", "spectrum", "hologram")
        val INKS = setOf("white", "black")

        fun normalizeUrl(raw: String): String {
            var u = raw.trim().trimEnd('/')
            if (u.isEmpty()) return u
            if (!u.startsWith("http://") && !u.startsWith("https://")) u = "http://$u"
            // "192.168.1.64" -> the bridge's default port
            val hostPart = u.substringAfter("://").substringBefore('/')
            if (!hostPart.contains(':')) u = u.replaceFirst(hostPart, "$hostPart:8765")
            return u
        }
    }
}
