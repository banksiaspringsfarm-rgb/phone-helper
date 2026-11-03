package local.phonehelper

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.media.AudioManager
import android.net.Uri
import android.os.Build
import android.os.SystemClock
import android.telephony.SmsManager
import android.telephony.SubscriptionManager
import android.util.Log

/**
 * The `text` order on the phone (design §11.4): check the body, re-check the pick and the number rules (a mobile or
 * saved overseas number), wait until the Claude call has ended exactly as a call does, then send it ourselves when
 * SEND_SMS is granted (route A) or post a "Tap to text" notification that opens Messages filled in (route B).
 * Runs on its own thread (PhoneService allows one call or text at a time). Logs never hold the number or the words.
 */
object Texter {
    private const val TAG = PhoneService.TAG
    private const val CHANNEL = "text"
    private const val NOTE_ID = 3
    private const val NOTE_LIFE_MS = 10 * 60_000L

    fun text(ctx: Context, find: String, pick: String, body: String, leftMs: Long): Orders.Answer {
        val started = SystemClock.elapsedRealtime()
        val bad = Texts.bodyProblem(body)
        if (bad.isNotEmpty()) {
            Log.i(TAG, "text refused: body $bad")
            return Orders.Answer(false, "refused", null, "the text is $bad")
        }
        val p = Contacts.finds.take(find, pick)
        val saved = p?.let { Contacts.number(ctx, it) }
        val checked = Calls.check(p, "text", saved, Contacts.emergencyCheck(ctx))
        if (p == null || checked.whyNot.isNotEmpty()) {
            Log.i(TAG, "text refused: ${checked.whyNot}")
            return Orders.Answer(false, "refused", null, checked.whyNot)
        }
        val how = Texts.route(ctx.checkSelfPermission(Manifest.permission.SEND_SMS) == PackageManager.PERMISSION_GRANTED)
        // SmsManager only for route A: on the user's Motorola, without SEND_SMS, it threw a SecurityException before
        // route B could post its notice (3 Nov). Route B counts parts itself; Messages splits the text anyway.
        val sms = if (how == "sms") smsManager(ctx) else null
        val parts = sms?.divideMessage(body) ?: Texts.parts(body)
        val audio = ctx.getSystemService(AudioManager::class.java)
        Log.i(TAG, "text to …${p.last3} (${how}, ${parts.size} parts): waiting for the Claude call to end")
        val waited = Calls.waitForEnd(
            SystemClock::elapsedRealtime, { Thread.sleep(it) }, { Calls.callOn(Dialler.signals(audio)) },
            Calls.giveUpMs(leftMs),
        ) { on, at -> Log.i(TAG, "text wait +${at} ms: ${if (on) "a call is on" else "quiet"}") }
        if (waited.step == Calls.Step.CANCEL) {
            Log.i(TAG, "text to …${p.last3} cancelled: the Claude call did not end")
            return Orders.Answer(false, "cancelled", null, "the Claude call did not end within a minute")
        }
        val after = Calls.afterS(waited.quietMs!!, SystemClock.elapsedRealtime())
        if (Prefs.dryRun(ctx)) {
            Log.i(TAG, "text dry_run parts=${parts.size} len=${body.codePointCount(0, body.length)} route=$how (${after} s after the call ended, not sent)")
            return Orders.Answer(true, "sent", mapOf("after_s" to after, "parts" to parts.size, "how" to "dry_run",
                DONE_AT to SystemClock.elapsedRealtime()), "")
        }
        if (how == "messages_app") {
            tapToText(ctx, checked.dial, body, p.let { Contacts.name(ctx, it) })
            Log.i(TAG, "text to …${p.last3}: Tap to text posted ${after} s after the Claude call ended")
            return Orders.Answer(true, "done", mapOf("after_s" to after, "parts" to parts.size, "how" to "messages_app",
                DONE_AT to SystemClock.elapsedRealtime()), "")
        }
        val left = leftMs - (SystemClock.elapsedRealtime() - started)
        val r = send(ctx, sms!!, checked.dial, parts, Texts.sentWaitMs(left), Texts.deliverWaitMs(left))
        Log.i(TAG, "text to …${p.last3}: ${if (r.ok) "sent" else "failed (${r.why})"} ${after} s after the Claude call ended, delivered=${r.delivered}")
        return Orders.Answer(r.ok, if (r.ok) "sent" else "failed", mapOf("after_s" to after, "parts" to parts.size, "how" to "sms",
            DONE_AT to r.sentAt, "delivered" to r.delivered), r.why)
    }

    /** The default SMS SIM's manager, so Android never asks which SIM (design §11.4). */
    private fun smsManager(ctx: Context): SmsManager {
        val base = ctx.getSystemService(SmsManager::class.java)
        val sub = SubscriptionManager.getDefaultSmsSubscriptionId()
        return if (sub == SubscriptionManager.INVALID_SUBSCRIPTION_ID) base else base.createForSubscriptionId(sub)
    }

    /** Elapsed-realtime ms when it went; PhoneService turns it into `sent_after_s` (seconds after the result's `at`). */
    const val DONE_AT = "done_at_ms"

    class Sent(val ok: Boolean, val why: String, val sentAt: Long, val delivered: Boolean?)

    /** Route A: a sent and a delivery PendingIntent per part. The sent reports fold into one answer within [waitMs];
     *  once all are sent, delivery reports are waited for up to [deliverMs] (null delivered = the network never said). */
    private fun send(ctx: Context, sms: SmsManager, number: String, parts: ArrayList<String>, waitMs: Long, deliverMs: Long): Sent {
        val fold = Texts.SentFold(parts.size)
        val got = Texts.DeliveredFold(parts.size)
        val tag = SystemClock.elapsedRealtimeNanos()
        val action = "${ctx.packageName}.SMS_SENT.$tag"
        val deliveredAction = "${ctx.packageName}.SMS_DELIVERED.$tag"
        val lock = Object()
        val receiver = object : BroadcastReceiver() {
            override fun onReceive(c: Context, i: Intent) {
                val part = i.getIntExtra("part", -1)
                if (i.action == action) fold.report(part, resultCode)
                else got.report(part, deliveryStatus(i))
                synchronized(lock) { lock.notifyAll() }
            }
        }
        // Not exported: the PendingIntents below are sent as this app, so only they can reach it.
        ctx.registerReceiver(receiver, IntentFilter(action).apply { addAction(deliveredAction) }, Context.RECEIVER_NOT_EXPORTED)
        try {
            fun intents(a: String, base: Int) = ArrayList(parts.indices.map { i ->
                PendingIntent.getBroadcast(
                    ctx, base + i, Intent(a).setPackage(ctx.packageName).putExtra("part", i),
                    PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_ONE_SHOT,
                )
            })
            try {
                sms.sendMultipartTextMessage(number, null, parts, intents(action, 0), intents(deliveredAction, 100))
            } catch (e: RuntimeException) {           // SecurityException if SEND_SMS went between the check and now
                Log.w(TAG, "sendMultipartTextMessage failed (${e.javaClass.simpleName})")
                return Sent(false, "the phone would not send it (${e.javaClass.simpleName})", SystemClock.elapsedRealtime(), null)
            }
            val end = SystemClock.elapsedRealtime() + waitMs
            val (ok, why) = synchronized(lock) {
                var r: Pair<Boolean, String>?
                while (true) {
                    val left = end - SystemClock.elapsedRealtime()
                    r = fold.outcome(timedOut = left <= 0)
                    if (r != null) break
                    lock.wait(left.coerceAtLeast(1))
                }
                r!!
            }
            val sentAt = SystemClock.elapsedRealtime()
            if (!ok) return Sent(false, why, sentAt, null)
            val stop = sentAt + deliverMs
            val delivered = synchronized(lock) {
                while (got.outcome() == null && SystemClock.elapsedRealtime() < stop) {
                    lock.wait((stop - SystemClock.elapsedRealtime()).coerceAtLeast(1))
                }
                got.outcome()
            }
            return Sent(true, "", sentAt, delivered)
        } finally {
            runCatching { ctx.unregisterReceiver(receiver) }
        }
    }

    /** A delivery report's TP-Status (SmsMessage.getStatus): 0-31 delivered, 32-63 still trying, 64+ failed. */
    private fun deliveryStatus(i: Intent): Int = runCatching {
        android.telephony.SmsMessage.createFromPdu(i.getByteArrayExtra("pdu"), i.getStringExtra("format")).status
    }.getOrDefault(-1)

    /** Route B (design §11.4): a tap opens Messages with the number and words filled in; the user presses Send. */
    private fun tapToText(ctx: Context, number: String, body: String, name: String?) {
        val nm = ctx.getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(CHANNEL, "Texts Claude set up", NotificationManager.IMPORTANCE_HIGH))
        val open = Intent(Intent.ACTION_SENDTO, Uri.fromParts("smsto", number, null))
            .putExtra("sms_body", body).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        val tap = PendingIntent.getActivity(ctx, 2, open, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        nm.notify(NOTE_ID, Notification.Builder(ctx, CHANNEL)
            .setSmallIcon(android.R.drawable.sym_action_email)
            .setContentTitle(if (name.isNullOrBlank()) "Tap to send the text Claude set up" else "Tap to text $name")
            .setContentText("Opens Messages with it filled in; press Send")
            .setContentIntent(tap)
            .setAutoCancel(true)
            .setTimeoutAfter(NOTE_LIFE_MS)
            .build())
    }
}
