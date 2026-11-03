package local.phonehelper

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.media.AudioManager
import android.net.Uri
import android.os.Bundle
import android.os.SystemClock
import android.telecom.TelecomManager
import android.util.Log

/**
 * The `call` order on the phone (design §6): re-check the pick and the number rules, wait until the Claude call
 * has ended (2.5 s quiet), give up after 60 s, then ring with the normal dialler and confirm it started. Runs on
 * its own thread (PhoneService allows one at a time) and returns the answer for the Mac. Logs never hold the number.
 */
object Dialler {
    private const val TAG = PhoneService.TAG
    private const val CHANNEL = "call"
    private const val NOTE_ID = 2

    fun call(ctx: Context, find: String, pick: String, leftMs: Long): Orders.Answer {
        if (ctx.checkSelfPermission(Manifest.permission.CALL_PHONE) != PackageManager.PERMISSION_GRANTED) {
            throw SecurityException("CALL_PHONE")
        }
        val p = Contacts.finds.take(find, pick)
        val checked = Calls.check(p, "call", p?.let { Contacts.number(ctx, it) }, Contacts.emergencyCheck(ctx))
        if (p == null || checked.whyNot.isNotEmpty()) {
            Log.i(TAG, "call refused: ${checked.whyNot}")
            return Orders.Answer(false, "refused", null, checked.whyNot)
        }
        val audio = ctx.getSystemService(AudioManager::class.java)
        Log.i(TAG, "call to …${p.last3}: waiting for the Claude call to end")
        val waited = Calls.waitForEnd(
            SystemClock::elapsedRealtime, { Thread.sleep(it) }, { Calls.callOn(signals(audio)) },
            Calls.giveUpMs(leftMs),
        ) { on, at -> Log.i(TAG, "call wait +${at} ms: ${if (on) "a call is on" else "quiet"}") }
        if (waited.step == Calls.Step.CANCEL) {
            Log.i(TAG, "call to …${p.last3} cancelled: the Claude call did not end")
            return Orders.Answer(false, "cancelled", null, "the Claude call did not end within a minute")
        }
        val after = Calls.afterS(waited.quietMs!!, SystemClock.elapsedRealtime())
        if (Prefs.dryRun(ctx)) {
            Log.i(TAG, "call to …${p.last3}: dry run, not dialled (${after} s after the call ended)")
            return Orders.Answer(true, "dialled", mapOf("after_s" to after, "how" to "dry_run",
                Texter.DONE_AT to SystemClock.elapsedRealtime()), "")
        }
        val uri = Uri.fromParts("tel", checked.dial, null)
        try {
            ctx.getSystemService(TelecomManager::class.java).placeCall(uri, Bundle())
        } catch (e: RuntimeException) {               // SecurityException included: offer it as a tap instead
            Log.w(TAG, "placeCall failed (${e.javaClass.simpleName}): offering a tap to call")
            tapToCall(ctx, uri)
            return Orders.Answer(false, "failed", mapOf("after_s" to after, "how" to "notification"),
                "the phone would not dial by itself, so it shows a tap to call instead")
        }
        val placedAt = SystemClock.elapsedRealtime()
        Log.i(TAG, "call to …${p.last3} placed ${after} s after the Claude call ended")
        val end = SystemClock.elapsedRealtime() + Calls.CONFIRM_MS
        while (SystemClock.elapsedRealtime() < end) {
            if (audio.mode == AudioManager.MODE_IN_CALL) {
                Log.i(TAG, "call to …${p.last3} is ringing out")
                return Orders.Answer(true, "dialled", mapOf("after_s" to after, "how" to "placeCall", Texter.DONE_AT to placedAt), "")
            }
            Thread.sleep(Calls.TICK_MS)
        }
        Log.w(TAG, "call to …${p.last3}: placed but no call started within 8 s")
        return Orders.Answer(false, "failed", mapOf("after_s" to after, "how" to "placeCall"), "the call did not start")
    }

    fun signals(a: AudioManager) = Calls.Signals(
        a.mode, a.isMusicActive,
        a.activePlaybackConfigurations.map { it.audioAttributes.usage to it.audioAttributes.contentType },
        a.activeRecordingConfigurations.isNotEmpty(),
    )

    /** design §6.2 fallback 2: a "Tap to call" notification; a tap is always allowed to start the call. */
    private fun tapToCall(ctx: Context, uri: Uri) {
        val nm = ctx.getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(CHANNEL, "Calls Claude set up", NotificationManager.IMPORTANCE_HIGH))
        val tap = PendingIntent.getActivity(
            ctx, 1, Intent(Intent.ACTION_CALL, uri).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        nm.notify(NOTE_ID, Notification.Builder(ctx, CHANNEL)
            .setSmallIcon(android.R.drawable.stat_sys_phone_call)
            .setContentTitle("Tap to make the call Claude set up")
            .setContentIntent(tap)
            .setAutoCancel(true)
            .build())
    }
}
