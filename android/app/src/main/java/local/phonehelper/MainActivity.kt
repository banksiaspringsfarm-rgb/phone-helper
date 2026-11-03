package local.phonehelper

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.graphics.Typeface
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView

/** One screen: is it paired and connected, and the box for the one-time pairing code from the Mac (design §2.3). */
class MainActivity : Activity() {
    private lateinit var status: TextView
    private lateinit var code: EditText
    private lateinit var pair: Button
    private lateinit var texts: Button
    private var askedForTexts = false
    private var note = ""
    private val ui = Handler(Looper.getMainLooper())
    private val tick = object : Runnable {
        override fun run() { show(); ui.postDelayed(this, 1000) }      // the service's state changes on its own
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val pad = (24 * resources.displayMetrics.density).toInt()
        status = TextView(this).apply { textSize = 18f }
        code = EditText(this).apply {
            hint = "Pairing code from the Mac"
            inputType = InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_VARIATION_PASSWORD
            typeface = Typeface.DEFAULT                                  // a password field defaults to monospace
        }
        pair = Button(this).apply {
            text = "Pair with the Mac"
            setOnClickListener { pairNow() }
        }
        // SEND_SMS, READ_SMS or READ_CALL_LOG declared but not granted (an update in place, or a "Deny"): texts fall
        // back to the Tap to text notice and the inbox stays empty, so the screen says so and this asks again; once
        // Android stops asking it opens App info instead.
        texts = Button(this).apply {
            text = "Allow texts and calls"
            setOnClickListener { askedForTexts = true; requestPermissions(smsMissing().toTypedArray(), 2) }
        }
        setContentView(LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(pad, pad, pad, pad)
            addView(status)
            addView(code)
            addView(pair)
            addView(texts)
        })
        // Only what this build declares: the first Play build has no SEND_SMS (route B, design §11.1)
        val missing = arrayOf(Manifest.permission.POST_NOTIFICATIONS, Manifest.permission.READ_CONTACTS, Manifest.permission.CALL_PHONE,
            Manifest.permission.SEND_SMS, Manifest.permission.READ_SMS, Manifest.permission.READ_CALL_LOG)
            .filter { it in declared && checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
        if (missing.isNotEmpty()) requestPermissions(missing.toTypedArray(), 1)
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        if (requestCode == 2 && askedForTexts && smsMissing().isNotEmpty() &&
            smsMissing().none { shouldShowRequestPermissionRationale(it) }) {
            note = "Android will not ask again. In App info, open Permissions, allow SMS and Call logs. (If it says " +
                "restricted setting, first tap the three dots at the top right, Allow restricted settings.)"
            startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.fromParts("package", packageName, null)))
        }
        askedForTexts = false
        show()
    }

    private val declared by lazy {
        packageManager.getPackageInfo(packageName, PackageManager.GET_PERMISSIONS).requestedPermissions.orEmpty().toSet()
    }

    private fun textsDeclared() = Manifest.permission.SEND_SMS in declared

    private fun textsAllowed() = checkSelfPermission(Manifest.permission.SEND_SMS) == PackageManager.PERMISSION_GRANTED

    private val smsPerms = listOf(Manifest.permission.SEND_SMS, Manifest.permission.READ_SMS, Manifest.permission.READ_CALL_LOG)

    /** What this build declares for texts and the inbox but is not yet granted. */
    private fun smsMissing() = smsPerms.filter { it in declared && checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }

    override fun onResume() {
        super.onResume()
        PhoneService.start(this)
        ui.post(tick)
    }

    override fun onPause() {
        ui.removeCallbacks(tick)
        super.onPause()
    }

    private fun show() {
        val creds = Prefs.creds(this)
        status.text = when {
            creds == null -> "Not paired yet. Ask Claude for a pairing code, type it below.\n"
            Prefs.refused(this) -> "The Mac no longer knows this phone. Pair again below.\n".also { note = "" }
            else -> "Paired.\n\n${PhoneService.status.removePrefix("Phone helper: ").replaceFirstChar { it.uppercase() }}.\n"
        } + textsLine() + (if (note.isEmpty()) "" else "\n$note\n")
        texts.visibility = if (smsMissing().isNotEmpty()) View.VISIBLE else View.GONE
    }

    private fun textsLine() = when {
        !textsDeclared() -> ""
        textsAllowed() -> "\nTexts: the phone sends them itself.\n"
        else -> "\nTexts: not allowed yet, so each one comes as a Tap to text notice. Tap Allow texts and calls below.\n"
    } + inboxLine()

    private fun inboxLine(): String {
        if (Manifest.permission.READ_SMS !in declared) return ""
        val missing = listOf(Manifest.permission.READ_SMS to "texts", Manifest.permission.READ_CALL_LOG to "missed calls")
            .filter { checkSelfPermission(it.first) != PackageManager.PERMISSION_GRANTED }.map { it.second }
        return if (missing.isEmpty()) "Incoming texts and missed calls: Claude can tell you about them.\n"
        else "Incoming ${missing.joinToString(" and ")}: not allowed yet, so Claude can't tell you about them. Tap Allow texts and calls below.\n"
    }

    private fun pairNow() {
        val typed = code.text.toString()
        pair.isEnabled = false
        Thread {
            val err = Channel.pair(this, typed)
            runOnUiThread {
                pair.isEnabled = true
                code.setText("")
                if (err == null) PhoneService.start(this)
                note = err ?: "Paired with the Mac."
                show()
            }
        }.start()
    }
}
