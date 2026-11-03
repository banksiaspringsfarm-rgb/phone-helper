package local.phonehelper

import local.phonehelper.ContactMatch.Row
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.random.Random

class ContactMatchTest {
    // Fictitious numbers (ACMA's reserved 0491 570 xxx and 5550 xxxx ranges).
    private val lena = Row("kJ", 11, "Lena Brennan", listOf("Lenny"), "Mobile", "0491 570 156")
    private val lenaHome = Row("kJ", 12, "Lena Brennan", listOf("Lenny"), "Home", "(07) 5550 1234")
    private val lenaSmith = Row("kS", 21, "Lena Smith", emptyList(), "Mobile", "0491 570 157")
    private val mum = Row("kM", 31, "Mum", emptyList(), "Mobile", "+61 491 570 158")
    private val bob = Row("kB", 41, "Bob Jones", listOf("Bobby"), "Work", "(02) 5550 4321")
    private val zoe = Row("kZ", 51, "Zoë O'Brien", emptyList(), "Mobile", "0491 570 110")
    private val noName = Row("kN", 61, "0491 570 159", emptyList(), "Mobile", "0491 570 159")
    private val all = listOf(lena, lenaHome, lenaSmith, mum, bob, zoe, noName)

    private fun names(spoken: String) = ContactMatch.match(spoken, all).map { "${it.name}/${it.label}" }

    @Test fun exactNameWins() {
        assertEquals(listOf("Mum/Mobile"), names("Mum"))
        assertEquals(listOf("Lena Brennan/Mobile", "Lena Brennan/Home"), names("lena brennan"))
    }

    @Test fun caseAccentsAndPunctuationAreFolded() {
        assertEquals(listOf("Zoë O'Brien/Mobile"), names("zoe obrien"))
        assertEquals(listOf("Zoë O'Brien/Mobile"), names("  ZOE  "))
        assertEquals("zoe obrien", ContactMatch.fold(" Zoë O’Brien! "))
    }

    @Test fun nicknameFindsTheContact() {
        assertEquals(listOf("Lena Brennan/Mobile", "Lena Brennan/Home"), names("Lenny"))
        assertEquals(listOf("Bob Jones/Work"), names("bobby"))
    }

    @Test fun twoContactsWithTheSameFirstNameBothCome() {
        assertEquals(listOf("Lena Brennan/Mobile", "Lena Brennan/Home", "Lena Smith/Mobile"), names("Lena"))
    }

    @Test fun neverMoreThanThree() {
        val many = (1..6).map { Row("k$it", it.toLong(), "Sam $it", emptyList(), "Mobile", "0491 570 15$it") }
        assertEquals(3, ContactMatch.match("Sam", many).size)
    }

    @Test fun exactBeatsWeakerMatches() {
        val rows = listOf(Row("a", 1, "Mumford", emptyList(), "Mobile", "0491 570 737"), mum)
        assertEquals(listOf("Mum"), ContactMatch.match("Mum", rows).map { it.name })   // no "Mumford" offered
    }

    @Test fun surnameAndPrefixMatch() {
        assertEquals(listOf("Bob Jones/Work"), names("Jones"))
        assertEquals(listOf("Lena Brennan/Mobile", "Lena Brennan/Home", "Lena Smith/Mobile"), names("Len"))
    }

    @Test fun oneLetterSlipOnlyForLongerNames() {
        assertEquals(listOf("Lena Brennan/Mobile", "Lena Brennan/Home", "Lena Smith/Mobile"), names("Lene"))
        assertEquals(emptyList<String>(), names("Rob"))          // "Bob" is one letter off but too short to guess
        assertTrue(ContactMatch.oneEdit("lene", "lena"))
        assertTrue(ContactMatch.oneEdit("lenny", "lenn"))
        assertFalse(ContactMatch.oneEdit("lenny", "lena"))
    }

    @Test fun noMatchIsEmpty() {
        assertEquals(emptyList<String>(), names("Gertrude"))
        assertEquals(emptyList<String>(), names(""))
        assertEquals(emptyList<String>(), names("123"))
    }

    @Test fun contactWithoutANameIsSkipped() {
        assertEquals(emptyList<String>(), names("0491 570 159"))
        assertEquals(emptyList<String>(), ContactMatch.match("Mobile", listOf(noName)))
    }

    @Test fun sameNumberTwiceOnOneContactIsOfferedOnce() {
        val dup = lena.copy(rowId = 99, number = "0491570156")
        assertEquals(1, ContactMatch.match("Lena Brennan", listOf(lena, dup)).size)
    }

    @Test fun offerSendsNameLabelLast3AndNoNumberOrKey() {
        val finds = ContactMatch.Finds({ 0L }, Random(1))
        val data = finds.offer(listOf(lena, mum), "call") { NumberRules.check(it.number) }
        val matches = data["matches"] as List<*>
        val m = matches[0] as Map<*, *>
        assertEquals(setOf("pick", "name", "label", "last3", "callable", "why_not", "overseas"), m.keys)
        assertEquals("156", m["last3"])
        assertEquals("158", (matches[1] as Map<*, *>)["last3"])
        val text = data.toString()
        assertFalse(text, Regex("""\d(?:[\s()./,_+-]*\d){5,}""").containsMatchIn(text))
        assertFalse(text, text.contains("kJ") || text.contains("11"))
        // ids are the shape the Mac accepts (phone.py OPAQUE_ID): a prefix and letters only
        val ids = listOf(data["find"]) + matches.map { (it as Map<*, *>)["pick"] }
        ids.forEach { assertTrue("$it", Regex("^[a-z]_[A-Za-z]{1,32}$").matches(it as String)) }
    }

    @Test fun aPickWorksOnceAndDiesAfterTwoMinutes() {
        var now = 0L
        val finds = ContactMatch.Finds({ now })
        val data = finds.offer(listOf(lena, mum), "call") { NumberRules.check(it.number) }
        val find = data["find"] as String
        val picks = (data["matches"] as List<*>).map { (it as Map<*, *>)["pick"] as String }
        val first = finds.take(find, picks[0])
        assertNotNull(first)
        assertEquals(ContactMatch.Pick("kJ", 11, "call", "156"), first)
        assertNull(finds.take(find, picks[0]))                    // used up
        assertNull(finds.take("f_nothere", picks[1]))
        now = ContactMatch.KEEP_MS + 1
        assertNull(finds.take(find, picks[1]))                    // too old
    }

    @Test fun numberRuleVerdictIsCarried() {
        val finds = ContactMatch.Finds({ 0L })
        val m = (finds.offer(listOf(lena), "call") { NumberRules.Verdict(false, "", "an emergency number") }["matches"] as List<*>)[0] as Map<*, *>
        assertEquals(false, m["callable"])
        assertEquals("an emergency number", m["why_not"])
    }
}
