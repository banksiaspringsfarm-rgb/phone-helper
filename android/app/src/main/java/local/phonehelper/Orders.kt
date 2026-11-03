package local.phonehelper

/**
 * What the phone answers to one order (design §2.6). Pure Kotlin on plain maps, so the JVM tests cover it;
 * the service turns org.json into maps and back. Unknown kinds get `unsupported`, so a newer Mac never breaks
 * an older app and later kinds slot in without a new channel.
 */
object Orders {
    /** 6+ digits in a row, spacing allowed: the same shape the Mac refuses (mac/phone.py NUMBER_LIKE). */
    private val NUMBER_LIKE = Regex("""\d(?:[\s()./,_+-]*\d){5,}""")

    /** The result for [order], or null when it has no id to answer to. [overdue]: its deadline passed before
     *  the phone got to it (arrival + ttl_s − 5 s), so it is cancelled rather than acted on. */
    fun answer(
        order: Map<String, Any?>, version: String, nowS: Double, overdue: Boolean,
        find: (name: String, action: String) -> Map<String, Any?> = { _, _ -> throw SecurityException() },
        call: (find: String, pick: String) -> Answer = { _, _ -> Answer(false, "failed", null, "calls are not wired") },
        text: (find: String, pick: String, body: String) -> Answer = { _, _, _ -> Answer(false, "failed", null, "texts are not wired") },
        ping: () -> Map<String, Any?>,
    ): Map<String, Any?>? {
        val id = order["id"] as? String ?: return null
        val kind = order["kind"] as? String ?: ""
        val (ok, outcome, data, error) = when {
            overdue -> Answer(false, "cancelled", null, "order arrived too late")
            kind == "ping" -> Answer(true, "done", ping(), "")
            kind == "find" -> find(order["args"] as? Map<*, *> ?: emptyMap<String, Any?>(), find)
            kind == "call" -> call(order["args"] as? Map<*, *> ?: emptyMap<String, Any?>(), call)
            kind == "text" -> text(order["args"] as? Map<*, *> ?: emptyMap<String, Any?>(), text)
            else -> Answer(false, "unsupported", null, "this app version does not know '$kind'")
        }
        return scrub(
            mapOf(
                "v" to 1, "id" to id, "kind" to kind, "ok" to ok, "outcome" to outcome, "data" to data,
                "error" to error, "app_version" to version, "at" to nowS,
            )
        ) as Map<String, Any?>
    }

    /** [res] with data[[key]] (elapsed-realtime ms when the call was placed or the text went) turned into
     *  data.sent_after_s, seconds after the result's `at` (read at elapsed [t0Ms]); the Mac adds the two for the time it
     *  says. A raw clock reading never leaves the phone (it would look like a number to the Mac). */
    fun sentAfter(res: Map<String, Any?>, key: String, t0Ms: Long): Map<String, Any?> {
        val data = res["data"] as? Map<*, *> ?: return res
        val at = (data[key] as? Number)?.toLong() ?: return res
        val d = data.entries.filter { it.key != key }.associate { it.key.toString() to it.value } +
            ("sent_after_s" to Math.round((at - t0Ms) / 100.0) / 10.0)
        return res + ("data" to d.filter { it.value != null })
    }

    /** The phone's deadline for an order, in elapsed-realtime ms: arrival + ttl_s − 5 s (design §2.4). */
    fun deadlineMs(order: Map<String, Any?>, arrivedMs: Long): Long {
        val ttl = (order["ttl_s"] as? Number)?.toDouble() ?: 0.0
        return arrivedMs + ((ttl - 5.0) * 1000).toLong()
    }

    /** Every string anywhere in [x] with number-like runs replaced by "…", so no full number leaves the phone
     *  (design §3 digit scrub; the Mac refuses them anyway). */
    fun scrub(x: Any?): Any? = when (x) {
        is String -> NUMBER_LIKE.replace(x, "…")
        is Map<*, *> -> x.entries.associate { (k, v) -> scrub(k.toString()) as String to scrub(v) }
        is List<*> -> x.map { scrub(it) }
        else -> x
    }

    /** `find` (design §3): args {name, action: call|text, default call}; data {find, matches[≤3]}. No match is
     *  still `done` with an empty list, so voice Claude can say "no Lena in your contacts". */
    private fun find(args: Map<*, *>, look: (String, String) -> Map<String, Any?>): Answer {
        val name = (args["name"] as? String)?.trim().orEmpty()
        val action = (args["action"] as? String) ?: "call"
        return when {
            name.isEmpty() || name.length > 80 -> Answer(false, "refused", null, "no name to look up")
            action != "call" && action != "text" -> Answer(false, "refused", null, "action must be call or text")
            else -> try {
                Answer(true, "done", look(name, action), "")
            } catch (e: SecurityException) {
                Answer(false, "failed", null, "the app is not allowed to read contacts")
            }
        }
    }

    /** The phone's own ids, as the Mac checks them (phone.py OPAQUE_ID): letters, or at most 5 characters. */
    private val ID = Regex("""[a-z]_(?:[A-Za-z]{1,32}|[A-Za-z0-9]{1,5})""")

    /** `call` (design §2.6, §6): args {find, pick}, never a number. [dial] does the re-check, the wait and the dial
     *  (it can take a minute); its answer goes back as it is. */
    private fun call(args: Map<*, *>, dial: (String, String) -> Answer): Answer {
        val find = args["find"] as? String ?: ""
        val pick = args["pick"] as? String ?: ""
        if (!ID.matches(find) || !ID.matches(pick) || !find.startsWith("f_") || !pick.startsWith("p_")) {
            return Answer(false, "refused", null, "a call needs the lookup it came from")
        }
        return try {
            dial(find, pick)
        } catch (e: SecurityException) {
            Answer(false, "failed", null, "the app is not allowed to make calls")
        }
    }

    /** `text` (design §11.4): args {find, pick, body}, never a number. [send] re-checks, waits and sends; the body is
     *  never put in the answer (its `data` is only after_s, parts and how). */
    private fun text(args: Map<*, *>, send: (String, String, String) -> Answer): Answer {
        val find = args["find"] as? String ?: ""
        val pick = args["pick"] as? String ?: ""
        val body = args["body"] as? String
        if (!ID.matches(find) || !ID.matches(pick) || !find.startsWith("f_") || !pick.startsWith("p_")) {
            return Answer(false, "refused", null, "a text needs the lookup it came from")
        }
        if (body == null) return Answer(false, "refused", null, "the text is empty")
        return try {
            send(find, pick, body)
        } catch (e: SecurityException) {
            // Was always "read contacts", which hid that the 3 Nov failure came from the SMS side.
            Answer(false, "failed", null, deniedWhy(e.message))
        }
    }

    /** Plain words for a text's SecurityException: Contacts throws "READ_CONTACTS"; anything else is the SMS side. */
    fun deniedWhy(message: String?): String =
        if (message == "READ_CONTACTS") "the app is not allowed to read contacts"
        else "the app is not allowed to send texts; open the phone helper and allow SMS"

    data class Answer(val ok: Boolean, val outcome: String, val data: Map<String, Any?>?, val error: String)
}
