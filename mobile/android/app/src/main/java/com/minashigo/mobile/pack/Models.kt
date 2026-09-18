package com.minashigo.mobile.pack

/**
 * 脚本包数据模型（非业务：只描述契约，不执行动作）。
 * 与 mobile/pack/SCHEMA.md v0.1 对齐。
 */
data class PackManifest(
    val schemaVersion: String,
    val id: String,
    val name: String,
    val version: String,
    val platform: String,
    val entry: String = "script.json",
    val targetPackage: String? = null,
    val refWidth: Int? = null,
    val refHeight: Int? = null,
    val author: String? = null,
    val createdAt: String? = null,
)

data class PackScript(
    val schemaVersion: String,
    val initial: String,
    val states: Map<String, PackState>,
)

data class PackState(
    val label: String? = null,
    val detect: List<PackDetect> = emptyList(),
    val actions: List<PackAction> = emptyList(),
    val onMiss: String? = null,
)

data class PackDetect(
    val image: String,
    val threshold: Double = 0.85,
    val goto: String,
)

data class PackAction(
    val type: String,
    val image: String? = null,
    val threshold: Double? = null,
    val expect: String? = null,
    val appearImage: String? = null,
    val x: Int? = null,
    val y: Int? = null,
    val nx: Double? = null,
    val ny: Double? = null,
    val ms: Int? = null,
    val msMin: Int? = null,
    val msMax: Int? = null,
    val state: String? = null,
    val message: String? = null,
    val ok: Boolean? = null,
)

data class PackSummary(
    val manifest: PackManifest,
    val script: PackScript,
    val imageNames: List<String>,
    val readme: String?,
)
