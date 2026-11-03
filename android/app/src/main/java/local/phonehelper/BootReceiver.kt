package local.phonehelper

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log

/** After a reboot or an app update, open the channel again (design §7). Does nothing until paired. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED || intent.action == Intent.ACTION_MY_PACKAGE_REPLACED) {
            Log.i(PhoneService.TAG, "start after ${intent.action}")
            PhoneService.start(context)
        }
    }
}
