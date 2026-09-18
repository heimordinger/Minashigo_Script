# Minashigo Mobile（方案二 · 非业务地基）

手机一体：**素材 → 云端生成 → 本机执行**。本目录先只放 **非业务** 部分，不含识图点击业务循环。

## 目录

```
mobile/
  README.md                 # 本说明
  pack/
    SCHEMA.md               # 脚本包契约（人读）
    schema/                 # JSON Schema
    examples/demo_pack/     # 示例包（可导入校验）
  tools/
    validate_pack.py        # PC 侧校验脚本包
  android/                  # Kotlin 壳工程（权限引导占位）
```

## 当前阶段（Phase 0）

| 做 | 不做 |
|----|------|
| pack 格式 + 示例 | OpenCV 识图 |
| `validate_pack.py` | 无障碍手势点击 |
| App 壳 + 权限引导页占位 | LLM 生成 |
| 导入 zip/目录列出内容（后续） | 游戏业务脚本 |

## 快速校验示例包

```bash
python mobile/tools/validate_pack.py mobile/pack/examples/demo_pack
```

## Android 壳

用 Android Studio 打开 `mobile/android/`。首屏为权限引导占位，业务执行器后续再接。
