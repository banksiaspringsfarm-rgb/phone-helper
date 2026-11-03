package local.phonehelper

import kotlin.random.Random

/** Small pure rules for the order channel (design §2.4, §9, §10), kept here so the JVM tests cover them. */
object Net {
    const val TAILNET_URL = BuildConfig.SERVER_URL
    const val EMULATOR_URL = "http://10.0.2.2:8769"     // the emulator's alias for the Mac's loopback: the harness daemon
    const val WAIT_S = 25
    private val STEPS_S = intArrayOf(1, 2, 4, 8, 16, 30)
    const val MAX_BACKOFF_MS = 30_000L

    /** Debug builds on an emulator talk to the harness daemon; everything else to the Mac over the tailnet. */
    fun serverUrl(debug: Boolean, emulator: Boolean): String = if (debug && emulator) EMULATOR_URL else TAILNET_URL

    fun isEmulator(hardware: String): Boolean = hardware == "ranchu" || hardware == "goldfish"

    /** Wait before retry number [fails] (1 = first failure): 1, 2, 4, 8, 16, then 30 s, each ±20 % but never over
     *  30 s, so a Mac daemon back after any outage is reached within 35 s (design §7). */
    fun backoffMs(fails: Int, rnd: Random = Random): Long {
        val base = STEPS_S[(fails - 1).coerceIn(0, STEPS_S.size - 1)] * 1000.0
        return (base * (0.8 + 0.4 * rnd.nextDouble())).toLong().coerceAtMost(MAX_BACKOFF_MS)
    }

    /** MagicDNS gave a tailnet address (100.64.0.0/10)? Anything else means Tailscale is off or split-tunnelled. */
    fun isTailnet(addr: ByteArray): Boolean =
        addr.size == 4 && (addr[0].toInt() and 0xff) == 100 && (addr[1].toInt() and 0xc0) == 64
}
