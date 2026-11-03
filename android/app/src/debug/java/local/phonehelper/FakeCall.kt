package local.phonehelper

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioTrack

/**
 * Debug builds only (design §9): stands in for a Claude voice call so the `call` order has something to wait for.
 * Sets MODE_IN_COMMUNICATION and loops a silent CONTENT_TYPE_SPEECH voice track (Android resets the mode of an app
 * that holds it with nothing playing). Stopping puts the mode back to normal, which is "the call ended".
 */
object FakeCall {
    private var track: AudioTrack? = null

    @Synchronized
    fun start(ctx: Context) {
        if (track != null) return
        val rate = 8000
        val silence = ByteArray(rate * 2)                       // one second of 16-bit mono silence
        track = AudioTrack.Builder()
            .setAudioAttributes(AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
            .setAudioFormat(AudioFormat.Builder().setSampleRate(rate)
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT).setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build())
            .setTransferMode(AudioTrack.MODE_STATIC)
            .setBufferSizeInBytes(silence.size)
            .build().apply {
                write(silence, 0, silence.size)
                setLoopPoints(0, rate, -1)
                play()
            }
        ctx.getSystemService(AudioManager::class.java).mode = AudioManager.MODE_IN_COMMUNICATION
    }

    @Synchronized
    fun stop(ctx: Context) {
        track?.run { runCatching { stop() }; release() }
        track = null
        ctx.getSystemService(AudioManager::class.java).mode = AudioManager.MODE_NORMAL
    }

    val on get() = track != null
}
