package local.phonehelper

import java.text.Normalizer
import kotlin.random.Random

/**
 * The `find` order's pure part (design §3): which saved numbers a spoken name means, and the 120 s memory of what
 * was offered. Plain Kotlin so the JVM tests cover it; Contacts.kt reads the rows off the phone.
 * The full number, lookup key and row id stay in here; only name, label and last 3 digits go into a result.
 */
object ContactMatch {
    /** One phone-number row of a contact, with the contact's nicknames. */
    data class Row(
        val key: String, val rowId: Long, val name: String, val nicknames: List<String>,
        val label: String, val number: String,
    )

    /** What a pick remembers on the phone, never sent: enough to re-read the number and check it at dial time. */
    data class Pick(val key: String, val rowId: Long, val action: String, val last3: String)

    const val MAX = 3
    const val KEEP_MS = 120_000L

    // Ranks, best first. Exact name, exact nickname and a whole word of the name are one tier ("Lena" offers both
    // "Lena" and "Lena Smith"); a prefix and a one-letter slip only count when nothing in a better tier matched.
    private const val EXACT = 0
    private const val NICK = 1
    private const val WORD = 2
    private const val PREFIX = 3
    private const val SLIP = 4
    private val TIER = intArrayOf(0, 0, 0, 1, 2)

    /** Up to [MAX] rows for [spoken], best first; rows of the best tier that matched only. */
    fun match(spoken: String, rows: List<Row>): List<Row> {
        val want = fold(spoken)
        if (want.isEmpty()) return emptyList()
        val seen = HashSet<Pair<String, String>>()
        val ranked = rows
            .filter { hasName(it) && seen.add(it.key to digits(it.number)) }      // one per contact and number
            .mapNotNull { r -> rank(want, r)?.let { it to r } }
        val best = ranked.minOfOrNull { TIER[it.first] } ?: return emptyList()
        return ranked.filter { TIER[it.first] == best }
            .sortedWith(compareBy({ it.first }, { fold(it.second.name) }))     // stable: a contact's numbers keep order
            .take(MAX).map { it.second }
    }

    private fun rank(want: String, r: Row): Int? {
        val name = fold(r.name)
        val nicks = r.nicknames.map(::fold).filter { it.isNotEmpty() }
        val words = name.split(' ')
        return when {
            name == want -> EXACT
            want in nicks -> NICK
            want in words -> WORD
            name.startsWith(want) || nicks.any { it.startsWith(want) } || words.any { it.startsWith(want) } -> PREFIX
            want.length >= 4 && (listOf(name) + nicks + words).any { oneEdit(want, it) } -> SLIP
            else -> null
        }
    }

    /** A contact with no name shows its number as the name: skip it (nothing to read back). */
    private fun hasName(r: Row) = fold(r.name).isNotEmpty()

    /** Lower case, accents off, letters and single spaces only: "  Zoë  O'Brien " → "zoe obrien". */
    fun fold(s: String): String =
        Normalizer.normalize(s, Normalizer.Form.NFD)
            .replace(Regex("\\p{M}+"), "")
            .lowercase()
            .replace(Regex("['’]"), "")
            .replace(Regex("[^\\p{L}]+"), " ")
            .trim()

    /** Exactly one letter added, dropped or changed ("lene" ~ "lena"). */
    fun oneEdit(a: String, b: String): Boolean {
        if (a == b || kotlin.math.abs(a.length - b.length) > 1) return false
        var i = 0
        while (i < a.length && i < b.length && a[i] == b[i]) i++
        return when {
            a.length == b.length -> a.substring(i + 1) == b.substring(i + 1)
            a.length > b.length -> a.substring(i + 1) == b.substring(i)
            else -> a.substring(i) == b.substring(i + 1)
        }
    }

    fun digits(number: String) = number.filter { it in '0'..'9' }
    fun last3(number: String) = digits(number).takeLast(3)

    /** Letters only: the Mac refuses any id that could carry a number or a lookup key (phone.py OPAQUE_ID). */
    fun newId(prefix: String, rnd: Random = Random.Default) =
        prefix + "_" + (1..10).map { ('a'..'z').random(rnd) }.joinToString("")

    /**
     * The phone's memory of recent finds: find id → pick id → [Pick], dropped after [KEEP_MS]. In memory only, so
     * it is gone if the app restarts (voice Claude just finds again). A pick is used up by its first take.
     */
    class Finds(private val nowMs: () -> Long, private val rnd: Random = Random.Default) {
        private class Entry(val at: Long, val picks: MutableMap<String, Pick>)
        private val finds = HashMap<String, Entry>()

        /**
         * Remember [rows] for [action]; returns the find id and the result `matches` (no number, no key), each with
         * [check]'s number-rules verdict (design §5) as `callable`, `why_not` and `overseas`.
         */
        @Synchronized
        fun offer(rows: List<Row>, action: String, check: (Row) -> NumberRules.Verdict): Map<String, Any?> {
            prune()
            val picks = LinkedHashMap<String, Pick>()
            val matches = rows.map { r ->
                val pick = newId("p", rnd)
                val last3 = last3(r.number)
                picks[pick] = Pick(r.key, r.rowId, action, last3)
                val verdict = check(r)
                mapOf("pick" to pick, "name" to r.name, "label" to r.label, "last3" to last3,
                    "callable" to verdict.ok, "why_not" to verdict.whyNot, "overseas" to verdict.overseas)
            }
            val find = newId("f", rnd)
            finds[find] = Entry(nowMs(), picks)
            return mapOf("find" to find, "matches" to matches)
        }

        /** The pick, once: a second take, an unknown id or one older than [KEEP_MS] gives null. */
        @Synchronized
        fun take(find: String, pick: String): Pick? {
            prune()
            return finds[find]?.picks?.remove(pick)
        }

        private fun prune() {
            val now = nowMs()
            finds.entries.removeAll { now - it.value.at > KEEP_MS }
        }
    }
}
