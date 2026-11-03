package local.phonehelper

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log

/**
 * adb only, debug builds only (design §9):
 *   am broadcast -a local.phonehelper.TEST -n local.phonehelper/.DebugReceiver --es do pair --es code <code>
 *   ... --es do dump      logs paired / refused / service status under the PhoneHelper tag
 *   ... --es do fakecall --ez on true|false   start or end a fake Claude call (FakeCall)
 *   ... --es do dryrun --ez on true|false     the D3 dry-run switch (Prefs.dryRun)
 * The code is never logged.
 */
class DebugReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val app = context.applicationContext
        when (intent.getStringExtra("do")) {
            "pair" -> {
                val code = intent.getStringExtra("code") ?: return
                val done = goAsync()
                Thread {
                    val err = Channel.pair(app, code)
                    Log.i(PhoneService.TAG, "debug pair: ${err ?: "paired"}")
                    // From the background Android may refuse the service start; the harness opens the screen anyway.
                    if (err == null) runCatching { PhoneService.start(app) }
                    done.finish()
                }.start()
            }
            "dump" -> Log.i(
                PhoneService.TAG,
                "debug dump: paired=${Prefs.creds(app) != null} refused=${Prefs.refused(app)} " +
                    "server=${Channel.server()} status=${PhoneService.status} " +
                    "dry_run=${Prefs.dryRun(app)} fake_call=${FakeCall.on}"
            )
            "fakecall" -> {
                val on = intent.getBooleanExtra("on", true)
                if (on) FakeCall.start(app) else FakeCall.stop(app)
                Log.i(PhoneService.TAG, "debug fake call ${if (on) "started" else "ended"}")
            }
            "dryrun" -> {
                Prefs.setDryRun(app, intent.getBooleanExtra("on", true))
                Log.i(PhoneService.TAG, "debug dry run ${Prefs.dryRun(app)}")
            }
        }
    }
}
