package local.phonehelper

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** design §11.6 unit line: text callability, body rules, the sent-result fold, the route pick, the `text` order. */
class TextsTest {
    private val noEmergency: (String) -> Boolean = { false }
    private fun text(n: String) = Texts.verdict("text", n, noEmergency)

    @Test fun mobilesTakeTexts() {
        assertEquals(NumberRules.Verdict(true, "0491570156", ""), text("0491 570 156"))
        assertEquals(NumberRules.Verdict(true, "0491570158", ""), text("+61 491 570 158"))
    }

    @Test fun landlinesTakeNoTexts() {
        for (n in listOf("(02) 5550 4321", "(07) 5550 1234", "03 5550 1111", "08 5550 2222", "+61 7 5550 1234")) {
            assertEquals(n, NumberRules.Verdict(false, "", Texts.LANDLINE), text(n))
        }
    }

    @Test fun businessNumbersTakeNoTexts() {
        for (n in listOf("13 12 34", "1300 555 123", "1800 555 123")) {
            assertEquals(n, NumberRules.Verdict(false, "", Texts.NOT_MOBILE), text(n))
        }
    }

    @Test fun savedOverseasTakesTexts() {
        val v = text("+44 7700 900123")
        assertTrue(v.ok); assertTrue(v.overseas); assertEquals("+447700900123", v.dial)
    }

    @Test fun numberRulesStillComeFirst() {
        assertEquals(NumberRules.EMERGENCY, text("000").whyNot)
        assertEquals(NumberRules.PREMIUM, text("1902 555 123").whyNot)
        assertEquals(NumberRules.EMERGENCY, Texts.verdict("text", "0491 570 156") { it == "0491570156" }.whyNot)
        assertEquals(NumberRules.COSTLY_OVERSEAS, text("+881 2 1234 5678").whyNot)
    }

    @Test fun callsAreUnchangedByTheTextRule() {
        assertTrue(Texts.verdict("call", "(07) 5550 1234", noEmergency).ok)
        assertTrue(Texts.verdict("call", "1300 555 123", noEmergency).ok)
    }

    @Test fun textPickIsCheckedAsAText() {
        val pick = ContactMatch.Pick("k1", 7, "text", "321")
        assertEquals(Calls.Checked("", Texts.LANDLINE), Calls.check(pick, "text", "(02) 5550 4321", noEmergency))
        assertEquals(Calls.Checked("0491570321", ""), Calls.check(pick, "text", "0491 570 321", noEmergency))
        assertEquals(Texts.WRONG_ACTION, Calls.check(pick.copy(action = "call"), "text", "0491 570 321", noEmergency).whyNot)
        assertEquals(Calls.WRONG_ACTION, Calls.check(pick, "call", "0491 570 321", noEmergency).whyNot)
        assertEquals(Calls.CONTACT_CHANGED, Calls.check(pick, "text", "0491 570 999", noEmergency).whyNot)
    }

    @Test fun bodyRules() {
        assertEquals("", Texts.bodyProblem("I'm running late"))
        assertEquals("", Texts.bodyProblem("line one\nline two"))            // a newline is kept
        assertEquals("", Texts.bodyProblem("a".repeat(300)))
        assertEquals("", Texts.bodyProblem("😀".repeat(300)))                 // 300 characters, 600 UTF-16 units
        assertEquals("empty", Texts.bodyProblem(""))
        assertEquals("empty", Texts.bodyProblem("   "))
        assertEquals("empty", Texts.bodyProblem(null))
        assertEquals("long", Texts.bodyProblem("a".repeat(301)))
        assertEquals("control", Texts.bodyProblem("bell\u0007"))
        assertEquals("control", Texts.bodyProblem("tab\there"))
        assertEquals("control", Texts.bodyProblem("cr\r"))
        assertEquals("control", Texts.bodyProblem("sep "))
        assertEquals("control", Texts.bodyProblem("c1\u0085"))
    }

    @Test fun allPartsOkIsSent() {
        val f = Texts.SentFold(2)
        f.report(0, Texts.RESULT_OK)
        assertNull(f.outcome(timedOut = false))
        f.report(1, Texts.RESULT_OK)
        assertEquals(true to "", f.outcome(timedOut = false))
    }

    @Test fun oneErrorFailsWithItsReason() {
        val f = Texts.SentFold(2)
        f.report(0, Texts.RESULT_OK)
        f.report(1, 4)
        assertEquals(false to "the phone has no mobile service", f.outcome(timedOut = false))
        assertEquals("the phone's radio is off", Texts.reason(2))
        assertTrue(Texts.reason(1).startsWith("the text did not go"))
    }

    @Test fun silenceFailsAtTheTimeout() {
        val f = Texts.SentFold(2)
        f.report(0, Texts.RESULT_OK)
        assertNull(f.outcome(timedOut = false))
        assertEquals(false to Texts.NO_REPORT, f.outcome(timedOut = true))
    }

    @Test fun strayAndRepeatReportsDoNotCount() {
        val two = Texts.SentFold(2)
        two.report(0, Texts.RESULT_OK)
        two.report(7, Texts.RESULT_OK)                                        // no such part: not the second one
        assertNull(two.outcome(timedOut = false))
        val f = Texts.SentFold(1)
        f.report(-1, Texts.RESULT_OK)
        assertNull(f.outcome(timedOut = false))
        f.report(0, 2)
        f.report(0, Texts.RESULT_OK)                                          // the first report for a part stands
        assertEquals(false, f.outcome(timedOut = false)!!.first)
    }

    @Test fun sentWaitIs30sOrWhatTheDeadlineLeaves() {
        assertEquals(30_000L, Texts.sentWaitMs(80_000))
        assertEquals(9_000L, Texts.sentWaitMs(10_000))
        assertEquals(1_000L, Texts.sentWaitMs(0))
    }

    @Test fun routePick() {
        assertEquals("sms", Texts.route(true))
        assertEquals("messages_app", Texts.route(false))
    }

    private val pingData = mapOf<String, Any?>("version" to "0.1.0")
    private fun order(args: Map<String, Any?>?, send: (String, String, String) -> Orders.Answer) =
        Orders.answer(mapOf("id" to "o_abcd", "kind" to "text", "args" to args), "0.1.0", 0.0, false, text = send) { pingData }!!

    @Test fun textOrderPassesFindPickAndBodyAndNeverEchoesTheBody() {
        var got = ""
        val r = order(mapOf("find" to "f_abc", "pick" to "p_2", "body" to "running late, back at 530 pm")) { f, p, b ->
            got = "$f/$p/$b"
            Orders.Answer(true, "sent", mapOf("after_s" to 2.5, "parts" to 1, "how" to "sms"), "")
        }
        assertEquals("f_abc/p_2/running late, back at 530 pm", got)
        assertEquals("sent", r["outcome"])
        assertFalse(r.toString().contains("running late"))
    }

    @Test fun textOrderWithoutALookupOrBodyIsRefusedBeforeSending() {
        for (args in listOf(null, mapOf("find" to "f_abc", "body" to "hi"), mapOf("find" to "x_abc", "pick" to "p_2", "body" to "hi"),
                            mapOf("find" to "f_abc", "pick" to "p_2"), mapOf("find" to "f_abc", "pick" to "p_2", "body" to 5))) {
            val r = order(args) { _, _, _ -> throw AssertionError("sent with $args") }
            assertEquals("$args", "refused", r["outcome"])
        }
    }

    @Test fun textOrderWithoutContactsFails() {
        val r = order(mapOf("find" to "f_abc", "pick" to "p_2", "body" to "hi")) { _, _, _ -> throw SecurityException() }
        assertEquals("failed", r["outcome"])
    }

    // 3 Nov: the Motorola's route B text failed with a SecurityException from the SMS side, reported as "contacts".
    @Test fun textDeniedSaysWhichPermission() {
        val c = order(mapOf("find" to "f_abc", "pick" to "p_2", "body" to "hi")) { _, _, _ -> throw SecurityException("READ_CONTACTS") }
        assertEquals("the app is not allowed to read contacts", c["error"])
        val s = order(mapOf("find" to "f_abc", "pick" to "p_2", "body" to "hi")) { _, _, _ -> throw SecurityException("Sending SMS message: uid 10200 does not have android.permission.SEND_SMS.") }
        assertTrue(s["error"].toString(), s["error"].toString().contains("not allowed to send texts"))
    }

    @Test fun routeBCountsPartsLikeSmsManager() {
        assertEquals(1, Texts.parts("Running late, see you at 5:30").size)
        assertEquals(1, Texts.parts("a".repeat(160)).size)
        assertEquals(2, Texts.parts("a".repeat(161)).size)
        assertEquals(2, Texts.parts("a".repeat(306)).size)
        assertEquals(3, Texts.parts("a".repeat(307)).size)
        assertEquals(1, Texts.parts("café ☕".repeat(10)).size)       // 60 code points, unicode: 70 a text
        assertEquals(2, Texts.parts("☕".repeat(71)).size)
        assertEquals("a".repeat(161), Texts.parts("a".repeat(161)).joinToString(""))
    }
}
