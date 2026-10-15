# 协作资源注册中心

`resource_registry.py` 是协作 Agent 的资源真值层。GUI 的 `session.assets` 仍是素材区兼容视图，
模型和工具则以注册中心中的稳定 `resource_id`、版本、状态和内容哈希为准。

## 状态

- `candidate`：新裁剪等候选资源，需要复核。
- `active`：当前任务可使用。
- `verified`：已由 `inspect_asset(mode=verify)` 复核。
- `superseded`：已有替代资源；执行层会返回 `replaced_by`。
- `invalid`：文件消失、内容变化或复核失败。
- `archived` / `deleted`：退出当前工作集。

只有 `candidate/active/verified` 会进入模型工作集。裁剪素材即使属于 `candidate`，在复核前仍会
被匹配工具的既有验证闸拦住。

## 资源类型

- `asset`：场景图、模板、标注图；文件使用 SHA-256 内容寻址。
- `tool` / `skill`：本轮实际可用能力的版本快照。
- `evidence`：匹配、识图、探针和数量实测。
- `claim`：按图片作用域登记的用户说法，不等于事实。

派生素材通过 `derived_from` 和 `provenance` 记录来源。源资源失效时，
`invalidate_resource()` 会沿派生关系传播失效。

## Agent API

- `search_resources`：默认只查当前有效资源。
- `get_resource`：按精确 ID 查看状态、版本、哈希和来源。
- `supersede_resource`：确认新版可用后替换旧版。

素材工具仍兼容 `asset_id` 字段，因为素材 ID 就是资源 ID。执行前
`resolve_asset_resource()` 会重新检查状态、类型、文件和哈希；不会回退到焦点素材。

每个 LLM 回合开始时生成资源工作集快照，保存在
`session.artifacts["resource_snapshots_v1"]`，用于会话恢复和问题诊断。
