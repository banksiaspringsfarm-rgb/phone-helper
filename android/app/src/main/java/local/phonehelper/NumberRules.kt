package local.phonehelper

/**
 * Which saved numbers the phone may ever ring or text (design §5). Pure Kotlin so the JVM tests cover every rule;
 * applied at `find` (sets `callable`) and again just before a `call`/`text`. It sees the number exactly as saved.
 * A refusal's [Verdict.whyNot] is plain words with no digits, so voice Claude can say it as it stands.
 */
object NumberRules {
    /** [dial] is the number to hand the dialler ("" when refused); [overseas] means the read-back must say so. */
    data class Verdict(val ok: Boolean, val dial: String, val whyNot: String, val overseas: Boolean = false)

    const val SYMBOLS = "the saved number has symbols in it that phone codes use"
    const val EMERGENCY = "it is an emergency number, and I never ring those"
    const val PREMIUM = "it is a premium or paid service number"
    const val NOT_AU = "it is not an Australian phone or mobile number I am allowed to ring"
    const val COSTLY_OVERSEAS = "it is a satellite or worldwide service number that costs a lot"
    const val BAD_OVERSEAS = "it does not look like a whole overseas number"
    const val EMPTY = "there is no number saved"

    private val EMERGENCY_NUMBERS = setOf("000", "112", "106", "911", "999")
    private val COSTLY_CODES = listOf("870", "881", "882", "883", "979", "991")
    private val AU_SHAPES = listOf(
        Regex("04\\d{8}"),                  // mobiles
        Regex("0[2378]\\d{8}"),             // landlines
        Regex("1[38]00\\d{6}"),             // 1300 / 1800
        Regex("13\\d{4}"),                  // 13 numbers
    )
    private val PREMIUM_START = listOf("19", "0055", "0198")

    /**
     * The verdict for [saved]. [isEmergency] is the phone's own list (TelephonyManager.isEmergencyNumber), asked
     * about the cleaned number and the one that would be dialled; the fixed list above applies regardless.
     */
    fun check(saved: String, isEmergency: (String) -> Boolean = { false }): Verdict {
        // 1. Clean: spacing and grouping marks go; any other symbol (*, #, pauses, a + not in front) is refused.
        val clean = saved.filterNot { it.isWhitespace() || it in "-.()" }
        if (clean.isEmpty()) return no(EMPTY)
        val body = clean.removePrefix("+")
        if (body.isEmpty() || body.any { it !in '0'..'9' }) return no(SYMBOLS)

        // 2. Normalise: 0011 is Australia's way of dialling out, the same as a leading +.
        val intl = when {
            clean.startsWith("+") -> body
            clean.startsWith("0011") -> clean.removePrefix("0011")
            else -> null
        }
        val number = when {
            intl == null -> clean
            intl.startsWith("61") -> domestic(intl.removePrefix("61"))
            else -> null                    // another country: rule 5
        }

        // 3. Emergency, always refused, in every form it might be saved in.
        val forms = listOfNotNull(clean, body, intl, number)
        if (forms.any { it in EMERGENCY_NUMBERS || isEmergency(it) }) return no(EMERGENCY)

        // 5. International, only because the contact is saved that way (v1 dials saved contacts only).
        if (number == null) return overseas(intl!!)       // number is only null for another country code

        // 4. Allowed Australian shapes only; everything else (premium, short codes, satellite 014x) is refused.
        if (AU_SHAPES.any { it.matches(number) }) return Verdict(true, number, "")
        return no(if (PREMIUM_START.any { number.startsWith(it) }) PREMIUM else NOT_AU)
    }

    /**
     * "+61 4…" → "04…". Numbers starting 1 (13, 1300, 1800, and the 19xx and short codes rule 4 refuses) have no
     * trunk 0, and "+61 (0)4…", saved with its 0, keeps the one it has.
     */
    private fun domestic(rest: String) = if (rest.startsWith("1") || rest.startsWith("0")) rest else "0$rest"

    /** [intl] is the country code and number after the + or 0011: E.164 is at most 15 digits. */
    private fun overseas(intl: String): Verdict {
        if (COSTLY_CODES.any { intl.startsWith(it) }) return no(COSTLY_OVERSEAS)
        if (intl.length !in 8..15 || intl.startsWith("0")) return no(BAD_OVERSEAS)
        return Verdict(true, "+$intl", "", overseas = true)
    }

    private fun no(why: String) = Verdict(false, "", why)
}
