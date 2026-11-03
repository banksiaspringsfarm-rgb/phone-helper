package local.phonehelper

import android.media.AudioAttributes
import android.media.AudioManager

/**
 * The `call` order's pure part (design §6): is the Claude call still on, when has it ended (2.5 s quiet in a row),
 * when to give up (60 s after the order arrived), and which number a taken pick may ring. Plain Kotlin so the JVM
 * tests cover it; Dialler.kt reads the audio state and places the call.
 */
object Calls {
    const val QUIET_MS = 2_500L
    const val GIVE_UP_MS = 60_000L
    const val CONFIRM_MS = 8_000L
    const val TICK_MS = 250L

    /** One reading of the audio state: [players] are the active playbacks' (usage, contentType). */
    data class Signals(val mode: Int, val musicActive: Boolean, val players: List<Pair<Int, Int>>, val recording: Boolean)

    /**
     * A call is on (design §6.1, copied from Speaker Button's inCall/reclaim, plus recording): the audio mode is not
     * normal, or music is active, or something plays as voice communication, or as media that is not music or a
     * film (Claude's voice on the speaker is CONTENT_TYPE_UNKNOWN), or any app has the microphone open.
     */
    fun callOn(s: Signals): Boolean =
        s.mode != AudioManager.MODE_NORMAL || s.musicActive || s.recording || s.players.any { (usage, type) ->
            usage == AudioAttributes.USAGE_VOICE_COMMUNICATION ||
                (usage == AudioAttributes.USAGE_MEDIA &&
                    type != AudioAttributes.CONTENT_TYPE_MUSIC && type != AudioAttributes.CONTENT_TYPE_MOVIE)
        }

    enum class Step { WAIT, DIAL, CANCEL }

    /** Feed it readings; it says when to dial or give up. [quietSince] is when the current quiet run began. */
    class Wait(private val startMs: Long, private val giveUpMs: Long = GIVE_UP_MS) {
        var quietSince: Long? = null
            private set

        /** How long to sleep before the next reading: a tick, or less so the dial lands right on the 2.5 s mark. */
        fun sleepMs(nowMs: Long): Long =
            quietSince?.let { (it + QUIET_MS - nowMs).coerceIn(1, TICK_MS) } ?: TICK_MS

        fun step(nowMs: Long, on: Boolean): Step {
            if (on) quietSince = null else if (quietSince == null) quietSince = nowMs
            val q = quietSince
            return when {
                q != null && nowMs - q >= QUIET_MS -> Step.DIAL
                nowMs - startMs >= giveUpMs -> Step.CANCEL
                else -> Step.WAIT
            }
        }
    }

    /** Ended: dial now; [quietMs] is when the call was seen to end. Cancelled: it never ended in time. */
    data class Waited(val step: Step, val quietMs: Long?)

    /** Read [on] every [TICK_MS] (sleep overruns do not add up) until the call has ended or [giveUpMs] has passed; [onChange] hears each flip. */
    fun waitForEnd(
        nowMs: () -> Long, sleep: (Long) -> Unit, on: () -> Boolean, giveUpMs: Long = GIVE_UP_MS,
        onChange: (on: Boolean, atMs: Long) -> Unit = { _, _ -> },
    ): Waited {
        val start = nowMs()
        val w = Wait(start, giveUpMs)
        var last: Boolean? = null
        while (true) {
            val now = nowMs()
            val o = on()
            if (o != last) { onChange(o, now - start); last = o }
            when (w.step(now, o)) {
                Step.DIAL -> return Waited(Step.DIAL, w.quietSince)
                Step.CANCEL -> return Waited(Step.CANCEL, null)
                Step.WAIT -> sleep(w.sleepMs(now))
            }
        }
    }

    /** How long the wait may run for an order whose phone deadline is [leftMs] away: 60 s, less if the result
     *  would otherwise land after the Mac stopped waiting for it (it still needs [CONFIRM_MS] to confirm a dial). */
    fun giveUpMs(leftMs: Long): Long = minOf(GIVE_UP_MS, leftMs - CONFIRM_MS).coerceAtLeast(0)

    /** The number to dial for a taken [pick], or the plain-words reason not to. */
    data class Checked(val dial: String, val whyNot: String)

    const val LOOKUP_GONE = "that lookup is too old or was already used, so look the name up again"
    const val WRONG_ACTION = "that lookup was for a text, not a call"
    const val CONTACT_CHANGED = "the contact has changed since the lookup, so look the name up again"

    /**
     * design §4 + §5 at dial time: the pick must still be in the phone's own memory (taken once), for [action],
     * and the number saved on that row now must end in the digits that were read back; then the number rules again
     * (for a text, also mobile or overseas: Texts.verdict).
     */
    fun check(pick: ContactMatch.Pick?, action: String, saved: String?, isEmergency: (String) -> Boolean): Checked {
        if (pick == null) return Checked("", LOOKUP_GONE)
        if (pick.action != action) return Checked("", if (action == "text") Texts.WRONG_ACTION else WRONG_ACTION)
        if (saved == null || ContactMatch.last3(saved) != pick.last3) return Checked("", CONTACT_CHANGED)
        val v = Texts.verdict(action, saved, isEmergency)
        return if (v.ok) Checked(v.dial, "") else Checked("", v.whyNot)
    }

    /** Seconds from the call ending to the dial, one decimal: the Mac refuses a long run of digits. */
    fun afterS(quietMs: Long, dialMs: Long): Double = Math.round((dialMs - quietMs) / 100.0) / 10.0
}
