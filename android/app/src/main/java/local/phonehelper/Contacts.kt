package local.phonehelper

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.os.SystemClock
import android.provider.ContactsContract.CommonDataKinds.Nickname
import android.provider.ContactsContract.CommonDataKinds.Phone
import android.provider.ContactsContract.Data
import android.telephony.TelephonyManager

/**
 * Reads the phone's own contacts for a `find` order (design §3, READ_CONTACTS only): every phone-number row with
 * its contact's name and nicknames, matched by ContactMatch. Relation rows ("Mum" on someone else's card) are
 * deliberately not read: matching one would ring the wrong person.
 */
object Contacts {
    val finds = ContactMatch.Finds(SystemClock::elapsedRealtime)

    /** The `find` result data for [spoken] (`callable` per [action]: a text needs a mobile, §11.2); throws SecurityException without READ_CONTACTS. */
    fun find(ctx: Context, spoken: String, action: String): Map<String, Any?> {
        if (ctx.checkSelfPermission(Manifest.permission.READ_CONTACTS) != PackageManager.PERMISSION_GRANTED) {
            throw SecurityException("READ_CONTACTS")
        }
        val rows = ContactMatch.match(spoken, rows(ctx))
        val isEmergency = emergencyCheck(ctx)
        return finds.offer(rows, action) { Texts.verdict(action, it.number, isEmergency) }
    }

    /**
     * The phone's own emergency list (SIM, network, locale) for NumberRules, on top of its fixed one. If telephony
     * cannot answer (no SIM service, a tablet) it says no and the fixed list still holds.
     */
    fun emergencyCheck(ctx: Context): (String) -> Boolean {
        val tm = ctx.getSystemService(TelephonyManager::class.java) ?: return { false }
        return { n -> try { tm.isEmergencyNumber(n) } catch (e: RuntimeException) { false } }
    }

    /** The number saved on [pick]'s row now, or null if that row is gone or no longer this contact's phone number. */
    fun number(ctx: Context, pick: ContactMatch.Pick): String? {
        if (ctx.checkSelfPermission(Manifest.permission.READ_CONTACTS) != PackageManager.PERMISSION_GRANTED) {
            throw SecurityException("READ_CONTACTS")
        }
        ctx.contentResolver.query(
            Data.CONTENT_URI, arrayOf(Data.LOOKUP_KEY, Data.MIMETYPE, Data.DATA1),
            "${Data._ID} = ?", arrayOf(pick.rowId.toString()), null,
        )?.use { c ->
            if (c.moveToFirst() && c.getString(0) == pick.key && c.getString(1) == Phone.CONTENT_ITEM_TYPE) return c.getString(2)
        }
        return null
    }

    /** The display name on [pick]'s contact now (for route B's "Tap to text" notification), or null. */
    fun name(ctx: Context, pick: ContactMatch.Pick): String? {
        ctx.contentResolver.query(
            Data.CONTENT_URI, arrayOf(Data.LOOKUP_KEY, Data.DISPLAY_NAME_PRIMARY),
            "${Data._ID} = ?", arrayOf(pick.rowId.toString()), null,
        )?.use { c -> if (c.moveToFirst() && c.getString(0) == pick.key) return c.getString(1) }
        return null
    }

    private fun rows(ctx: Context): List<ContactMatch.Row> {
        val nicks = HashMap<String, MutableList<String>>()
        val phones = ArrayList<Array<Any?>>()
        ctx.contentResolver.query(
            Data.CONTENT_URI,
            arrayOf(Data._ID, Data.LOOKUP_KEY, Data.DISPLAY_NAME_PRIMARY, Data.MIMETYPE, Data.DATA1, Phone.TYPE, Phone.LABEL),
            "${Data.MIMETYPE} IN (?, ?)", arrayOf(Phone.CONTENT_ITEM_TYPE, Nickname.CONTENT_ITEM_TYPE),
            "${Data.DISPLAY_NAME_PRIMARY}, ${Data.IS_SUPER_PRIMARY} DESC, ${Data._ID}",
        )?.use { c ->
            while (c.moveToNext()) {
                val key = c.getString(1) ?: continue
                val value = c.getString(4) ?: continue
                if (c.getString(3) == Nickname.CONTENT_ITEM_TYPE) {
                    nicks.getOrPut(key) { mutableListOf() } += value
                } else {
                    val label = Phone.getTypeLabel(ctx.resources, c.getInt(5), c.getString(6)).toString()
                    phones += arrayOf(key, c.getLong(0), c.getString(2) ?: "", label, value)
                }
            }
        }
        return phones.map { p ->
            val key = p[0] as String
            ContactMatch.Row(key, p[1] as Long, p[2] as String, nicks[key].orEmpty(), p[3] as String, p[4] as String)
        }
    }
}
