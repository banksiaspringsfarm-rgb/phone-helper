package local.phonehelper

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * The pairing: device id plus the device key the Mac handed out (design §2.3). The key is kept in app-private
 * prefs, wrapped with an Android Keystore AES key, and allowBackup is off, so it never leaves the phone.
 */
object Prefs {
    private const val FILE = "pairing"
    private const val ALIAS = "phonehelper.devicekey"

    data class Creds(val device: String, val key: String)

    fun creds(ctx: Context): Creds? {
        val p = ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE)
        val device = p.getString("device", null) ?: return null
        val iv = p.getString("iv", null) ?: return null
        val ct = p.getString("ct", null) ?: return null
        return try {
            val c = Cipher.getInstance("AES/GCM/NoPadding")
            c.init(Cipher.DECRYPT_MODE, secret(), GCMParameterSpec(128, Base64.decode(iv, Base64.NO_WRAP)))
            Creds(device, String(c.doFinal(Base64.decode(ct, Base64.NO_WRAP))))
        } catch (e: Exception) {
            null                                   // the Keystore key is gone (app data restored elsewhere): pair again
        }
    }

    fun save(ctx: Context, device: String, key: String) {
        val c = Cipher.getInstance("AES/GCM/NoPadding")
        c.init(Cipher.ENCRYPT_MODE, secret())
        ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE).edit()
            .putString("device", device)
            .putString("iv", Base64.encodeToString(c.iv, Base64.NO_WRAP))
            .putString("ct", Base64.encodeToString(c.doFinal(key.toByteArray()), Base64.NO_WRAP))
            .putBoolean("refused", false)
            .apply()
    }

    /** The Mac said 401: the key was revoked or replaced. The service stops knocking until he pairs again. */
    fun markRefused(ctx: Context) =
        ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE).edit().putBoolean("refused", true).apply()

    fun refused(ctx: Context): Boolean =
        ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE).getBoolean("refused", false)

    /**
     * design §9, D3: in a debug build the call is played out up to the dial and stops there, by default everywhere
     * but the emulator, so a test job on the user's phone never rings anyone. The debug receiver can flip it.
     * A release build has no such switch: it is always false there.
     */
    fun dryRun(ctx: Context): Boolean = BuildConfig.DEBUG &&
        ctx.getSharedPreferences(DEBUG_FILE, Context.MODE_PRIVATE)
            .getBoolean("dry_run", !Net.isEmulator(android.os.Build.HARDWARE))

    fun setDryRun(ctx: Context, on: Boolean) =
        ctx.getSharedPreferences(DEBUG_FILE, Context.MODE_PRIVATE).edit().putBoolean("dry_run", on).apply()

    private const val DEBUG_FILE = "debug"

    private fun secret(): SecretKey {
        val ks = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (ks.getKey(ALIAS, null) as? SecretKey)?.let { return it }
        val g = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        g.init(
            KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .build()
        )
        return g.generateKey()
    }
}
