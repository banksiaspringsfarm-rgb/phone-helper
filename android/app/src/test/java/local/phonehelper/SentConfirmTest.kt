package local.phonehelper

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** sent-confirmations (3 Nov): when it went, delivery reports, and results kept to resend after no signal. */
class SentConfirmTest {
    @Test fun sentAfterTurnsTheElapsedClockIntoSecondsAfterAt() {
        val res = mapOf("id" to "o_x", "data" to mapOf("how" to "sms", "done_at_ms" to 15_340L, "delivered" to null))
        val d = Orders.sentAfter(res, "done_at_ms", 3_000L)["data"] as Map<*, *>
        assertEquals(12.3, d["sent_after_s"])
        assertFalse(d.containsKey("done_at_ms"))
        assertFalse(d.containsKey("delivered"))           // unknown delivery is left out, not sent as null
        assertEquals(res - "data" + ("data" to null), Orders.sentAfter(res - "data" + ("data" to null), "done_at_ms", 0))
    }

    @Test fun deliveredFoldNeedsEveryPartAndFailsOnAPermanentError() {
        val f = Texts.DeliveredFold(2)
        assertNull(f.outcome())
        f.report(0, 0); assertNull(f.outcome())
        f.report(1, 0); assertEquals(true, f.outcome())
        val g = Texts.DeliveredFold(1)
        g.report(0, 32); assertNull(g.outcome())            // still trying
        g.report(0, 64); assertEquals(false, g.outcome())
        val h = Texts.DeliveredFold(1)
        h.report(0, -1); h.report(5, 0); assertNull(h.outcome())   // unreadable or a stray part: unknown
    }

    @Test fun deliverWaitIsTenSecondsAndShrinksNearTheDeadline() {
        assertEquals(10_000L, Texts.deliverWaitMs(80_000))
        assertEquals(3_000L, Texts.deliverWaitMs(35_000))
        assertEquals(0L, Texts.deliverWaitMs(20_000))
    }

    @Test fun outboxKeepsOnlyRecentCallsAndTextsOncePerId() {
        val now = 1_000_000.0
        fun r(id: String, kind: String, at: Double) = mapOf("id" to id, "kind" to kind, "at" to at)
        val kept = Outbox.keep(listOf(r("o_a", "call", now - 10), r("o_b", "ping", now), r("o_c", "text", now - 4 * 86400)),
            now, r("o_a", "call", now))
        assertEquals(listOf("o_a"), kept.map { it["id"] })
        val many = (1..15).map { r("o_$it", "text", now) }
        assertEquals(Outbox.MAX, Outbox.keep(many, now).size)
        assertEquals("o_15", Outbox.keep(many, now).last()["id"])
        assertTrue(Outbox.done(200)); assertTrue(Outbox.done(409)); assertFalse(Outbox.done(502)); assertFalse(Outbox.done(-1))
    }
}
