package local.phonehelper

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.net.Uri
import android.provider.CallLog
import android.provider.ContactsContract.CommonDataKinds.Phone
import android.provider.ContactsContract.PhoneLookup
import android.provider.Telephony
import android.util.Log

/**
 * Incoming texts and missed calls to the Mac (the user, 4 Nov 2026, so voice Claude can say
 * "Lena tried to call you at 5:40" and read his texts out. Sideload only (READ_SMS, READ_CALL_LOG; the Play build
 * removes both until its SMS and call-log declarations). Reads the phone's own SMS inbox and call log for anything
 * newer than what the Mac already took; the sender goes as a saved name, a sender name ("Telstra") or only the last 3
 * digits, never a full number. A text's words go to the Mac (tailnet only) and nowhere else, never to logcat.
 * Not seen: RCS chat messages and MMS (Google Messages keeps those outside the SMS inbox).
 */
object Inbox {
    private const val FILE = "inbox"
    const val MAX_POST = 50                          // the Mac's INBOX_MAX_POST
    const val BODY_MAX = 1500

    private val CONTROL = Regex("[\\x00-\\x09\\x0b-\\x1f\\x7f-\\x9f\\u2028\\u2029]")
    private val NUMBER_LIKE = Regex("\\d(?:[\\s()./,_+-]*\\d){5,}")     // the Mac's NUMBER_LIKE

    /** Pure: one item for the Mac. [address] is the raw sender or caller, [saved] the contact's name if any. */
    fun item(kind: String, atMs: Long, address: String?, saved: String?, body: String? = null): Map<String, Any?> {
        val a = address.orEmpty().trim()
        val letters = a.any { it.isLetter() }
        val digits = a.filter { it.isDigit() }
        val name = saved?.trim().orEmpty()
        val who = when {
            name.isNotEmpty() && !NUMBER_LIKE.containsMatchIn(name) -> name.take(60)
            letters && !NUMBER_LIKE.containsMatchIn(a) -> a.take(40)      // an alphanumeric sender: "Telstra"
            else -> ""
        }
        return buildMap {
            put("kind", kind)
            put("at", atMs / 1000.0)
            put("who", CONTROL.replace(who, " "))
            put("known", who.isNotEmpty() && who == name.take(60))
            put("last3", if (letters || digits.length < 3) "" else digits.takeLast(3))   // "-2" = a hidden caller
            if (kind == "sms") put("body", CONTROL.replace(body.orEmpty(), " ").take(BODY_MAX))
        }
    }

    /** Pure: what may be read, for the X-Phone-Reads header ("sms,calls", "sms", "calls" or "none"). */
    fun reads(sms: Boolean, calls: Boolean): String =
        listOfNotNull("sms".takeIf { sms }, "calls".takeIf { calls }).joinToString(",").ifEmpty { "none" }

    fun reads(ctx: Context): String = reads(granted(ctx, Manifest.permission.READ_SMS), granted(ctx, Manifest.permission.READ_CALL_LOG))

    /** Start from now the first time (a new install or the update to 0.2.3), so old history is never sent. */
    fun baseline(ctx: Context) {
        val p = ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE)
        if (p.contains("sms_after")) return
        val now = System.currentTimeMillis()
        p.edit().putLong("sms_after", now).putLong("call_after", now).apply()
    }

    /** Send anything new; the marks only move once the Mac took it, so a missed post is sent again next time. */
    @Synchronized fun check(ctx: Context, creds: Prefs.Creds) {
        val p = ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE)
        if (!p.contains("sms_after")) return
        val sms = if (granted(ctx, Manifest.permission.READ_SMS)) texts(ctx, p.getLong("sms_after", 0)) else emptyList()
        val calls = if (granted(ctx, Manifest.permission.READ_CALL_LOG)) missed(ctx, p.getLong("call_after", 0)) else emptyList()
        val all = (sms + calls).sortedBy { it.first }.take(MAX_POST)
        if (all.isEmpty()) return
        val code = runCatching { Channel.inbox(creds, all.map { it.second }).code }.getOrDefault(-1)
        // 400: the Mac refused the shape; sending it again would block everything behind it, so it is passed
        if (code != 200 && code != 400) {
            Log.i(PhoneService.TAG, "inbox: ${all.size} new not taken yet (${if (code < 0) "not delivered" else code})")
            return
        }
        val e = p.edit()
        all.filter { it.second["kind"] == "sms" }.maxOfOrNull { it.first }?.let { e.putLong("sms_after", it) }
        all.filter { it.second["kind"] == "missed" }.maxOfOrNull { it.first }?.let { e.putLong("call_after", it) }
        e.apply()
        Log.i(PhoneService.TAG, "inbox: ${all.size} sent (${if (code == 200) "taken" else "refused, passed"})")
    }

    private fun texts(ctx: Context, after: Long): List<Pair<Long, Map<String, Any?>>> {
        val out = ArrayList<Pair<Long, Map<String, Any?>>>()
        ctx.contentResolver.query(
            Telephony.Sms.Inbox.CONTENT_URI, arrayOf(Telephony.Sms.ADDRESS, Telephony.Sms.BODY, Telephony.Sms.DATE),
            "${Telephony.Sms.DATE} > ?", arrayOf(after.toString()), "${Telephony.Sms.DATE} ASC",
        )?.use { c ->
            while (c.moveToNext() && out.size < MAX_POST) {
                val at = c.getLong(2)
                val from = c.getString(0)
                out += at to item("sms", at, from, name(ctx, from), c.getString(1))
            }
        }
        return out
    }

    private fun missed(ctx: Context, after: Long): List<Pair<Long, Map<String, Any?>>> {
        val out = ArrayList<Pair<Long, Map<String, Any?>>>()
        ctx.contentResolver.query(
            CallLog.Calls.CONTENT_URI, arrayOf(CallLog.Calls.NUMBER, CallLog.Calls.DATE, CallLog.Calls.CACHED_NAME),
            "${CallLog.Calls.TYPE} = ? AND ${CallLog.Calls.DATE} > ?",
            arrayOf(CallLog.Calls.MISSED_TYPE.toString(), after.toString()), "${CallLog.Calls.DATE} ASC",
        )?.use { c ->
            while (c.moveToNext() && out.size < MAX_POST) {
                val at = c.getLong(1)
                val from = c.getString(0)
                out += at to item("missed", at, from, name(ctx, from) ?: c.getString(2))
            }
        }
        return out
    }

    /** Pure: the same phone number written two ways ("0491 570 158" and "+61 491 570 158"): the last 9 digits, or
     *  all of them when either is shorter. */
    fun sameNumber(a: String?, b: String?): Boolean {
        val x = a.orEmpty().filter { it.isDigit() }
        val y = b.orEmpty().filter { it.isDigit() }
        if (x.length < 3 || y.length < 3) return false
        return if (x.length >= 9 && y.length >= 9) x.takeLast(9) == y.takeLast(9) else x == y
    }

    /** The saved contact's name for [number], or null (no READ_CONTACTS, not saved, a hidden number). Android's own
     *  lookup first; it can miss a text from 04.. against a contact saved as +61 4.., so then the saved numbers. */
    private fun name(ctx: Context, number: String?): String? {
        if (number.isNullOrBlank() || !number.any { it.isDigit() } || !granted(ctx, Manifest.permission.READ_CONTACTS)) return null
        return runCatching {
            ctx.contentResolver.query(
                Uri.withAppendedPath(PhoneLookup.CONTENT_FILTER_URI, Uri.encode(number)),
                arrayOf(PhoneLookup.DISPLAY_NAME), null, null, null,
            )?.use { c -> if (c.moveToFirst()) c.getString(0) else null }
                ?: ctx.contentResolver.query(Phone.CONTENT_URI, arrayOf(Phone.NUMBER, Phone.DISPLAY_NAME), null, null, null)
                    ?.use { c ->
                        var found: String? = null
                        while (found == null && c.moveToNext()) if (sameNumber(c.getString(0), number)) found = c.getString(1)
                        found
                    }
        }.getOrNull()
    }

    private fun granted(ctx: Context, p: String) = ctx.checkSelfPermission(p) == PackageManager.PERMISSION_GRANTED
}
