package local.phonehelper

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.database.ContentObserver
import android.net.ConnectivityManager
import android.net.Network
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.os.SystemClock
import android.provider.CallLog
import android.provider.Telephony
import android.util.Log
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * The one foreground service (type specialUse, design §7): keeps ONE long-poll open to the Mac and answers each
 * order as it lands. On an error it backs off 1-30 s; a network change or the screen turning on retries at once;
 * a 401 (key revoked or replaced) stops it and asks to pair again instead of knocking (design §2.4).
 */
class PhoneService : Service() {
    companion object {
        const val TAG = "PhoneHelper"
        private const val NOTE_ID = 1
        private const val CHANNEL = "status"
        private const val CONNECTED = "Phone helper: connected to the Mac"
        @Volatile var status = "Phone helper: not running"
            private set

        fun start(ctx: Context) {
            if (Prefs.creds(ctx) == null || Prefs.refused(ctx)) return
            ctx.startForegroundService(Intent(ctx, PhoneService::class.java))
        }
    }

    @Volatile private var running = false
    private var loop: Thread? = null
    private val wake = Object()
    private var netCallback: ConnectivityManager.NetworkCallback? = null

    private var network: Network? = null

    private val screenOn = object : BroadcastReceiver() {
        override fun onReceive(c: Context, i: Intent) = poke("screen on", drop = false)
    }

    // Inbox (4 Nov): a new text or call log row sends at once, on its own thread, so the long-poll is never held up;
    // every finished poll checks too, in case a change was missed.
    private val inboxRun = java.util.concurrent.Executors.newSingleThreadScheduledExecutor()
    private val inboxDue = java.util.concurrent.atomic.AtomicBoolean(false)
    private val inboxSeen = object : ContentObserver(null) {
        override fun onChange(selfChange: Boolean) = inboxSoon()
    }

    private fun inboxSoon(delayMs: Long = 1_500) {
        if (!inboxDue.compareAndSet(false, true)) return
        runCatching {
            inboxRun.schedule({
                inboxDue.set(false)
                val creds = Prefs.creds(this)
                if (creds != null && !Prefs.refused(this)) {
                    runCatching { Inbox.check(this, creds) }.onFailure { Log.w(TAG, "inbox check failed (${it.javaClass.simpleName})") }
                }
            }, delayMs, java.util.concurrent.TimeUnit.MILLISECONDS)
        }.onFailure { inboxDue.set(false) }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        getSystemService(NotificationManager::class.java).createNotificationChannel(
            NotificationChannel(CHANNEL, "Connection to the Mac", NotificationManager.IMPORTANCE_LOW)
        )
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (!running) status = "Phone helper: connecting to the Mac"
        val note = note(status)
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(NOTE_ID, note, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(NOTE_ID, note)
        }
        if (!running) {
            running = true
            registerReceiver(screenOn, IntentFilter(Intent.ACTION_SCREEN_ON))
            Inbox.baseline(this)
            for (uri in listOf(Telephony.Sms.CONTENT_URI, CallLog.Calls.CONTENT_URI)) {
                runCatching { contentResolver.registerContentObserver(uri, true, inboxSeen) }
            }
            netCallback = object : ConnectivityManager.NetworkCallback() {
                override fun onAvailable(n: Network) {
                    val changed = network != null && network != n      // the first call just reports the network we have
                    network = n
                    poke("network available", drop = changed)
                }
            }.also { getSystemService(ConnectivityManager::class.java).registerDefaultNetworkCallback(it) }
            loop = Thread(::run, "phone-poll").apply { start() }
        } else {
            poke("restarted", drop = false)   // e.g. a new pairing: skip any backoff wait (the loop re-reads the key)
        }
        return START_STICKY
    }

    override fun onDestroy() {
        running = false
        runCatching { unregisterReceiver(screenOn) }
        runCatching { contentResolver.unregisterContentObserver(inboxSeen) }
        inboxRun.shutdownNow()
        netCallback?.let { runCatching { getSystemService(ConnectivityManager::class.java).unregisterNetworkCallback(it) } }
        Channel.abort()
        loop?.interrupt()
        super.onDestroy()
    }

    /** Retry now: end a sleeping backoff, and with [drop] also the long-poll (a network change: its socket may be
     *  dead). Never drop a healthy poll for anything else: an order handed to a dropped poll is lost. */
    private fun poke(why: String, drop: Boolean) {
        Log.i(TAG, "reconnect: $why${if (drop) " (dropping the open poll)" else ""}")
        if (drop) Channel.abort()
        synchronized(wake) { wake.notifyAll() }
    }

    private fun run() {
        var fails = 0
        var since = 0L
        while (running) {
            val creds = Prefs.creds(this)
            if (creds == null || Prefs.refused(this)) {
                show("Phone helper: pair again on the app's screen")
                stopSelf()
                return
            }
            try {
                if (!Channel.tailnetReady()) throw TailnetDown()
                // The first poll after (re)connecting is a 1 s one, so the screen says "connected" at once.
                val r = Channel.next(this, creds, if (status == CONNECTED) Net.WAIT_S else 1)
                when (r.code) {
                    200 -> { fails = 0; connected(); resend(creds); inboxSoon(0); handle(creds, JSONObject(r.body)) }
                    204 -> { fails = 0; connected(); resend(creds); inboxSoon(0) }
                    401 -> {
                        Log.w(TAG, "the Mac refused this device key: stopping until paired again")
                        Prefs.markRefused(this)
                        show("Phone helper: pair again (the Mac no longer knows this phone)")
                        stopSelf()
                        return
                    }
                    else -> throw java.io.IOException("the Mac said ${r.code}")
                }
            } catch (e: Exception) {
                if (!running) return
                fails++
                if (since == 0L) since = System.currentTimeMillis()
                val at = SimpleDateFormat("HH:mm", Locale.getDefault()).format(Date(since))
                show(if (e is TailnetDown) "Phone helper: turn Tailscale on" else "Phone helper: not connected since $at")
                val ms = Net.backoffMs(fails)
                Log.i(TAG, "poll failed (${e.javaClass.simpleName}), retry in $ms ms")
                synchronized(wake) { runCatching { wake.wait(ms) } }
                continue
            }
            since = 0L
        }
    }

    private fun connected() {
        if (status != CONNECTED) show(CONNECTED)
    }

    /** At most one call or text waits for the Claude call to end; a second one meanwhile is refused (design §4, §11.4). */
    private val calling = java.util.concurrent.atomic.AtomicBoolean(false)

    private fun handle(creds: Prefs.Creds, order: JSONObject) {
        val arrived = SystemClock.elapsedRealtime()
        val o = Channel.toMap(order)
        val deadline = Orders.deadlineMs(o, arrived)
        val overdue = SystemClock.elapsedRealtime() > deadline
        if ((o["kind"] != "call" && o["kind"] != "text") || overdue) return reply(creds, o, overdue, deadline)
        if (!calling.compareAndSet(false, true)) return reply(creds, o, false, deadline, busy = true)
        // The call or text waits up to a minute on its own thread, so the long-poll stays open meanwhile.
        Thread({
            try { reply(creds, o, false, deadline) } finally { calling.set(false) }
        }, "phone-call").start()
    }

    private fun reply(creds: Prefs.Creds, o: Map<String, Any?>, overdue: Boolean, deadline: Long, busy: Boolean = false) {
        val t0 = SystemClock.elapsedRealtime()                      // the moment `at` is read, for sent_after_s
        val raw = Orders.answer(
            o, BuildConfig.VERSION_NAME, System.currentTimeMillis() / 1000.0, overdue,
            ping = ::pingData,
            find = { name, action -> Contacts.find(this, name, action) },
            call = { find, pick ->
                if (busy) Orders.Answer(false, "refused", null, BUSY)
                else Dialler.call(this, find, pick, deadline - SystemClock.elapsedRealtime())
            },
            text = { find, pick, body ->
                if (busy) Orders.Answer(false, "refused", null, BUSY)
                else Texter.text(this, find, pick, body, deadline - SystemClock.elapsedRealtime())
            },
        ) ?: return
        val res = Orders.sentAfter(raw, Texter.DONE_AT, t0)
        Log.i(TAG, "order ${o["id"]} ${o["kind"]} -> ${res["outcome"]}")
        val action = o["kind"] == "call" || o["kind"] == "text"
        try {
            val r = Channel.result(creds, res)
            if (r.code != 200) Log.w(TAG, "result for ${o["id"]} refused: ${r.code}")
            if (action && r.code >= 500) Outbox.add(this, res)
        } catch (e: Exception) {
            Log.w(TAG, "result for ${o["id"]} not delivered (${e.javaClass.simpleName})${if (action) ": kept to resend" else ""}")
            if (action) Outbox.add(this, res)
        }
    }

    /** Resend kept call and text results now the Mac answers (sent-confirmations): oldest first, stop at the first
     *  that cannot get through. */
    private fun resend(creds: Prefs.Creds) {
        val kept = Outbox.load(this)
        if (kept.isEmpty()) return
        val left = kept.toMutableList()
        for (res in kept) {
            val code = runCatching { Channel.result(creds, res).code }.getOrDefault(-1)
            Log.i(TAG, "resent result for ${res["id"]} ${res["kind"]}: ${if (code < 0) "not delivered" else code}")
            if (!Outbox.done(code)) break
            left.remove(res)
        }
        Outbox.save(this, left)
    }

    private val BUSY = "another call or text is already waiting for the Claude call to end"

    private fun pingData(): Map<String, Any?> = mapOf(
        "version" to BuildConfig.VERSION_NAME,
        "sdk" to Build.VERSION.SDK_INT,
        "perms" to mapOf(
            "call_phone" to granted(Manifest.permission.CALL_PHONE),
            "read_contacts" to granted(Manifest.permission.READ_CONTACTS),
            "notifications" to granted(Manifest.permission.POST_NOTIFICATIONS),
            "send_sms" to granted(Manifest.permission.SEND_SMS),      // route A or B for texts (design §11.1)
            "read_sms" to granted(Manifest.permission.READ_SMS),      // inbox (4 Nov)
            "read_call_log" to granted(Manifest.permission.READ_CALL_LOG),
        ),
        "battery_exempt" to getSystemService(PowerManager::class.java).isIgnoringBatteryOptimizations(packageName),
    )

    private fun granted(p: String) = checkSelfPermission(p) == PackageManager.PERMISSION_GRANTED

    private fun show(text: String) {
        status = text
        getSystemService(NotificationManager::class.java).notify(NOTE_ID, note(text))
    }

    private fun note(text: String): Notification {
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE
        )
        return Notification.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_notify)
            .setContentTitle(text)
            .setContentIntent(open)
            .setOngoing(true)
            // shown at once: the default 10 s deferral lost it when show() updated it meanwhile (fresh pairing, emulator)
            .setForegroundServiceBehavior(Notification.FOREGROUND_SERVICE_IMMEDIATE)
            .build()
    }

    private class TailnetDown : Exception()
}
