package local.phonehelper

/**
 * The `text` order's pure part (design §11.2, §11.4): which saved numbers may take a text (§5 plus mobile or saved
 * overseas), the body rules (the Mac's, checked again), the route (A: send it ourselves when SEND_SMS is granted,
 * B: a "Tap to text" notification), and folding the per-part sent reports into one outcome. Plain Kotlin so the
 * JVM tests cover it; Texter.kt reads the phone and sends.
 */
object Texts {
    const val BODY_MAX = 300                    // code points, as the Mac counts them (Python len)
    const val SENT_WAIT_MS = 30_000L

    const val LANDLINE = "that is a landline, so it can't take a text"
    const val NOT_MOBILE = "that is not a mobile number, so it can't take a text"
    const val WRONG_ACTION = "that lookup was for a call, not a text"

    /** The Mac's control-character rule (mac/phone.py CONTROL): a newline is kept. */
    private val CONTROL = Regex("[\\x00-\\x09\\x0b-\\x1f\\x7f-\\x9f\\u2028\\u2029]")

    /** The number rules for [action]: a call takes §5 as it stands, a text also needs a mobile or saved overseas number. */
    fun verdict(action: String, saved: String, isEmergency: (String) -> Boolean): NumberRules.Verdict {
        val v = NumberRules.check(saved, isEmergency)
        if (action != "text" || !v.ok || v.overseas || v.dial.startsWith("04")) return v
        return NumberRules.Verdict(false, "", if (v.dial.startsWith("0")) LANDLINE else NOT_MOBILE)
    }

    /** "" if [body] may be sent, else "empty", "long" or "control" (the Mac's words for the same rules). */
    fun bodyProblem(body: String?): String = when {
        body == null || body.isBlank() -> "empty"
        body.codePointCount(0, body.length) > BODY_MAX -> "long"
        CONTROL.containsMatchIn(body) -> "control"
        else -> ""
    }

    /** How long route A may wait for the sent reports: 30 s, less when the order's deadline is [leftMs] away (1 s spare). */
    fun sentWaitMs(leftMs: Long): Long = minOf(SENT_WAIT_MS, leftMs - 1_000).coerceAtLeast(1_000)

    /** How long route A waits for delivery reports once sent: 10 s, less near the order's deadline (2 s spare for the
     *  answer); 0 = do not wait. Delivery is a bonus: the network may never report it. */
    fun deliverWaitMs(leftMs: Long): Long = minOf(DELIVER_WAIT_MS, leftMs - SENT_WAIT_MS - 2_000).coerceAtLeast(0)
    const val DELIVER_WAIT_MS = 10_000L

    /** Route A ("sms") when the phone may send texts itself, else route B ("messages_app"). */
    fun route(sendSmsGranted: Boolean) = if (sendSmsGranted) "sms" else "messages_app"

    /** Route B's stand-in for SmsManager.divideMessage (only logged and reported): 160 GSM characters a text, or
     *  70 once anything is outside plain ASCII, and 153/67 a part when it takes more than one. */
    fun parts(body: String): ArrayList<String> {
        val cps = body.codePoints().toArray()
        val (one, many) = if (cps.all { it in 0x20..0x7e || it == 0x0a }) 160 to 153 else 70 to 67
        val size = if (cps.size <= one) one else many
        return ArrayList(cps.toList().chunked(size).map { String(it.toIntArray(), 0, it.size) }.ifEmpty { listOf("") })
    }

    /** Android's Activity.RESULT_OK, the code a part's sent PendingIntent carries when it went. */
    const val RESULT_OK = -1

    /** Plain words for SmsManager's error codes (RESULT_ERROR_*); anything else is "the text did not go". */
    fun reason(code: Int): String = when (code) {
        2 -> "the phone's radio is off"                                  // RESULT_ERROR_RADIO_OFF
        4 -> "the phone has no mobile service"                            // RESULT_ERROR_NO_SERVICE
        5, 7, 8 -> "the phone refused to send it (a limit or a short code)" // LIMIT_EXCEEDED, SHORT_CODE_*
        else -> "the text did not go (error $code)"
    }
    const val NO_REPORT = "the phone never said the text had gone"

    /** One text's parts: each reports once; all OK is sent, the first error fails it, silence at the end fails it. */
    class SentFold(private val parts: Int) {
        private val codes = HashMap<Int, Int>()

        @Synchronized fun report(part: Int, code: Int) { if (part in 0 until parts) codes.putIfAbsent(part, code) }

        /** null while still waiting; [timedOut] turns the wait into a failure. Else (ok, reason). */
        @Synchronized fun outcome(timedOut: Boolean): Pair<Boolean, String>? {
            codes.values.firstOrNull { it != RESULT_OK }?.let { return false to reason(it) }
            if (codes.size == parts) return true to ""
            return if (timedOut) false to NO_REPORT else null
        }
    }

    /** One text's delivery reports (TP-Status per part): true once every part says delivered (0-31), false at the
     *  first permanent failure (64+, or an unreadable report counts as unknown), null while unknown. */
    class DeliveredFold(private val parts: Int) {
        private val status = HashMap<Int, Int>()

        @Synchronized fun report(part: Int, tpStatus: Int) { if (part in 0 until parts && tpStatus >= 0) status[part] = tpStatus }

        @Synchronized fun outcome(): Boolean? = when {
            status.values.any { it >= 64 } -> false
            status.size == parts && status.values.all { it < 32 } -> true
            else -> null
        }
    }
}
