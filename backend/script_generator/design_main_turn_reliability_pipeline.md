# 主轮可靠性管线：面向一类问题的统一实施方案

> 状态：当前最终决策，可直接实施；2026-10-01增加LangGraph控制层修订；2026-10-03完成
> `asset_analysis` 控制权交接并补充“LLM会漏填/遗忘契约”的上位设计原则。  
> 日期：2026-10-01；最近备案：2026-10-03。  
> 目标：不再按单个报错补丁修复，而是让每个主轮都经过同一条“定向—绑定—契约—语义—证据”管线。  
> 相关讨论与历史：`design_local_api_contract_ticket.md`及Phase 0—3报告。

## 1. 统一问题定义

目前看似不同的问题实际属于同一类：**模型输出的意图、资源、调用和结论，没有在本地逐层绑定到
可验证事实**。

典型表现：

- 用户要求分析图片，模型擅自进入脚本生成；
- 素材ID/token被当作普通路径；
- 模型根据简略索引猜API和返回值；
- 普通Python方法被误判成本地Runtime API；
- `iterable`被误解成“多个业务对象”；
- 工具失败后改变任务类型或要求用户反复“继续”；
- 只通过部分静态检查，却向用户表达成“脚本已通过”。

统一解法不是增加更多提示词，而是建立五层本地管线：

```text
任务定向闸
→ 资源与感知证据绑定层
→ 调用归属与契约闸
→ 语义类型与数据流闸
→ 证据与完成闸
```

LLM负责理解目标、选择算法、写业务Python和根据诊断修复；本地系统负责事实、权限、资源、API、
类型、状态转换和最终结论的机械约束。

### 1.1 2026-10-01控制层修订：引入LangGraph，但不重做底层事实系统

2026-10-01真实主轮 `mt_20261001T042948Z_A9ACC5` 证明：仅靠“LLM每副轮自由选择action，
Runtime事后用闸门拒绝”的结构仍会空转。该轮在收到 `VISUAL_EVIDENCE_REQUIRED` 后继续反复查询
能力契约、资源和请求用户；`INTERACTION_NOT_JUSTIFIED` 连续出现十余次；模型长期声明
`script_generation`，而本地推导一直是 `script_design`。各闸门本身多数正确，但没有一个本地控制器
强制模型沿正确恢复路径前进。

因此新增一个**本地编排控制层**：使用LangGraph表达主轮阶段、允许动作、条件边、重试预算和人工中断。
它只替换现有“自由动作循环”的控制权，不替换以下已经建立的事实源：

- `main_turns/tasks/domain_events/subturns`及现有SQLite投影；
- `ResourceBinder`与资源版本、hash、角色、用途；
- `VisualEvidenceBroker`与evidence；
- API Contract Registry、ticket、receipt与校验器；
- 权限、试运行记录、伪录制和GUI状态。

LangGraph不是新的长期记忆，也不是第二套资源/证据/任务数据库。它只保存**执行游标和权威事实的引用**；
每次恢复都从现有Runtime投影重建状态。不得把完整素材、旧工具结果或模型推测复制进LangGraph checkpoint。

控制原则从：

```text
LLM自由选择任意action → Runtime拒绝不合法动作 → LLM再自由选择
```

修订为：

```text
本地graph_node确定当前合法工作 → LLM只完成该节点的结构化产出
→ 本地验证节点结果 → 条件边选择唯一下一类节点
```

LangGraph解决的是流程漂移与空转，不替代模型理解、视觉取证、算法设计和语义校验。

### 1.2 与前文规则的冲突及裁决

| 原规则 | 冲突 | 新裁决 |
| --- | --- | --- |
| 每副轮由模型声明 `task_type`/orientation | 模型能长期声明错误阶段，并把同一任务在design/generation间漂移 | `task_type`与`graph_node`由本地主轮状态决定；模型声明只作一致性断言，不能驱动跳转 |
| 七类action在任意副轮均可返回，再由闸门拒绝 | 正确错误码仍会被模型反复撞击 | 每个节点只暴露允许的输出schema/工具子集；非法动作不进入通用循环 |
| `VISUAL_EVIDENCE_REQUIRED`以文字告诉模型下一步 | 模型继续查工具文档或请求用户 | 错误直接连到 `acquire_evidence` 节点；该节点只允许本地CV/视觉Broker及证据查询 |
| `INTERACTION_NOT_JUSTIFIED`后让模型自行修复 | 模型可连续重复request_interaction | 转到 `resolve_gap_locally`；只有本地证明搜索/派生/替代路线穷尽后才允许 `human_input` |
| script_design/script_generation依赖模型自报 | 阶段只影子告警，不阻止提前生成 | 两者成为不同图节点；确认的draft hash是唯一转换条件 |
| 校验失败后模型自行决定下一动作 | 可重问需求、重做计划或大幅换稿 | 只转 `repair_script`，且保持goal/draft binding；实质逻辑变化必须退回design并重新确认 |
| `trial.finished`可进入完成回复 | 进程正常结束不等于业务成功 | 转 `evaluate_trial`; 只有验收证据满足才到 `completed` |

迁移期不得立即删除Phase A的orientation闸。影子图阶段继续要求orientation，用它比较
`model_declared_task_type`与`graph_expected_task_type`；图正式接管路由后，orientation不再承担每副轮选路职责，
只在主轮首次定向、用户补充导致兼容性评估、以及显式目标修订时提交。该条**覆盖**后文“每副轮必须靠
orientation重新定向”的长期设计，但保留它作为迁移兼容协议，直到自由循环退出生产路径。

### 1.3 上位可靠性假设：LLM会漏填、忘参数、忘契约

从2026-10-03起，后续实现与评审统一采用以下前提，而不是把它们当作偶发模型失误：

> **LLM会忘记填表、遗漏必填字段、忘记刚读过的参数和契约，也可能在恢复、长上下文或多步任务中
> 回到自己熟悉但不属于本项目的写法。系统必须在这种前提下仍然安全推进或明确收口。**

因此，“提示词已经写过”“上一副轮已经告诉过”“模型理应记得”均不能作为可靠性证明。所有新增功能
必须先回答：**如果模型没填、少填、填旧值、填错类型、忘了契约，这一步是否仍能由本地事实完成，或在
产生副作用前给出稳定诊断？**

设计规则如下：

1. **Runtime已知的事实不得要求LLM重复回填。** 已确认的task type、graph node、资源id/version/hash、
   evidence id、权限作用域、契约revision、已验证脚本hash等，优先由本地继承、绑定或投影；不得因为模型
   漏抄一次就丢弃已经完成的工作。
2. **LLM负责新判断，不负责抄本地账本。** 用户目标解释、算法设计、回复组织仍由模型完成；资源真实路径、
   证据绑定、当前阶段、调用签名与权限状态由Runtime确定。自动补全只允许来自唯一事实源，不得替用户做
   高风险选择、扩大权限或猜测现实事实。
3. **宽进严出。** 输入侧兼容缺省字段、资源id、虚拟路径和旧token，并在本地规范化；执行前必须完成
   Binder、契约、权限、类型与结果校验。无法由事实源确定时结构化拒绝，不能静默猜测。
4. **节点只暴露当下最小协议。** LangGraph节点提供当前允许动作、所需字段、已有结果与精确下一步，避免
   让模型在全局动作、全量工具和相互无关的契约中自行回忆。
5. **契约按需取回，不依赖记忆。** `search`只做发现，`get`返回完整schema；ToolAdapter和校验器以本地
   registry为准。模型忘记查契约时，Runtime给一次精确补查/修复路径，而不是允许凭记忆执行。
6. **修复必须幂等且有上限。** 第一次遗漏返回缺失字段和可提交范例；重复同类遗漏走确定性恢复或停止，
   不能靠用户反复说“继续”维持生命。
7. **每项验收必须包含健忘反例。** 至少覆盖：漏整个结构块、漏单个必填字段、使用旧参数名、忘记证据id、
   忘记orientation、重复查询、恢复后忘记节点、以及模型输出与Runtime事实冲突。

该原则不是“替模型把一切都猜出来”。边界是：**本地已经知道的可以确定性继承；只有用户能决定或会显著
改变结果、成本、架构、权限和不可逆后果的内容，仍须由模型明确询问用户并取得新证据。**

## 2. 主轮权威上下文

每个主轮建立一个本地 `TurnIntentContext`，它不是LLM自报事实：

```json
{
  "main_turn_id": "mt_...",
  "task_type": "asset_analysis",
  "goal": "统计结界1里的勋章数量",
  "intent_evidence": {
    "message_id": "msg_...",
    "quote": "请你用模板匹配，回答我结界1里有多少勋章"
  },
  "workspace_snapshot_id": "ws_...",
  "runtime_target": "unresolved",
  "resource_revision": "...",
  "permission_revision": "..."
}
```

原则：

1. LLM提出task type，本地验证用户证据后才写入；
2. 后续副轮只能继承，不能自行改变；
3. 用户补充新目标时，终止旧快照驱动的未来计划，再做兼容性评估；
4. “继续、重来、再试一次”只控制当前任务，不构成新目标；
5. 工具失败、协议错误和资源错误都不能改变task type；
6. 主轮之间不继承未提交推测、旧资源权限和未验证API事实。

## 3. 第一层：任务定向闸

### 3.1 定向协议（自由循环迁移期每副轮必填，图接管后仅在边界提交）

同一次模型响应包含结构化orientation和一个action，不额外调用模型。任务类型封闭为：

- `conversation`
- `requirement_clarification`
- `asset_analysis`
- `tool_operation`
- `script_design`
- `script_generation`
- `script_modification`
- `trial_diagnosis`

任务动作必须匹配本地矩阵。关键规则：

- `asset_analysis`允许查询、分析工具、主张和直接回答；禁止建脚本Task、逻辑确认、校验脚本和去试运行；
- 三种script任务必须有真实用户消息中的明确脚本/代码/自动化意图；
- `trial_diagnosis`先查询试运行证据，不重问整套需求；
- 任务类型变化必须引用新的用户消息并通过兼容性评估。

此处“每副轮必填”是当前自由动作循环的兼容约束，不是LangGraph接管后的最终形态。图接管后：

- 主轮入口的 `orient` 节点产生并本地确认task type；
- 普通节点从graph state读取task type，不要求模型重复选择；
- 用户补充可能改变目标时进入 `reorient`，旧快照驱动的未来节点作废；
- 模型若声明与当前节点冲突，只形成 `MODEL_STAGE_CONFLICT`，不能改变图状态；
- “继续/重来”、工具失败、模型协议错误均没有通向 `reorient` 的边。

缺少脚本意图返回 `SCRIPT_INTENT_NOT_EXPLICIT`，但不自动追问用户是否写脚本，而是要求模型留在
原任务内继续完成。

### 3.2 协议错误恢复

`MODEL_PROTOCOL_ERROR`后只允许一次格式修复。修复响应必须重新提交完整orientation；Runtime不得恢复
一个尚未通过定向闸的旧计划。第二次仍失败则保存进度并报告协议失败，不让用户用“继续”循环唤醒。

### 3.3 LangGraph主状态与节点

图状态只保存控制字段和权威对象引用：

```json
{
  "main_turn_id": "mt_...",
  "run_id": "run_...",
  "task_type": "script_design",
  "graph_node": "acquire_evidence",
  "goal_revision": 2,
  "workspace_snapshot_id": "ws_...",
  "resource_revision": "resrev_...",
  "permission_revision": "permrev_...",
  "missing_evidence": ["layout:opponent_cards"],
  "evidence_refs": ["ev_..."],
  "logic_draft_hash": "sha256:...",
  "confirmed_draft_hash": "",
  "script_hash": "",
  "validation_receipt_id": "",
  "trial_run_id": "",
  "attempts": {"acquire_evidence": 1},
  "last_progress_fingerprint": "sha256:..."
}
```

封闭节点至少包括：

| 节点 | 允许的工作 | 成功边 | 失败/缺口边 |
| --- | --- | --- | --- |
| `orient` | 任务类型与用户证据 | 对话/分析/脚本对应入口 | `human_input`或`failed` |
| `discover_context` | 查任务、资源、能力、必要契约 | `bind_resources` | `resolve_gap_locally` |
| `bind_resources` | 调ResourceBinder | 当前任务下一节点 | `resolve_gap_locally` |
| `assess_evidence` | 判断已有证据是否覆盖当前问题 | `answer`/`design_logic` | `acquire_evidence` |
| `acquire_evidence` | 本地CV、视觉Broker、证据落库 | `assess_evidence` | `report_insufficient_evidence` |
| `design_logic` | 生成结构化逻辑草稿 | `confirm_logic` | `resolve_gap_locally` |
| `confirm_logic` | 仅展示并绑定draft hash | `generate_script` | `design_logic`/`human_input` |
| `generate_script` | 按已确认hash写普通Python | `validate_script` | `failed` |
| `validate_script` | Binder/ticket/类型/数据流/smoke | `deliver` | `repair_script` |
| `repair_script` | 仅修诊断指出的问题 | `validate_script` | `design_logic`或`failed` |
| `deliver` | receipt绑定、保存、分层报告 | `await_trial`或`completed` | `failed` |
| `evaluate_trial` | 读取日志、事件、关键帧与验收条件 | `completed` | `diagnose_trial` |
| `diagnose_trial` | 基于真实运行证据定位 | `repair_script`/`human_input` | `report_blocked` |
| `resolve_gap_locally` | 搜索、派生、替代能力、低风险默认 | 原节点 | `human_input` |
| `human_input` | 只承载四类允许询问及逻辑确认 | 对应恢复节点 | `paused` |
| `report_blocked` | 保存成果、证据和剩余工作 | `paused` | — |

每个节点有独立的 `input_schema`、`output_schema`、允许工具集合、最大尝试次数与“何为新增进展”的判据。
模型不再直接返回任意全局action；节点适配器把结构化结果转换为现有Runtime命令和领域事件。

### 3.4 空转检测成为图级不变量

`progress_fingerprint`由节点名、关键资源/证据/草稿/脚本revision、结构化缺口和诊断码组成。以下情况
不算进展：重复查看同一契约、重复查询已有资源、重复提交同一interaction、仅改action_id或措辞、
没有新增evidence的视觉调用、校验失败后提交相同script hash。

- 同一节点连续2次无进展：走该节点的确定性恢复边；
- 恢复后再次无进展：走 `report_blocked`，不得自动续轮；
- 同一诊断码不能由模型自由重试超过节点预算；
- 新用户补充会建立新输入revision，可重置**相关节点**预算，但不能清空无关失败历史；
- 预算耗尽只暂停并保存，不把“继续”当作新增证据。

## 4. 第二层A：统一资源绑定

### 4.1 资源不是字符串

所有工具schema和脚本素材引用使用结构化 `ResourceRef`：

```json
{"id":"asset_...","version":1,"type":"image","role":"template"}
```

LLM可在源码中写稳定token，但不能写绝对路径。Runtime的 `ResourceBinder`统一完成：

```text
ResourceRef/token
→ 查询当前工作区资源
→ 检查版本、状态、类型、权限和文件存在
→ 绑定ResourceHandle/确定路径
→ 记录content hash与来源
```

### 4.2 两条入口共用Binder

- 工具调用：在进入工具代码前绑定，工具不再各自记得调用 `resolve_image`；
- 脚本交付：source检查后绑定生成bundled文本，再进行契约/类型检查。

稳定诊断：`RESOURCE_TOKEN_UNRESOLVED`、`RESOURCE_NOT_FOUND`、`RESOURCE_VERSION_EXPIRED`、
`RESOURCE_TYPE_MISMATCH`。禁止把token解析失败降级为脚本生成。

### 4.3 2026-10-01资源执行闭环修订：宽进严出，不要求LLM守资源契约

真实主轮 `mt_20261001T103541Z_FDB516` 已证明：查询器能够找到正确素材，Binder也能报告绑定成功，
但旧工具仍可能收到 `[[asset_id]]` 字符串并只返回裸 `TOOL_FAILURE`。因此“资源可查询”和“工具真正
拿到可执行资源”是两件事。此前4.1要求LLM稳定提交结构化 `ResourceRef`，仍把系统正确性建立在
模型守约之上；本节覆盖该要求的模型侧部分。

资源事实源不重建，继续使用现有 `resources` 表、`resource.search/get`、`ResourceBinder`、版本、
content hash、type、role、allowed uses、scope和permission。新增的只是唯一执行边界：

```text
LLM给出的资源表达（不可信）
→ ResourceResolver规范化
→ ResourceBinder按当前事实校验
→ Runtime内部BoundResource/ResourceHandle（可信）
→ ToolAdapter转换为真实路径、图像或对象
→ 工具只消费转换后的参数
```

模型可以提交资源id、结构化引用、历史 `[[asset_id]]` 或本地提供的虚拟路径；Runtime负责兼容解析。
模型表达不是执行凭据，工具不得直接消费它。工具代码不得再自行猜文件名、解析token、回退焦点素材，
也不得自行查询第二份资源表。

### 4.4 虚拟路径是模型界面，不是第二套资源索引

为了让模型继续使用其熟悉的普通Python和路径写法，本地向模型渲染稳定虚拟路径，例如：

```python
scene_path = r"__resources__\asset_8e39a67d\结界1.png"
template_path = r"__resources__\asset_225333e3\勋章.png"
```

不使用伪造的真实盘符绝对路径。虚拟路径只携带可定位的资源id和可读名称；版本、hash、权限、真实路径
以及句柄有效期仍由Runtime从权威资源索引补齐。它不是新的数据库、不是新的长期事实，也不要求模型
理解句柄结构。

两种使用场景必须分开：

- 即时工具调用：ToolAdapter在执行前把虚拟路径或其他兼容表达解析成内部handle，再生成真实实参；
- 脚本源码：材料化阶段识别虚拟路径或旧 `[[asset_id]]` token，生成bundled文本；保存的source仍保留稳定引用。

资源替换不得用无边界的全文字符串替换。即时调用在参数schema边界处理，脚本在AST/材料化边界处理，
不得修改注释、用户文案、日志文本或与资源名偶然相同的普通字符串。

### 4.5 Handle只属于Runtime

`ResourceHandle`至少绑定：resource id、version、content hash、type、role、allowed use、参数名、工具名、
环境revision和作用域。LLM不填写也不持久复用handle；旧主轮、旧版本或环境变化后的handle自动失效。
执行器只接受handle派生出的可信实参。若适配器仍准备把 `[[...]]`、`resource://...` 或
`__resources__\...` 原样传给旧工具，必须在工具启动前以 `RESOURCE_ARGUMENT_NOT_MATERIALIZED` 拒绝。

### 4.6 参数语义约束

资源参数不能只声明“这是image”。每个参数至少声明：基础类型、资源角色、允许用途和业务语义。
例如 `slot_template` 必须明确是“空槽参考模板”，`template_path` 是“待寻找目标模板”，
`target_path` 是“被搜索场景”。勋章模板即使类型同为image，也不能作为空槽模板通过本地校验。

模型负责选择算法；资源语义是否满足工具前置条件由Runtime确定。用途不匹配必须在工具启动前返回
`RESOURCE_USE_MISMATCH`，不得把它转化成用户需求，也不得要求用户选择内部算法。

### 4.7 查询不是取证尝试

资源查询、能力查询、契约查询、缓存命中、资源绑定、参数规范化和参数修复均不消耗视觉/CV取证次数。
重复查询通过以下指纹去重，而不是靠共用节点次数限制：

```text
query_name + normalized_arguments + relevant_resource_revision
```

空结果同样缓存到相关revision变化为止。只有工具已经越过绑定与参数校验、真正启动算法或模型调用时，
才增加 `evidence_attempts`；只有产出有效结构化证据时才记 `evidence_progress`。Runtime/网络/适配器故障、
参数错误以及 `tool_started=false` 的失败均不扣取证次数。

### 4.8 分阶段工具失败协议

禁止工具边界只返回裸 `TOOL_FAILURE`。Binder、参数材料化、工具执行和结果校验分别捕获异常，统一输出：

```json
{
  "status": "failed",
  "code": "RESOURCE_ARGUMENT_NOT_MATERIALIZED",
  "stage": "argument_materialization",
  "tool_started": false,
  "retryable": true,
  "failed_parameter": "template_path",
  "message": "资源已绑定，但工具参数仍是资源占位表达",
  "suggested_action": "使用绑定后的真实路径重新调用"
}
```

稳定分类至少包含 `RESOURCE_BINDING_FAILED`、`RESOURCE_ARGUMENT_NOT_MATERIALIZED`、
`ARGUMENT_CONTRACT_MISMATCH`、`TOOL_EXECUTION_FAILED`、`TOOL_RESULT_INVALID`。完整异常和traceback写入本地
运行日志，给模型的载荷只包含安全且足够恢复的字段。图根据 `stage/tool_started/retryable` 走确定性恢复边，
而不是让模型继续搜索能力猜原因。

## 5. 第二层B：视觉证据调度——按证据缺口调用，不按主轮调用

### 5.1 决策

允许逻辑模型主动使用自身视觉或辅助视觉，但禁止实现成“每个主轮开始都自动识图”。视觉调用的
触发条件是：

```text
当前任务的下一步依赖图片事实
且
Runtime没有覆盖当前资源版本与问题类型的有效视觉证据
```

主轮空白表示LLM不自动继承旧上下文，不表示Runtime删除已经验证的视觉事实。后续主轮先查询
本地证据；缓存命中时不重新发送图片。

### 5.2 视觉与本地CV的职责

- 视觉模型：理解语义目标、布局、区域关系、用户指代和模板是否合理；
- 本地CV：多目标模板匹配、重复检测、精确计数、坐标和overlay；
- 逻辑模型：结合用户目标选择能力、判断证据充分性并组织回复；
- Runtime：决定是否缺视觉证据、控制预算、保存证据和失效。

用户已经明确提供底图、模板并要求模板匹配时，优先调用本地多目标匹配；它能可靠返回命中与
overlay时可以不调用收费视觉。只有需要理解布局、选择搜索区域、判断模板质量、解释冲突或本地
结果异常时，才增加视觉调用。

### 5.3 调度顺序

```text
查询本地视觉/CV证据
→ 若已有兼容证据，直接复用
→ 若精确本地算法足以回答，先调用本地CV
→ 若仍缺语义理解，主逻辑模型支持视觉则优先自己看
→ 主模型视觉不可用、失败或结构化结果不合格时，调用辅助识图模型
→ 本地CV与视觉结果冲突时，允许一次针对性裁剪复核
→ 仍无法消除冲突则报告证据不足，不继续自动烧token
```

主视觉与辅助视觉指向同一后端时不得重复调用。

### 5.4 必须取得视觉证据的情况

以下情况在提交结论或图片相关logic draft前必须有适用证据：

- 用户问图片中的对象、数量、位置、颜色、布局或区别；
- 用户说“这张图、这里、截图里、素材区里的图片”；
- 需要根据新素材设计脚本的视觉流程；
- 模型准备陈述图片事实、坐标、区域关系或模板含义；
- 本地算法结果异常、冲突或无法解释；
- 新裁剪/标注需要验证内容是否正确。

没有证据时返回 `VISUAL_EVIDENCE_REQUIRED`，阻止图片事实结论和依赖图片内容的logic draft，
但不强迫所有任务调用视觉。

以下情况默认不调用：普通聊天、纯文本日志、Python语法修复、与图片内容无关的控制流修改、已有
兼容证据、或本地CV已经提供足够证据。

### 5.5 证据缓存

视觉证据结构化保存于Runtime，而不是写入LLM长期上下文：

```json
{
  "evidence_id": "ev_...",
  "resource_id": "asset_...",
  "resource_version": 1,
  "content_hash": "sha256:...",
  "mark_revision": 2,
  "question_kind": "object_count_and_layout",
  "target": "勋章",
  "region": "full_image",
  "model_id": "...",
  "model_version": "...",
  "observations": [],
  "groundings": [],
  "confidence": 0.94,
  "status": "verified",
  "created_at": "..."
}
```

缓存键不能使用完整自然语言问题，避免“有多少勋章/数一下勋章/每张卡几个”重复调用。使用：

```text
content_hash
+ mark_revision
+ question_kind
+ normalized target
+ region
+ vision protocol/model revision
```

新问题可由旧证据完全回答时直接复用；旧证据只含总数而新问题要求逐卡数量时，执行增量识别而
不是无条件重看整图。

#### 权威存储与migration决策

跨主轮视觉证据写入现有V3 `evidence`表，不建立第二张视觉专用事实表。实际发布使用migration 8
（migration 7已用于Phase A主轮定向字段）；后续一律按数据库下一个连续可用编号分配。该迁移为通用
证据增加可空的 `fingerprint` 字段并建立 `(kind, fingerprint, status)` 索引；`subject_id/version`
继续绑定资源身份，完整视觉字段继续放在 `payload_json`。fingerprint由本节缓存键规范确定性计算。

这样既能按fingerprint高效查询，又不把 `content_hash/question_kind/region/model`复制成多组数据库
事实。payload必须包含构成fingerprint的规范化字段，读取时重新计算并核对，防止索引键与载荷漂移。

现有 `session.artifacts["vision_cache"]`保留为主轮内L1缓存，但不得成为独立事实源：

```text
L1命中 → 校验其evidence_id仍指向有效L2证据
L1未命中 → 查询SQLite evidence（L2）
新视觉结果 → 先写L2，再把evidence_id和紧凑结果放入L1
失效/冲突 → 更新L2状态，同时清除对应L1项
```

应用重启后L1丢失无影响，仍可从L2恢复；V2旧会话cache只能作为迁移输入，不能与L2并行产生两份
权威结论。

### 5.6 失效条件

以下变化使相关证据失效或需要重新核验：

- 图片content hash变化；
- 标记层/选区发生与问题相关的变化；
- 用户改问不兼容的问题类型或目标；
- 证据状态被标记为invalid/conflicted；
- 视觉协议或坐标换算规则发生不兼容升级；
- 证据只覆盖裁剪区域，但结论需要整图。

主轮变化本身不使证据失效。

### 5.7 默认视觉预算

- 每个主轮针对新证据缺口默认允许1次新视觉调用；
- 首次结构化失败、低置信度或本地CV冲突时允许1次针对性复核；
- 自动视觉总计默认最多2次；
- 缓存查询不计预算；
- 用户明确“重新识图/再看一眼”可授权额外1次；
- 第三次自动调用前必须停止并报告现有证据与冲突，不能无限重试。

每次调用记录触发原因、资源、区域、是否缓存、模型、输入/输出token、是否产生有效证据。没有
产生有效证据的调用不能被当作完成进度。

### 5.8 结构化输出

视觉模型默认接收针对性问题，不做无目标长篇描述。输出至少包含：

- `image_size`；
- `observations`；
- `candidates/groundings`及bbox、confidence；
- 与问题类型对应的count/layout/verified字段；
- `uncertainties`和建议的验证方法。

视觉模型的数量不自动成为最终事实。需要精确重复计数时优先由本地CV复核；视觉数量与本地匹配
冲突时保留双方证据并进入一次针对性复核。

### 5.9 与task type的结合

- `asset_analysis`：取得分析证据后直接回答，不创建脚本；
- `script_design`：logic draft依赖图片内容时，先取得视觉/CV证据；
- `script_generation`：复用已确认设计所引用的证据，不因进入新副轮重复看图；
- `script_modification`：只在素材、视觉目标或相关布局改变时重新识图；
- `trial_diagnosis`：优先读取试运行关键帧和本地聚合证据，必要时只查看失败区域。

当前已有“共同视口不自动发送、模型主动调用视觉工具”的省token机制应保留为普通默认；本节新增
的是“图片事实不可无证据”的本地调度与硬闸，而不是恢复每轮自动传图。

### 5.10 闸门顺序与错误优先级

同一动作同时存在多个缺口时按以下顺序裁决，避免V2/V3给出不同错误：

```text
1. 任务意图/动作是否合法
2. 引用资源是否有效并已绑定
3. 即将陈述或依赖的图片事实是否有适用证据
4. 能力/API/权限是否有效
5. 返回语义、数据流和完成证据
```

因此：

- 分析请求试图建立脚本时先报 `SCRIPT_INTENT_NOT_EXPLICIT`；
- 资源token无效时报资源错误；
- `asset_analysis`可以执行用于取得证据的视觉/CV `act`，不会被视觉证据闸挡住；
- 只有在提交图片事实claim、依赖图片内容的logic draft或finish时，缺证据才报
  `VISUAL_EVIDENCE_REQUIRED`；
- 已有证据但API调用不合法时继续走contract错误，不能用视觉证据掩盖代码问题。

### 5.11 V2/V3共用实现

从现有 `collaborator/llm_turn.py` 中抽出单一 `VisualEvidenceBroker`服务，负责：

- 生成fingerprint；
- 查询L1/L2；
- 判断兼容与失效；
- 扣减视觉预算；
- 主模型视觉→辅助视觉回退；
- 写evidence与遥测；
- 返回统一结构化结果。

现有 `_describe_frame_with_fallback/_inspect_asset_with_fallback`保留为薄适配器或迁移到Broker内部；
V2工具桥和V3 capability adapter只能调用同一个Broker，不得分别维护缓存键、预算或失效规则。

## 6. 第三层：调用归属与契约

### 6.1 四个调用命名空间

AST先解析接收者来源，再选择校验器：

| 命名空间 | 例子 | 事实来源 |
| --- | --- | --- |
| Runtime API | `browser.match_image()` | API Contract Registry + ticket |
| Python/依赖库 | `raw.append()`、`np.mean()` | Python/Dependency Policy + stub |
| 当前源码 | `_iou()`、`choose_best()` | 本次AST符号表 |
| 无法归属 | 动态反射、未知实例 | 明确不确定诊断 |

只有接收者能追溯到Runtime注入对象、别名或登记服务时，才要求contract ticket。普通list/str/dict和
第三方库方法绝不能因为Registry没有同名方法而成为 `not_approved`。

### 6.2 新库接入

新依赖通过 `DependencyPolicy`登记模块、版本、允许import、允许调用、副作用等级和类型stub。未知依赖
报告 `DEPENDENCY_UNVERIFIED`，不冒充Runtime API错误。禁止为每个第三方调用签Runtime ticket。

### 6.3 查询契约的闭环

主轮先获得紧凑能力索引；详细契约必须通过 `api.get/get_many`取得并由本地签ticket。若交付时AST
发现确定的Runtime调用缺ticket：

1. 影子/修复模式返回精确缺失方法列表；
2. Runtime把相应完整契约发送给模型并记录 `CONTRACT_VIEWED`；
3. 只允许一次契约修复副轮；
4. 模型必须重新提交代码；
5. 仍缺票才成为拒绝候选。

不能自动补签后直接沿用旧代码，因为模型尚未依据契约重新确认写法。

### 6.4 系统脚手架归本地

`enable_pseudo_record/finish_pseudo_record`等纯运行包装尽量由可信materializer注入，不要求模型重复书写
和查询。业务API仍由模型选择并取得ticket。系统注入调用带明确来源区间，不计入LLM幻觉。

### 6.5 工具契约不是任务前提：防止能力文档锚定

2026-10-01真实主轮 `mt_20261001T042948Z_A9ACC5` 暴露了一类独立问题：用户要求设计
“识别每张对手卡及其勋章数”的脚本，模型读取 `cap_v2_count_medals` 后，看到该工具的
`slot_template`（空格子模板）是必填参数，便主动让用户选择“用空格子反推”并索要空格子模板。
它并不是从图片、用户原话或历史事实中得知空格子，而是被单个工具的实现契约锚定。

必须区分三类事实：

| 事实 | 含义 | 能否升级成用户需求 |
| --- | --- | --- |
| 任务事实 | 用户要识别卡片并统计勋章 | 是，来自用户原话 |
| 能力事实 | `count_medals` 需要 `slot_template` | 否，只是选择该工具的调用前提 |
| 资源事实 | 当前工作区是否真的存在空格子模板 | 否，必须由Binder/资源查询证明 |

因此能力的required参数只表示“若选择调用这个能力，就必须满足什么”，不能推导为“完成任务必须
让用户提供什么”。模型在某条能力路线缺参数时，固定执行：

1. 查当前资源库与已绑定资源；
2. 判断能否从现有素材低风险派生（裁剪、已有grounding、当前视口选区）；
3. 比较其他已登记能力以及本地CV组合能否完成同一目标；
4. 必要时调用视觉取证理解布局、区域与模板适用性；
5. 全部失败后，才可用 `RESOURCE_REQUIRED` 请求用户补资源。

`REQUIREMENT_AMBIGUOUS` 不得用于让用户选择阈值、模板策略、CV算法或内部工具。专业实现路线属于
Agent职责；只有不同路线会显著改变用户可见目标、成本、架构或产生不可逆后果时，才构成需求歧义。

视觉闸返回 `VISUAL_EVIDENCE_REQUIRED` 时，下一步必须取得证据（本地CV或视觉Broker）；继续查看
更多能力契约不等于取得视觉证据。能力搜索结果应携带本地语义标签：`tool_precondition`、
`derivable_resource`、`known_alternatives`，并在模型工作区明确提示“required参数不是任务需求”。

2026-10-01修订：删除或退役 `count_medals` 这类把勋章、45个槽位、42个空格、固定expected count等
历史任务知识写入通用调度面的旧技能/能力说明。若底层函数因兼容性暂时保留，必须标记为专用、默认不可
推荐，且只有工作区已经存在语义为empty-slot-reference的资源时才可选择。旧技能不得继续向模型暴露
历史数量、坐标、阈值或把 `slot_template` 暗示为通用必需资源。

替代技能使用通用“视觉模板计数”格式：scene + target template → 模板质量检查 → 多目标匹配 →
结构化结果 → 必要时阈值调整 → overlay复核。示例使用虚拟路径；示例数值必须带
`example_only=true/not_runtime_fact=true/not_parameter_default=true`，不得参与自动填参。

通用工具/技能描述至少包含：用途与不适用条件；每个参数的类型、是否必填、默认值、业务语义、单位、
范围、资源type/role/allowed use、互斥与联动关系；目标输出、输出格式/schema、item type、是否可迭代/
可计数/可下标、空结果语义、坐标系、排序和去重规则；副作用、成本等级；稳定失败码、失败阶段、
`tool_started`、是否可重试和推荐恢复动作。多目标匹配必须明确返回多个独立MatchResult，而不是单个
MatchResult的内部字段；`expected_count`仅能作为已有证据支持的校验条件，不能从历史案例或槽位总数猜出。

## 7. 第四层：语义类型与轻量数据流

函数签名之外，返回契约必须包含：

```json
{
  "type": "MatchResult",
  "semantic_role": "single_match",
  "iterable": true,
  "iteration_semantics": "tuple_fields",
  "iteration_item_types": ["int|None", "int|None", "float"],
  "sized": false,
  "collection": false,
  "multi_result": false
}
```

多目标能力则显式声明 `semantic_role=match_collection`、`collection=true`、`sized=true`及元素类型。

第一版数据流只做高价值传播：

- API返回值到变量；
- 简单赋值和重新赋值；
- if分支合流；
- `for`迭代元素类型；
- `len`、下标、属性访问、空集合首项；
- 参数传入有注解的本地helper。

必须区分“语言层可迭代”和“业务层是多目标集合”。允许 `x,y,score=result`，但禁止把
`MatchResult`迭代项当作多个匹配对象、对其计数或交给要求Match对象的函数。

当前真实API保持事实：`match_image_multi`在browser侧返回 `list[dict]`，不是
`list[MatchResult]`；离线分析能力可另行提供统一结构，但校验器不能假装现有API已经统一。

## 8. 第五层：证据、恢复与完成

### 8.1 固定失败恢复策略

```text
识别错误类别
→ 在当前task type内修复一次
→ 必要时换等价能力
→ 仍失败则报告证据不足和已完成成果
```

禁止用“继续”作为系统内部重试机制；禁止工具失败后改变交付物类型。

### 8.2 三层结论

用户界面和finish协议分别报告：

1. 架构真实性：ticket对账；
2. 代码可运行性：语法、参数、类型、smoke/Fake Runtime；
3. 需求符合性：分析证据或真机试运行。

不得用一个“静态校验通过”遮蔽其他未通过项。例如：

```text
API调用对账：未通过（match_image未取得契约）
基础语法：通过
返回语义：未通过（单结果被当作多目标集合）
业务结果：未验证
```

### 8.3 分析任务直接完成

`asset_analysis`只有在取得结构化工具结果和evidence ID后才能陈述数量。完成后直接回答，不创建脚本，
不要求逻辑确认，不显示“去试运行”。无法得到证据时明确说未知，不制造标准答案。

## 9. 一条标准主轮如何运行

### 9.1 一次图片分析

```text
用户：“回答图里有多少勋章”
→ orient(asset_analysis，引用用户消息)
→ discover_context
→ bind_resources
→ assess_evidence
   ├─ 本地证据足够：answer
   └─ 缺证据：acquire_evidence（本地CV优先，视觉Broker按需）
                → assess_evidence
→ answer（引用matches/count/overlay/evidence）
→ completed
```

### 9.2 明确制作脚本

```text
用户：“写一个以后自动统计勋章的脚本”
→ orient(script_design，脚本意图证据有效)
→ discover_context → bind_resources → assess_evidence
→ 必要时acquire_evidence
→ design_logic
→ confirm_logic（用户确认具体draft hash）
→ generate_script
→ validate_script
   ├─ 机械错误：repair_script → validate_script
   ├─ 实质逻辑改变：design_logic → 重新确认
   └─ 通过：deliver
→ 明确分层报告并等待真机试运行
→ 试运行后evaluate_trial；只有验收证据满足才completed
```

## 10. 实施顺序（固定，不再边讨论边扩建）

### Phase A：任务定向硬闸

- 增加task type、用户证据和动作矩阵；
- 自由循环与主轮边界要求orientation；图接管后的内部动作继承本地已确认Intent，不重复要求LLM填表；
- 修复“继续/重来/协议错误”继承；
- 用2026-09-29勋章主轮建立失败测试。

### Phase B：统一ResourceBinder

- 资源参数schema；
- 工具调用前绑定；
- 脚本bundled复用同一Binder；
- 删除工具自行猜token路径的要求。

### Phase B2：视觉证据调度

- `VisualEvidenceBroker`：证据查询、兼容性、失效和预算；
- 本地CV优先、主模型视觉优先、副模型回退；
- `VISUAL_EVIDENCE_REQUIRED`与图片事实/logic draft闸门；
- 跨主轮证据缓存和调用token遥测；
- 不改变普通无图任务的视觉调用行为。

### Phase B3：LangGraph控制层（先影子，后接管）

- 新增图状态、节点schema、条件边、节点预算和progress fingerprint；
- 复用现有SQLite事实源，LangGraph checkpoint只保存游标与id/revision引用；
- 先让影子图消费真实领域事件，比较“当前自由循环实际动作”与“图允许动作”，不改变交付；
- 首先接管 `asset_analysis`，验证资源→证据→回答的短链；
- 再接管 `script_design → confirm_logic → script_generation → validation`；
- 最后移除生产路径中让模型任意选择全局action的自由循环；
- 禁止在本阶段重写Binder、Broker、Registry、ticket、receipt和试运行存储。

### Phase B4：资源执行闭环与通用技能收口

本阶段不新建资源索引，不重写查询器，也不扩大LangGraph节点集。目标是把已经存在的资源事实真正送到
工具执行边界，并让失败可诊断、可恢复。实施顺序固定如下：

1. 为现有资源查询结果增加统一虚拟路径渲染，兼容id、结构化引用和旧token输入；
2. 扩展现有 `BoundResource` 或新增仅限Runtime内部的短生命周期handle，不建立第二份持久事实；
3. 在唯一ToolAdapter中完成 `模型表达 → Binder → handle → 真实实参`，旧工具不得看到未材料化引用；
4. 给工具执行增加绑定、材料化、执行、结果校验四阶段异常分类和 `tool_started`；
5. 给资源参数补充type/role/allowed use/业务语义，先覆盖多目标匹配、单目标匹配、裁剪和标注；
6. 删除/退役勋章、45槽、42空格等领域专用旧技能，建立通用视觉模板计数技能；
7. 将图中的查询计数、基础设施失败与真实取证次数分离，并让生产图和影子图共用同一归约函数；
8. 用 `mt_20261001T103541Z_FDB516` 建端到端失败测试后再改实现。

禁止事项：不得让LLM填写真实路径、version、hash、permission或完整handle；不得为虚拟路径建立第二张
资源表；不得用全局字符串替换实现材料化；不得在工具内部保留文件名猜测和焦点素材回退；不得把
查询次数直接等同于取证次数。

### Phase C：调用命名空间修正

- Runtime receiver provenance；
- Python/Dependency Policy；
- 当前源码符号表；
- 修复append/sort/format误报和误报统计。

### Phase D：契约修复闭环

- 缺ticket返回精确api.get_many清单；
- 一次修复副轮和重新提交；
- 系统脚手架逐步迁入materializer；
- 保持影子模式。

### Phase E：语义类型与数据流

- semantic_role/iteration_semantics/element type；
- 单结果、多结果和集合操作规则；
- `MatchResult`真实反例测试。

### Phase F：证据和状态展示

- finish三层结论；
- asset_analysis直接答复；
- 工具恢复与停止策略；
- 真实主轮复测。

Phase B3接管后，后续C—F作为图节点内部的能力逐步增强。完成A—F（含B2/B3）并积累足够真实样本后，
才讨论分级开启生产硬闸。任何阶段不得顺手进入完整Writer DSL。

## 11. 必须跨层成立的验收场景

1. “回答图片里有多少”直接分析并返回证据，不生成脚本；
2. 分析工具token绑定失败时在原任务内修复，不转脚本；
3. “继续/重来”不改变task type；
4. 明确“写脚本”才进入script设计；
5. LLM未查业务API契约时被要求一次性补查并重交；
6. `list.append/str.format/np.mean`不要求Runtime ticket；
7. 未批准的 `browser.unknown()`仍被识别；
8. 新第三方库未登记时报依赖问题而非Runtime API问题；
9. `MatchResult`允许解包但不能当匹配集合计数；
10. browser `match_image_multi`按真实 `list[dict]`检查；
11. 系统注入脚手架不被算作模型幻觉；
12. 任何数量/坐标结论必须有evidence；
13. 已知ticket或语义失败时不能只显示“静态校验通过”；
14. 协议错误修复不会恢复错误任务计划；
15. 同一套规则同时覆盖V2/V3，不能各维护一份事实源。
16. 新主轮引用同一图片和兼容问题时复用视觉证据，不产生新视觉调用；
17. 图片hash或相关标记变化后旧视觉证据失效；
18. 本地多目标匹配已经可靠回答时不强制调用VL；
19. 图片相关logic draft在没有视觉/CV证据时被 `VISUAL_EVIDENCE_REQUIRED`阻止；
20. 主模型视觉成功时不再调用副模型，同后端也不重复请求；
21. 首次视觉失败只允许一次针对性复核，不能无限消耗；
22. 视觉模型给出的数量与本地CV冲突时不静默选一个；
23. 普通文本主轮不会因为存在共同视口而自动识图。
24. 某能力要求空格子模板时，模型不会把该参数自动提升成用户需求；
25. 单条能力缺资源时先搜索、派生并比较替代能力，全部失败后才请求用户；
26. `REQUIREMENT_AMBIGUOUS`不能用来让用户选择内部CV算法或工具实现；
27. `VISUAL_EVIDENCE_REQUIRED`之后继续查工具文档不能被算作已经取得视觉证据。
28. `acquire_evidence`节点不能输出request_interaction、logic draft或无关契约查询；
29. `script_design`不能由模型自报跳到`script_generation`，只有确认的draft hash能触发转换；
30. 同一`INTERACTION_NOT_JUSTIFIED`或`VISUAL_EVIDENCE_REQUIRED`不会在自由循环里出现十余次；
31. 同一节点两次无新增资源、证据、草稿或脚本revision后走确定性恢复或停止；
32. 草稿确认记录的hash/revision不得为空，生成脚本必须引用同一确认；
33. 视觉证据表明素材是整屏列表时，logic draft不能把它当单卡模板而仍获确认；
34. `trial.finished`但没有验收证据时进入diagnosis，不得标记业务完成；
35. 应用重启后从现有SQLite投影重建graph state，不依赖LangGraph中的第二份业务事实；
36. 影子图和生产图对相同状态给出相同下一节点，差异必须有结构化记录。
37. 查询器找到scene/template后，ToolAdapter收到的是Binder生成的可信资源，旧工具只看到真实路径或对象；
38. 即时工具参数中的旧 `[[asset_id]]` 被兼容规范化或在启动前明确拒绝，不能绑定成功后裸失败；
39. 虚拟路径、结构化ResourceRef和资源id解析到同一资源id/version/hash，不产生第二份资源事实；
40. 把目标模板传给要求empty-slot-reference的参数时，本地在工具启动前以用途不匹配拒绝；
41. 工具异常至少报告stage、tool_started、稳定诊断码、失败参数、retryable和建议动作；
42. resource/api/capability查询与缓存命中不增加evidence attempts，同revision重复空查询直接复用；
43. 资源绑定失败、参数错误、适配器异常和工具未启动均不扣取证次数；
44. 删除领域旧技能后，能力推荐和默认参数中不再出现45槽、42空格或历史expected count；
45. 通用模板计数链能以scene + target template返回结构化多目标结果、数量和overlay证据；
46. 零证据不能进入answer；同一状态下生产图与影子图使用同一归约逻辑并得到相同下一节点；
47. 本次真实失败主轮可回放为：查询资源→绑定→材料化→工具启动→有效证据→直接回答，且不建脚本Task。
48. `asset_analysis`首次定向和取证完成后，模型的`finish`漏掉orientation仍可继承本地主轮Intent并提交；
49. 用户补充新目标、请求跨任务族或执行脚本塑形动作时，不得用继承规则绕过重新定向；
50. `propose_claim`与`finish`对同一份本地CV证据使用同一兼容性判据，不出现“图认为足够、完成闸认为不足”；
51. `answer`节点连续两次提交禁用动作后确定性收口，且`required_next_action`必须是真实协议动作而非节点名；
52. 外层模型响应保持JSON，`final_response.text`按气泡Markdown渲染；二维数据输出合法Markdown表格；
53. `capability.search`只返回摘要、`capability.get`返回完整schema，提示词和Runtime不得各说一套；
54. 模型未填写`ev_*`时，Runtime只可绑定本轮已有且语义兼容的真实证据，不得创造或猜测证据；
55. 每个新增协议字段都必须有“模型遗漏该字段”的验收用例，并明确本地继承、一次修复或停止三者之一。

## 12. 完成定义

- 五层管线均在生产入口接线；
- LangGraph已接管主轮阶段转换，自由action循环不再拥有最终路由权；
- LangGraph checkpoint不含资源、证据、权限、ticket和任务正文的第二份权威副本；
- 2026-09-29真实反例的失败测试全部转绿；
- 不新增API、资源或类型的重复事实源；
- 模型侧资源表达不再被当作可信执行输入，所有工具资源参数均经同一Binder与ToolAdapter；
- 工具调用链中不存在只返回裸 `TOOL_FAILURE` 的生产分支；
- 查询、缓存、绑定、真实取证和有效证据五类计数可分别查询；
- 领域专用旧技能不再参与通用能力推荐，技能示例不能成为运行时事实或默认参数；
- 影子统计不再把普通对象方法计为Runtime拒绝候选；
- 至少20个真实新主轮、50个本地API调用点，确定性拒绝候选人工误报为0；
- 分析任务和脚本任务各完成一次真实GUI全链路；
- 真实GUI验证同一视觉证据可跨主轮查询复用，且缓存命中视觉token为0；
- 视觉调用遥测能说明触发原因、缓存、token和证据产出；
- 应用重启后主轮、ticket和资源失效语义仍正确；
- 最终报告明确哪些结论来自测试、哪些经过真机验证；
- 真实复现证明同类interaction/视觉闸错误不会重复空转，节点预算与停止原因可查询；
- 硬闸仍由人工明确开启，readiness不得自动改配置。

## 13. 2026-10-03实施备案：回答链收口与`asset_analysis`控制权交接

### 13.1 真实问题与证据

本次由两个真实主轮推动：

- `mt_20261002T145944Z_A2F300`：模板匹配已经成功并产生证据，生产图进入`answer`；模型仍重复
  `act`数十次，同时视觉完成闸与图对本地CV证据给出相反判断，最终预算耗尽。
- `mt_20261003T052947Z_D19849`：模型正确执行`capability.search → capability.get →
  match_template_multi`并得到23个结构化命中；进入`answer`后，`finish`仅因漏填orientation被Phase A
  拒绝，随后模型再次查询并被图以`GRAPH_NODE_NO_PROGRESS`收口，已有正确结果未能交付。

两轮共同证明：问题不是单个提示词写得不够重，而是本地已经掌握阶段、证据和任务类型后，仍要求模型
重复抄写这些事实；一旦漏填，旧闸门会否决新控制层已经允许的动作。

### 13.2 已实施改动

1. **证据判定统一**：`propose_claim`与`finish`共用“能力输出语义 × question_kind”判定。本地CV已
   支撑并绑定的主张，不再在finish阶段被误判为必须追加VL证据。
2. **answer节点空转收口**：执行前拒绝也进入节点拒绝周期；同节点连续两次重复禁用动作即
   `GRAPH_NODE_NO_PROGRESS`。提示中的`required_next_action`改为`propose_claim`或`finish`等真实协议动作，
   不再返回不可提交的节点名`answer`。
3. **提示词分层**：明确外层是单个JSON对象，`final_response.text`是气泡Markdown源文；合法表格必须包含
   `| --- |`分隔行。同步消除`capability.search`“既返回摘要又返回完整schema”、资源id“既是标识又被当
   图片事实证据”等冲突。
4. **控制权交接**：`asset_analysis`首次动作仍经Phase A确认；生产图接管后，普通图内动作从
   `main_turns.intent_json`继承task type。模型填写的orientation只作一致性诊断，不能驱动图跳转；漏填
   orientation不再阻断已经合法的finish。
5. **边界仍保持硬闸**：尚未接管的脚本流程、用户补充导致任务族变化、跨任务族请求和脚本塑形动作仍须
   显式orientation，不能借“继承”扩权或改变任务。

### 13.3 验证与当前边界

- 新增真实回归：本地CV证据完成链、answer重复非法动作、Markdown/JSON分层、search/get契约一致性、
  图内finish漏orientation仍提交、非图脚本流程仍要求orientation。
- 2026-10-03完整协作回归：`1228 passed, 1 skipped`；唯一warning为既有
  `QuickScriptOverlay.py`无效转义，与本次无关。
- 当前只完成`asset_analysis`控制权交接；`script_design → confirm_logic → script_generation →
  validation`尚未由生产图完整接管，在接管前继续保留原orientation硬闸。

## 14. 2026-10-03协议减负：最小意图输入，Runtime自动补全

### 14.1 目标与边界

模型不应反复抄写 Runtime 已经知道的事实。新协议把字段分为两类：

- **模型语义字段**：只有模型才知道的业务意图，例如动作类型、查询名和参数、工具业务参数、
  脚本正文、要问用户的问题、最终 Markdown 正文。
- **Runtime 机械字段**：本地能唯一推导的字段，例如 `protocol_version`、`action_id`、
  `payload` 外壳、当前 capability version、任务/视口 revision、资源版本与 hash、证据引用、
  permission grant、idempotency key、已验证脚本与完成证明。

一条硬规则：**能从当前节点、工作区投影、Registry、Binder、凭证或最新查询结果唯一推导的字段，
不得再设为 LLM 必填项。** Runtime 不确定时不得猜测，而是保留为语义缺口或转入明确的查询/交互节点。

### 14.2 模型可用的最小写法

保留旧的完整 `ActionEnvelope` 以兼容旧会话与测试，同时允许模型输出扁平最小动作：

```json
{"type":"query","name":"resource.search","arguments":{"type":"image"}}
{"type":"act","capability_id":"cap_v2_match_template_multi","arguments":{"asset_id":"asset_x"}}
{"type":"validate_script","script":"...","script_name":"demo.py"}
{"type":"finish","text":"已找到 **3** 个目标。"}
```

适配器先将已知的扁平字段收进 `payload`，再由 Runtime 生成 `action_id`、补 `protocol_version=1`、
把省略的 capability version 解析为当前已发布版本。`finish.text` 只是 `final_response.text`
的简写；它不能绕过任务完成闸、证据闸或脚本校验闸。

### 14.3 仍然必须由模型表达的内容

Runtime 不代填以下内容：

1. `type`：在当前节点的允许集里选哪个语义动作；
2. query 的 `name/arguments`，act 的 `capability_id/arguments`；
3. 需要用户决定时的 `question/options/reason_code`；
4. 脚本正文与用户可见的最终 Markdown 正文；
5. 新主轮或跨任务族边界的 `orientation.task_type/intent_quote`。

`orientation` 内的 `requested_changes/must_preserve/known_facts/unknowns/can_act_now/
next_action_reason` 不再作为通用必填表格；它们仅作兼容诊断字段。需要存储的事实必须来自
实际 query/tool/evidence 或用户原话，不得因模型在 orientation 中自报而升级为事实。

### 14.4 归一化顺序与安全性

1. 先解析外层 JSON，拒绝未知顶层字段与思维链；
2. 按 `type` 将已登记的扁平业务字段归一化进 `payload`；
3. 模型同时给了 `payload.x` 与顶层 `x` 且两者不同时，拒绝为协议冲突，不静默覆盖；
4. 生成本地 `action_id`，再进入现有定向闸、图节点闸、Binder、权限、证据和完成闸；
5. Runtime 自动补全不等于放宽校验：缺业务语义仍结构化拒绝，已有事实冲突仍以本地事实为准。

### 14.5 验收

- 最小 query/act/validate_script/finish 可归一化为旧 `ActionEnvelope`；
- 旧完整协议逐字段仍可用；
- 省略 capability version 时本地解析当前版本，且执行记录中落的是具体版本；
- 扁平字段和 `payload` 冲突必须拒绝；
- 自动生成字段不能绕过orientation边界、资源、权限、证据、脚本校验和完成闸；
- 每个可省略字段都有一条“模型真的漏填”测试，不只测理想完整协议。
- 后续修改一律按§1.3的“LLM健忘是常态”原则设计；不得以增加粗体、重复提示或要求模型再次抄写
  Runtime事实作为主要修复方案。

### 14.6 2026-10-03内部API实链验收：匹配后继续标图

最新两个实况主轮暴露：本地多目标匹配已返回可靠数量与坐标，但普通工具回执会冒充图片证据；
模型漏填 `subject_class`后真证据无法自动绑定；`answer`节点又禁止使用标图工具，导致系统
“匹配成功却回答未知”，也无法继续完成用户下一步的标注要求。

收口规则：

1. `GraphDriver.has_evidence()`只认结构化视觉观察或通过能力输出语义校验的本地CV量测；
   `list_session_assets`、裁剪回执、普通 `tool_measurement` 不得推进图片结论。
2. 匹配证据已有 `count + matches`时，模型漏填 `subject_class`，Runtime可确定性推导为
   `count`并绑定最近的兼容证据，不再要求模型抄 `ev_*`。
3. `answer`节点额外允许唯一的后处理动作 `cap_v2_mark_on_image`；成功后仍留在
   `answer`等待 `finish`，其他 `act` 仍被拒绝，避免重复匹配空转。
4. 模型只需表达“把匹配结果标出来”。Runtime从最近匹配证据的 `matches[].box/bbox`
   生成 `marks`、编号、目标底图和标题；模型不必再抄一遍坐标表。
5. `mark_on_image`是创建新标注图，不修改原图，因此权限语义从 `modify`更正为
   `create_reversible`；无Task的分析主轮可在当前Turn作用域内生成这类可撤销产物。

验收不以mock掉工具为完成：内部API测试实际生成底图与模板，通过
`ActionRunner → ResourceBinder → LegacyResolverExecutor → match_template_multi`得到3个真实命中，
随后在不提供 `marks`的情况下调用真实 `mark_on_image`，验证生成3个带编号的框、
产出PNG并将两次结果都记为本轮证据，最后才允许 `finish`。

### 14.7 省略型跨主轮继续：可重复操作配方

“换这张”、“结界2”、“再试一次”不要求LLM记住上一主轮的能力名和参数。Runtime从最近一条
可靠的 `match_template_multi` 证据动态投影 `repeatable_operation`，只包含具体能力版本、固定模板、
可替换的 `target_asset_id` 槽位、上一目标及结果摘要；不另建配方表，不复制图片内容。

对“语义正确但能力名编错”的视觉调用，`CapabilityHub`可从Registry按动作词和
`current_frame` 约束做唯一候选修复；仅当候选恰好一个时修复，不唯一则仍报
`CAPABILITY_NOT_FOUND`，禁止默认猜一个。回归用实况中出现的
`vision_describe / vision_broker_describe / image_analyze` 验证可确定性归一到已登记能力，
并保留“两个候选时必须拒绝”的反例。

### 14.8 2026-10-03副轮结构化留痕

实况出现“用户只要求执行一次，模型却连续调用同一匹配能力十余次”。旧 `subturns` 表只保存
动作类型、action id 和 outcome，无法区分模型是在调参、换资源、重复确认，还是根本没有看到
上一次成功。因此新增 migration 9，为副轮增加 `trace_json`。

留痕遵循两条边界：

1. **不索取或保存隐含思维链**。模型不新增任何必填字段；已兼容的
   `orientation.next_action_reason/known_facts/unknowns` 若主动提交则保存，否则留空。
2. **Runtime 自动记录可验证事实**：模型作决定前收到的 `input_result`、动作签名、同签名
   `repeat_index`、是否重复、声明任务类型、由动作机械推导的预期进展、实际结果、失败阶段和 outcome。

动作签名由动作类型与规范化 payload 生成，不含每次都会变化的 action id。同一执行段中签名再次
出现即可确定为重复动作；旧副轮的 `trace_json` 保持 `{}`，不得根据后来的信息回填原因。留痕失败
仍是旁路故障，不得影响主轮执行。

该留痕用于诊断与后续图控制，不自动把模型的理由升级为事实，也不单凭“理由写得合理”允许重复
执行。确定性控制仍应依据工具结果、证据 revision 与图节点完成条件。

### 14.9 2026-10-04模型名称层与本地资源身份层

实况副轮已经收到 `repeatable_operation.fixed_arguments.asset_id`，仍会漏抄固定模板、把上一轮
目标 id 当作新目标。结论是：**内部资源标识不应继续作为模型必须正确搬运的业务参数。**

资源协议拆成两层：

- 模型层只表达用户可理解的资源名称和语义槽位，例如“模板=勋章、目标=空格子”；
- Runtime 层继续以 `asset_id + version + content_hash` 作为唯一身份，用于执行、凭证、证据和失效。

本地名称解析规则：

1. 只在当前主轮可见且 active 的资源索引内做**精确名称匹配**；不扫描磁盘、不按相似度猜测、
   不回退焦点素材或历史素材；
2. id / token / 虚拟路径仍兼容，但只作为本地或旧客户端输入；新模型提示优先展示和接收名称；
3. 名称唯一时绑定为具体 id/version/hash；同名对应多个不同资源时返回结构化歧义，绝不任选；
4. 绑定成功后即冻结身份；后续改名不改变已绑定调用，资源内容或版本变化仍使旧句柄失效；
5. 留痕同时记录 `model_ref`、`resolved_by=name|id`、解析后的 id/version/hash 与参数槽位。

可重复操作再加一层确定性合成：Runtime 自动注入 `fixed_arguments`；模型只提交可替换槽位的
名称。若本轮只有一个新增且兼容的候选资源，Runtime 可将它绑定到替换槽位；候选不唯一时返回
歧义，不允许继续使用 `previous_target_asset_id` 冒充“已经替换”。模型遗漏固定模板不再导致工具
启动后报错。

这不是新增第二套资源事实源：名称只是当前资源索引的展示键，绑定后的 id/version/hash 仍来自
统一 `ResourceBinder`。所有自动注入与名称解析都必须进入副轮 `trace_json` 和 `ResourceBound`
事件，不能成为不可见的魔法修复。

### 14.10 2026-10-04本地定向与会话级资源查询

新实况主轮 `mt_20261003T165638Z_FB7951` 验证了两个架构问题：

1. 模型已按新协议使用“勋章”、“结界1”等精确显示名称，但名称查询只覆盖
   当前主轮创建资源、当前Task和全局资源，漏掉了从早先主轮保留在当前素材区的
   会话资源；同一资源换成内部id即可绑定，证明是查询作用域缺口，不是素材不存在。
2. 工具曾成功执行，但模型在13个副轮中6次漏填 `orientation`，最终被
   `TASK_ORIENTATION_EXCESSIVE` 取消。该字段要模型重复抄写 Runtime 已经掌握的
   任务类型，既不是执行事实，也不再拥有图路由权，却能否决正常工作。

据此锁定两项裁决：

1. **资源名称查询覆盖当前整个会话素材区**。Runtime 把本轮快照中的
   `session_asset_ids` 传给唯一 `ResourceBinder`；名称查询在这些显式可见id、
   当前Task和全局可复用资源的并集内做精确匹配。它不扫描磁盘、不纳入已移出素材区的
   历史资源，不将“跨主轮”误判为过期。同名多资源仍报歧义，不自动选择。
2. **移出模型协议中的 `orientation`**。任务定向改为 Runtime 本地事实：继承已落库的
   主轮Intent；首次定向使用用户原话、基线脚本/Task上下文、当前动作和可重复操作投影
   确定。脚本操作仍必须由用户原话或已存在脚本上下文授权；LangGraph节点仍决定
   允许动作。移除漏填重试、主轮漏填上限和 `TASK_ORIENTATION_EXCESSIVE` 生产路径。

兼容边界：旧客户端传入 `orientation` 可被解析但不作为任务类型事实、不改变图路由、
不因缺失或写错中断主轮。需要记录“为什么做这一步”时，继续使用副轮 `trace_json` 中的
可验证输入、动作签名、实际结果与节点状态，不要求模型额外填表。

验收要求：

- 素材在前一主轮加入、本轮仍在素材区时，可用精确名称绑定；移出后同名查询不再命中；
- 会话素材区存在两个同名不同id时必须返回 `RESOURCE_NAME_AMBIGUOUS`；
- 模型连续多个 query/act/finish 都不提交 `orientation`，不出现
  `TASK_ORIENTATION_REQUIRED/EXCESSIVE`；
- 用户没有脚本意图时，模型即使尝试创建、校验或交付脚本，仍被
  `SCRIPT_INTENT_NOT_EXPLICIT` 拒绝；
- 本地定向和资源名称解析都不增加LLM必填字段。

### 14.11 2026-10-04目标、步骤与结果收束（本次实施基准）

实况 `mt_20261003T175315Z_1FC62E` 中，同一多目标匹配成功返回零命中后仍连续执行，
得到非零结果后仍未回答。根因包括：tool_operation 未受图控制、每次成功都生成新证据ID、
成功次数被当作进展、移除orientation后缺少轻量执行意图。

职责固定如下：LLM解释用户目标并选择算法、工具和必要验证；Runtime管理资源身份、权限、
执行、结果有效性与重复控制。目标由用户原话初始化；模型可在动作payload中补充
`goal_contract={objective,deliverable,success_evidence}`，只在目标建立或修订时提交，
不作为每副轮必填表。步骤可选 `step_intent={purpose,expected_result,retry_change}`。
不得通过该字段取得脚本授权、伪造事实或自动判定整个任务合格。

能力从自身schema的 `x-completion` 派生完成语义，包括
`result_is_terminal_by_default/business_result_fields/retry_requires_changed_inputs/cacheable`。
没有声明时，读取操作的一次有效结果默认完成当前步骤；创建/修改操作仍按其真实结果及
既有校验判定。步骤完成不等于用户整体目标完成；多素材、多算法任务允许执行不同步骤。
模型读取Runtime反馈中的step_completed与required_next_action后决定回答或指出下一缺口。

新增唯一纯函数实现执行指纹：能力及版本 + 绑定后资源id/version/hash + 规范化参数 +
环境/目标上下文。名称、id、虚拟路径解析到同一资源后指纹相同；action_id与自然语言
理由不参与。参数默认值归一。只缓存固定资源上的可复用读取结果；实时抓帧、外部副作用、
未声明可缓存的创建/修改工具不因参数相同而缓存。缓存查找必须经过当次Binder与权限检查。
主轮内成功证据携带call_fingerprint/result_fingerprint及原始引用，恢复沿现有workspace
checkpoint读取，不能建立第二张资源或证据表。

同指纹已有有效结果时回放结果、不启动工具、不消耗单次授权、不新增证据、不增加进展。
首次重复给出已有结果和完成提示；再次持续重复由统一控制器收口，保存已有证据并明确诊断。
零命中是有效结果，不能被当成工具失败。新参数、资源版本或目标上下文允许新尝试。
结果指纹按业务输出与能力版本生成；重复内容不因新UUID成为进展。

tool_operation接入现有GraphDriver的通用执行/评价/回答策略，复用现有节点和诊断。
图允许查询、不同工具步骤与必要派生产物，但连续相同结果不能回到自由循环。
所有任务的执行入口均使用相同去重规则；脚本授权、校验和人工试运行路径保持既有契约。
asset_analysis仍使用其证据覆盖判据，不能把裁剪或一般成功回执当成数量证据。

实施顺序：能力完成语义与纯函数 → 执行入口和证据去重 → 通用图接管 →
模型目标/步骤提示与workspace投影 → 内部API真实匹配、重复、变参、恢复测试及协作回归。
验收必须覆盖：零命中一次即可回答；同资源不同引用写法只执行一次；重复不新增证据；
变参可重跑；修改/实时工具不错误缓存；checkpoint恢复仍识别旧结果；多步骤仍可推进；
不填新增字段仍能工作；目标声明不能绕过脚本授权；真实工具返回与最终回答一致。

这一节取代“成功动作次数/新证据ID就是进展”的旧口径。保留meaningful_actions作历史统计，
不以它证明业务进展。此次不引入Writer DSL或额外任务表。

实现补充：执行去重指纹与业务进展指纹分开。换阈值可生成一份新执行记录，但同一组
固定资源上的count/matches等业务字段完全不变时，不增加业务进展；重试两次无进展进入
resolve_gap_locally，再次无进展才report_blocked。查询不会清零该停滞计数。
图片分析的answer节点允许进入具有不同实参的后续取证步骤；同资源不同名称表达仍在
统一执行入口合并。创建/修改回执保留副作用记录，实时工具不缓存。
completion与输出schema参与能力版本；改变本地契约后需重新同步能力，旧客户端不可
继续靠旧版本执行。新GoalContract及缓存重放次数均随既有checkpoint保存，无DB迁移。
仅改变模型自填环境字符串不得绕过重复控制，环境指纹只取Runtime与绑定句柄事实。

#### 14.11.1 实施备案与验证（2026-10-04）

- 唯一语义与指纹函数：`collaborator_v3/runtime/convergence.py`。
- `runner.py`：绑定和权限检查之后查已有结果；执行之前决定缓存回放；证据保存调用、
  执行结果与业务结果指纹；重放次数可随checkpoint恢复；副轮事件带启动/缓存/进展字段。
  再次重复的收口消息展示已知标量结果，不把已量出的零命中藏起来。
- `graph_mode.py`：tool_operation进入执行/评价/回答策略；两次停滞走恢复，之后仍无进展
  才报告阻塞。asset_analysis保留证据覆盖判定，同时允许不同实参的后续取证。
- `workspace.py`：goal_contract进模型摘要和checkpoint；用户补充修订目标与控制状态。
  `provider_model.py/model_callable.py/queries.py`：可选语义字段、默认完成反馈、完整能力
  查询中的completion_contract；资源名称优先策略一致。
- `v2_capabilities.py/llm_turn.py`：工具schema里的x-completion成为能力完成语义来源；
  match_template_multi声明固定模板与固定目标才可缓存。完成语义和输出schema纳入版本。
- `shadow_graph.py/events/schema.py`：生产与影子使用真实evidence_delta/业务指纹，
  注册目标修订与工具结果复用事件。删除已停用的orientation拒绝函数，保留历史错误码解码。
- `skills/visual-template-count/SKILL.md`：精确名称、零命中交付、缓存复用、多目标逐项推进
  与有具体差异的复核规则。

内部真实工具验收：`tests/test_collab_v3_convergence.py`共13个案例，使用生成底图与模板
走ActionRunner→Binder→LegacyResolverExecutor→真实match_template_multi；验证3命中、
0命中、名称/id等价、重复不启动、不增证据、阈值变化、业务结果不变、不同步骤、
checkpoint、实时/修改工具不缓存、目标声明不得绕过脚本授权、能力版本与可选协议字段。
原真实匹配→mark_on_image的整条标注验收保留并通过。

验证结果：全部协作测试1252 passed / 1 skipped；邻近资源/能力/Provider/适配器专项
84 passed；最后的执行上下文与恢复路径专项24 passed。未删除或放宽原失败护栏。
全协作回归首次发现通用图在两次失败后提前收口，已修实现，保留模型报告失败/替代方案的
恢复机会后复跑全绿。

外部模型实测未完成：`tools/_scratch/verify_convergence_live.py`已提供最多8次响应的
独立验收入口，仅用生成测试图和临时数据库，读取应用配置、不输出凭据。首次尝试返回
MODEL_UNAVAILABLE、模型token计数为0；提高网络权限的重试被自动审批拒绝，理由是尚无
向该外部目的地发送系统提示词与测试上下文的明确授权。没有绕过审批或继续外部调用。
本地真实工具通过不能替代真实模型/GUI的行为结论；批准后可补这一项验收。

生效方式：重启应用加载新模块并同步新版能力；无需清空历史或删除资源。
这次收束不宣称能保证模型理解所有用户需求：用户目标解释仍属于模型，Runtime提供
可验证结果、完成反馈和预算边界。脚本生成/试运行仍复用原契约与控制路径，未整段重写。
