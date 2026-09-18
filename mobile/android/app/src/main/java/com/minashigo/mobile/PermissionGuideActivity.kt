package com.minashigo.mobile

import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.NotificationManagerCompat
import com.minashigo.mobile.databinding.ActivityPermissionGuideBinding

/**
 * Phase 0：权限引导占位（非业务）。
 * 只负责跳转系统设置 / 申请通知权；录屏与无障碍在执行器阶段再接。
 */
class PermissionGuideActivity : AppCompatActivity() {
    private lateinit var binding: ActivityPermissionGuideBinding

    private val notifPermission = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        Toast.makeText(
            this,
            if (granted) R.string.perm_notif_ok else R.string.perm_notif_denied,
            Toast.LENGTH_SHORT
        ).show()
        refreshStatus()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityPermissionGuideBinding.inflate(layoutInflater)
        setContentView(binding.root)

        binding.btnOverlay.setOnClickListener {
            val intent = Intent(
                Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                Uri.parse("package:$packageName")
            )
            startActivity(intent)
        }
        binding.btnAccessibility.setOnClickListener {
            startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
        }
        binding.btnNotification.setOnClickListener {
            if (Build.VERSION.SDK_INT >= 33) {
                notifPermission.launch(android.Manifest.permission.POST_NOTIFICATIONS)
            } else {
                Toast.makeText(this, R.string.perm_notif_legacy, Toast.LENGTH_SHORT).show()
            }
        }
        binding.btnRefresh.setOnClickListener { refreshStatus() }
        refreshStatus()
    }

    override fun onResume() {
        super.onResume()
        refreshStatus()
    }

    private fun refreshStatus() {
        val overlay = Settings.canDrawOverlays(this)
        val notif = NotificationManagerCompat.from(this).areNotificationsEnabled()
        binding.txtOverlayStatus.text = getString(
            if (overlay) R.string.status_on else R.string.status_off
        )
        binding.txtNotifStatus.text = getString(
            if (notif) R.string.status_on else R.string.status_off
        )
        binding.txtA11yStatus.text = getString(R.string.perm_a11y_placeholder)
        binding.txtCaptureStatus.text = getString(R.string.perm_capture_placeholder)
    }
}
