package com.minashigo.mobile

import android.content.Intent
import android.os.Bundle
import androidx.appcompat.app.AppCompatActivity
import com.minashigo.mobile.databinding.ActivityMainBinding

/**
 * Phase 0 壳：入口。业务执行器后续再接。
 */
class MainActivity : AppCompatActivity() {
    private lateinit var binding: ActivityMainBinding

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        binding.btnPermissions.setOnClickListener {
            startActivity(Intent(this, PermissionGuideActivity::class.java))
        }
        binding.txtHint.text = getString(R.string.main_hint)
    }
}
