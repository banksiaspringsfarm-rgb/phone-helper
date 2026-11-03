package local.phonehelper

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** Inbox (4 Nov): who a text or missed call is from goes as a name or the last 3 digits, never a full number. */
class InboxTest {
    private val numberLike = Regex("\\d(?:[\\s()./,_+-]*\\d){5,}")

    @Test fun savedContactGoesByNameAndLastThree() {
        val i = Inbox.item("sms", 1_700_000_000_123, "+61 412 345 678", "Lena", "Running late")
        assertEquals("Lena", i["who"])
        assertEquals(true, i["known"])
        assertEquals("678", i["last3"])
        assertEquals(1_700_000_000.123, i["at"])
        assertEquals("Running late", i["body"])
    }

    @Test fun unknownNumberGoesAsLastThreeOnly() {
        val i = Inbox.item("missed", 1_000, "0412345678", null)
        assertEquals("", i["who"])
        assertEquals(false, i["known"])
        assertEquals("678", i["last3"])
        assertFalse(i.containsKey("body"))
        assertTrue(i.values.none { numberLike.containsMatchIn(it.toString()) })
    }

    @Test fun senderNameKeptButANumberSavedAsTheNameIsNot() {
        val t = Inbox.item("sms", 1_000, "Telstra", null, "Your bill")
        assertEquals("Telstra", t["who"]); assertEquals(false, t["known"]); assertEquals("", t["last3"])
        val n = Inbox.item("sms", 1_000, "0412345678", "0412 345 678", "hi")
        assertEquals("", n["who"]); assertEquals(false, n["known"])
        val mixed = Inbox.item("sms", 1_000, "Ref 0412345678", null, "hi")
        assertEquals("", mixed["who"])
    }

    @Test fun hiddenCallerHasNoDigits() {
        for (a in listOf("", "-1", "-2", null)) {
            val i = Inbox.item("missed", 1_000, a, null)
            assertEquals("", i["who"]); assertEquals("", i["last3"])
        }
    }

    @Test fun bodyLosesControlCharactersKeepsNewlinesAndIsCapped() {
        val i = Inbox.item("sms", 1_000, "0412345678", "Mum", "a\u0007b\nc" + "x".repeat(3000))
        val b = i["body"] as String
        assertTrue(b.startsWith("a b\nc"))
        assertEquals(Inbox.BODY_MAX, b.length)
    }

    @Test fun readsHeader() {
        assertEquals("sms,calls", Inbox.reads(true, true))
        assertEquals("calls", Inbox.reads(false, true))
        assertEquals("none", Inbox.reads(false, false))
    }

    @Test fun sameNumberAcrossPlusSixtyOneAndZero() {
        assertTrue(Inbox.sameNumber("0491570158", "+61 491 570 158"))
        assertTrue(Inbox.sameNumber("(07) 5550 1234", "+61 7 5550 1234"))
        assertFalse(Inbox.sameNumber("0491570158", "0491570157"))
        assertFalse(Inbox.sameNumber("Telstra", "Telstra"))          // no digits: never a number match
        assertFalse(Inbox.sameNumber("-2", "2"))                     // a hidden caller
        assertTrue(Inbox.sameNumber("000", "000"))
    }
}
