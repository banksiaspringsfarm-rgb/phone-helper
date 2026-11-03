package local.phonehelper

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class OrdersTest {
    private val pingData = mapOf<String, Any?>("version" to "0.1.0", "sdk" to 34, "perms" to mapOf("call_phone" to false))
    private fun answer(order: Map<String, Any?>, overdue: Boolean = false) =
        Orders.answer(order, "0.1.0", 1762060001.2, overdue) { pingData }

    @Test fun pingAnswersWithItsVersion() {
        val r = answer(mapOf("id" to "o_abcd", "kind" to "ping", "ttl_s" to 9.6, "args" to emptyMap<String, Any?>()))!!
        assertEquals("o_abcd", r["id"])
        assertEquals("ping", r["kind"])
        assertEquals(true, r["ok"])
        assertEquals("done", r["outcome"])
        assertEquals("0.1.0", (r["data"] as Map<*, *>)["version"])
        assertEquals("0.1.0", r["app_version"])
        assertEquals(1, r["v"])
        assertEquals(1762060001.2, r["at"])
    }

    @Test fun unknownKindIsUnsupportedNotAnError() {
        val r = answer(mapOf("id" to "o_abcd", "kind" to "teleport"))!!
        assertEquals(false, r["ok"])
        assertEquals("unsupported", r["outcome"])
        assertNull(r["data"])
        assertTrue((r["error"] as String).contains("teleport"))
    }

    @Test fun overdueOrderIsCancelledNotDone() {
        val r = answer(mapOf("id" to "o_abcd", "kind" to "ping"), overdue = true)!!
        assertEquals(false, r["ok"])
        assertEquals("cancelled", r["outcome"])
    }

    @Test fun orderWithoutIdGetsNoAnswer() {
        assertNull(answer(mapOf("kind" to "ping")))
    }

    @Test fun deadlineIsArrivalPlusTtlLessFive() {
        assertEquals(1000L + 4600L, Orders.deadlineMs(mapOf("ttl_s" to 9.6), 1000L))
        assertEquals(1000L - 5000L, Orders.deadlineMs(emptyMap(), 1000L))       // no ttl: already overdue
    }

    @Test fun scrubRemovesFullNumbersEverywhere() {
        val dirty = mapOf(
            "name" to "Lena 0412 345 678", "list" to listOf("+61 (412) 345-678", "ok 123"),
            "nested" to mapOf("a" to "0412.345.678"), "0412345678" to "key",
        )
        val clean = Orders.scrub(dirty) as Map<*, *>
        val all = clean.toString()
        assertFalse(all, Regex("""\d(?:[\s()./,_+-]*\d){5,}""").containsMatchIn(all))
        assertEquals("Lena …", clean["name"])
        assertEquals("ok 123", (clean["list"] as List<*>)[1])       // short runs stay
    }

    @Test fun answerIsScrubbed() {
        val r = Orders.answer(mapOf("id" to "o_abcd", "kind" to "ping"), "0.1.0", 0.0, false) {
            mapOf("label" to "call 0412345678")
        }!!
        assertEquals("call …", (r["data"] as Map<*, *>)["label"])
    }

    private val found = mapOf<String, Any?>("find" to "f_abc", "matches" to emptyList<Any?>())
    private fun find(args: Map<String, Any?>?, look: (String, String) -> Map<String, Any?> = { _, _ -> found }) =
        Orders.answer(mapOf("id" to "o_abcd", "kind" to "find", "args" to args), "0.1.0", 0.0, false, look) { pingData }!!

    @Test fun findPassesNameAndActionDefaultingToCall() {
        var got = ""
        val r = find(mapOf("name" to " Lena ")) { n, a -> got = "$n/$a"; found }
        assertEquals("Lena/call", got)
        assertEquals(true, r["ok"])
        assertEquals("done", r["outcome"])
        assertEquals(found, r["data"])
        find(mapOf("name" to "Lena", "action" to "text")) { n, a -> got = "$n/$a"; found }
        assertEquals("Lena/text", got)
    }

    @Test fun findWithNoMatchIsStillDone() {
        val r = find(mapOf("name" to "Gertrude"))
        assertEquals(true, r["ok"])
        assertEquals(emptyList<Any?>(), (r["data"] as Map<*, *>)["matches"])
    }

    @Test fun findWithoutANameOrWithABadActionIsRefused() {
        for (args in listOf(null, mapOf("name" to "  "), mapOf("name" to "Lena", "action" to "dial"))) {
            val r = find(args)
            assertEquals("$args", "refused", r["outcome"])
            assertEquals(false, r["ok"])
        }
    }

    @Test fun findWithoutContactsPermissionFails() {
        val r = find(mapOf("name" to "Lena")) { _, _ -> throw SecurityException("READ_CONTACTS") }
        assertEquals("failed", r["outcome"])
        assertTrue((r["error"] as String).contains("contacts"))
    }

    @Test fun findResultIsScrubbedToo() {
        val dirty = mapOf("find" to "f_abc", "matches" to listOf(mapOf("name" to "0491 570 159", "label" to "x")))
        val r = find(mapOf("name" to "x")) { _, _ -> dirty }
        assertEquals("…", (((r["data"] as Map<*, *>)["matches"] as List<*>)[0] as Map<*, *>)["name"])
    }

    private fun call(args: Map<String, Any?>?, dial: (String, String) -> Orders.Answer) =
        Orders.answer(mapOf("id" to "o_abcd", "kind" to "call", "args" to args), "0.1.0", 0.0, false, call = dial) { pingData }!!

    @Test fun callPassesFindAndPickAndReturnsTheDiallersAnswer() {
        var got = ""
        val r = call(mapOf("find" to "f_abcdefghij", "pick" to "p_2")) { f, p ->
            got = "$f/$p"; Orders.Answer(true, "dialled", mapOf("after_s" to 2.6, "how" to "placeCall"), "")
        }
        assertEquals("f_abcdefghij/p_2", got)
        assertEquals("dialled", r["outcome"])
        assertEquals(true, r["ok"])
        assertEquals(2.6, (r["data"] as Map<*, *>)["after_s"])
    }

    @Test fun callWithoutAProperLookupIsRefusedBeforeDialling() {
        for (args in listOf(null, mapOf("find" to "f_abc"), mapOf("find" to "f_abc", "pick" to "0491570158"),
                mapOf("find" to "p_abc", "pick" to "f_abc"), mapOf("find" to "f_abc", "pick" to "p_123456"))) {
            val r = call(args) { _, _ -> throw AssertionError("dialled with $args") }
            assertEquals("$args", "refused", r["outcome"])
        }
    }

    @Test fun callWithoutCallPermissionFails() {
        val r = call(mapOf("find" to "f_abc", "pick" to "p_abc")) { _, _ -> throw SecurityException("CALL_PHONE") }
        assertEquals("failed", r["outcome"])
        assertTrue((r["error"] as String).contains("calls"))
    }

    @Test fun overdueCallIsCancelledNotDialled() {
        val r = Orders.answer(mapOf("id" to "o_abcd", "kind" to "call", "args" to mapOf("find" to "f_abc", "pick" to "p_abc")),
            "0.1.0", 0.0, true, call = { _, _ -> throw AssertionError("dialled") }) { pingData }!!
        assertEquals("cancelled", r["outcome"])
    }
}
