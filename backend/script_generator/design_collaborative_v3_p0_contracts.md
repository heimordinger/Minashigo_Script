# 协作 Agent V3：P0 技术契约

> 状态：P0 协议冻结草案  
> 日期：2026-09-24  
> 上位方案：`design_collaborative_v3.md`  
> 目标：把产品需求转换为可实现、可测试、与编排框架无关的技术契约

实施进度（2026-09-24）：P0 契约及首个可灰度版本已落地。SQLite schema 已迁移到
v3，持久化 Task/Resource/TaskFact/Capability/Grant/Viewport/Checkpoint/Commit；V3 已接入
现有协作 GUI 的双轨开关且 V2 保持默认。现有素材与工具通过版本化 adapter 接入；共同
视口底图、独立标记层和焦点选择只在显式查询后进入本轮工作区。初版逻辑必须由真实
用户交互确认，模型自报确认无效；脚本须通过与试运行页一致的本地校验器，证据与交付
资源真实存在后才能原子提交。自动副轮、补充重规划、暂停/恢复、重复失败熔断、安全
进度事件和试运行页路由均有回归覆盖。LangGraph adapter 按产品决策延后，不是灰度前置项。

---

## 1. P0 决策摘要

P0 固定以下技术决策：

1. V3 新建独立包 `backend/script_generator/collaborator_v3/`，不原地改写 V2。
2. 领域模型首版使用标准库 `dataclasses`、`Enum` 和显式校验函数。
3. 领域真值使用标准库 `sqlite3`；单次 Commit 使用一个数据库事务。
4. SQLite 首版使用 rollback journal 与 `BEGIN IMMEDIATE`，不依赖 exFAT 上的 WAL 行为。
5. 图片、脚本和大对象仍保存在文件系统，数据库只保存受控路径、哈希、版本和元数据。
6. 现有 runtime JSONL 日志继续用于诊断；它不是领域状态恢复的权威来源。
7. Native Orchestrator 先实现状态机；LangGraph 以后通过相同 Port 接入。
8. LLM 每个副轮只返回一个顶层 ActionEnvelope。
9. Store 只能由 Committer 持久化修改；LLM、GUI、Executor 都不能直接写领域状态。
10. 首个端到端目标是通用脚本协作流程，不绑定具体识图业务。

---

## 2. 包结构

```text
backend/script_generator/collaborator_v3/
├─ __init__.py
├─ domain/
│  ├─ ids.py
│  ├─ enums.py
│  ├─ models.py
│  ├─ commands.py
│  ├─ events.py
│  ├─ errors.py
│  └─ validate.py
├─ ports/
│  ├─ stores.py
│  ├─ model.py
│  ├─ executor.py
│  ├─ viewport.py
│  └─ clock.py
├─ stores/
│  ├─ sqlite_store.py
│  ├─ schema.sql
│  └─ migrations/
├─ runtime/
│  ├─ workspace.py
│  ├─ context_gateway.py
│  ├─ policy.py
│  ├─ state_machine.py
│  ├─ committer.py
│  └─ service.py
├─ orchestration/
│  ├─ native.py
│  └─ langgraph.py       # P5 才实现
├─ adapters/
│  ├─ v2_capabilities.py
│  ├─ gui_events.py
│  └─ legacy_import.py
└─ testsupport/
   ├─ fake_model.py
   ├─ fake_executor.py
   └─ fixtures.py
```

依赖方向固定为：

```text
domain ← ports ← runtime ← orchestration/adapters
```

`domain` 不得 import GUI、V2 collaborator、SQLite、模型 SDK 或 LangGraph。

---

## 3. 标识符与版本

### 3.1 ID

所有 ID 为不透明字符串，不包含业务名称：

```text
task_<uuid7-or-uuid4>
turn_<uuid>
subturn_<uuid>
step_<uuid>
res_<uuid>
ev_<uuid>
claim_<uuid>
cap_<uuid>
grant_<uuid>
interaction_<uuid>
commit_<uuid>
event_<uuid>
snapshot_<uuid>
```

首版可使用 `uuid.uuid4()`。任何显示名称、文件名、数组下标都不能充当主键。

### 3.2 乐观并发版本

Task、Resource、Claim、Grant 和 ViewportSnapshot 都有整数 `revision`：

- 新建时为 `1`；
- 每次成功提交加一；
- Command 可带 `expected_revision`；
- 不匹配返回 `REVISION_CONFLICT`，不得静默覆盖。

### 3.3 Schema 版本

所有持久化记录包含 `schema_version`。P0 初始版本为 `1`。字段语义变化必须新增迁移，不允许读取时靠猜测兼容。

---

## 4. 枚举契约

```python
class TaskStatus(str, Enum):
    ACTIVE = "active"
    WAITING_USER = "waiting_user"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

class TurnStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    PAUSED = "paused"
    COMMITTED = "committed"
    CANCELLED = "cancelled"
    ABORTED = "aborted"

class RuntimePhase(str, Enum):
    INTAKE = "intake"
    HYDRATE = "hydrate"
    PLAN = "plan"
    EXECUTE = "execute"
    OBSERVE = "observe"
    REPLAN = "replan"
    VALIDATE = "validate"
    COMMIT = "commit"
    RESPOND = "respond"
    WAIT_USER = "wait_user"
    PAUSE = "pause"
    FAIL = "fail"

class ResourceStatus(str, Enum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    VERIFIED = "verified"
    SUPERSEDED = "superseded"
    INVALID = "invalid"
    ARCHIVED = "archived"
    DELETED = "deleted"

class ClaimStatus(str, Enum):
    CANDIDATE = "candidate"
    VERIFIED = "verified"
    CONFLICTED = "conflicted"
    STALE = "stale"
    REJECTED = "rejected"

class EvidenceKind(str, Enum):
    USER_STATEMENT = "user_statement"
    MODEL_OBSERVATION = "model_observation"
    TOOL_MEASUREMENT = "tool_measurement"
    RUN_OBSERVATION = "run_observation"

class ActionType(str, Enum):
    QUERY = "query"
    ACT = "act"
    UPDATE_PLAN = "update_plan"
    PROPOSE_CLAIM = "propose_claim"
    REQUEST_INTERACTION = "request_interaction"
    FINISH = "finish"
```

数据库和 API 只保存枚举的字符串值。未知值读取时返回 `UNSUPPORTED_SCHEMA_VALUE`，不得默认映射到最接近状态。

---

## 5. 领域对象最小 Schema

### 5.1 TaskRecord

```python
@dataclass(frozen=True)
class TaskRecord:
    id: str
    objective: str
    constraints: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    status: TaskStatus
    current_step_id: str | None
    plan_revision: int
    revision: int
    created_at: str
    updated_at: str
    schema_version: int = 1
```

校验：

- `objective` 非空；
- `COMPLETED` 必须有验收证明引用，由 CommitProposal 提供；
- `WAITING_USER` 必须有未解决 Interaction；
- `PAUSED` 必须有 checkpoint；
- Task 不保存模型自然语言推理。

### 5.2 PlanRecord 与 PlanStepRecord

```python
@dataclass(frozen=True)
class PlanStepRecord:
    id: str
    goal: str
    kind: str
    status: str
    depends_on: tuple[str, ...]
    done_when: tuple[dict[str, Any], ...]
    required_capabilities: tuple[str, ...]
    attempt_limit: int
    expected_viewport_revision: int | None

@dataclass(frozen=True)
class PlanRecord:
    task_id: str
    revision: int
    steps: tuple[PlanStepRecord, ...]
    created_in_turn: str
```

`done_when` 只能包含 Policy 注册过的谓词，禁止自由文本作为唯一完成条件。

### 5.3 ResourceRecord

```python
@dataclass(frozen=True)
class ResourceRef:
    id: str
    version: int

@dataclass(frozen=True)
class ResourceRecord:
    id: str
    type: str
    version: int
    revision: int
    status: ResourceStatus
    display_name: str
    content_path: str | None
    content_hash: str | None
    source_kind: str
    source_ref: str | None
    derived_from: tuple[ResourceRef, ...]
    metadata: dict[str, Any]
    reusable_scope: str       # task | global | none
    task_id: str | None
    created_in_turn: str
    schema_version: int = 1
```

路径要求：

- 数据库中保存规范化绝对路径或由 ResourceStorage 解释的相对 locator；
- LLM 不得提供任意落盘路径；
- 读取文件前复核 `content_hash`；
- 新版本在同一事务内创建，旧版本按策略转为 `SUPERSEDED`；
- 软删除只改变状态；物理删除由单独 destructive capability 执行。

### 5.4 ViewportSnapshot

```python
@dataclass(frozen=True)
class ViewportSnapshot:
    id: str
    turn_id: str
    revision: int
    base: ResourceRef | None
    overlays: tuple[ResourceRef, ...]
    zoom: float
    pan_x: float
    pan_y: float
    selections: tuple[ResourceRef, ...]
    captured_at: str
```

主轮启动必须创建快照。用户仅操作 GUI 时只更新 Viewport Service；用户发送补充消息时创建本主轮的新快照，并触发兼容性评估。

### 5.5 EvidenceRecord

```python
@dataclass(frozen=True)
class EvidenceRecord:
    id: str
    kind: EvidenceKind
    subject: ResourceRef | None
    payload: dict[str, Any]
    producer_capability_id: str | None
    producer_version: str | None
    confidence: float | None
    status: str
    turn_id: str
    created_at: str
    schema_version: int = 1
```

`USER_STATEMENT` 允许没有 Resource subject，但若内容涉及图像、数量或坐标，成为 verified Claim 前必须绑定明确 subject。

### 5.6 ClaimRecord

```python
@dataclass(frozen=True)
class ClaimRecord:
    id: str
    subject: ResourceRef | None
    predicate: str
    value: Any
    evidence_ids: tuple[str, ...]
    confidence: float | None
    status: ClaimStatus
    scope: dict[str, Any]
    revision: int
    created_in_turn: str
    schema_version: int = 1
```

Claim 的事实键为：

```text
subject.id + subject.version + predicate + canonical_scope
```

相同事实键存在互斥值时，新旧 Claim 均进入 `CONFLICTED`，由验证动作解决，禁止 latest-wins。

### 5.7 CapabilityRecord

```python
@dataclass(frozen=True)
class CapabilityRecord:
    id: str
    kind: str                 # tool | skill | model
    name: str
    version: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    permission_actions: tuple[str, ...]
    cost_class: str
    availability: str
    scope: str                # task | global
    metadata: dict[str, Any]
```

注册要求：

- Tool 的 input/output schema 不能为空对象，除非确实无参数或无返回且显式声明；
- Skill 不得声明 execute 权限；
- 临时 Tool 默认 `scope=task`；
- 升级创建新版本，不原地修改旧版本定义。

### 5.8 PermissionGrantRecord

```python
@dataclass(frozen=True)
class PermissionGrantRecord:
    id: str
    capability_id: str
    capability_version: str | None
    actions: tuple[str, ...]
    task_id: str
    resource_refs: tuple[ResourceRef, ...]
    stage_id: str | None
    batch_id: str | None
    expires_kind: str
    expires_value: str | None
    environment_fingerprint: str
    target_fingerprint: str
    revision: int
    revoked_at: str | None
```

有效性由 Policy Engine 计算，模型不能根据旧聊天文本认定权限有效。

### 5.9 FailureFingerprint

```python
@dataclass(frozen=True)
class FailureFingerprint:
    capability_id: str
    capability_version: str
    input_resources: tuple[ResourceRef, ...]
    normalized_argument_hash: str
    error_code: str
    failure_phase: str
    retry_eligibility: str
    occurrence_count: int
```

不保存模型分析文本。非确定性错误可在退避后重试；确定性参数或兼容性错误在输入未变化时不得原样重试。

### 5.10 MainTurnRecord

```python
@dataclass(frozen=True)
class MainTurnRecord:
    id: str
    task_id: str | None
    user_event_id: str
    status: TurnStatus
    phase: RuntimePhase
    active_snapshot_id: str
    checkpoint_id: str | None
    commit_id: str | None
    model_id: str | None
    model_version: str | None
    started_at: str
    updated_at: str
```

主逻辑模型不可用时不创建 RUNNING 主轮；可以记录拒绝启动的 UI/runtime 事件，但不得形成空跑 Task。

### 5.11 InteractionRequestRecord

```python
@dataclass(frozen=True)
class InteractionRequestRecord:
    id: str
    turn_id: str
    reason_code: str
    question: str
    reason: str
    options: tuple[dict[str, str], ...]
    allow_free_text: bool
    blocking: bool
    resume_token_hash: str
    status: str
```

`reason_code` 只能是：

```text
PERMISSION_REQUIRED
REQUIREMENT_AMBIGUOUS
REALITY_CONFLICT
RESOURCE_REQUIRED
LOGIC_DRAFT_CONFIRMATION
```

脚本初版逻辑确认使用 `LOGIC_DRAFT_CONFIRMATION`，属于领域强制闸，不表示需求不明确。

---

## 6. 用户事件契约

```python
@dataclass(frozen=True)
class UserEvent:
    id: str
    type: str
    text: str
    attachment_refs: tuple[ResourceRef, ...]
    viewport_snapshot_id: str
    task_hint: str | None
    interaction_id: str | None
    created_at: str
```

允许的 `type`：

```text
message
supplement
selection
permission_decision
resume
cancel
undo
```

规则：

- 普通聊天没有明显任务意图时 `task_hint=None`，不自动创建 Task；
- 正在运行的主轮收到 `supplement` 时不创建新主轮；
- `selection` 必须引用未解决 Interaction；
- `resume` 必须引用 PAUSED checkpoint；
- UI 切图、缩放和标记本身不是 UserEvent message，不唤醒 LLM。

---

## 7. LLM 行动协议

### 7.1 ActionEnvelope

每个模型响应必须且只能解析为一个顶层信封：

```python
@dataclass(frozen=True)
class ActionEnvelope:
    protocol_version: int
    action_id: str
    type: ActionType
    payload: dict[str, Any]
    expected_task_revision: int | None
    expected_viewport_revision: int | None
```

不得输出第二个并列动作、表外回复或推理文字。解析失败只允许一次格式修复；仍失败返回 `MODEL_PROTOCOL_ERROR`。

### 7.2 QueryAction

```json
{
  "protocol_version": 1,
  "action_id": "act_...",
  "type": "query",
  "payload": {
    "name": "resource.search",
    "arguments": {"types": ["image"], "scope": "task", "limit": 20}
  }
}
```

允许的内核查询名称由白名单注册，不允许模型提供模块名或函数路径。

### 7.3 ExecuteAction

```json
{
  "protocol_version": 1,
  "action_id": "act_...",
  "type": "act",
  "payload": {
    "capability_id": "cap_...",
    "capability_version": "1.2",
    "arguments": {},
    "input_resources": [{"id": "res_...", "version": 3}]
  },
  "expected_task_revision": 7,
  "expected_viewport_revision": 4
}
```

执行前依次验证能力可用性、schema、资源、权限、版本和 FailureFingerprint。

### 7.4 UpdatePlanAction

只更新 Turn Workspace 的临时计划。长期 Plan 只能在 Commit 中落库。

### 7.5 ProposeClaimAction

只创建候选 Claim。Policy Engine 根据证据决定能否验证；模型不能直接指定 `VERIFIED`。

### 7.6 RequestInteractionAction

Runtime 验证是否确属四类阻塞或脚本逻辑强制确认点。系统可自行解决的问题返回 `INTERACTION_NOT_JUSTIFIED` 并要求继续执行。

### 7.7 FinishAction

```python
FinishPayload = {
    "outcome": "complete | blocked | pause | cancel",
    "commit_proposal": {},
    "response_draft": {},
}
```

`complete` 必须通过全部完成闸；`blocked` 必须有关联 Interaction；`pause` 必须达到硬预算并包含 checkpoint；`cancel` 必须来自用户取消事件。

---

## 8. Runtime 结果信封

```python
@dataclass(frozen=True)
class RuntimeResult:
    ok: bool
    action_id: str
    summary: dict[str, Any] | None
    refs: tuple[str, ...]
    error: RuntimeErrorInfo | None

@dataclass(frozen=True)
class RuntimeErrorInfo:
    code: str
    recoverable: bool
    retry_policy: str
    details_ref: str | None
    user_visible: bool
```

模型默认只获得 `summary` 和 `refs`；大对象或原始日志必须通过定向查询取得。

---

## 9. 错误码冻结

### 9.1 输入与协议

```text
MODEL_UNAVAILABLE
MODEL_PROTOCOL_ERROR
UNSUPPORTED_PROTOCOL_VERSION
INVALID_ACTION
INVALID_ARGUMENTS
INVALID_STATE_TRANSITION
UNSUPPORTED_SCHEMA_VALUE
```

### 9.2 状态与资源

```text
TASK_NOT_FOUND
TURN_NOT_FOUND
RESOURCE_NOT_FOUND
STALE_RESOURCE
INVALID_RESOURCE
REVISION_CONFLICT
VIEWPORT_REVISION_CHANGED
```

### 9.3 能力与权限

```text
CAPABILITY_NOT_FOUND
CAPABILITY_UNAVAILABLE
CAPABILITY_VERSION_CHANGED
CAPABILITY_SCHEMA_MISMATCH
PERMISSION_REQUIRED
PERMISSION_EXPIRED
PERMISSION_SCOPE_MISMATCH
```

### 9.4 证据与计划

```text
EVIDENCE_INSUFFICIENT
EVIDENCE_CONFLICT
INVALID_COORDINATES
PLAN_INVALID
INTERACTION_NOT_JUSTIFIED
COMPLETION_NOT_PROVEN
LOGIC_CONFIRMATION_REQUIRED
```

### 9.5 执行与生命周期

```text
TOOL_FAILURE
TOOL_TIMEOUT
TOOL_NO_PROGRESS
ACTION_CANCELLED
ACTION_RESULT_QUARANTINED
BUDGET_EXCEEDED
COMMIT_REJECTED
COMMIT_CONFLICT
```

错误码语义一经发布不得重用。自然语言说明可以修改，恢复策略必须有测试。

---

## 10. 主轮状态转换

### 10.1 合法转换表

| 当前状态/阶段 | 事件 | 下一状态/阶段 |
|---|---|---|
| CREATED | model available | RUNNING / INTAKE |
| CREATED | model unavailable | ABORTED / FAIL |
| RUNNING / INTAKE | task intent absent | RUNNING / RESPOND |
| RUNNING / INTAKE | task intent found | RUNNING / HYDRATE |
| RUNNING / HYDRATE | workspace ready | RUNNING / PLAN |
| RUNNING / PLAN | valid plan | RUNNING / EXECUTE |
| RUNNING / EXECUTE | action result | RUNNING / OBSERVE |
| RUNNING / OBSERVE | more work | RUNNING / REPLAN |
| RUNNING / REPLAN | valid plan | RUNNING / EXECUTE |
| RUNNING | justified interaction | WAITING_USER / WAIT_USER |
| WAITING_USER | valid supplement/decision | RUNNING / HYDRATE or REPLAN |
| RUNNING | hard budget reached | PAUSED / PAUSE |
| PAUSED | explicit resume | RUNNING / HYDRATE |
| RUNNING | finish proposal | RUNNING / VALIDATE |
| RUNNING / VALIDATE | rejected, recoverable | RUNNING / REPLAN |
| RUNNING / VALIDATE | accepted | RUNNING / COMMIT |
| RUNNING / COMMIT | committed | COMMITTED / RESPOND |
| any nonterminal | user cancel | CANCELLED |

未列出的转换全部拒绝并记录 `INVALID_STATE_TRANSITION` 事件。

### 10.2 主轮出口不变量

- 中间步骤完成不能结束主轮；
- soft budget 只能触发策略调整，不能暂停；
- hard budget 暂停需要 checkpoint 和用户可见剩余工作；
- PAUSED 只有用户明确 resume 才能恢复；
- WAITING_USER 必须有原因码和未解决 Interaction；
- COMMITTED 必须有 commit_id；
- CANCELLED 保留有效事实和资源，丢弃未提交推测。

### 10.3 脚本制作状态片段

```text
DISCOVER_INTENT
  ├─ 非任务 → 普通对话
  └─ 制作脚本
       ↓
ESTABLISH_TASK
       ↓
RESOLVE_REQUIREMENTS_AND_RESOURCES
       ↓
BUILD_LOGIC_DRAFT
       ↓
WAIT_LOGIC_CONFIRMATION       # 强制
       ↓ 用户确认
GENERATE_SCRIPT
       ↓
VALIDATE_SCRIPT
       ↓
READY_FOR_TRIAL_PAGE
       ↓
COMMIT + 引导跳转 V2 试运行页
```

---

## 11. 主轮内补充与并发调用

### 11.1 补充处理

收到 supplement：

1. 冻结旧计划调度器，不再启动未来步骤；
2. 捕获新 ViewportSnapshot；
3. 标记所有进行中 Action；
4. 可取消且无副作用的 Action 发出 cancel；
5. 不可取消的读取/分析继续完成，但结果进入 quarantine；
6. 外部副作用 Action 在执行边界校验 task/resource/viewport revision；
7. 运行兼容性检查；
8. 采用兼容结果，使不兼容临时结果失效；
9. Replan。

### 11.2 兼容性判据

结果可以保留，当且仅当：

- 所有输入 ResourceRef 版本仍一致；
- 结果不依赖已改变的 Viewport selection/overlay；
- 任务目标和约束未使其语义失效；
- Capability 版本仍满足后续消费者的 schema；
- 对外副作用没有越过新授权范围。

### 11.3 能力热更新

- 进行中调用使用启动时锁定版本直至完成；
- 尚未开始的调用解析到当前新版本；
- 新版本 input/output schema 与计划不兼容时回到 Replan；
- 旧版本结果能否被新版本消费者使用由 adapter compatibility 声明决定；
- 未声明兼容时默认不兼容。

---

## 12. Policy 契约

### 12.1 允许询问用户的条件

```python
def may_block_for_user(reason: str, context: PolicyContext) -> PolicyDecision:
    """仅允许权限、关键歧义、现实冲突、资源穷尽和逻辑草稿确认。"""
```

资源不足在以下尝试全部完成前不得询问：

```text
workspace → task resources → derive → available capabilities → reusable global resources
```

权限不足在检查现有 Grant 和低权限替代方案前不得询问。

### 12.2 权限有效期

| 风险类别 | 默认有效期 |
|---|---|
| read | task |
| create_reversible | task |
| modify | stage 或 batch |
| overwrite/delete/external_side_effect | single 或 batch |

Grant 必须绑定 task、action、资源范围和环境/目标 fingerprint。环境、资源或目标变化时重新判定，不能只比较时间。

### 12.3 事实规则

可直接保存为事实：

- 用户目标；
- 用户偏好；
- 用户授权；
- 用户作出的选择；
- 已发生的执行动作；
- 绑定输入版本的工具实测；
- 经过验证的状态。

只能保存为主张或候选：

- 用户对图片内容、数量和坐标的判断；
- 模型视觉观察；
- 模型对外部现实的陈述；
- 未经执行验证的完成判断。

### 12.4 完成规则

`complete` 同时需要：

1. 用户目标未被计划隐式替换；
2. acceptance criteria 全部有 proof ref；
3. 必需交付物存在、版本有效且可读取；
4. 不存在未解决 hard conflict；
5. 不存在 Runtime 可继续执行的 pending step；
6. 脚本任务的初版逻辑已确认；
7. 回复中的确定性陈述能解析到 verified Claim 或执行事实。

### 12.5 “不知道”规则

证据不足时允许并鼓励返回未知。用户回复需包含：

- 已确认部分；
- 无法确认部分；
- 缺少的证据；
- 下一种可行验证方法。

不得为了生成完整表格或迎合用户而填入“标准答案”。

---

## 13. Context Gateway 契约

### 13.1 固定内核查询

```python
class ContextGateway(Protocol):
    def get_task(self, task_id: str) -> QueryPage: ...
    def search_memory(self, query: MemoryQuery) -> QueryPage: ...
    def search_resources(self, query: ResourceQuery) -> QueryPage: ...
    def get_resource(self, ref: ResourceRef) -> QueryPage: ...
    def get_viewport_snapshot(self, snapshot_id: str) -> QueryPage: ...
    def search_capabilities(self, query: CapabilityQuery) -> QueryPage: ...
    def get_capability(self, capability_id: str, version: str | None) -> QueryPage: ...
    def search_claims(self, query: ClaimQuery) -> QueryPage: ...
    def check_permission(self, query: PermissionQuery) -> QueryPage: ...
    def get_trace(self, trace_ref: str, detail: str) -> QueryPage: ...
```

### 13.2 查询边界

- 默认 `task_id=current`；
- 跨任务搜索只返回 `reusable_scope=global`；
- 搜索先返回摘要和 ref，正文二次获取；
- QueryPage 有稳定分页 token 和最大条数；
- 查询加入 Workspace 时记录其 revision；
- 新主轮重新查询，不复用旧模型的隐式上下文。

### 13.3 共同视口

共同视口快照不会自动注入模型。只有用户明确指代或模型主动查询时提供；“显示过”“查询过”“长期事实”是三种不同状态。

---

## 14. Capability 与 Executor Port

```python
class CapabilityCatalog(Protocol):
    def search(self, intent: str, *, task_id: str, cost_limit: str | None) -> list[CapabilityRecord]: ...
    def resolve(self, capability_id: str, version: str | None) -> CapabilityRecord: ...

class Executor(Protocol):
    def start(self, request: ExecutionRequest) -> ExecutionHandle: ...
    def cancel(self, handle_id: str) -> CancelResult: ...
    def poll(self, handle_id: str) -> ExecutionResult: ...
```

ExecutionRequest 固定包含：

- capability ID/version；
- 规范化参数；
- 输入 ResourceRef；
- PermissionGrant ref；
- task/viewport expected revision；
- idempotency key；
- timeout 和 cost budget。

逻辑模型是唯一决策者。视觉模型通过 Executor 注册为 Capability，只能返回 VisualResult，不能写 Task、Claim、Grant 或用户回复。

主逻辑模型不可用时 Runtime 发出警告且不启动任务。视觉能力不可用时按照本地工具 → 其他视觉能力 → 阻塞的顺序降级。

---

## 15. Skill 与临时工具

### 15.1 Skill

Skill 是带版本的过程知识：

- 描述适用条件、能力组合、验证方法和失败恢复；
- 不保存任务事实；
- 不持有 PermissionGrant；
- 不直接执行代码；
- 不覆盖 Policy。

### 15.2 临时工具晋升流程

```text
draft → schema_validated → sandbox_tested → task_available
                                      ↓
                          reviewed → global_available
```

任务级临时工具在任务结束时归档。晋升全局需：

- 非空且准确的 input/output schema；
- 权限和副作用声明；
- 依赖声明；
- 正常、边界、错误路径测试；
- 适用范围；
- 人工或策略审核记录。

---

## 16. Commit 契约

### 16.1 CommitProposal

```python
@dataclass(frozen=True)
class CommitProposal:
    idempotency_key: str
    turn_id: str
    expected_task_revision: int | None
    task_patch: dict[str, Any]
    plan: PlanRecord | None
    resources_to_create: tuple[ResourceRecord, ...]
    resource_transitions: tuple[dict[str, Any], ...]
    evidence_to_create: tuple[EvidenceRecord, ...]
    claims_to_create_or_update: tuple[ClaimRecord, ...]
    grants_to_update: tuple[PermissionGrantRecord, ...]
    interactions_to_update: tuple[dict[str, Any], ...]
    memory_summary: dict[str, Any]
    acceptance_proofs: tuple[str, ...]
    final_response: dict[str, Any]
```

### 16.2 原子提交顺序

```text
BEGIN IMMEDIATE
  1. 校验 idempotency_key
  2. 校验 expected revisions
  3. 写 DomainEvent: CommitStarted
  4. 写/更新 Task、Plan、Resource、Evidence、Claim、Grant、Interaction
  5. 写 MainTurn 状态
  6. 写 DomainEvent: MainTurnCommitted
  7. 写 Commit 结果
COMMIT
```

文件内容先写到受控临时路径并计算哈希，数据库事务只发布已完成文件的 locator。发布失败则回滚数据库；孤立临时文件由清理任务回收。

### 16.3 幂等性

同一个 `idempotency_key`：

- 请求内容哈希相同：返回已有 Commit；
- 请求内容哈希不同：返回 `COMMIT_CONFLICT`；
- 不得重复创建资源或事件。

---

## 17. SQLite 最小表

```text
schema_migrations
tasks
plans
plan_steps
resources
resource_parents
evidence
claims
claim_evidence
capabilities
permission_grants
main_turns
subturns
viewport_snapshots
interactions
failure_fingerprints
commits
domain_events
```

关键约束：

- `resources(id, version)` 唯一；
- `capabilities(id, version)` 唯一；
- `commits(idempotency_key)` 唯一；
- `domain_events(sequence)` 单调递增；
- 所有外键开启；
- JSON 字段写入前 canonicalize，读取后显式 schema 校验。

首版连接设置：

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = DELETE;
PRAGMA synchronous = FULL;
PRAGMA busy_timeout = 5000;
```

V3 Runtime 同一时刻只允许一个 writer；读取可使用独立连接。后续确认部署文件系统和并发需求后再评估 WAL。

---

## 18. Event 契约

```python
@dataclass(frozen=True)
class DomainEvent:
    id: str
    sequence: int | None
    type: str
    task_id: str | None
    turn_id: str | None
    aggregate_id: str | None
    payload: dict[str, Any]
    created_at: str
    schema_version: int = 1
```

P0 冻结事件：

```text
MainTurnStarted
ViewportSnapshotCaptured
WorkspaceHydrated
PlanProposed
PlanAccepted
PlanRejected
ActionRequested
ActionStarted
ActionSucceeded
ActionFailed
ActionCancelled
ActionResultQuarantined
SupplementReceived
CompatibilityEvaluated
ResourceCreated
ResourceTransitioned
EvidenceRecorded
ClaimProposed
ClaimStatusChanged
PermissionGranted
PermissionExpired
InteractionRequested
InteractionResolved
LogicDraftConfirmationRequested
LogicDraftConfirmed
CommitStarted
CommitRejected
MainTurnCommitted
MainTurnPaused
MainTurnCancelled
```

用户进度 UI 只订阅白名单事件，并由本地模板生成文案。DomainEvent payload 不保存或展示模型链式思考。

---

## 19. Checkpoint 契约

Checkpoint 只在 WAITING_USER、PAUSED、任务切换和安全关闭时创建，包含：

- Task/Plan revision；
- 当前 Runtime phase；
- active ViewportSnapshot ref；
- 已完成 Step ID；
- 进行中 Action 的状态及幂等键；
- Workspace 中已取得的结构化 ref；
- 未解决 Interaction；
- 预算使用摘要；
- 下一恢复阶段。

不包含：

- 模型内部推理；
- 无引用的大图或工具全文；
- 未验证的自由文本猜测；
- 明文 resume token。

预算暂停只有显式 UserEvent `resume` 才能恢复。任务切换自动 checkpoint，并隔离 Workspace、资源查询 scope 和 Grant。

---

## 20. GUI Adapter 契约

GUI 只发送 UserEvent 和 Viewport state change，只接收 ViewUpdate：

```python
@dataclass(frozen=True)
class ViewUpdate:
    task_id: str | None
    turn_id: str | None
    status_text: str
    progress: tuple[dict[str, Any], ...]
    messages: tuple[dict[str, Any], ...]
    interaction: dict[str, Any] | None
    logic_draft: dict[str, Any] | None
    trial_page_ready: bool
```

规则：

- 进度来自 Runtime Event，不来自模型表外文字；
- Interaction 以内联授权卡/ABC 卡显示；
- 初版脚本逻辑继续使用现有 V2 GUI 的逻辑展示区；
- 用户确认初版逻辑后继续生成和校验；
- 达到试运行条件只设置 `trial_page_ready=True` 并引导跳转现有试运行页；
- GUI 不自行推断 Task/Turn 状态。

---

## 21. 首批架构测试

### 21.1 依赖边界

```text
test_domain_imports_no_gui_v2_sqlite_or_langgraph
test_only_committer_writes_domain_store
test_executor_cannot_mutate_task_or_claim
test_visual_capability_cannot_emit_user_reply
```

### 21.2 主轮生命周期

```text
test_no_task_created_for_general_chat
test_clear_script_request_starts_without_confirmation
test_intermediate_step_does_not_end_turn
test_recoverable_failure_replans_without_user_wakeup
test_only_four_blocker_classes_can_wait_user
test_hard_budget_pause_requires_explicit_resume
test_cancel_preserves_facts_and_drops_speculation
```

### 21.3 快照和补充

```text
test_viewport_change_alone_does_not_reach_model
test_turn_uses_start_snapshot
test_supplement_creates_new_snapshot_in_same_turn
test_supplement_stops_future_old_plan_steps
test_uncancellable_result_is_quarantined
test_side_effect_checks_revision_immediately_before_execute
```

### 21.4 事实和证据

```text
test_user_goal_is_fact
test_user_image_answer_is_unverified_statement
test_conflicting_claims_do_not_use_latest_wins
test_unknown_is_allowed_when_evidence_missing
test_new_resource_version_stales_old_scoped_claim
```

### 21.5 资源和权限

```text
test_resource_search_is_task_scoped_by_default
test_global_resource_requires_reusable_flag
test_unknown_legacy_asset_is_not_auto_used
test_permission_lifetime_follows_risk_class
test_environment_change_invalidates_grant
test_delete_is_soft_by_default
```

### 21.6 能力

```text
test_logic_model_unavailable_does_not_start_task
test_switched_logic_model_rehydrates_workspace
test_running_action_keeps_old_capability_version
test_future_action_uses_new_version_after_compatibility_check
test_task_tool_is_archived_not_globalized
test_skill_cannot_hold_permission_or_execute
```

### 21.7 脚本制作流程

```text
test_ambiguous_script_request_collects_only_material_gaps
test_reference_script_can_be_found_by_name_and_relation_saved
test_image_reference_resolves_snapshot_then_task_resources
test_logic_draft_confirmation_is_mandatory
test_confirmed_logic_continues_to_generation_without_continue_message
test_ready_script_routes_to_existing_trial_page
```

---

## 22. P0 完成定义

P0 完成必须同时满足：

1. 本文的枚举、错误码、顶层行动类型和合法状态转换通过评审；
2. 领域 dataclass 可以实例化并完成纯函数校验；
3. SQLite schema 能创建、迁移和执行原子 Commit；
4. FakeModel/FakeExecutor 能跑通普通对话和脚本制作的状态机骨架；
5. 首批架构测试名称全部落成测试，允许部分 P1/P2 行为先标记为预期失败，但不能没有契约；
6. V2 测试保持通过，V3 默认关闭；
7. 没有引入 LangGraph 运行依赖；
8. P1 开发不再需要重新解释“事实、资源、权限、主轮和副轮”的语义。

P0 评审通过后，字段新增允许向后兼容扩展；删除字段、改变错误码语义、改变状态所有权或放宽权限规则必须新增 ADR。
