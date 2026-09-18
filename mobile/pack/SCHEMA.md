# 脚本包契约 v0（非业务）

一个可导入、可校验、可执行的最小单元。扩展字段须保持向后兼容。

## 目录结构

```
{pack_name}/
  manifest.json     # 必填：元信息
  script.json       # 必填：状态机
  readme.txt        # 可选：用户说明（生成器用）
  images/           # 必填目录（可为空，但校验会警告）
    *.png | *.jpg | *.webp
```

也可打成 `{pack_name}.zip`，根目录直接含上述文件（不要多套一层无意义文件夹，或只允许一层同名根目录）。

## manifest.json

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `schema_version` | string | 是 | 固定 `"0.1"` |
| `id` | string | 是 | 包唯一 id，建议 slug |
| `name` | string | 是 | 展示名 |
| `version` | string | 是 | 如 `0.1.0` |
| `platform` | string | 是 | 固定 `"android"`（v0） |
| `entry` | string | 是 | 默认 `"script.json"` |
| `target_package` | string | 否 | 目标 App 包名，占位 |
| `ref_size` | object | 否 | `{ "w": 1080, "h": 2400 }` 素材基准分辨率 |
| `author` | string | 否 | |
| `created_at` | string | 否 | ISO8601 |

## script.json

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `schema_version` | string | 是 | `"0.1"` |
| `initial` | string | 是 | 初始状态 id |
| `states` | object | 是 | `状态id → State` |

### State

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `label` | string | 否 | 人读标签 |
| `detect` | Detect[] | 否 | 场景识别（按序，先命中先生效） |
| `actions` | Action[] | 否 | 进入该状态后执行的动作 |
| `on_miss` | string | 否 | 未识别时跳转的状态 id，默认留在当前/回 initial（执行器定） |

### Detect

| 字段 | 类型 | 说明 |
|------|------|------|
| `image` | string | 相对 `images/` 的文件名 |
| `threshold` | number | 默认 `0.85` |
| `goto` | string | 命中后进入的状态 |

### Action（v0 仅占位枚举，执行器后期实现）

| `type` | 字段 | 说明 |
|--------|------|------|
| `click_image` | `image`, `threshold?`, `expect?` | expect: `none\|gone\|appear` |
| `click_xy` | `x`, `y` 或 `nx`, `ny`（0~1 归一化） | |
| `sleep` | `ms` 或 `ms_min`/`ms_max` | |
| `goto` | `state` | |
| `log` | `message` | |
| `exit` | `ok?` | 结束脚本 |

v0 **校验器只检查结构与图片引用**，不执行动作。

## 校验规则（validate_pack）

1. `manifest.json` / `script.json` 可解析且通过 schema  
2. `initial` 必须在 `states` 中  
3. 所有 `goto` / `on_miss` / `actions.goto` 目标状态存在  
4. 所有引用的 `image` 在 `images/` 中存在  
5. `schema_version` 为支持列表之一（当前仅 `0.1`）
