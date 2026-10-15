# Script Generator 资源归类

协作侧能被模型**发现和调用**的东西统一叫「能力」（capability），进同一张表，
用同一套查询接口读。分两类：

| kind | 是什么 | 能 act 调用吗 | 完整内容在哪 |
|---|---|---|---|
| `skill` | **说明书**：怎么组合能力做某类事 | ❌ 只读正文照做 | `capability.get` → `body` |
| `tool` | **可调用能力** | ✅ | `capability.get` → `input_schema` |

## 查询接口（V3）

| 查询 | 用途 |
|---|---|
| `capability.search` | 列能力，只回摘要。可按 `kind` / `query` / `limit` 筛 |
| `capability.get` | 取单个能力的完整内容（工具的参数表 / 技能的正文） |

`capability.search` 的关键词同时扫**摘要和技能正文**——SKILL.md 常是英文写的，
中文提问（例：搜「标注」）也必须能命中，否则归类了却查不到。

`capability.search` 返回 `total` / `truncated` / `kinds`，默认只给 20 条：
30 个工具带描述就是 13.5 KB，先缩小范围比"塞进去再砍掉"合理。

## 三类资源及其来源

### 1. 内置工具（代码在仓库里）

`backend/script_generator/collaborator/llm_turn.py` 的 `openai_collab_tools()`。
随仓库分发，模型每轮工具表都带上。

### 2. 内置的"固化工具"（曾经的自造工具）

`backend/script_generator/collaborator/bundled_tools/`

| 工具 | 作用 | 固化时的使用次数 |
|---|---|---|
| `combined_mark` | 一张底图上双色标注：勋章红框 + 空格子绿框，自带模板可信度自检 | 31 |
| `count_medals` | 按卡片归组数勋章 | 12 |
| `annotate_cn` | 画中文标注（自动加载中文字体） | 2 |

这些原本只活在 `user_data/collab_tools.json`（本机、不进版本库、换机器就没）。
固化后随仓库分发，**不可删除**，同名自造会被拒绝。

> 改这些工具：先在探针里跑通，再跑
> `python tools\_scratch\_export_bundled_tools.py` 重新导出。**不要手改** `.py`，
> 它是导出产物。

### 3. 模型自造工具（本机沉淀）

`user_data/collab_tools.json`，由 `define_tool` 登记。本机可用、可删。
和内置同名时**内置优先**。

### 4. 技能说明书（SKILL.md）

`backend/script_generator/skills/<name>/SKILL.md`

扫描器：`collaborator_v3/adapters/skill_capabilities.py`（`sync_skill_capabilities`）。
由 `gui_bridge` 在每轮开始同步进能力表，所以文档一改就自动升版本（版本号 = 内容哈希）。

| 技能 | 何时用 |
|---|---|
| `mark-match-result` | 模板已定，要把匹配结果标在图上交回 |
| `visual-locate-and-mark` | 目标或位置尚未可靠确定；先建目标契约与候选，再验证并标图 |
| `repeated-ui-crop` | 要**裁剪**重复 UI（卡片/勋章/格子/列表项） |
| `vision-count-select` | 多实例计数并选最多（例：打勋章最多的结界） |
| `window-click-retarget` | 「点不动 / 识图成功但无反应」诊断 |
| `verify-step-effect` | 按"动作 + 该步完成条件"验证一步是否生效 |
| `optimize-script-perf` | 用 pseudo-record 指标优化脚本性能 |
| `collab-script-coach` | 整段共创的总纲（阶段/原则） |

## 加一个新技能

1. 写 `backend/script_generator/skills/<kebab-name>/SKILL.md`
2. front matter 至少给 `name` 和 `description`（一句话说清"何时用"）
3. 正文写：**何时走这条 / 流程 / 不要做 / 完成判据**
4. 跑 `python -m pytest tests\test_collab_resource_registry.py -q`
5. 不需要改代码——扫描器会自动收录，`capability.search(kind="skill")` 立刻能查到

可选：`.cursor/skills/<name>/SKILL.md` 放一个薄包装指回本目录（给人用，模型读不到）。

## 仍然缺的一块

`collaborator/skills/registry.py` 的 `default_registry()` 是**空的**：那里放的是
"能出脚本的玩法插件"（`CollabSkill` 协议，有 `emit()`），和上面的说明书技能不是一回事。
所以 V2 的 `list_skills` 会把两边都列出来，避免模型看到空列表就以为本地没有技能。
