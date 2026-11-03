package local.phonehelper

import android.media.AudioAttributes.CONTENT_TYPE_MOVIE
import android.media.AudioAttributes.CONTENT_TYPE_MUSIC
import android.media.AudioAttributes.CONTENT_TYPE_SONIFICATION
import android.media.AudioAttributes.CONTENT_TYPE_SPEECH
import android.media.AudioAttributes.CONTENT_TYPE_UNKNOWN
import android.media.AudioAttributes.USAGE_ALARM
import android.media.AudioAttributes.USAGE_MEDIA
import android.media.AudioAttributes.USAGE_VOICE_COMMUNICATION
import android.media.AudioManager.MODE_IN_CALL
import android.media.AudioManager.MODE_IN_COMMUNICATION
import android.media.AudioManager.MODE_NORMAL
import local.phonehelper.Calls.Step.CANCEL
import local.phonehelper.Calls.Step.DIAL
import local.phonehelper.Calls.Step.WAIT
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class CallsTest {
    private val quiet = Calls.Signals(MODE_NORMAL, false, emptyList(), false)

    @Test fun quietPhoneIsNoCall() = assertFalse(Calls.callOn(quiet))

    @Test fun eachSignOfACallCounts() {
        assertTrue(Calls.callOn(quiet.copy(mode = MODE_IN_COMMUNICATION)))          // XM5 headset route
        assertTrue(Calls.callOn(quiet.copy(mode = MODE_IN_CALL)))
        assertTrue(Calls.callOn(quiet.copy(musicActive = true)))
        assertTrue(Calls.callOn(quiet.copy(recording = true)))                      // Claude listening, nothing playing
        assertTrue(Calls.callOn(quiet.copy(players = listOf(USAGE_VOICE_COMMUNICATION to CONTENT_TYPE_SPEECH))))
        assertTrue(Calls.callOn(quiet.copy(players = listOf(USAGE_MEDIA to CONTENT_TYPE_UNKNOWN))))   // Claude on the speaker
        assertTrue(Calls.callOn(quiet.copy(players = listOf(USAGE_MEDIA to CONTENT_TYPE_SPEECH))))
    }

    @Test fun musicFilmsAndAlarmsAreNoCall() {
        val players = listOf(USAGE_MEDIA to CONTENT_TYPE_MUSIC, USAGE_MEDIA to CONTENT_TYPE_MOVIE, USAGE_ALARM to CONTENT_TYPE_SONIFICATION)
        assertFalse(Calls.callOn(quiet.copy(players = players)))
    }

    @Test fun dialsOnlyAfterTwoAndAHalfSecondsQuiet() {
        val w = Calls.Wait(0)
        assertEquals(WAIT, w.step(0, true))
        assertEquals(WAIT, w.step(10_000, false))          // ended at 10 s
        assertEquals(WAIT, w.step(12_499, false))
        assertEquals(DIAL, w.step(12_500, false))
        assertEquals(10_000L, w.quietSince)
    }

    @Test fun aPauseInTheCallStartsTheQuietAgain() {
        val w = Calls.Wait(0)
        assertEquals(WAIT, w.step(1_000, false))
        assertEquals(WAIT, w.step(3_000, true))            // Claude spoke again after a 2 s pause
        assertEquals(WAIT, w.step(4_000, false))
        assertEquals(WAIT, w.step(6_000, false))
        assertEquals(DIAL, w.step(6_500, false))
    }

    @Test fun alreadyQuietStillWaitsTheTwoAndAHalfSeconds() {
        val w = Calls.Wait(0)
        assertEquals(WAIT, w.step(0, false))
        assertEquals(WAIT, w.step(2_250, false))
        assertEquals(DIAL, w.step(2_500, false))
    }

    @Test fun neverEndingCallCancelsAtSixtySeconds() {
        val w = Calls.Wait(5_000)
        assertEquals(WAIT, w.step(64_999, true))
        assertEquals(CANCEL, w.step(65_000, true))
    }

    @Test fun quietThatHasNotLastedByTheMinuteStillCancels() {
        val w = Calls.Wait(0)
        assertEquals(WAIT, w.step(59_000, false))
        assertEquals(CANCEL, w.step(60_000, false))        // only 1 s quiet: never dial late
    }

    @Test fun waitForEndDialsTwoAndAHalfSecondsAfterTheEnd() {
        var now = 1_000L
        val flips = mutableListOf<String>()
        val w = Calls.waitForEnd({ now }, { now += it }, { now < 8_000 }) { on, at -> flips += "$on@$at" }
        assertEquals(DIAL, w.step)
        assertEquals(8_000L, w.quietMs)
        assertEquals(10_500L, now)
        assertEquals(listOf("true@0", "false@7000"), flips)
    }

    @Test fun slowReadingsDoNotPushTheDialLate() {
        var now = 0L
        // every reading costs 15 ms and every sleep overruns by 10 ms; the call ends at 3 s
        val w = Calls.waitForEnd({ now }, { now += it + 10 }, { now += 15; now < 3_000 })
        assertEquals(DIAL, w.step)
        val late = now - w.quietMs!!
        assertTrue("dialled ${late} ms after the quiet began", late in 2_500..2_540)
    }

    @Test fun waitForEndGivesUpAtTheLimit() {
        var now = 0L
        val w = Calls.waitForEnd({ now }, { now += it }, { true }, giveUpMs = 60_000)
        assertEquals(CANCEL, w.step)
        assertEquals(60_000L, now)
    }

    @Test fun giveUpLeavesTimeToConfirmBeforeTheMacStopsWaiting() {
        assertEquals(60_000L, Calls.giveUpMs(85_000))
        assertEquals(57_000L, Calls.giveUpMs(65_000))
        assertEquals(0L, Calls.giveUpMs(3_000))
    }

    private val pick = ContactMatch.Pick("key1", 7, "call", "158")
    private val noEmergency: (String) -> Boolean = { false }

    @Test fun checkGivesTheNumberToDial() {
        assertEquals(Calls.Checked("0491570158", ""), Calls.check(pick, "call", "+61 491 570 158", noEmergency))
    }

    @Test fun checkRefusesALookupThatIsGone() =
        assertEquals(Calls.LOOKUP_GONE, Calls.check(null, "call", "0491 570 158", noEmergency).whyNot)

    @Test fun checkRefusesALookupForTheOtherAction() =
        assertEquals(Calls.WRONG_ACTION, Calls.check(pick.copy(action = "text"), "call", "0491 570 158", noEmergency).whyNot)

    @Test fun checkRefusesAContactChangedOrGone() {
        assertEquals(Calls.CONTACT_CHANGED, Calls.check(pick, "call", "0491 570 999", noEmergency).whyNot)
        assertEquals(Calls.CONTACT_CHANGED, Calls.check(pick, "call", null, noEmergency).whyNot)
    }

    @Test fun checkAppliesTheNumberRulesAgain() {
        assertEquals(NumberRules.EMERGENCY, Calls.check(pick.copy(last3 = "000"), "call", "000", noEmergency).whyNot)
        assertEquals(NumberRules.PREMIUM, Calls.check(pick.copy(last3 = "123"), "call", "1902 555 123", noEmergency).whyNot)
        assertEquals(NumberRules.EMERGENCY, Calls.check(pick, "call", "0491 570 158") { it == "0491570158" }.whyNot)
    }

    @Test fun afterSecondsHasOneDecimal() {
        assertEquals(2.6, Calls.afterS(1_000, 3_634), 0.0)
        assertEquals(2.5, Calls.afterS(0, 2_500), 0.0)
    }
}
