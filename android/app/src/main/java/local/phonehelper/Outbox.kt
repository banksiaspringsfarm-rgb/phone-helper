package local.phonehelper

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject

/**
 * Call and text results the Mac did not get (no signal, Mac restarting): kept and resent once the phone reaches the
 * Mac again, so the user still hears "your text to Lena sent at 4:05" (sent-confirmations, 3 Nov). Results hold no
 * number and no words (Orders.scrub), so keeping them is safe. Only calls and texts; at most [MAX], none older than
 * [KEEP_S] (the Mac's rundown looks back 3 days).
 */
object Outbox {
    private const val FILE = "outbox"
    const val MAX = 10
    const val KEEP_S = 3 * 86400.0

    /** Pure: which kept results (oldest first) still go, after adding [add]. */
    fun keep(all: List<Map<String, Any?>>, nowS: Double, add: Map<String, Any?>? = null): List<Map<String, Any?>> =
        (all + listOfNotNull(add))
            .filter { it["kind"] == "call" || it["kind"] == "text" }
            .filter { nowS - ((it["at"] as? Number)?.toDouble() ?: 0.0) < KEEP_S }
            .distinctBy { it["id"] }
            .takeLast(MAX)

    /** Pure: after a resend the Mac said [code]; true = drop it (taken, or refused for good), false = keep trying. */
    fun done(code: Int): Boolean = code in 200..499

    @Synchronized fun add(ctx: Context, res: Map<String, Any?>) =
        save(ctx, keep(load(ctx), System.currentTimeMillis() / 1000.0, res))

    @Synchronized fun load(ctx: Context): List<Map<String, Any?>> {
        val raw = ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE).getString("results", null) ?: return emptyList()
        return runCatching { JSONArray(raw).let { a -> (0 until a.length()).map { Channel.toMap(a.getJSONObject(it)) } } }
            .getOrDefault(emptyList())
    }

    @Synchronized fun save(ctx: Context, all: List<Map<String, Any?>>) {
        ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE).edit()
            .putString("results", JSONArray(all.map { JSONObject(it) }).toString()).apply()
    }
}
