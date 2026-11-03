package local.phonehelper

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.Build
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.InetAddress
import java.net.URL

/**
 * HTTP to the Mac's /phone endpoints (design §2.1). The phone always calls out; it never listens.
 * Plain HttpURLConnection and Android's own org.json, no libraries.
 */
object Channel {
    data class Reply(val code: Int, val body: String)

    @Volatile private var poll: HttpURLConnection? = null          // the open long-poll, the only thing abort() drops

    fun server(): String = Net.serverUrl(BuildConfig.DEBUG, Net.isEmulator(Build.HARDWARE))

    /** Trade a pairing code for a device key. Returns null when paired, else what went wrong in plain words. */
    fun pair(ctx: Context, code: String): String? {
        val body = JSONObject(mapOf("code" to code.trim(), "name" to "phone-helper", "app_version" to BuildConfig.VERSION_NAME))
        val r = try {
            request("POST", "/phone/api/pair", body.toString(), null, 15_000)
        } catch (e: IOException) {
            return "Can't reach the Mac (${e.javaClass.simpleName})"
        }
        if (r.code != 200) return if (r.code == 403) "Wrong or used code" else "The Mac said ${r.code}"
        val j = JSONObject(r.body)
        Prefs.save(ctx, j.getString("device"), j.getString("key"))
        return null
    }

    /** One long-poll: 200 with an order, 204 after [waitS] s (at most [Net.WAIT_S]) with nothing. */
    fun next(ctx: Context, creds: Prefs.Creds, waitS: Int = Net.WAIT_S): Reply {
        val extra = mapOf("X-App-Version" to BuildConfig.VERSION_NAME, "X-Phone-Net" to net(ctx), "X-Phone-Reads" to Inbox.reads(ctx))
        return request("GET", "/phone/api/next?wait=$waitS", null, creds, (waitS + 10) * 1000, extra, isPoll = true)
    }

    fun result(creds: Prefs.Creds, res: Map<String, Any?>): Reply =
        request("POST", "/phone/api/result", JSONObject(res).toString(), creds, 15_000)

    /** New incoming texts and missed calls (Inbox.kt): 200 taken, 400 refused, 404 a Mac older than the inbox. */
    fun inbox(creds: Prefs.Creds, items: List<Map<String, Any?>>): Reply =
        request("POST", "/phone/api/inbox", JSONObject(mapOf("items" to JSONArray(items.map { JSONObject(it) }))).toString(), creds, 15_000)

    /** Drop the open long-poll at once. Only for a real network change (the old socket may be dead without knowing
     *  it): the Mac may hand an order to a poll the phone has dropped, and that order is then lost (a safe "no
     *  answer", never a double call), so nothing else drops it. Never touches a result being sent. */
    fun abort() {
        poll?.disconnect()
    }

    /** design §10: the tailnet name must resolve to a tailnet address, or Tailscale is off. Emulator route skips it. */
    fun tailnetReady(): Boolean {
        val host = URL(server()).host
        if (host == "10.0.2.2") return true
        return try {
            Net.isTailnet(InetAddress.getByName(host).address)
        } catch (e: Exception) {
            false
        }
    }

    private fun request(
        method: String, path: String, body: String?, creds: Prefs.Creds?, readMs: Int,
        extra: Map<String, String> = emptyMap(), isPoll: Boolean = false,
    ): Reply {
        val c = URL(server() + path).openConnection() as HttpURLConnection
        if (isPoll) poll = c
        try {
            c.requestMethod = method
            c.connectTimeout = 10_000
            c.readTimeout = readMs
            c.useCaches = false
            if (creds != null) {
                c.setRequestProperty("Authorization", "Bearer ${creds.key}")
                c.setRequestProperty("X-Phone-Device", creds.device)
            }
            extra.forEach { (k, v) -> c.setRequestProperty(k, v) }
            if (body != null) {
                val bytes = body.toByteArray()
                c.doOutput = true
                c.setRequestProperty("Content-Type", "application/json")
                c.setFixedLengthStreamingMode(bytes.size)
                c.outputStream.use { it.write(bytes) }
            }
            val code = c.responseCode
            val stream = if (code >= 400) c.errorStream else c.inputStream
            return Reply(code, stream?.bufferedReader()?.use { it.readText() } ?: "")
        } finally {
            c.disconnect()
            if (poll === c) poll = null
        }
    }

    private fun net(ctx: Context): String {
        val cm = ctx.getSystemService(ConnectivityManager::class.java)
        val caps = cm.getNetworkCapabilities(cm.activeNetwork) ?: return "other"
        return when {
            caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) -> "wifi"
            caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) -> "cellular"
            else -> "other"
        }
    }

    /** org.json → plain maps and lists, for [Orders]. */
    fun toMap(j: JSONObject): Map<String, Any?> = j.keys().asSequence().associateWith { plain(j.opt(it)) }

    private fun plain(x: Any?): Any? = when (x) {
        is JSONObject -> toMap(x)
        is JSONArray -> (0 until x.length()).map { plain(x.opt(it)) }
        JSONObject.NULL -> null
        else -> x
    }
}
