package local.phonehelper

import local.phonehelper.NumberRules.BAD_OVERSEAS
import local.phonehelper.NumberRules.COSTLY_OVERSEAS
import local.phonehelper.NumberRules.EMERGENCY
import local.phonehelper.NumberRules.EMPTY
import local.phonehelper.NumberRules.NOT_AU
import local.phonehelper.NumberRules.PREMIUM
import local.phonehelper.NumberRules.SYMBOLS
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * One test per rule of design §5, each asserting the refusal's reason, so removing or loosening any one rule line
 * fails a test here (a refused number would then come back allowed, or refused for a different reason).
 * Numbers are fictitious where they could be real (ACMA's 0491 570 xxx and 5550 xxxx, Ofcom's 020 7946 0xxx).
 */
class NumberRulesTest {
    private fun dial(saved: String) = NumberRules.check(saved).also { assertTrue("$saved: $it", it.ok) }.dial
    private fun why(saved: String, isEmergency: (String) -> Boolean = { false }) =
        NumberRules.check(saved, isEmergency).also { assertFalse("$saved: $it", it.ok); assertEquals("", it.dial) }.whyNot

    // Rule 1: clean.
    @Test fun spacingAndGroupingMarksAreDropped() {
        assertEquals("0491570156", dial("0491 570 156"))
        assertEquals("0491570156", dial("0491-570-156"))
        assertEquals("0491570156", dial("0491.570.156"))
        assertEquals("0755501234", dial("(07) 5550 1234"))
        assertEquals("0491570156", dial(" 0491 570\t156 "))       // no-break space and tab from a vCard
    }

    @Test fun symbolsThatDialCodesUseAreRefused() {
        assertEquals(SYMBOLS, why("*21*0491570156#"))                  // call forwarding
        assertEquals(SYMBOLS, why("**21*0491570156#"))
        assertEquals(SYMBOLS, why("#31#0491570156"))                   // hide caller id
        assertEquals(SYMBOLS, why("0491570156,1234"))                  // pause then digits
        assertEquals(SYMBOLS, why("0491570156;1234"))                  // wait then digits
        assertEquals(SYMBOLS, why("04915701+56"))                      // + not in front
        assertEquals(SYMBOLS, why("++61491570156"))
        assertEquals(SYMBOLS, why("+"))
        assertEquals(SYMBOLS, why("0491 570 15six"))
        assertEquals(SYMBOLS, why("０４９１５７０１５６"))                   // full-width digits are not 0-9
        assertEquals(EMPTY, why(""))
        assertEquals(EMPTY, why(" ( ) - "))
    }

    // Rule 2: normalise.
    @Test fun plus61BecomesTheDomesticNumber() {
        assertEquals("0491570156", dial("+61 491 570 156"))
        assertEquals("0491570156", dial("+61491570156"))
        assertEquals("0755501234", dial("+61 7 5550 1234"))
        assertEquals("1300555123", dial("+61 1300 555 123"))           // 13, 1300 and 1800 have no trunk 0
        assertEquals("1800555123", dial("+61 1800 555 123"))
        assertEquals("131234", dial("+61 13 12 34"))
        assertEquals("0491570156", dial("+61 (0)491 570 156"))         // saved with its 0 kept
        assertFalse(NumberRules.check("+61 491 570 156").overseas)
    }

    @Test fun zero011IsDialledOutLikeAPlus() {
        assertEquals("0491570156", dial("0011 61 491 570 156"))       // Australia the long way round: domestic
        assertFalse(NumberRules.check("0011 61 491 570 156").overseas)
        assertEquals("+442079460958", dial("0011 44 20 7946 0958"))
        assertTrue(NumberRules.check("0011 44 20 7946 0958").overseas)
    }

    @Test fun sixtyOneWithoutThePlusIsNotGuessedAt() {
        assertEquals(NOT_AU, why("61 491 570 156"))
        assertEquals(NOT_AU, why("61491570156"))
    }

    // Rule 3: emergency.
    @Test fun emergencyNumbersAreAlwaysRefused() {
        for (n in listOf("000", "112", "106", "911", "999", "0 0 0", "(000)", "00-0")) assertEquals(n, EMERGENCY, why(n))
        for (n in listOf("+61 000", "+61 112", "+61 106", "+000", "+112", "+911", "0011 000", "0011 112", "0011 61 000"))
            assertEquals(n, EMERGENCY, why(n))
    }

    @Test fun thePhonesOwnEmergencyListIsAsked() {
        // A carrier or roaming emergency number the fixed list does not know, in an allowed shape.
        val carrier: (String) -> Boolean = { it == "131444" }
        assertEquals(EMERGENCY, why("13 14 44", carrier))
        assertEquals(EMERGENCY, why("+61 13 14 44", carrier))          // asked about the number that would dial
        assertEquals(EMERGENCY, why("+44 20 7946 0958") { it == "442079460958" })
        assertTrue(NumberRules.check("0491 570 156", carrier).ok)
    }

    // Rule 4: Australian shapes only.
    @Test fun allowedAustralianShapes() {
        assertEquals("0491570156", dial("0491 570 156"))               // mobile
        for (area in listOf("02", "03", "07", "08")) assertEquals("${area}55501234", dial("$area 5550 1234"))
        assertEquals("1300555123", dial("1300 555 123"))
        assertEquals("1800555123", dial("1800 555 123"))
        assertEquals("131234", dial("13 12 34"))
    }

    @Test fun premiumNumbersAreRefused() {
        for (n in listOf("1900 555 123", "1901 555 123", "1902 555 123", "1906 555 123", "1900555", "190", "19",
            "0055 12345", "0055 1234 5678", "0198 555 123", "+61 1900 555 123", "+61 1902 555 123", "0011 61 1902 555 123"))
            assertEquals(n, PREMIUM, why(n))
    }

    @Test fun shortCodesAndOtherShapesAreRefused() {
        for (n in listOf(
            "1194", "125111", "1223", "12455", "13 00", "1300 555", "1800 555 12", "13 12 345",   // short codes, half numbers
            "0145 555 123", "0147 555 123",                                                  // satellite
            "0491 570 15", "0491 570 1567", "0591 570 156", "0691 570 156", "0991 570 156", // wrong length or prefix
            "0155 501 234", "0455 5012", "5550 1234", "04", "0", "1", "100", "1100",
            "+61 491 570 15", "+61 0", "+61",
        )) assertEquals(n, NOT_AU, why(n))
    }

    // Rule 5: international, saved that way.
    @Test fun savedOverseasNumbersAreAllowedAndSaidAsOverseas() {
        val uk = NumberRules.check("+44 20 7946 0958")
        assertTrue(uk.ok)
        assertTrue(uk.overseas)
        assertEquals("+442079460958", uk.dial)
        assertEquals("+6421555123", dial("+64 21 555 123"))
        assertEquals("+15555550100", dial("+1 (555) 555-0100"))
    }

    @Test fun costlyWorldwideCodesAreRefusedEvenWhenSaved() {
        for (code in listOf("870", "881", "882", "883", "979", "991")) {
            assertEquals(code, COSTLY_OVERSEAS, why("+$code 555 123 456"))
            assertEquals(code, COSTLY_OVERSEAS, why("0011 $code 555 123 456"))
        }
    }

    @Test fun brokenOverseasNumbersAreRefused() {
        assertEquals(BAD_OVERSEAS, why("+44 12"))                      // too short to be a whole number
        assertEquals(BAD_OVERSEAS, why("+44 1234 5678 9012 34"))       // over E.164's 15 digits
        assertEquals(BAD_OVERSEAS, why("+0 491 570 156"))              // no country code starts with 0
        assertEquals(BAD_OVERSEAS, why("0011"))
    }

    @Test fun noRefusalReasonCarriesDigits() {
        for (w in listOf(SYMBOLS, EMERGENCY, PREMIUM, NOT_AU, COSTLY_OVERSEAS, BAD_OVERSEAS, EMPTY))
            assertFalse(w, w.any { it.isDigit() })
    }
}
