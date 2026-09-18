package com.minashigo.mobile.pack

import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * 从目录加载 pack（非业务：解析 + 结构摘要，不跑状态机）。
 */
object PackLoader {
    fun loadDir(root: File): PackSummary {
        require(root.isDirectory) { "pack root must be directory: $root" }
        val manJson = JSONObject(File(root, "manifest.json").readText(Charsets.UTF_8))
        val scriptJson = JSONObject(File(root, "script.json").readText(Charsets.UTF_8))
        val imagesDir = File(root, "images")
        val images = if (imagesDir.isDirectory) {
            imagesDir.walkTopDown()
                .filter { it.isFile && it.extension.lowercase() in setOf("png", "jpg", "jpeg", "webp") }
                .map { it.relativeTo(imagesDir).invariantSeparatorsPath }
                .sorted()
                .toList()
        } else emptyList()
        val readmeFile = File(root, "readme.txt")
        val readme = if (readmeFile.isFile) readmeFile.readText(Charsets.UTF_8) else null
        return PackSummary(
            manifest = parseManifest(manJson),
            script = parseScript(scriptJson),
            imageNames = images,
            readme = readme,
        )
    }

    fun parseManifest(o: JSONObject): PackManifest {
        val ref = o.optJSONObject("ref_size")
        return PackManifest(
            schemaVersion = o.getString("schema_version"),
            id = o.getString("id"),
            name = o.getString("name"),
            version = o.getString("version"),
            platform = o.getString("platform"),
            entry = o.optString("entry").ifEmpty { "script.json" },
            targetPackage = o.optionalString("target_package"),
            refWidth = ref?.optInt("w"),
            refHeight = ref?.optInt("h"),
            author = o.optionalString("author"),
            createdAt = o.optionalString("created_at"),
        )
    }

    fun parseScript(o: JSONObject): PackScript {
        val statesObj = o.getJSONObject("states")
        val states = linkedMapOf<String, PackState>()
        val keys = statesObj.keys()
        while (keys.hasNext()) {
            val id = keys.next()
            states[id] = parseState(statesObj.getJSONObject(id))
        }
        return PackScript(
            schemaVersion = o.getString("schema_version"),
            initial = o.getString("initial"),
            states = states,
        )
    }

    private fun parseState(o: JSONObject): PackState {
        return PackState(
            label = o.optionalString("label"),
            detect = parseDetectList(o.optJSONArray("detect")),
            actions = parseActionList(o.optJSONArray("actions")),
            onMiss = o.optionalString("on_miss"),
        )
    }

    private fun parseDetectList(arr: JSONArray?): List<PackDetect> {
        if (arr == null) return emptyList()
        return buildList {
            for (i in 0 until arr.length()) {
                val d = arr.getJSONObject(i)
                add(
                    PackDetect(
                        image = d.getString("image"),
                        threshold = d.optDouble("threshold", 0.85),
                        goto = d.getString("goto"),
                    )
                )
            }
        }
    }

    private fun parseActionList(arr: JSONArray?): List<PackAction> {
        if (arr == null) return emptyList()
        return buildList {
            for (i in 0 until arr.length()) {
                val a = arr.getJSONObject(i)
                add(
                    PackAction(
                        type = a.getString("type"),
                        image = a.optionalString("image"),
                        threshold = if (a.has("threshold")) a.getDouble("threshold") else null,
                        expect = a.optionalString("expect"),
                        appearImage = a.optionalString("appear_image"),
                        x = if (a.has("x")) a.getInt("x") else null,
                        y = if (a.has("y")) a.getInt("y") else null,
                        nx = if (a.has("nx")) a.getDouble("nx") else null,
                        ny = if (a.has("ny")) a.getDouble("ny") else null,
                        ms = if (a.has("ms")) a.getInt("ms") else null,
                        msMin = if (a.has("ms_min")) a.getInt("ms_min") else null,
                        msMax = if (a.has("ms_max")) a.getInt("ms_max") else null,
                        state = a.optionalString("state"),
                        message = a.optionalString("message"),
                        ok = if (a.has("ok")) a.getBoolean("ok") else null,
                    )
                )
            }
        }
    }

    private fun JSONObject.optionalString(key: String): String? {
        if (!has(key) || isNull(key)) return null
        return getString(key).ifEmpty { null }
    }
}
