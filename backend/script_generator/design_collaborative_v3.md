# Script Generator 协作 Agent V3 架构方案书

> 文档状态：V3 预览实现已落地，可双轨灰度  
> 版本：0.3  
> 日期：2026-09-24  
> 适用范围：协作式脚本生成、素材识别、视觉定位、脚本试跑与修订  
> 核心定位：本地 Runtime 掌握状态、资源、权限和事实；LLM 是按主轮临时启用的规划器  
> 与 V2 的关系：保留 GUI、素材与执行能力，重写会话编排和状态所有权；不在 V2 主循环上继续叠加例外
> P0 技术契约：`design_collaborative_v3_p0_contracts.md`

实施状态（2026-09-24）：V3 已作为独立 Runtime 接入现有 GUI，默认仍为稳定版 V2，
可按会话切换“V3 预览”。领域存储、空白主轮、按需查询、版本化能力、权限、快照、
补充消息、暂停恢复、事实/主张隔离、原子提交、真实证据引用、初版逻辑确认、脚本
本地校验及试运行页路由均已落地。共同视口只保存本地快照；底图、独立标记层、焦点
选择需由模型显式调用 `viewport.get_current` / `resource.get` 才能取得。LangGraph 仍按
既定决策延后，不属于当前灰度运行依赖。

同日实况修订：副轮输入增加有界 `runtime_workspace`，保存本主轮已取得的查询结果、
资源句柄和计划；相同状态下的重复查询直接命中缓存并触发无进展闸。checkpoint 保存
该工作区及主轮最初用户原话，恢复时“继续”不会覆盖原始目标。无进展暂停不再生成
“继续”选项；已落库的通用占位目标可从会话中的用户目标做带审计、带 revision guard
的恢复。

---

## 1. 背景

V2 已经具备对话、共同视口、素材区、视觉工具、标注、脚本生成和试跑能力，但其运行核心逐渐形成了以下结构性问题：

1. 会话状态、提示词上下文、工具结果和 GUI 状态互相渗透，难以判断谁是事实来源。
2. LLM 同时承担理解、记忆、计划、权限判断、工具选择、事实校验和用户回复，任何一环出错都会污染后续步骤。
3. 为修复单次会话现象而增加的提示词、分支和硬闸会互相影响，系统行为越来越难以预测。
4. 素材、共同视口、标记图、裁剪结果和工具能力缺少统一、严格的生命周期语义。
5. 用户主张、模型观察和工具实测曾被混成“答案”，导致无证据结论被权威化。
6. 内部副轮容易提前停止，需要用户反复发送“继续”；失败能力也可能被重复调用和重复提示。
7. 旧上下文过多会让模型误用已删除素材、过期坐标、失效权限和其他图片的结论。

这些问题不能继续通过“缺什么补什么”的方式根治。V3 将重新定义 Agent 的状态所有权、运行协议和提交边界。

---

## 2. 建设目标与非目标

### 2.1 建设目标

- 每个主轮的 LLM 从最小上下文开始，按需查询本地状态。
- 用户无需反复唤醒 Agent；系统可完成的步骤默认持续执行到底。
- 资源、能力、权限、事实和任务状态拥有独立且明确的数据模型。
- 所有确定性结论均可追溯到具体资源版本和证据。
- 工具失败、视觉不确定、权限不足和用户信息缺失具有固定恢复路径。
- 内部副轮可以重试和修正，但不会把推理草稿与调试日志直接展示给用户。
- 任一主轮都能暂停、恢复、审计和重放关键决策。
- 未来接入 LangGraph 时，不需要改写领域模型和存储协议。
- 更换 LLM、视觉模型或具体工具后，产品行为边界保持稳定。

### 2.2 非目标

- V3 首版不追求完全迁移所有 V2 工具。
- 不要求 LLM 永远正确，而是限制错误的传播和提交。
- 不把 LangGraph 当作数据库、资源库或事实验证器。
- 不把完整历史记录默认塞进每次模型请求。
- 不让 GUI 控件直接修改 Agent 的内部计划或证据状态。
- 不在首版实现多 Agent 自由协商；逻辑模型、视觉模型和工具通过结构化协议协作。

---

## 3. 术语与运行单位

### 3.1 主轮 Main Turn

用户发送一次消息，到系统给出一次可见回复，称为一个主轮。主轮是产品层面的事务边界。

主轮负责：

- 理解当前用户意图；
- 建立本轮临时工作区；
- 完成必要的查询、工具执行和验证；
- 原子提交任务、资源和证据变更；
- 生成一次用户可见回复。

### 3.2 副轮 Subturn

一个主轮内部，系统与 LLM 之间的一次请求和响应称为一个副轮。工具调用结果返回后继续推理，也会形成新的副轮。

副轮仅属于当前主轮的临时运行上下文，默认不进入后续主轮的模型上下文。

### 3.3 提交 Commit

主轮结束时，将已校验的状态变化一次性写入本地存储，称为提交。没有通过校验的临时判断、失败调用和草稿不得进入长期状态。

### 3.4 工作区 Turn Workspace

当前主轮已经取得的任务信息、资源句柄、权限、临时结论和计划集合。每个主轮独立创建，结束后销毁；只有提交结果长期保存。

### 3.5 轨迹 Trace

副轮、工具输入输出、错误、校验结果和状态转换的审计记录。轨迹用于诊断、恢复和解释，不等于记忆，也不默认提供给 LLM。

---

## 4. 总体设计原则

### 4.1 状态本地化

LLM 不拥有长期会话状态。任务、资源、事实、权限和执行记录由本地 Runtime 管理。

### 4.2 空白主轮

每个主轮启动时，模型只得到：

- 当前用户原话；
- 最小运行协议；
- 内核查询与行动 API。

模型需要什么，就在当前主轮重新查询什么。上一主轮取得的临时资源句柄和权限不会凭模型记忆自动延续。

“空白”不等于系统失忆。长期信息仍保存在本地，只是不未经选择地注入模型上下文。

### 4.3 事实与主张分离

用户陈述、模型观察、视觉模型结果和确定性工具测量分别保存。它们可以支持同一 Claim，但来源不可混淆。

### 4.4 提议与执行分离

LLM 只能提出结构化请求。本地 Runtime 负责参数校验、权限判断、真实执行、结果登记和状态提交。

### 4.5 查询与授权分离

知道资源存在，不代表可以读取；能够读取，不代表可以修改或执行。所有能力都由显式权限和作用域控制。

### 4.6 版本与作用域优先

任何图像结论、坐标、裁剪和匹配结果必须绑定资源 ID、资源版本和坐标空间。资源发生变化后，相关派生结论自动失效或进入待复核状态。

### 4.7 自主执行优先

只在以下四种情况暂停并询问用户：

1. 缺少必要权限，且不存在低权限替代方案；
2. 需求未明确，且不同理解会显著改变结果、成本、架构或带来不可逆后果；
3. 需求与现实证据或系统能力冲突；
4. 缺少必要资源，且工作区、任务资源、派生能力和替代能力都无法补齐。

模型参数错误、可恢复工具失败、能力搜索和普通视觉不确定性不应转嫁给用户。

对低风险、可撤销、影响局部的决策，Runtime 记录合理默认值并继续；后续证据推翻默认值时，自动使受影响的局部结果失效并重做。需求与现实冲突时，Agent 必须指出具体冲突和证据，区分“事实不成立”和“当前能力做不到”，再提出最接近原目标的可行替代方案交给用户决定。

### 4.8 失败必须类型化

错误通过稳定错误码和恢复策略传播，不能仅靠模型解读自然语言报错。

### 4.9 事实化记忆

新主轮默认保留事实，丢弃推测：

- 保留用户明确决定、实际资源及版本、已执行动作、工具实测、已确认状态和结构化失败指纹；
- 未验证观察只能作为候选，不得冒充事实；
- 丢弃内部推理、未验证猜测、未采用计划和冗长失败过程；
- 失败指纹只保存能力与版本、输入资源版本、规范化关键参数、错误类型、失败阶段和重试资格。

用户的目标、偏好、授权和选择可以直接确认为用户事实；用户对图片内容、数量、坐标和外部现实的判断只形成 `user_statement` Evidence，仍需验证。

### 4.10 主轮合法出口

在不存在真实阻塞时，Runtime 必须持续执行多个副轮，不能因完成中间步骤、一次工具失败或副轮数量软限制要求用户发送“继续”。主轮只有四种合法出口：

1. 满足目标、验收条件和交付物验证；
2. 遇到 §4.7 定义的真实阻塞；
3. 达到模型、时间或费用硬预算，保存 checkpoint 后暂停；
4. 用户主动取消。

预算暂停必须报告已完成成果、验证程度、剩余工作、暂停原因和恢复位置，且只有用户明确点击或表达“继续”才能恢复。主观体验或用户明确要求验收时才需要满意度确认，不把“是否满意”设为所有任务的通用完成闸。

---

## 5. 系统架构

```text
┌───────────────────────────────────────────────────────────┐
│ GUI                                                       │
│ 对话 · 共同视口 · 素材区 · 标记编辑器 · 审批/选择卡片       │
└──────────────────────────┬────────────────────────────────┘
                           │ UIEvent
┌──────────────────────────▼────────────────────────────────┐
│ Interaction Controller                                    │
│ 识别新请求、补充、选择、取消、恢复、撤销                    │
└──────────────────────────┬────────────────────────────────┘
                           │ Start/Resume MainTurn
┌──────────────────────────▼────────────────────────────────┐
│ Agent Runtime                                              │
│ 主轮生命周期 · 副轮循环 · 预算 · 暂停恢复 · 原子提交         │
├──────────────────────┬────────────────────────────────────┤
│ Context Gateway       │ Policy Engine                      │
│ 按需读取本地状态       │ 权限、证据、计划与提交校验           │
├──────────────────────┼────────────────────────────────────┤
│ Task Store            │ Resource Hub                       │
│ 目标、计划、进度       │ 素材、标记、裁剪、产出物、版本关系    │
├──────────────────────┼────────────────────────────────────┤
│ Claim/Evidence Store  │ Capability Hub                     │
│ 主张、观察、实测       │ Tool、Skill、Model 能力目录          │
├──────────────────────┴────────────────────────────────────┤
│ Executor                                                   │
│ 本地图像工具 · 视觉模型 · 脚本生成 · 试运行 · 文件操作       │
└──────────────────────────┬────────────────────────────────┘
                           │ DomainEvent
┌──────────────────────────▼────────────────────────────────┐
│ Event Store / Trace                                        │
│ 可恢复、可审计、可诊断、可重放                              │
└───────────────────────────────────────────────────────────┘
```

### 5.1 状态所有权

| 数据 | 唯一所有者 | LLM 能否直接修改 |
|---|---|---|
| 用户目标与验收条件 | Task Store | 否，只能提交修订建议 |
| 素材与共同视口底图 | Resource Hub / Viewport Service | 否，只能调用命令 |
| 标记和裁剪结果 | Resource Hub | 否，只能创建派生资源请求 |
| 事实结论 | Claim Store | 否，只能提交候选 Claim |
| 工具实测 | Evidence Store | 否，由 Executor 写入 |
| 工具和 Skill 清单 | Capability Hub | 否 |
| 授权 | Policy Engine | 否 |
| 本轮计划 | Turn Workspace | 可以提议，Runtime 校验后采用 |
| 用户可见回复 | Response Composer | LLM 起草，提交闸校验 |
| 副轮轨迹 | Event Store | 否 |

---

## 6. 核心领域模型

以下字段是语义要求，不限定首版必须使用 dataclass、Pydantic 或数据库表实现。

### 6.1 Task

```python
Task = {
    "id": "task_...",
    "objective": "用户真正要完成的事情",
    "constraints": [],
    "acceptance_criteria": [],
    "status": "active | waiting_user | paused | completed | failed",
    "current_step_id": "step_... | null",
    "plan_version": 3,
    "revision": 12,
}
```

不变量：

- 模型修改计划不能隐式修改 `objective`。
- `completed` 必须满足验收条件并存在交付物或可验证结果。
- `waiting_user` 必须关联一个有效的 `InteractionRequest`。
- “预算耗尽”只能暂停，不能伪装为完成或等待用户。

### 6.2 Plan 与 Step

```python
PlanStep = {
    "id": "step_...",
    "goal": "定位九张对手卡",
    "kind": "query | observe | act | verify | ask_user",
    "status": "pending | running | blocked | done | invalidated",
    "depends_on": [],
    "done_when": [],
    "required_capabilities": [],
    "attempt_policy": {"max_attempts": 3, "dedupe_key": "..."},
}
```

`done_when` 必须是可验证条件列表，不能只写“等待用户确认”。用户确认属于 Interaction，而不是系统步骤的完成条件，除非该步骤本身就是产品确认节点。

### 6.3 Resource

```python
Resource = {
    "id": "res_...",
    "type": "image | overlay | region | template | script | run_result | document",
    "version": 4,
    "status": "candidate | active | verified | superseded | invalid | archived | deleted",
    "content_ref": "受控文件或内容地址",
    "content_hash": "sha256:...",
    "source": {"kind": "user | tool | model | import", "ref": "..."},
    "derived_from": [{"resource_id": "res_...", "version": 2}],
    "metadata": {},
}
```

不变量：

- 所有执行调用优先使用资源 ID，不接受模型任意拼接文件路径。
- 派生资源保存精确的父资源版本。
- 父资源内容变化时，派生关系触发失效检查。
- `superseded/invalid/archived/deleted` 默认不可进入新主轮工作区。
- GUI 的素材区是 Resource Hub 的一种查询视图，不是另一套资源真值。

### 6.4 Region 与坐标空间

```python
Region = {
    "resource_id": "res_region_...",
    "source_image": {"resource_id": "res_image_...", "version": 5},
    "shape": "rect | polygon | brush_mask",
    "points": [],
    "coordinate_space": {
        "kind": "source_pixel",
        "width": 1920,
        "height": 1080,
    },
}
```

内部真值统一使用原图像素坐标。视觉模型输入缩放坐标必须由 Runtime 根据实际送入尺寸换算，不能相信模型自报尺寸。

### 6.5 Evidence

```python
Evidence = {
    "id": "ev_...",
    "kind": "user_statement | model_observation | tool_measurement | run_observation",
    "subject": {"resource_id": "res_...", "version": 5},
    "payload": {},
    "producer": {"capability_id": "cap_...", "version": "..."},
    "confidence": 0.0,
    "created_in_turn": "turn_...",
    "status": "active | contradicted | stale | invalid",
}
```

### 6.6 Claim

```python
Claim = {
    "id": "claim_...",
    "subject": {"resource_id": "res_...", "version": 5},
    "predicate": "medal_counts",
    "value": [3, 3, 3, 1, 1, 0, 0, 0],
    "evidence_ids": ["ev_..."],
    "scope": {"image": "res_...", "region": None},
    "confidence": 0.82,
    "status": "candidate | verified | conflicted | stale | rejected",
}
```

不变量：

- 用户说法默认只能产生 `user_statement` Evidence 和 `candidate` Claim。
- 确定性回复引用的 Claim 必须是 `verified`，或在回复中明确表达不确定性。
- Claim 不得跨资源版本复用。
- 同一 subject/predicate 下出现不一致值时进入 `conflicted`，不能静默选择更迎合用户的答案。

### 6.7 Capability

```python
Capability = {
    "id": "cap_...",
    "kind": "tool | skill | model",
    "name": "match_repeated_regions",
    "version": "1.2",
    "input_schema": {},
    "output_schema": {},
    "permissions": [],
    "cost_class": "local | low | medium | high",
    "availability": "available | unavailable | degraded",
    "failure_memory": {},
}
```

Capability Hub 负责发现、版本、可用性和成本信息；Skill 是指导如何组合能力的资源，不直接获得绕过权限的执行权。

### 6.8 PermissionGrant

```python
PermissionGrant = {
    "id": "grant_...",
    "capability_id": "cap_...",
    "actions": ["read", "execute"],
    "scope": {"task_id": "task_...", "resource_ids": []},
    "issued_in_turn": "turn_...",
    "expires": "turn | task | timestamp | explicit_revoke",
    "confirmation_required": False,
}
```

### 6.9 MainTurn、Subturn 与 Commit

```python
MainTurn = {
    "id": "turn_...",
    "task_id": "task_...",
    "user_input": {},
    "status": "running | waiting_user | paused | committed | aborted",
    "workspace_snapshot_id": "ws_...",
    "commit_id": "commit_... | null",
}

Subturn = {
    "id": "subturn_...",
    "main_turn_id": "turn_...",
    "model_request": {},
    "model_response": {},
    "actions": [],
    "result_refs": [],
}

Commit = {
    "id": "commit_...",
    "task_patch": {},
    "resource_changes": [],
    "claim_changes": [],
    "memory_summary": {},
    "final_response": {},
}
```

---

## 7. Context Gateway：上下文按需查询

### 7.1 记忆分层

| 层级 | 内容 | 默认提供 |
|---|---|---|
| L0 | 当前用户原话及本轮附件引用 | 是 |
| L1 | 当前任务的短摘要、状态和未解决问题 | 通过首次查询取得 |
| L2 | 结构化记忆：用户决定、资源、权限、Claim | 按需查询 |
| L3 | 主轮可见对话 | 发生指代或语义歧义时查询 |
| L4 | 副轮轨迹、工具原始结果、错误细节 | 诊断、证明、恢复时定向查询 |

后续主轮首先读取上个主轮提交的结构化摘要，而不是自动读取全部副轮记录。

### 7.2 内核查询 API

首版只向 LLM 暴露少量稳定 API：

```text
task.get_current()
memory.search(query, kinds, limit)
resource.search(query, types, statuses, scope)
resource.get(resource_id, version?)
viewport.get_state()
capability.search(intent, kind?, cost_limit?)
capability.get(capability_id)
claim.search(subject?, predicate?, status?)
permission.check(capability_id, action, scope)
trace.get(ref, detail_level)
```

约束：

- 搜索结果返回摘要和稳定 ID，不直接灌入大对象。
- 大图、长日志和完整工具输出必须二次获取。
- 查询结果加入本轮 Workspace，并记录取得时间与版本。
- 同一主轮中的查询可缓存；跨主轮必须重新建立句柄。

---

## 8. Agent Runtime 主轮状态机

```text
CREATED
  ↓
INTAKE ──无法识别用户事件──→ REPAIR_INPUT
  ↓
HYDRATE_WORKSPACE
  ↓
PLAN
  ↓
EXECUTE ↔ OBSERVE ↔ REPLAN
  │           │
  │           └──需要用户独占信息/授权──→ WAIT_USER
  ↓
VALIDATE_COMMIT
  ├──可修复──→ REPLAN
  ├──预算耗尽──→ PAUSE
  ├──不可恢复──→ FAIL
  └──通过──→ COMMIT → RESPOND
```

### 8.1 Intake

Interaction Controller 先把 UI 输入归一化为事件：

```text
NewRequest
UserCorrection
UserSelection
PermissionDecision
CancelRequest
ResumeRequest
UndoRequest
ResourceChanged
```

这一步不由 LLM 猜按钮含义。

用户没有明显任务意图时保持普通对话，不创建 Task。识别出制作脚本的意图后，Runtime 在本地形成结构化目标、约束和验收条件；信息足够且可安全执行时直接开始，不固定执行“复述需求并等待确认”。

### 8.2 Hydrate Workspace

创建空 Workspace，注入当前任务 ID、用户事件和内核 API。模型根据需要查询任务摘要、资源、能力和证据。

### 8.3 Plan

模型提交结构化计划。Runtime 验证：

- 每一步是否可执行或可验证；
- 能力是否存在；
- 依赖是否闭合；
- 是否错误地把系统可做步骤改成询问用户；
- 是否包含与任务目标无关的外部动作。

### 8.4 Execute / Observe / Replan

Runtime 执行一个动作，将结果写入临时 Workspace 和 Event Store。模型只能看到动作的结构化结果摘要，需要细节时再查询。

计划未完成且不存在真实阻塞时，Runtime 自动继续，不需要用户输入“继续”。

GUI 可以显示“正在调用工具”“已验证 6/9”等非阻塞进度。这些内容不是主轮回复，只能来自可核验 Runtime 事件；禁止展示模型推测、内部推理或“马上完成”等无法保证的承诺。执行计划默认留在 Runtime 内部，GUI 只显示简洁阶段；重大变更、需要用户决定或用户主动查看时才展示详细计划。

### 8.4.1 主轮内用户补充

主轮开始时记录共同视口快照。运行中单纯操作 GUI 不改变本轮输入；用户主动发送补充内容时，Runtime 在同一主轮记录新快照 revision，并执行：

1. 立即停止调度由旧快照驱动、尚未开始的未来步骤；
2. 不立即终止整个主轮，先做兼容性评估；
3. 保留仍适用于新快照的事实、证据和产出；
4. 使依赖旧快照且不兼容的临时结果失效；
5. 基于剩余有效状态重新规划。

无外部副作用且支持取消的进行中调用立即取消；不可取消的读取或分析允许完成，但结果先隔离，兼容性通过后才能采用；点击、覆盖等外部副作用动作执行前必须再次校验预期 revision。

### 8.5 Validate Commit

提交闸至少执行：

1. 目标一致性检查；
2. 资源存在性和版本检查；
3. 权限检查；
4. Claim—Evidence 覆盖检查；
5. 矛盾 Claim 检查；
6. 交付物存在性检查；
7. 未完成可执行步骤检查；
8. 回复确定性措辞检查；
9. 重复提问检查；
10. 用户可见内容泄漏检查。

### 8.6 Commit

使用单一事务或等价的写前日志机制提交：

- Task patch；
- Resource 变更；
- Claim/Evidence 变更；
- 主轮摘要；
- 用户回复；
- checkpoint。

关键写入失败时不得形成半提交状态。

### 8.7 脚本制作领域闸

脚本制作任务具有一个强制领域确认点：在资源链和脚本逻辑完整后，Agent 必须生成 GUI 可展示的“初版脚本逻辑”并交给用户确认，即使原始需求非常明确也不能跳过。

用户确认后，脚本生成与静态校验继续由 LLM 和本地验证器完成。需要试运行时，协作页只引导用户前往现有 V2 试运行页面，不在 V3 对话 Runtime 内自行执行试运行。

---

## 9. 行动协议

LLM 输出不再依赖一个包含大量可选字段的万能表单。每个副轮只允许一种顶层意图：

```json
{"type": "query", "name": "resource.search", "arguments": {}}
{"type": "act", "capability_id": "cap_...", "arguments": {}}
{"type": "update_plan", "plan": {}}
{"type": "propose_claim", "claim": {}}
{"type": "request_interaction", "request": {}}
{"type": "finish", "commit_proposal": {}}
```

Runtime 返回统一信封：

```json
{
  "ok": false,
  "result": null,
  "error": {
    "code": "STALE_RESOURCE",
    "recoverable": true,
    "retry_policy": "refresh_resource",
    "details_ref": "trace_..."
  }
}
```

同一副轮不能一边申请用户选择，一边宣称任务已经完成。

---

## 10. 共同视口与素材区

### 10.1 共同视口是本地展示状态

```python
ViewportState = {
    "base": {"resource_id": "res_...", "version": 2},
    "overlays": [{"resource_id": "res_overlay_...", "version": 1}],
    "transform": {"zoom": 1.0, "pan": [0, 0]},
    "selection": [],
    "revision": 8,
}
```

- 用户切换底图、缩放、画标记不会自动发消息给 LLM。
- LLM 调用 `viewport.get_state` 或视觉能力时才读取当前状态。
- LLM 可调用 `viewport.present(resource_id)` 把素材展示给用户，但这只是 UI 命令，不产生事实。
- 截图编辑窗口是同一 ViewportState 的放大视图，关闭时按产品规则提交 Overlay 变更。
- 每个主轮开始时保存底图索引、标记、缩放、平移、选区和 revision 快照，供本轮或后续主轮定向查询。
- 被显示过不等于被模型知晓；被模型查询过也不等于进入长期事实记忆。
- 用户明确引用“这张图”“这里”“我标的区域”时，模型主动请求对应 Runtime 快照；系统仍不把图片隐式塞入上下文。

### 10.2 标记是独立资源

矩形、多边形和画笔标记以 Overlay/Region 资源保存，不修改原图像素。清空标记只改变 Overlay 集合，原图和历史版本仍可恢复。

### 10.3 素材区是资源视图

素材区双击图片相当于执行本地 `viewport.present`。用户说“素材区里的图片”时，模型应查询 Resource Hub；只有明确提到共同视口或需要展示时才读取 ViewportState。

资源默认搜索顺序为：当前主轮工作区 → 当前任务资源 → 当前任务可派生资源 → 明确标记可复用的全局资源。只有当前任务范围无法满足需求时才允许跨任务搜索；来源不明的旧素材不得自动使用。

---

## 11. 视觉协作协议

逻辑模型、视觉模型和本地图像工具不通过松散自然语言互相委派，而通过统一请求协作。

```python
VisualRequest = {
    "objective": "找出九个重复卡片区域",
    "image": {"resource_id": "res_...", "version": 3},
    "regions": [],
    "expected_output": "region_candidates",
    "coordinate_space": "source_pixel",
    "precision": "layout | object | pixel",
    "budget": "low | medium | high",
}

VisualResult = {
    "observations": [],
    "regions": [],
    "measurements": [],
    "uncertainty": [],
    "source_image": {"resource_id": "res_...", "version": 3},
    "producer": {"capability_id": "cap_...", "version": "..."},
}
```

### 11.1 成本阶梯

默认按以下顺序选择能力：

1. 本地确定性工具：尺寸、哈希、裁剪、颜色、轮廓、模板匹配；
2. 本地候选生成：重复区域、网格、相似块；
3. 轻量视觉模型：布局、语义候选、粗定位；
4. 强视觉模型：复杂语义和候选消歧；
5. 逻辑模型：综合证据、选择策略和生成下一步计划。

高成本视觉调用前应优先利用已有 Region 缩小输入范围，但不能因成本原因降低必要准确性而不告知用户。

逻辑模型是唯一决策者。视觉模型只返回结构化观察、Region、测量和不确定性，不能修改任务、调用高风险工具或直接向用户下事实结论。主逻辑模型不可用时发出警告并且不启动任务；切换模型后必须重新查询本轮工作区，不能继承上一模型未提交的推测。视觉模型不可用时先尝试本地图像工具和其他已配置视觉能力，没有满足任务所需的替代能力时才进入阻塞。

### 11.2 坐标不变量

- Runtime 记录实际送入视觉模型的像素尺寸。
- 模型返回坐标先解释为送入尺寸坐标，再换算到原图。
- 模型自报 `image_size` 仅作诊断信息。
- 小于最小有效面积或越界的 Region 标记为无效，不能进入裁剪或点击步骤。

### 11.3 Skill、临时工具与能力版本

- Skill 只描述如何组合能力完成某类任务，不保存事实、不持有权限，也不能绕过 Runtime 执行。
- 现有能力不足时，Agent 可以在受限沙箱中编写临时小工具；工具必须声明输入、输出、依赖和权限，并通过测试后才能执行。
- 临时工具默认属于当前任务，任务结束后归档；只有具备稳定性测试、适用范围说明并通过人工或策略审核，才能提升为全局能力。
- 能力在主轮执行期间升级时，已经开始的调用保持其锁定版本；尚未开始的步骤改用新版本，并在执行前重新验证输入、输出和行为兼容性。
- 满足准确性前提下优先使用本地、低成本能力，但不能为了节省成本跳过任务必需的验证。

---

## 12. Policy Engine

### 12.1 权限判定

权限至少区分：

- discover：发现能力或资源；
- read：读取内容；
- derive：创建裁剪、标记等派生资源；
- modify：修改任务或资源状态；
- execute：调用工具或试运行；
- destructive：删除、覆盖等不可逆操作。

权限有作用域和有效期。新主轮必须调用 `permission.check`，但本地系统可以返回仍有效的 task 级授权，不要求用户重复确认。

默认有效期按风险分级：

1. 读取类权限：任务级有效；
2. 创建且可撤销的权限：任务级有效；
3. 修改类权限：阶段级或批次级有效；
4. 覆盖、删除和外部副作用权限：单次或单批次确认；
5. 所有授权必须绑定作用域和资源范围；
6. 环境、资源或目标变化时，受影响的旧授权自动失效。

缺少权限时先检查现有 Grant，再寻找不需要该权限的替代路径；确实无法绕开时只申请当前步骤所需的最小权限。

### 12.2 证据判定

| 输出措辞 | 最低要求 |
|---|---|
| “就是/确定/共有 N 个” | verified Claim，且无活跃冲突 |
| “看起来/可能” | candidate Claim + 有效 Evidence |
| “用户认为” | user_statement Evidence |
| “工具测得” | tool_measurement Evidence |
| “不知道/无法判断” | 允许，无需伪造候选答案 |

证据不足时，Agent 必须明确说明“不知道”或“证据不足”，同时列出已经确认的部分、缺少的证据和下一种可行验证方法。

### 12.3 用户交互判定

```python
InteractionRequest = {
    "id": "interaction_...",
    "question": "...",
    "reason": "为什么必须由用户决定",
    "options": [],
    "allow_free_text": True,
    "blocking": True,
    "resume_token": "...",
}
```

如果 `blocking=False`，Runtime 必须继续执行不依赖该答案的步骤。GUI 用授权卡/ABC 选项卡展示，不在输入框附近长期占位。

### 12.4 任务切换、取消、撤销与删除

- 用户切换到其他任务时，当前任务自动保存 checkpoint 并暂停；任务之间的 Workspace、资源和权限默认隔离。
- 用户取消主轮时，立即停止未来步骤并取消可取消的进行中动作；已经产生的有效事实和资源保留，未提交推测丢弃。
- 撤销通过反向变更或恢复旧版本实现，保留完整审计记录，不物理抹去历史。
- 删除资源默认采用软删除并保留恢复期；只有用户明确要求永久删除时才物理清理，同时使关联证据和派生资源失效。
- 长期用户偏好只保存用户明确表达且确有跨任务价值的内容；模型推断出的偏好不得自动写入长期记忆。

---

## 13. 错误模型与恢复策略

| 错误码 | 典型原因 | 默认恢复 |
|---|---|---|
| `RESOURCE_NOT_FOUND` | 素材已移出或删除 | 重新查询资源；不得焦点回退 |
| `STALE_RESOURCE` | 文件内容或版本变化 | 刷新版本并使派生证据失效 |
| `CAPABILITY_UNAVAILABLE` | API 未配置或服务下线 | 标记本轮不可用，搜索替代能力 |
| `PERMISSION_DENIED` | 没有有效授权 | 仅在确需执行时请求授权 |
| `EVIDENCE_INSUFFICIENT` | 无法支持确定结论 | 补充观察，或明确“不知道” |
| `EVIDENCE_CONFLICT` | 用户、视觉或实测不一致 | 保留冲突并追加验证 |
| `INVALID_COORDINATES` | 坐标退化、越界或空间不明 | 重新定位，不执行裁剪/点击 |
| `TOOL_INPUT_INVALID` | 参数结构错误 | 自动修正一次，再换策略 |
| `TOOL_NO_PROGRESS` | 同图同参数重复无结果 | 熔断当前尝试，禁止原样重试 |
| `USER_INPUT_REQUIRED` | 只有用户知道的信息 | 生成一个明确交互请求 |
| `BUDGET_EXCEEDED` | 副轮/成本/时间达到上限 | 保存 checkpoint，显示可恢复状态 |
| `COMMIT_REJECTED` | 提交闸失败 | 回到 Replan，不能输出完成结论 |

### 13.1 去重与熔断

每个动作计算：

```text
attempt_key = capability_id + normalized_arguments + input_resource_versions
```

相同 `attempt_key` 连续失败时按能力策略退避；达到阈值后本主轮熔断。API 未配置提示只生成一次，之后返回同一个不可用状态引用，避免重复刷屏。

---

## 14. Event Store 与可观测性

### 14.1 事件类型

```text
MainTurnStarted
WorkspaceHydrated
PlanProposed
ActionRequested
ActionStarted
ActionSucceeded
ActionFailed
ResourceCreated
ResourceInvalidated
EvidenceRecorded
ClaimProposed
ClaimVerified
InteractionRequested
CommitValidated
CommitRejected
MainTurnCommitted
MainTurnPaused
```

### 14.2 日志级别

- 用户视图：必要进度、交互请求和最终回复；
- 开发视图：节点、错误码、耗时、资源引用；
- 审计视图：完整副轮和工具输入输出；
- 诊断视图：经过脱敏和裁剪的模型原始响应。

用户视图不得显示链式思考、内部提示词或无意义的格式修复过程。

### 14.3 可重放边界

重放以事件和确定性工具结果为基础。外部模型调用默认不重新执行，而读取已保存结果；显式要求重新验证时才生成新 Evidence。

---

## 15. LangGraph 适配方案

V3 的领域内核先保持框架无关。LangGraph 后续作为编排适配器引入：

```text
intake
  ↓
hydrate_workspace
  ↓
plan
  ↓
execute_action ↔ observe_result ↔ replan
  ↓
validate_commit
  ├─ repair → replan
  ├─ interrupt → wait_user
  ├─ pause → checkpoint
  └─ commit → respond
```

### 15.1 映射关系

| V3 概念 | LangGraph 对应 |
|---|---|
| Turn Workspace 投影 | Graph State |
| Runtime 阶段 | Node |
| 错误码、校验结果 | Conditional Edge |
| 用户授权和选择 | Interrupt / Resume |
| 主轮暂停 | Checkpointer |
| Task/Resource/Claim Store | 外部领域存储，不直接等同 Graph State |
| Event Store | Trace/事件适配器 |

首阶段开发视图应展示 LangGraph 节点轨迹、当前节点、暂停原因和工具耗时，但不得展示模型内部思维文本。

### 15.2 禁止事项

- 不把完整 Resource、图片字节和全部历史消息塞进 Graph State。
- 不以 LangGraph checkpoint 取代领域数据库。
- 不让节点直接写多个 Store；写入通过 Runtime Command 和 Committer。
- 不把业务规则散落在 conditional edge 的匿名函数中。

这样既能展示 LangGraph 的状态图、interrupt、checkpoint 和 trace 能力，也不会让产品语义被框架锁死。

---

## 16. 模块与代码边界建议

```text
backend/script_generator/collaborator_v3/
├─ domain/
│  ├─ task.py
│  ├─ resource.py
│  ├─ evidence.py
│  ├─ capability.py
│  ├─ permission.py
│  └─ turn.py
├─ stores/
│  ├─ task_store.py
│  ├─ resource_store.py
│  ├─ evidence_store.py
│  └─ event_store.py
├─ runtime/
│  ├─ runtime.py
│  ├─ workspace.py
│  ├─ state_machine.py
│  ├─ commit.py
│  └─ recovery.py
├─ policy/
│  ├─ permission_policy.py
│  ├─ evidence_policy.py
│  ├─ plan_policy.py
│  └─ response_policy.py
├─ gateways/
│  ├─ context_gateway.py
│  ├─ model_gateway.py
│  ├─ vision_gateway.py
│  └─ executor_gateway.py
├─ capabilities/
│  ├─ registry.py
│  └─ adapters/
├─ interaction/
│  ├─ controller.py
│  └─ response_composer.py
└─ orchestration/
   ├─ native_graph.py
   └─ langgraph_adapter.py       # 后续阶段
```

V2 模块保持不动，由功能开关选择 `collab_runtime=v2|v3`。V3 不直接 import V2 的主循环和表单解析器，只能通过明确 adapter 复用工具实现。

---

## 17. 持久化与事务

首版可以继续使用 JSON/JSONL，但接口必须按未来 SQLite 事务设计：

- Store 层对上提供版本检查和 compare-and-swap；
- Commit 写入唯一 `commit_id`，重复提交具有幂等性；
- Event Store 先记录 intent，再更新物化状态，崩溃后可恢复；
- 图片正文仍放文件系统，Store 保存路径、哈希和版本；
- 所有对外展示名称都不是主键。

当 JSON 写入和并发成为瓶颈时，可将 Task、Resource、Claim、Grant、Turn 和 Event 迁入 SQLite，领域 API 不变。

---

## 18. 迁移方案

### 18.1 保留

- 当前协作 GUI 和聊天展示；
- 共同视口、截图放大窗口和独立标记层；
- 素材导入、复制和右键操作；
- 已有确定性图像工具；
- 脚本生成、保存、打包和试运行能力；
- Resource Registry 中资源 ID、版本、状态、哈希和来源思想；
- 现有运行日志备份设施。

### 18.2 重写

- `llm_turn.py` 的主循环和上下文组装；
- `collab_form.py` 的万能表单协议；
- 自动续轮和 `done_when` 判定；
- 权限通过提示词注入的方式；
- Session artifacts 充当无类型数据库的方式；
- 内部过程和用户回复混合的路径。

### 18.3 兼容导入

旧会话只做单向导入：

- 素材转为 Resource；
- 已确认用户决定转为 `user_statement` Evidence；
- 旧 ground truth 不直接转为 verified Claim；
- 旧工具结果仅在能确认输入资源版本时导入；
- 无法确定来源的字段放入 legacy archive，不进入工作区。

### 18.4 双轨策略

1. V3 使用独立目录、Store 和会话版本号。
2. 默认保持 V2 可用，内部配置开启 V3。
3. 先用回放和测试语料跑 V3，不让两个 Runtime 同时写同一会话。
4. V3 达到验收门槛后切为默认，保留有限时间的 V2 只读回退。
5. 不在运行中把同一个主轮从 V2 切换到 V3。

---

## 19. 分阶段实施

### P0：协议冻结

- 冻结本方案中的术语、状态所有权和错误码。
- 定义领域对象 schema 和版本策略。
- 定义 LLM 行动信封、Runtime 结果信封和 InteractionRequest。
- 建立架构测试，禁止跨层直接写状态。

交付：schema、接口、状态图、示例事件流，不接真实 LLM。

### P1：领域内核与 Store

- Task、Resource、Evidence/Claim、Capability、Permission、Turn。
- 内存 Store + JSON/JSONL 持久化适配器。
- 资源派生、失效传播和 Claim 冲突规则。
- Event Store 与幂等 Commit。

交付：纯本地单元测试能够覆盖完整事务。

### P2：确定性 Runtime

- 使用 FakeModel 驱动状态机。
- 支持查询、行动、计划更新、提交和暂停恢复。
- 实现错误分类、重试、去重、熔断和预算。
- 实现 Policy Engine 的首批硬规则。

交付：不依赖真实模型即可重现主要成功和失败路径。

### P3：接入真实 LLM

- 最小系统协议和内核 API。
- Context Gateway 分层查询。
- 结构化输出修复限一次，失败后类型化返回。
- 回复提交闸与证据措辞校验。

交付：主轮空白、按需查询和自主副轮闭环。

### P4：视觉与 GUI 接入

- 共同视口/素材区改为 Resource Hub 查询视图。
- 接入 VisualRequest/VisualResult。
- 迁移重复区域、裁剪、模板匹配和标记能力。
- 接入 Interaction 卡片、暂停恢复和可见进度。

交付：完成通用“对话澄清—资源链建立—初版逻辑确认—脚本生成与校验—跳转试运行”的端到端示例；具体图像识别任务作为能力回归用例，不作为架构唯一主场景。

### P5：LangGraph 编排

- 用 LangGraph adapter 替换 native graph 的流程承载。
- 接入 checkpointer、interrupt/resume 和节点 trace。
- 保持 Store、Policy、Executor 和协议不变。
- 提供开发者状态图和节点耗时视图。

交付：框架接入前后行为契约测试一致。

### P6：灰度与切换

- 回放典型历史会话；
- 故障注入和模型替换测试；
- 小范围开启 V3；
- 达标后设为默认。

---

## 20. 测试策略

### 20.1 架构契约测试

- LLM 不能直接写 Store；
- GUI 事件不能直接生成 verified Claim；
- Executor 不能绕过 Permission Policy；
- Committer 是长期状态的唯一写入口；
- Graph State 不得包含图片正文和完整历史。

### 20.2 状态机测试

- 可执行计划自动继续；
- 真正缺用户信息才进入 WAIT_USER；
- 预算耗尽进入 PAUSE 且可以恢复；
- 提交失败回到 Replan；
- 重复恢复不会重复执行已提交动作。

### 20.3 资源与证据测试

- 新版本图片使旧坐标和 Claim 失效；
- 删除素材后不能回退到焦点素材；
- 用户主张不会自动成为 verified Claim；
- 冲突证据不会被静默覆盖；
- 标记 Overlay 不修改原图；
- 视觉坐标只按实际送入尺寸换算。

### 20.4 故障注入

- API 未配置；
- 视觉服务超时；
- 工具返回空结果；
- 模型输出非法 JSON；
- 文件在执行前被替换；
- 应用在 Commit 中途退出；
- 用户在等待期间修改共同视口；
- 同一按钮或恢复请求被重复触发。

### 20.5 模型替换测试

使用不同逻辑模型和视觉模型执行同一组场景，验证：

- 权限边界一致；
- 无证据结论均被拦截；
- 资源失效规则一致；
- 用户交互次数不因模型风格发生数量级变化。

---

## 21. 验收标准

### 21.1 功能验收

- 切换、缩放和标记共同视口不会自动触发 LLM。
- LLM 可以主动查询素材区并把指定素材展示到共同视口。
- 新主轮不会继承上一主轮未提交的资源句柄和临时权限。
- 已提交的用户决定可以重新查询，且保留来源和作用域。
- 旧图片的结论不能套用到新图片或新版本。
- 无验证答案时，Agent 能明确回答没有可靠答案。
- API 未配置在同一主轮最多提示一次，并尝试可用替代能力。
- 系统可自行完成的步骤不会要求用户发送“继续”。
- 视觉定位失败会改变策略，不会原参数空转。
- 每个确定性结论能追溯到 Evidence 和资源版本。
- 应用重启后可从 checkpoint 恢复等待或暂停中的主轮。

### 21.2 质量指标

建议灰度期记录：

- 每个任务平均用户唤醒次数；
- 无意义重复工具调用率；
- API 未配置重复提示率；
- 提交闸拦截的无证据断言数；
- 资源过期引用命中数；
- 每主轮副轮数和 P50/P95 耗时；
- 视觉模型成本和本地工具命中率；
- WAIT_USER 中可以由系统自行解决的误停比例；
- 主轮恢复成功率；
- 用户纠正同一事实的次数。

### 21.3 首个端到端验收场景

首个正式验收使用可泛化的“从对话到脚本初版”场景，不绑定特定游戏界面：

1. 用户进入后可能直接说明脚本目的，也可能先闲聊、询问功能或查看素材；无明显任务意图时不创建 Task。
2. Agent 识别制作脚本意图后建立 Task，结构化记录目标、约束和初步验收条件。
3. Agent 检查当前主轮工作区、当前任务素材和可复用资源，判断需求与资源是否充足。
4. 对低风险局部细节采用默认值；只围绕影响结果的缺口持续追问，逐步明确触发条件、操作流程、分支、异常处理、所需素材、完成条件和必要权限。
5. 用户提到“按照某个脚本做”时，Agent 能查找对应脚本、验证版本和适用范围，并建立参考关系而非盲目复制。
6. 用户提到“把这个图片……”时，Agent 能查询本轮共同视口快照、素材区和最近明确引用；无法唯一定位时再询问。
7. 缺少资源时先搜索、派生和调用已有能力获取，全部失败后才请用户补充。
8. 资源链和脚本逻辑完整后，Agent 生成 GUI 可展示的初版脚本逻辑，并强制等待用户确认。
9. 用户确认后，LLM 继续生成脚本并完成静态校验，中间无需用户发送“继续”。
10. 脚本达到试运行条件后，GUI 引导用户跳转到现有试运行页面，由用户在那里执行试跑。
11. 全流程中确定结论必须有证据；无法确认时明确保留未知，不能迎合用户制造答案。

---

## 22. 风险与应对

| 风险 | 应对 |
|---|---|
| 空白主轮导致查询次数和延迟增加 | L1 摘要、小结果缓存、批量查询、工作区内复用 |
| 结构化模型过多导致开发量上升 | 先冻结最小字段，Store 接口稳定后逐步扩展 |
| 提交闸过严导致无法完成 | 区分 hard/soft policy；允许明确表达不确定性 |
| 双轨期间数据混用 | 会话版本隔离，禁止同轮跨 Runtime 写入 |
| LangGraph 过早反向塑造业务 | P5 才接入，先以 native graph 验证领域协议 |
| Event Store 体积增长 | 大对象外置、结果摘要、轮转与保留策略 |
| 模型不断查询造成成本上升 | 查询预算、分页、精确 ID、Capability 成本分级 |
| 旧工具输出不标准 | 使用 adapter 归一化，不把旧返回值直接暴露给模型 |

---

## 23. 架构决策记录

### ADR-001：本地 Runtime 是唯一状态权威

决定：LLM 不持有长期状态，不直接更新领域 Store。  
原因：避免上下文遗漏、过期记忆和模型风格改变影响产品事实。

### ADR-002：主轮默认空白，信息按需查询

决定：不自动注入完整主轮对话和副轮轨迹。  
原因：降低旧素材、旧权限和旧结论污染，同时保留本地长期记忆。

### ADR-003：Claim 与 Evidence 分离

决定：用户主张、模型观察和工具实测分别保存。  
原因：系统必须能够说“不知道”，不能为了顺从用户制造标准答案。

### ADR-004：共同视口不是消息

决定：用户调整共同视口只产生本地 UI/资源事件，不自动触发模型。  
原因：视图变化不等于用户指令，模型需要时主动查询。

### ADR-005：LangGraph 后置接入

决定：先稳定领域内核和协议，再以 adapter 接入 LangGraph。  
原因：既获得图编排与可观测性，也避免业务被框架状态结构锁死。

### ADR-006：V3 与 V2 双轨而非原地改写

决定：V3 使用独立运行时，通过功能开关灰度。  
原因：现有 V2 仍可使用，原地重写难以区分回归与遗留问题。

---

## 24. 评审后需要冻结的事项

开始编码前必须共同确认：

1. 主轮、提交、暂停和等待用户的精确定义；
2. Task、Resource、Evidence、Claim、Capability 的最小 schema；
3. 首版允许的 LLM 顶层行动类型；
4. hard policy 与 soft policy 清单；
5. 权限作用域和默认有效期；
6. JSON/JSONL 首版持久化还是直接使用 SQLite；
7. V3 功能开关和旧会话导入边界；
8. 首个端到端验收场景及基准会话；
9. P5 引入 LangGraph 的进入条件，而不是固定日期。

方案冻结后，后续新增需求必须先判断属于领域对象、Policy、Capability、Interaction 还是 GUI；不得直接向主循环追加无法归类的特殊分支。
