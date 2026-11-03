package local.phonehelper

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.random.Random

class NetTest {
    @Test fun emulatorRouteOnlyForDebugOnEmulator() {
        assertEquals(Net.EMULATOR_URL, Net.serverUrl(debug = true, emulator = true))
        assertEquals(Net.TAILNET_URL, Net.serverUrl(debug = true, emulator = false))
        assertEquals(Net.TAILNET_URL, Net.serverUrl(debug = false, emulator = true))   // release: tailnet, always
        assertTrue(Net.TAILNET_URL.startsWith("https://"))
    }

    @Test fun emulatorHardware() {
        assertTrue(Net.isEmulator("ranchu"))
        assertTrue(Net.isEmulator("goldfish"))
        assertFalse(Net.isEmulator("mt6879"))
    }

    @Test fun backoffDoublesToThirtyWithJitterNeverOverThirty() {
        val mid = object : Random() { override fun nextBits(bitCount: Int) = 0; override fun nextDouble() = 0.5 }
        assertEquals(listOf(1000L, 2000L, 4000L, 8000L, 16000L, 30000L, 30000L), (1..7).map { Net.backoffMs(it, mid) })
        val r = Random(7)
        repeat(200) {
            val ms = Net.backoffMs(6, r)
            assertTrue("$ms", ms in 24_000L..30_000L)
            assertTrue("$ms", Net.backoffMs(4, r) in 6_400L..9_600L)
        }
        val high = object : Random() { override fun nextBits(bitCount: Int) = 0; override fun nextDouble() = 0.999 }
        assertEquals(30_000L, Net.backoffMs(9, high))                      // jitter up never passes the cap
        assertEquals(1000L, Net.backoffMs(0, mid))
    }

    @Test fun tailnetRange() {
        fun ip(vararg b: Int) = ByteArray(b.size) { b[it].toByte() }
        assertTrue(Net.isTailnet(ip(100, 64, 0, 1)))
        assertTrue(Net.isTailnet(ip(100, 127, 255, 254)))
        assertTrue(Net.isTailnet(ip(100, 101, 7, 9)))
        assertFalse(Net.isTailnet(ip(100, 63, 255, 255)))
        assertFalse(Net.isTailnet(ip(100, 128, 0, 1)))
        assertFalse(Net.isTailnet(ip(192, 168, 1, 10)))
        assertFalse(Net.isTailnet(ByteArray(16)))                    // IPv6: not the tailnet's v4 answer
    }
}
