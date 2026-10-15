# 本地 API 契约票（ticket）Phase 0 只读审计报告

- **审计对象**：`backend/script_generator/design_local_api_contract_ticket.md`（标注"待实现"，378 行，§15 Phase 0）
- **审计范围**：只回答 Phase 0 §15 要求的 10 个问题；**只读**，未修改任何实现、测试、配置或既有设计文档
- **证据基线**：当前工作树（含此前 `design_executable_api_contract` 方案已实施但未提交的改动）
- **结论口径**：每条结论都带 `文件:符号:行号`，并标注 **【已确认】** / **【推断】** / **【未确认】**
- **纪律声明**：本报告不修任何顺手发现的问题，不进入 Phase 1，不追加审计轮次（方案 §18.1/§18.10）

---

## 1. 逐题结论

### Q1 契约现在怎么进入 LLM 上下文？能否确认"模型确实查看过"？

结论：**存在两条互不相通的路径；V3（协作）路径下"模型查看过契约"这件事目前完全没有记录，也没有对应的查询入口。**

| 项 | 证据 | 状态 |
| --- | --- | --- |
| V2/经典路径 = 提示词**整块注入**（always-on，非按需） | `agent.py:_contract_block_text` 调用 `api_contracts_block`：`agent.py:758-760`（唯一传 `profile` 的注入点）、`agent.py:9947`、`agent.py:10681`、`agent.py:10751`、`agent.py:10868`；实现在 `api_catalog.py:api_contracts_block:69-76` → `api_contracts/render.py:52`/`:127`（`"{owner}.{name}"`） | 【已确认】 |
| V2 另有按需工具 `lookup_api` | `api_catalog.py:lookup_api:50`；注册与分发 `agent.py:8525-8529`；工具名出现在提示词 `agent.py:8856` | 【已确认】 |
| V2 的"按需查看"**不落任何事件** | `lookup_api` 是纯函数（`api_catalog.py:50`），无 EventStore/日志写入调用点 | 【已确认】 |
| **V3 没有 `api.*` 查询** | `collaborator_v3/runtime/queries.py:QUERY_NAMES:1431`（23 个查询名）中不存在 `api.*` / `contract.*`（全目录 grep 无匹配） | 【已确认】 |
| V3 的 `capability.search/get` 给的是**另一套事实** | `queries.py:_exec_capability_search:400`、`_exec_capability_get:446`；返回 V3 `CapabilityRecord.input_schema`（`queries.py:366`/`:391`/`:1184`），不是 `api_contracts` 的方法签名 | 【已确认】 |
| V3 的 API 事实是**手写提示词** | `collaborator_v3/adapters/provider_model.py:_SYSTEM_PROTOCOL:14` 内 `:173-177`："必须定义 async def do_work(browser: UserWindow)"、"只调用 UserWindow 已有方法（match_image / click_image / wait_image / b_sleep / update_frame / note_state / script_log 等）"、"只有 wait_image 有 timeout；match_image 用 threshold/cheap/force_wide…" | 【已确认】 |
| 唯一存在的"模型看过什么"记录 | `EventType.QUERY_COMPLETED = "query.completed"`（`collaborator_v3/events/schema.py:57`；旧别名 `QueryBatchCompleted` 见 `:102`），写入点 `runner.py:1632-1642`，payload 仅 `name / query_key / ok / empty / refs`，注释自称供追溯"模型当时到底看到了什么"（设计 §18.2） | 【已确认】 |
| 该记录的粒度**不足以支撑 `CONTRACT_VIEWED`** | payload 无返回内容摘要、无 `contract_id`、无 `contract_revision`、无 schema hash（`runner.py:1635-1641`） | 【已确认】 |

- 因此方案 §5.1 的 `CONTRACT_VIEWED` 在当前实现里**没有任何对应物**：V3 里连"API 契约被查看过"这一事件都不可能产生（没有查询入口），V2 里只存在"契约被注入过"。【已确认】

### Q2 Registry 已经能提供 ticket 需要的哪些字段，缺哪些？

结论：**§6 的 20 个字段里约 8 个可从 Registry 直接派生，4 个只有近义物，其余 8 个属于签发行/Runtime 职责或全仓不存在。**

数据源：`api_contracts/schema.py:Parameter:33-48`、`ReturnContract:73-87`、`ApiContract:95-121`、`registry.contracts_revision:342`。

| ticket 字段（§6） | Registry 现状 | 状态 |
| --- | --- | --- |
| `contract_revision` | `registry.py:contracts_revision:342` | 【已确认】可派生 |
| `runtime_profile` | `schema.py:ApiContract.runtime_profile:100` | 【已确认】可派生 |
| `requested_method` | `schema.py:ApiContract.name:99` | 【已确认】可派生 |
| `call_kind` | `schema.py:ApiContract.call_kind:105`（`sync_method`/`async_method`） | 【已确认】可派生 |
| `parameters.required` / `parameters.allowed` | `schema.py:Parameter:33-48`（`name/kind/type/required/default/doc`）+ `ApiContract.parameter:130`、`keyword_names:136` | 【已确认】可派生 |
| `return_model.type` | `schema.py:ReturnContract.type:74` | 【已确认】可派生 |
| `capability_id` | `schema.py:ApiContract.id`，形如 `desktop_user_window.match_image`（`render.py:52`） | 【已确认】存在，但命名风格与 §6 示例 `runtime.image.match_one` 不同 → 需映射约定【推断】 |
| `return_model.iterable` / `sized` / `nullable_fields` | `ReturnContract`（`schema.py:73-87`）只有 `type/truthiness/fields/identity_check_unsafe/never_proves/lifetime`；四个名字在 `backend/script_generator` 全目录 grep **零命中** | 【已确认】缺失 |
| `receiver_type` / `resolved_owner`（真实类名如 `UserWindow`） | 只有 `schema.py:ApiContract.owner:101`，默认值 `"browser"`；契约定义处**没有任何一处覆写 `owner`**（grep `owner=` 零命中），仅 `registry.py:110` 在 portable 合并时改成 `"portable"` ⇒ 取值域实际是 `{"browser","portable"}`，是**占位变量名而非类型名** | 【已确认】缺失（不是"弱"，是语义不同） |
| `resolution_chain` | `schema.py:ApiContract.proxied:118` 只有布尔标记；链本身没有任何地方存储（`resolution_chain` 全仓零命中） | 【已确认】缺失 |
| `profile_source` | 属解析层：`validator.PROFILE_SOURCE_*`（`validator.py:1113-1115`）、`execution_profile.ProfileResolution`（`execution_profile.py:41`） | 【已确认】不在 Registry |
| `permission_scope` | `ApiContract` **无**此字段；V3 侧另有 `PermissionGrantRecord.actions`（`collaborator_v3/domain/contracts.py:49-52`） | 【已确认】不在 Registry |
| `resource_scope` | 全仓无 `resource_scope`（V3 用 `PermissionGrantRecord.resource_refs:54`） | 【已确认】缺失 |
| `environment_revision` | 全仓无 `environment_revision`（V3 用 `PermissionGrantRecord.environment_fingerprint:60`、`target_fingerprint:61`） | 【已确认】缺失 |
| `view_event_id` / `issued_at` / `integrity` / `main_turn_id` | 属签发行与 Runtime，Registry 不提供（`main_turn_id` 在 `events/schema.py:EventEnvelope:163` 有现成列） | 【已确认】 |
| `approved`（决定是否可签发） | `schema.py:ApiContract.approved:115` | 【已确认】可派生 |

### Q3 Validator 已经能提取哪些调用点信息？

结论：**receiver/method/参数/await/契约 id/行号都已有；缺"列区间"和"返回类型与数据流"。**

| 能力 | 证据 | 状态 |
| --- | --- | --- |
| 接收者提取 | `validator._receiver_aliases:521-564`、`validator._resolve_receiver:567-574` | 【已确认】 |
| 方法名 | `validator._check_browser_call:704`（读 `func.attr`） | 【已确认】 |
| 源码位置 | `validator.ContractIssue.line:88`，由 `node.lineno` 填充（如 `:718`、`:721`） | 【已确认】只有**行**，无列、无结束位置（`ContractIssue` 字段见 `:83-91`） |
| 参数 | 关键字名（`API_UNKNOWN_KEYWORD`，`validator.py:42`）、必需参数（`API_MISSING_ARGUMENT:43`）、位置参数个数（`API_TOO_MANY_POSITIONAL:44`）；另带 `allowed_parameters`（`:91`） | 【已确认】 |
| await | `API_AWAIT_REQUIRED:45` / `API_AWAIT_FORBIDDEN:46`，辅助函数 `validator._awaited_call:150-158` | 【已确认】 |
| 契约 id | `_check_browser_call` 内 `contract_id=item.id`（`validator.py:694` 起） | 【已确认】 |
| 返回模型 | 仅语义级：`_check_return_semantics:851` + `API_IDENTITY_CHECK:47` / `API_RETURN_FIELD:48` / `API_LITERAL_TYPE:57` | 【已确认】 |
| 返回类型 / 数据流（§10） | 无 `TYPE_NOT_SIZED` 等任何实现（全仓 grep 零命中）；`len(...)`、下标、nullable、空列表取首项均不检查 | 【已确认】缺失 |
| 结构化 issue 形态 | `ContractIssue`: `code/message/line/symbol/contract_id/allowed_parameters/repair_hint/severity`（`validator.py:83-105`）、`to_dict:95`、`render:107` | 【已确认】 |
| 稳定码清单 | `validator.py:35-61`（`SYNTAX_ERROR`…`UNBOUNDED_LOOP`） | 【已确认】 |

### Q4 接收者解析支持到什么程度？

| 情形 | 支持度 | 证据 | 状态 |
| --- | --- | --- | --- |
| 入口参数（`browser` / `win`） | ✅ 支持 | `validator.DEFAULT_TARGET_NAMES:67`、`entry_target_names:1097`、`VALID_TARGET_TYPES:64` | 【已确认】 |
| 简单别名 / 链式别名（`bb = browser`、`cc = bb`） | ✅ 支持 | `_receiver_aliases:521-564` | 【已确认】 |
| 重新赋值后来源失效 | ✅ 支持 | `_receiver_aliases` 内 `alias.pop` + invalidated 分支（`:555-560`） | 【已确认】 |
| 元组解包 / 属性赋值产生接收者 | ❌ 不解析（不猜） | `_receiver_aliases:554` 附近注释与分支 | 【已确认】 |
| 属性链接收者（`self.win.match_image`） | ❌ 不解析，而且**直接漏报** | `_receiver_unverified_issues:610` 仅当接收者是 `ast.Name` 才产生 issue，其余直接返回 `[]`（`:606-635`） | 【已确认】 |
| MRO / Mixin 解析 | ❌ 无任何实现 | `_resolve_receiver:567-574` 只回根名；无 mro 相关代码 | 【已确认】缺失 |
| `__getattr__` 代理链解析 | ❌ 不解析、不写链 | Registry 只有 `schema.py:ApiContract.proxied:118`；解析侧无链；仅测试 `tests/test_api_contract_registry.py:72`、`tests/test_api_contract_runtime_drift.py:77` 覆盖"proxied 不得被直接解析" | 【已确认】缺失 |
| 子类覆盖 | ❌ 无实现、无测试 | 全目录无相关代码 | 【已确认】缺失 |
| 单端契约（同名方法只在一边 profile 存在） | ⚠️ 部分 | `_single_sided_method_names:591` + `API_RECEIVER_UNVERIFIED:56`（`:56` 注释即为此场景） | 【已确认】 |
| "不确定就报告，不静默挑一个" | ⚠️ 部分满足 | 名字既非入口也非别名时报错（ERROR 单端 / WARNING 共有，`:618-629`）；但属性链接收者漏报（见上） | 【已确认】 |
| "父类属于本项目"作为判据 | ✅ 当前**没有**这种判据 | 无相关代码；与方案 §8 一致 | 【已确认】 |

### Q5 profile 的权威来源在哪？哪里还在用注解/默认值/交集？

| 项 | 证据 | 状态 |
| --- | --- | --- |
| 唯一权威来源 | GUI 解析的 `ProfileResolution`：`execution_profile.py:147-205`；`CONFIDENCE_AUTHORITATIVE:32` 来自 `SOURCE_TASK_TARGET:25`（`:170`/`:172`）或 controller 实例（`:26-27`、`:177`/`:181`） | 【已确认】 |
| 次一级（provisional） | `SOURCE_BASELINE_SCRIPT:28` → `CONFIDENCE_PROVISIONAL`（`:186`/`:197`） | 【已确认】 |
| 无事实 | `SOURCE_UNRESOLVED:29` → `:189`/`:202` | 【已确认】 |
| 传递链 | `gui/widgets/script_gen_panel/mixin_runtime.py:661-663` 写 `account["_target"]` → facade `execution_target` → `runner.py:_apply_execution_target:224-239`（写 workspace，拿不到权威就写 `UNRESOLVED_PROFILE`） | 【已确认】 |
| V3 校验用的 profile | `runner.py:_validation_profile:241-266`：权威优先 → 否则 `validator.profile_for_code` 注解 provisional（`:255`）→ 否则 `None`（validator 落 `portable_common`） | 【已确认】 |
| 注解判定实现 | `validator.profile_for_code:1118`、`profile_source_for_code:1146` | 【已确认】 |
| 仍在用**注解**的入口 | `agent.py:760` 传 `profile`；而 `agent.py:9947`、`:10681`、`:10751`、`:10868` **都不传 profile**（`api_contracts_block(explanation=...)`）→ 走注解/portable | 【已确认】 |
| V2 协作表单仍留 `None` 默认 | `collaborator/collab_form.py:_CONTRACT_PROFILE:1765`（`None`）+ 使用点 `:1834` | 【已确认】 |
| 安全交集 | `registry.PORTABLE_PROFILE:45`、`registry.effective_profile:228`、`registry._merge_portable:63`、`_build_portable:132`；对应"必须同时通过两端契约" | 【已确认】 |
| 硬编码重复事实（见 §4） | `validator.profile_for_code` 内 browser 分支用字面量 `"browser_user_browser"`（`:1140`），desktop 分支用 `registry.LEGACY_DEFAULT_PROFILE`（`:1142`） | 【已确认】 |
| `agent.py:760` 那个 `profile` 的最终上游 | 未追（本轮不扩范围） | 【未确认】 |

### Q6 materializer 三份文本与 hash 是否一致？

结论：**试运行路径上"同一函数、同一 build"，且有用真实磁盘文件做的断言 ⇒ 一致。仅剩"是否存在第二条写盘路径"未确认。**

| 项 | 证据 | 状态 |
| --- | --- | --- |
| 唯一 source→run-text 流水线 | `script_artifact.materialize_trial_script:253-297` | 【已确认】 |
| 三份文本与 hash 的定义 | `script_artifact.ArtifactBuild:67-117`（`source/normalized/bundled/artifact` + `*_sha256` 属性 `:99-113`） | 【已确认】 |
| 版本与变换标记 | `MATERIALIZER_VERSION="3"`（`script_artifact.py:48`）、`TRANSFORM_*:57-60`、`BAN_ZONES_MARKER:54` | 【已确认】 |
| 写盘唯一入口 | `script_artifact.write_materialized:211`；生产调用点 `mixin_runtime.py:277`（`written_sha = write_materialized(build, path)`，build 来自 `:252` 的 `materialize_trial_script(..., already_bundled=True)`） | 【已确认】 |
| 校验侧走同一函数 | `script_artifact.verify_artifact:584` 内部 `materialize_trial_script`（`:619`，`dry_run=True`） | 【已确认】 |
| 一致性有真实断言 | `tests/test_api_contract_materializer.py:303-313`（磁盘文件 `sha256_text(...)` == receipt `artifact_sha256`）、`:257`（GUI 侧与校验侧一致）、`:68`（三段 hash 互不相同） | 【已确认】 |
| 是否存在绕过该流水线的第二条写盘路径 | 本轮只找到 GUI 一处调用（`mixin_runtime.py:277`） | 【未确认】 |

### Q7 receipt / 事件存储 / 试运行记录能不能容纳 ticket、调用点映射、执行事件关联？

结论：**都能增量容纳，且都不需要新的 DDL 迁移；但"调用点到 ticket 的映射"目前没有任何存储位置，必须新建。**

| 载体 | 现状 | 结论 | 状态 |
| --- | --- | --- | --- |
| `ValidationReceipt` | 字段见 `api_contracts/receipt.py:59-92`：source/bundled/artifact sha、`transforms`、`validated_profiles`、`profile_source`、`target_revision`、`api_contract_revision`、`asset_versions`、`checks`、`validator_version`；**无** ticket / 调用点字段 | 需新增字段 | 【已确认】 |
| 新增字段是否破坏兼容 | `receipt.from_dict:159-166`：`cls(**{k: v for k, v in data.items() if k in known})` —— **未知键直接丢弃**，老 receipt 仍可读 | 纯增量、向后兼容 | 【已确认】 |
| 事件信封 | `events/schema.py:EventEnvelope:133-151`（`main_turn_id/run_id/subturn_id/action_id/trial_run_id/subject/payload/evidence_refs`）+ 保留元键 `ENVELOPE_META_KEY:44` | 足以承载 `CONTRACT_VIEWED` / `CALL_AUTHORIZED` / `CALL_EXTRACTED` 负载 | 【已确认】 |
| 事件类型登记 | `EventType:47-77` + `LEGACY_EVENT_ALIASES:78-110`；**护栏测试** `tests/test_collab_v3_event_literals.py:85`（AST 扫描：任何被 emit 的字面量必须能映射到 canonical type） | 新增类型必须同步登记，否则护栏红灯 | 【已确认】 |
| 事件表是否要迁移 | `schema.sql:domain_events:52-66`：`type TEXT NOT NULL`，**无 CHECK/枚举约束**；`EventStore.append:32-35` 不做类型校验 | 新事件类型**不需要 migration** | 【已确认】 |
| 试运行 run 记录 | `trial_runs.start_run:106-131`：`run_id/task_id/session_id/script_name/script_hash/account/status/started_at/ended_at/stop_frame[/main_turn_id]` | **无** ticket / source span 字段 | 【已确认】 |
| 试运行事件类型 | `trial_runs.EVENT_KINDS:369-375` 是**封闭 5 项**（`vision.match_completed` / `input.click_completed` / `frame.captured` / `runtime.exception` / `script.note`） | 要记"执行事件↔ticket↔span"需扩集合或塞 payload | 【已确认】 |
| receipt 落盘形态（可复用的先例） | `collaborator_v3/runtime/validation_store.py:22-75`：`_task_dir` / `receipt_path` / `save_receipt` / `get_receipt` / `latest_receipt` / `list_receipts`（一 receipt 一文件，**无 SQLite 新表**） | ticket 存储可照此形状 | 【已确认】 |
| 调用点→ticket 映射 | receipt 无字段、事件无类型、trial_runs 无字段 | **无处可存，必须新增** | 【已确认】 |

### Q8 materializer 注入 vs LLM 源码如何区分？

结论：**今天靠"文本分段"区分（bundled = 模型可写，artifact = 含注入），没有"注入块的行范围/身份凭证"。**

| 项 | 证据 | 状态 |
| --- | --- | --- |
| 注入实现 | `pseudo_codegen.inject_pseudo_record_for_trial:161`、`_inject_into_do_work:95`、`strip_pseudo_record_for_save:210` | 【已确认】 |
| 编译失败回退 | `script_artifact.TRANSFORM_PSEUDO_FALLBACK:60` | 【已确认】 |
| 分段语义 | `ArtifactBuild` docstring（`script_artifact.py:70-78`）：策略/AST 检查跑 `bundled`，import/fake-runtime 冒烟跑 `artifact` | 【已确认】 |
| 变换留痕 | `build.transforms`（`script_artifact.py:57-60`、`ArtifactBuild:100-104`） | 【已确认】 |
| ban 区标记 | `BAN_ZONES_MARKER="_COLLAB_BAN_ZONES"`（`script_artifact.py:54`）+ `ban_blocked` | 【已确认】 |
| "注入块的行区间 / 调用点级身份" | 无任何实现 | 【已确认】缺失 |
| 对 Phase 2 的直接含义 | 若按源码提取调用点做对账，**必须选 `bundled`（模型可写文本）**；`artifact` 只用于冒烟与真实运行。否则系统注入的伪记录会被当成 LLM 幻觉（正是 §16-14 要防的） | 【推断】 |

### Q9 §16 的 16 项必须先写红的测试，现有覆盖到哪些？

结论：**4 项已有等价覆盖，4 项部分覆盖，8 项完全缺失。**

| # | §16 原文（`design_local_api_contract_ticket.md:336-351`） | 现有覆盖 | 状态 |
| --- | --- | --- | --- |
| 1 | 不存在的本地方法无法取得 ticket | 无 ticket 概念；最接近的是 `API_UNKNOWN`（`validator.py:39`、`_check_browser_call:715`） | ❌ 缺 |
| 2 | LLM 伪造 ticket ID 或字段不能通过完整性检查 | 无 ticket、无完整性校验（`integrity` 全仓零命中） | ❌ 缺 |
| 3 | `UserWindow` 与 `UserBrowser` 同名异签名按 profile 区分 | 已有等价：`tests/test_api_contract_profile_policy.py`、`tests/test_api_contract_runtime_drift.py` | ✅ 有等价 |
| 4 | 登记代理链可以解析，未登记代理不能 | 部分：只有 `proxied` 标记层测试（`tests/test_api_contract_registry.py:72`、`tests/test_api_contract_runtime_drift.py:77`）；**链解析无实现无测试** | ⚠️ 部分 |
| 5 | 子类覆盖不能误用父类 ticket | 无 MRO/覆盖实现与测试 | ❌ 缺 |
| 6 | `bb = browser` 继承来源，重新赋值后来源失效 | 已有等价：`tests/test_api_contract_validator_aliases.py`（含"不解析元组解包"的负例） | ✅ 有等价 |
| 7 | 动态 `getattr` 不能绕过 | 缺：`getattr` **不在** `python_policy.BANNED_BUILTINS:107-121`（该集合为 `__import__, breakpoint, compile, eval, exec, exit, globals, input, locals, open, quit`），validator 也无 `getattr(...)` 规则，属性链接收者直接跳过（`validator.py:610`） | ❌ 缺 |
| 8 | 本地类内部未批准方法不能取得 ticket | 部分：`API_NOT_APPROVED`（`validator.py:40`、判定 `:739`）+ `tests/test_api_contract_profile_policy.py`；但无"脚本内部函数/本地类方法"这一分类 | ⚠️ 部分 |
| 9 | 无 ticket 调用被报 `UNREGISTERED_API_CALL` | 该码全仓零命中 | ❌ 缺 |
| 10 | Registry/profile/素材/权限变化使相关 ticket 失效 | 部分等价（receipt 层）：`receipt.mismatches:182`、`is_valid_for:260`，测试 `tests/test_api_contract_materializer.py:217`、`:257`、`:303-313`；ticket 层缺 | ⚠️ 部分 |
| 11 | ticket 合法仍不能掩盖 `len(MatchResult)` | `TYPE_NOT_SIZED` 与整个数据流层零命中（§10 未实现） | ❌ 缺 |
| 12 | 循环多次执行复用调用点 ticket 但生成多条执行事件 | 无 ticket；执行事件侧 `trial_runs.EVENT_KINDS:369-375` 本身是逐次记录 | ❌ 缺 |
| 13 | source 或 artifact 变化后旧 receipt 失效 | 已有等价：`tests/test_api_contract_materializer.py:303-313`、`tests/test_collab_final_artifact_gate.py` | ✅ 有等价 |
| 14 | 系统 materializer 注入调用不被误判为 LLM 幻觉 | 部分：分段与"检查跑 bundled"已存在（`script_artifact.py:70-78`），但"注入调用不要求 ticket"的显式测试无处安放（要等 ticket 存在） | ⚠️ 部分 |
| 15 | API 对账通过不会自动令需求验收通过 | 缺：GUI 无三层状态展示（§14 未实现） | ❌ 缺 |
| 16 | 同一脚本在协作入口与最终交付入口得到相同结论 | 部分：`tests/test_undefined_name_check_parity.py`（未定义名检查一致）；**整体结论一致性**无专项测试 | ⚠️ 部分 |

### Q10 Phase 1 最小改动文件清单 / 数据流 / 测试清单？

见本报告 §5「Phase 1 最小实现边界」。核心事实依据（也是最大风险）：

| 项 | 证据 | 状态 |
| --- | --- | --- |
| V3 **确实已经**在用 `api_contracts` Registry —— 但只用于**脚本校验**，不用于给模型看 | `runner.py:230`（`_apply_execution_target` 用 `has_profile`）、`runner.py:247-248`（`_validation_profile` 用 `profile_for_code`）、`runner.py:2093-2102`（receipt 里写 `api_contract_revision=contract_registry.contracts_revision()`） | 【已确认】 |
| V3 的 capability 事实源与 `api_contracts` 是**两套** | `collaborator_v3/adapters/v2_capabilities.py:sync_v2_capabilities:78` 从 **V2 工具表** `collaborator.llm_turn.all_collab_tools()`（`:79`）造 `CapabilityRecord`（`:59`）；skills 走 `adapters/skill_capabilities.py:134`。二者都不读 `api_contracts` | 【已确认】 |
| 因此"从唯一 Registry 生成 ticket"（§15 Phase 1）在 V3 需要**新增一条把 api_contracts 暴露给模型的入口**，因为现在模型看到的 `capability.*` 不是方法契约 | 同上两条 | 【推断，依据已确认】 |

---

## 2. 已有能力复用表

| 需要的能力 | 直接复用 | 复用方式 |
| --- | --- | --- |
| 契约唯一事实源 | `api_contracts/schema.py:ApiContract:95-121`、`Parameter:33-48`、`ReturnContract:73-87` | ticket 只存 `capability_id` + `contract_revision`，事实回查 Registry |
| 契约版本 | `registry.contracts_revision:342` | ticket `contract_revision`；receipt 已在用（`runner.py:2102`） |
| profile 权威解析 | `execution_profile.py:ProfileResolution:41`、`resolve_execution_profile:147` | ticket `runtime_profile/profile_source` |
| 安全交集 | `registry.PORTABLE_PROFILE:45`、`effective_profile:228` | 无权威事实时的 ticket 口径 |
| 接收者/别名解析 | `validator._receiver_aliases:521-564`、`_resolve_receiver:567-574`、`DEFAULT_TARGET_NAMES:67` | Phase 2 调用点提取的起点（继续增强，不另写一套） |
| 参数/await/契约比对 | `validator._check_browser_call`（`:694` 起）、`_policy_issues_for_call:475`、`_check_return_semantics:851` | 对账第 4/5 步的现成实现 |
| 稳定诊断码形态 | `validator.py:35-61` + `ContractIssue:83-105`（`code/line/symbol/contract_id/allowed_parameters/repair_hint/severity`） | §9 的 9 个码应落在同一形态；`PERMISSION_SCOPE_MISMATCH` 已在 `collaborator_v3/domain/errors.py:31` |
| 单一 source→run-text 流水线 | `script_artifact.materialize_trial_script:253`、`write_materialized:211`、`ArtifactBuild:67-117` | 调用点提取以 `bundled` 为基准；hash 直接取 `*_sha256` |
| receipt 结构 + 落盘 | `api_contracts/receipt.py:ValidationReceipt:59-92`、`collaborator_v3/runtime/validation_store.py:22-75` | 调用点映射/ticket 以新增字段挂 receipt；ticket 存储照 `validation_store` 形状 |
| 事件存储（不建第二张表） | `collaborator_v3/events/schema.py:EventEnvelope:133-151`、`events/store.py:EventStore:24-59`、`stores/schema.sql:domain_events:52-66` | 三类记录走既有 `domain_events`，`type TEXT` 无枚举约束 ⇒ 无迁移 |
| 权限/授权（**不要重造**） | `collaborator_v3/domain/contracts.py:PermissionGrantRecord:49-63`（`actions`/`resource_refs`/`environment_fingerprint`/`target_fingerprint`/`expires_*`/`revoked_at`）、`stores/sqlite_store.py:get_grant:533`/`consume_single_grants:561`、`runtime/capabilities.py:58-75` | `CALL_AUTHORIZED`（§5.2）在 V3 事实上已存在 → 应复用而非新建；§12 要求"不合并"，但必须是**关联**而非**重定义** |
| 查询级"模型看过什么" | `EventType.QUERY_COMPLETED:57`、`runner.py:1632-1642` | `CONTRACT_VIEWED` 的既有近似物；扩 payload 即可承载 `view_event_id` |
| 护栏测试 | `tests/test_collab_v3_event_literals.py:85` | 新增事件类型时必须同步登记 |
| 分层检查的既有切分 | `validator.validate_source:255`（策略/AST）vs `script_artifact.verify_artifact:584`（冒烟/交付） | §14/§3 的"三层不合并"已有物理对应物 |

---

## 3. 缺口表

| # | 缺口 | 现状证据 | 影响 Phase |
| --- | --- | --- | --- |
| G1 | V3 没有任何 `api.*` 查询，模型看不到方法契约 | `queries.py:QUERY_NAMES:1431` 无 `api.*`；V3 API 事实是手写提示词 `provider_model.py:173-177` | P1（阻塞） |
| G2 | `CONTRACT_VIEWED` 无载体 | payload 只有 `name/query_key/ok/empty/refs`（`runner.py:1635-1641`） | P1 |
| G3 | ticket 模型完全不存在 | `integrity`/`resolution_chain`/`receiver_type`/`nullable_fields` 全仓零命中 | P1 |
| G4 | `return_model.iterable/sized/nullable_fields` 在 Registry 里不存在 | `schema.py:ReturnContract:73-87` | P1/P3 |
| G5 | `receiver_type` 无法从 `owner` 派生（owner 恒为 `browser`/`portable`） | `schema.py:101`（无覆写点）、`registry.py:110` | P1 |
| G6 | 调用点→ticket 映射无处存储 | `receipt.py:59-92` 无字段；`trial_runs.start_run:106-131` 无字段；`EVENT_KINDS:369-375` 封闭 | P4 |
| G7 | 执行事件无法关联 ticket / source span | 同 G6 | P4 |
| G8 | `getattr` 动态调用既不拦也不报 | `python_policy.BANNED_BUILTINS:107-121` 无 `getattr`；`validator.py:610` 漏报属性链接收者 | P2/P3 |
| G9 | 属性链接收者漏报（静默通过） | `_receiver_unverified_issues:610` | P2 |
| G10 | MRO/Mixin/子类覆盖无实现 | `_resolve_receiver:567-574` | P2 |
| G11 | 代理链无解析、无存储 | 只有 `schema.py:proxied:118` 布尔 | P2 |
| G12 | 源码位置只有行，没有列/结束范围 | `ContractIssue.line:88`；§5.3 要求"行列范围" | P2 |
| G13 | §10 数据流/类型闸门整体缺失 | `TYPE_NOT_SIZED` 等零命中 | P3 |
| G14 | 9 个稳定码中 8 个不存在 | 只有 `PERMISSION_SCOPE_MISMATCH`（`collaborator_v3/domain/errors.py:31`） | P1–P3 |
| G15 | GUI 无三层质量状态展示 | §14 未实现 | P4 |
| G16 | 协作入口与最终交付入口结论一致性无专项测试 | 仅 `tests/test_undefined_name_check_parity.py` | P3 |

---

## 4. 重复事实源与漂移风险

| # | 重复事实 | 现有来源 | 漂移风险 | 状态 |
| --- | --- | --- | --- | --- |
| D1 | **UserWindow 方法清单** | ① `api_contracts` Registry（权威）② 手写提示词 `provider_model.py:173-177` ③ `agent.py:_none_browser_image_arg_errors:2007` 内硬编码 5 个方法名（`:2009-2012`：`click_image/click_image_effect/match_image/wait_image/match_image_multi`）④ `agent.py:_match_image_stem_from_call:6943` 内硬编码 4 个（`:6946-6948`） | ④⑤ 与 Registry 不同步时，同一个方法在不同闸门得到不同结论；方案 §18.6"不新增任何手写API白名单"针对的正是这一现状（它**已经存在**，不是新增与否的问题） | 【已确认】 |
| D2 | **profile 字面量** | `registry.LEGACY_DEFAULT_PROFILE:52`、`validator.profile_for_code:1142`（用常量）vs `validator.py:1140`（硬编码字符串 `"browser_user_browser"`） | 改名只改一处 → 静默漂移 | 【已确认】 |
| D3 | **"能力/契约"目录** | ① `api_contracts` Registry ② V3 `CapabilityRecord`（`adapters/v2_capabilities.py:78` 由 **V2 工具表**生成）③ V2 `collaborator/resource_registry.py:register_capability:237` | ticket 若从①签发、模型却从②看到事实 ⇒ 出现"看得见的不是签得出的" | 【已确认】①②；③【推断】 |
| D4 | **授权概念** | ① 方案 §5.2 `CALL_AUTHORIZED` ② V3 `PermissionGrantRecord`（`domain/contracts.py:49`，含 actions/resource_refs/expiry/revoked_at） | 两套"本地批准"并存 → §12"不得合并成一个字段"被误读成"另建一套" | 【已确认】，风险【推断】 |
| D5 | **稳定码命名** | 方案 §9 的 `UNREGISTERED_API_CALL`/`CALL_RECEIVER_UNRESOLVED`/`ARGUMENT_CONTRACT_MISMATCH` vs 既有 `API_UNKNOWN:39`/`API_RECEIVER_UNVERIFIED:56`/`API_UNKNOWN_KEYWORD:42`+`API_MISSING_ARGUMENT:43`+`API_TOO_MANY_POSITIONAL:44` | 同义不同名 ⇒ 同一错误在协作入口与交付入口显示为两个码 | 【已确认】 |
| D6 | **"模型看过什么"记录** | `query.completed`（`schema.py:57`）+ V2 的 `lookup_api`（无记录） | 两条路径的证据强度不同，若 `CONTRACT_VIEWED` 只接 V3，V2 交付将无法满足 §17"可追溯到 Registry" | 【推断】 |
| D7 | **事件类型集合** | `EventType:47-77` / `LEGACY_EVENT_ALIASES:78-110` / `trial_runs.EVENT_KINDS:369-375` | 三处需同步；护栏只管第一、二处（`tests/test_collab_v3_event_literals.py:85`），`EVENT_KINDS` 无护栏 | 【已确认】 |
| D8 | **hash 语义** | `ArtifactBuild` 三个 `*_sha256`（`script_artifact.py:99-113`）+ receipt 的 `source/bundled/artifact`（`receipt.py:59-92`）+ validation_store 落盘 | 已有真实断言（`tests/test_api_contract_materializer.py:303-313`）⇒ 风险低 | 【已确认】 |

---

## 5. Phase 1 最小实现边界（建议，不含实现）

### 5.1 必须新增

| 文件 | 内容 | 依据 |
| --- | --- | --- |
| `api_contracts/ticket.py`（新增） | ticket 值对象（§6 字段）+ `schema_version` + 完整性计算/校验 + 失效判定（§11：profile/revision/env/素材/权限/source 变化） | §6、§11 |
| `api_contracts/registry.py`（增量） | `ReturnContract` 增加 `iterable/sized/nullable_fields`（唯一事实源内扩展，**不另建类型表**） | §10、§17"不建立新的类型事实源" |
| `collaborator_v3/runtime/ticket_store.py`（新增） | 照 `validation_store.py:22-75` 形状：`save_ticket/get_ticket/list_tickets/latest_ticket`，一票一文件（**不新建 SQLite 表**，避免迁移） | §15 P1"定义存储" |
| `collaborator_v3/runtime/queries.py`（增量） | 新增契约详情查询（建议 `api.get`，与既有 `capability.get` 区分），返回"`ticket_id` + 准确契约 + 必要解释"，并在成功时写 `CONTRACT_VIEWED` | §5.1、§15 P1、§6 末段 |
| `collaborator_v3/events/schema.py`（增量） | `EventType` 增三类记录；同步 `LEGACY_EVENT_ALIASES`（否则 `tests/test_collab_v3_event_literals.py:85` 红灯） | §5 |
| `collaborator_v3/adapters/provider_model.py`（增量） | `:173-177` 手写方法清单**必须删除或改为引用查询结果**，否则违反 §18.6 且与 ticket 口径冲突 | §18.6 |

### 5.2 明确不做（Phase 1）

- 不接交付硬闸（§15 P1 末条）：`verify_artifact:584` / `materialize_trial_script:253` 本轮不改行为；
- 不做 AST 调用点提取与对账报告（Phase 2，`validator._receiver_aliases:521` 本轮不动）；
- 不做接收者属性链/MRO/代理链解析（Phase 2）；
- 不做 §10 类型/数据流闸门（Phase 3）；
- 不把 ticket 写进 receipt、不关联试运行事件、不碰 GUI（Phase 4）；
- 不合并 ticket 与 `PermissionGrantRecord`（§12），只做 ID 关联。

### 5.3 数据流（Phase 1 形态）

```
LLM 查询契约（新查询）
  → Registry 取 ApiContract（唯一事实源，contracts_revision()）
  → 写 CONTRACT_VIEWED（domain_events，携带 view_event_id）
  → 签发 ticket（ticket_store，落 ticket_id + integrity）
  → 上下文只回 ticket_id + 准确契约 + 解释
（无硬闸：源码里没有 ticket 时不拒绝，只做失效/查询接口）
```

### 5.4 复用约束

- ticket 不复制方法表（§15 P1、§17"不存在新的独立API白名单"）；
- 三层不合并（§3、§14）：架构真实性（ticket）/ 代码可运行性（`validator`）/ 需求符合性（`EVIDENCE_*` 码）；
- 全部新增走既有 `domain_events`（`stores/schema.sql:52-66` 无枚举约束 ⇒ 无 migration）。

---

## 6. 必须先写红的测试（Phase 1 切片）

严格按 §16，Phase 1 只能先红它覆盖得到的那几项（其余留给对应 Phase，不要在 Phase 1 抢跑）：

| 测试 | 对应 §16 | 建议落点 |
| --- | --- | --- |
| 不存在的本地方法取不到 ticket | #1 | 新文件 `tests/test_api_contract_ticket_issue.py` |
| 伪造 ticket_id / 篡改字段过不了完整性 | #2 | 同上 |
| profile 变化使 ticket 失效 | #10（ticket 层切片） | 同上（receipt 层等价物已有：`tests/test_api_contract_materializer.py:217`） |
| `CONTRACT_VIEWED` 只由"成功取到当前契约"产生，且带 `view_event_id` | #9 的前置 | 同上 + 复用 `tests/test_collab_v3_event_literals.py:85` 的登记护栏 |
| ticket 只引用 Registry，不自带方法表 | §17 不变量 | 新文件 `tests/test_api_contract_ticket_single_source.py` |

**Phase 2/3/4 才可红的**（本报告只登记，不在 Phase 1 写）：#4 代理链、#5 子类覆盖、#7 `getattr`、#8 内部方法、#9 `UNREGISTERED_API_CALL`、#11 `TYPE_NOT_SIZED`、#12 循环与多执行事件、#14 注入不误判、#15 三层分层、#16 双入口一致。

**已有等价覆盖可直接转正的**：#3（`tests/test_api_contract_profile_policy.py`）、#6（`tests/test_api_contract_validator_aliases.py`）、#13（`tests/test_api_contract_materializer.py:303-313`、`tests/test_collab_final_artifact_gate.py`）。

---

## 7. 需要拍板的事项（需用户决策，本报告不擅自选择）

1. **V3 是否新增契约查询**（如 `api.get`）。不新增则 §15 Phase 1 的 `CONTRACT_VIEWED` 在协作路径上无法产生（G1/G2 阻塞）。
2. **ticket 存储位置**：文件（照 `validation_store.py:22-75`，无迁移）还是 SQLite 新表（需 migration）。方案 §6 两可，但影响 Phase 1 体量。
3. **`capability_id` 命名**：沿用 `ApiContract.id`（如 `desktop_user_window.match_image`，`render.py:52`）还是 §6 示例风格（`runtime.image.match_one`）？需映射约定。
4. **`receiver_type` 的真实类名从哪来**：Registry `owner` 恒为 `browser`/`portable`（`schema.py:101`、`registry.py:110`）⇒ 是否在 `ApiContract` 新增 `receiver_type` 字段（改唯一事实源的数据模型）。
5. **`ReturnContract` 是否允许扩字段**（`iterable/sized/nullable_fields`）。§10 要求"不建立新的类型事实源"，因此只能加在这里。
6. **对账基准文本**：按 `bundled`（模型可写）还是 `artifact`（含系统注入）提取调用点。本报告【推断】建议 `bundled`（依据 `script_artifact.py:70-78`），需确认。
7. **9 个稳定码的命名冲突处理**：`CALL_RECEIVER_UNRESOLVED` 与既有 `API_RECEIVER_UNVERIFIED:56` 语义几乎重合、`ARGUMENT_CONTRACT_MISMATCH` 与既有三个 `API_*` 参数码重合 ⇒ 是复用旧码、还是新旧并存（会造成 D5 漂移）。
8. **`CALL_AUTHORIZED` 与 `PermissionGrantRecord` 的关系**：关联（推荐，符合 §12）还是另立一套（会形成 D4 重复事实源）。
9. **V2/经典入口是否纳入**：`agent.py:9947/10681/10751/10868` 目前连 `profile` 都不传（Q5），若只做 V3，§17"所有本地调用均可从最终 artifact 追溯"在 V2 交付上无法成立。
10. **`provider_model.py:173-177` 手写清单的处置**：删除、还是改为占位并强制走查询（§18.6 不允许它继续存在）。
11. **Phase 1 是否碰 GUI**：§14/Phase 4 才要求展示，建议 Phase 1 不碰。

---

## 8. 本轮结束声明

- 本轮只做只读审计，产出本报告；**未修改任何实现、测试、配置或既有设计文档**（含 `design_executable_api_contract.md` 与 `design_executable_api_contract_phase0.md`）。
- 未进入 Phase 1，未运行测试，未修复本报告 §3/§4 列出的任何缺口（含顺手发现的 D1 手写清单、D2 profile 字面量）。
- 到此停止，等待确认后再决定下一步。
