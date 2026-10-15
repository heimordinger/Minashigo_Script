# 本地 API 契约凭据（ticket）—— Phase 1 实施报告

- **范围**：严格按 `design_local_api_contract_ticket.md` §15 Phase 1 与人工决定的 11 条执行
- **状态**：**已完成**（先写失败测试 → 93 个新用例全部红/报错 → 再改实现 → 全绿），**未进入 Phase 2**
- **红线遵守**：未改 `design_executable_api_contract.md` / `design_executable_api_contract_phase0.md`；
  未做 AST 对账、未接交付硬闸、未改 GUI、未扩展 receipt、未顺手修无关历史缺陷
- **测试基线**：`api_contract` 154 passed ｜ 协作（collab）**982 passed / 1 skipped** ｜
  全量 `tests/` **1329 passed / 41 failed / 28 skipped**（41 = 既有无关的 `test_sgk_*.py` 两文件，见 §7.3）

---

## 1. 对 11 条决定的落实对照

| # | 决定 | 落点 | 证据 |
| --- | --- | --- | --- |
| 1 | V3 新增 `api.get` / `api.get_many`，只从 Registry 渲染 | `queries.py:name="api.get":1384`、`name="api.get_many":1407`、`_exec_api_get:563`、`_exec_api_get_many:619` | `tests/test_collab_v3_api_query.py`（12 例） |
| 1 | 调用成功后由**本地**记 `CONTRACT_VIEWED`，不靠 LLM 回填 | `ticket_service.py:view_contracts:143` → `_emit:386` | 同上 + `tests/test_api_contract_ticket_service.py::test_events_only_reference_the_ticket_never_copy_it` |
| 2 | ticket 用 SQLite 独立表 + 最小 migration | `stores/schema.sql:250`（表）、`:291-294`（索引）、`sqlite_store.py` migration 6 `:175` | `tests/test_collab_v3_ticket_store.py`（10 例，含老库补表） |
| 2 | 时间线事件进既有事件表，只引用 `ticket_id` | `events/schema.py:CONTRACT_VIEWED:77`、`CALL_AUTHORIZED:78`；payload 只含 id | `test_api_contract_ticket_service.py`（断言 payload 里没有 `integrity`/`return_model`/`parameters_allowed`） |
| 3 | `capability_id` 直接用 `ApiContract.id`，不造第二套命名 | `ticket.py:build_ticket:248`（`capability_id=contract.id`） | `test_api_contract_ticket_model.py::test_build_ticket_derives_identity_from_the_registry_only` |
| 4 | Registry 增加**复数** `receiver_types`，ticket 记单一 `receiver_type` | `schema.py:receiver_types:146`、`registry.py:_stamp_receiver_types:164`、`registry.py:receiver_types():351` | `tests/test_api_contract_receiver_types.py`（7 例） |
| 4 | 字段必须由 Registry 提供，签发器不得猜类名 | `ticket.py:build_ticket:248` 校验候选，否则 `ValueError`；`ticket_service._single_receiver:268` 多候选时**不签发** | `test_build_ticket_rejects_a_receiver_type_the_registry_does_not_declare`、`test_unresolved_profile_records_the_view_but_issues_nothing` |
| 5 | 扩展 `ReturnContract` 的机器可读字段，成为唯一事实源 | `schema.py:iterable:101 / sized:103 / mapping:105 / nullable:107 / nullable_fields:111 / machine_facts():125` | `tests/test_api_contract_return_facts.py`（18 例） |
| 6 | 对账以 `bundled` 为基准、注入块留来源与行段 | **本 Phase 未做**（属 Phase 2）；本 Phase 未改 materializer | 见 §9 |
| 7 | 复用既有诊断码，不建同义双码 | 只新增 3 个：`validator.py:API_TICKET_MISSING:61 / API_TICKET_EXPIRED:62 / API_TICKET_MISMATCH:63`；接收者仍用 `API_RECEIVER_UNVERIFIED`、参数仍用既有 `API_*` 参数码、权限沿用 `PERMISSION_SCOPE_MISMATCH` | `test_api_contract_ticket_model.py::test_new_diagnostic_codes_are_registered_as_stable_codes` |
| 8 | `CALL_AUTHORIZED` 关联 `PermissionGrantRecord` 但不合并 | ticket 只存 `permission_id` + `permission_scope` + `resource_scope` 摘要（`ticket.py:ContractTicket:97`）；`CALL_AUTHORIZED` payload 带 `permission_id` | `test_permission_grant_is_linked_not_copied`（并断言签发**不会**顺手造一条 grant） |
| 9 | V2 与 V3 共用同一签发服务 | 服务类 `ticket_service.py:TicketService:115`；V3 走 `queries._ticket_service:485`，V2 走 `api_catalog.record_contract_view` | §5 + `test_v2_lookup_and_v3_query_share_one_service` |
| 10 | 删除 V3 手写 API 事实清单，改由 Registry 紧凑索引 + `api.get/get_many` | `provider_model.py:173-180`（事实删除）、`_registry_api_index():198`、`_SYSTEM_PROTOCOL` 组装 | `tests/test_collab_v3_provider_api_index.py`（5 例） |
| 11 | Phase 1 不修改 GUI | 未动 `gui/` 下任何文件 | 修改文件清单（§2）无 `gui/` |

---

## 2. 修改文件清单

### 新增（实现，3 个）

| 文件 | 内容 |
| --- | --- |
| `backend/script_generator/api_contracts/ticket.py` | ticket 值对象、id 生成、内容摘要、`build_ticket`、`presented_matches`、`evaluate_ticket` |
| `backend/script_generator/api_contracts/ticket_service.py` | 公共 `TicketService`（签发/查询/失效/评估）、`ContractViewResult`、进程默认服务登记 |
| `backend/script_generator/collaborator_v3/runtime/ticket_store.py` | `SqliteTicketStore` 适配器、`service_for_store`、`register_default_service`、`ensure_default_service` |

### 新增（测试，8 个文件 / 93 例）

| 文件 | 例数 | 覆盖 |
| --- | --- | --- |
| `tests/test_api_contract_return_facts.py` | 18 | 返回模型机器可读事实 + 真实类核对 + 语义 revision |
| `tests/test_api_contract_receiver_types.py` | 7 | `receiver_types` 来源与 portable 并集 |
| `tests/test_api_contract_ticket_model.py` | 16 | 模型/摘要/篡改/失效判定/新诊断码 |
| `tests/test_api_contract_ticket_service.py` | 17 | 签发、事件引用、权限关联、失效、V2↔V3 共服务 |
| `tests/test_api_contract_ticket_v2_lookup.py` | 8 | V2 入口接线与"没服务也能用" |
| `tests/test_collab_v3_ticket_store.py` | 10 | 表结构、migration 6、CRUD、主键、幂等 |
| `tests/test_collab_v3_api_query.py` | 12 | `api.get`/`api.get_many` 端到端 |
| `tests/test_collab_v3_provider_api_index.py` | 5 | 手写清单消失 + Registry 索引逐字一致 |

### 修改（16 个）

| 文件 | 改动 |
| --- | --- |
| `api_contracts/schema.py` | `ReturnContract` 增 5 个机器可读字段 + `facts_declared`/`machine_facts`；`ApiContract.receiver_types` + `receiver_type()`；语义载荷纳入二者 |
| `api_contracts/registry.py` | `_stamp_receiver_types:164`、`_merge_return:69`（portable 保留两端一致的事实）、`receiver_types():351` |
| `api_contracts/desktop_user_window.py` | 15 条契约补真实返回事实（其余 6 条 `Any` 保持未知） |
| `api_contracts/browser_user_browser.py` | 13 条契约补真实返回事实（2 条 `Any` 保持未知） |
| `api_contracts/render.py` | `detail()` 的 `returns` 带机器事实；新增 `usage_facts_line`；`card_text` 增一行"返回用法" |
| `api_contracts/validator.py` | 新增 3 个 ticket 状态码（`:61-63`） |
| `api_contracts/__init__.py` | 导出 `ticket`/`ticket_service`/`receiver_types`/`usage_facts_line` |
| `api_catalog.py` | `lookup_api` 记 VIEW（`record_contract_view`），新增 `service=` 参数 |
| `agent.py` | `_ensure_ticket_service()`；`_tool_result_text(profile=…)`；`call_llm_with_tools(profile=…)`；`generate_script`/`revise_script` 入口接线 |
| `collaborator_v3/bootstrap.py` | `initialize_v3` 登记进程默认 ticket 服务 |
| `collaborator_v3/events/schema.py` | 2 个规范事件类型 + 2 条别名（`:77-78`、`:117-118`） |
| `collaborator_v3/runtime/queries.py` | `api.get`/`api.get_many` + 归一化 + 契约 profile 解析 + `CATALOG_CHARS:50` |
| `collaborator_v3/stores/schema.sql` | `contract_tickets` 表（`:250`）+ 2 个索引（`:291-294`） |
| `collaborator_v3/stores/sqlite_store.py` | migration 6（`:175`）、`put/get/list/revoke_ticket`（`:628-696`）、列清单常量 |
| `collaborator_v3/adapters/provider_model.py` | 删手写 API 事实、追加 Registry 索引、查询名单 23→25 |
| `tests/test_collab_v3_sqlite_store.py` | 迁移版本断言 `{1..5}` → `{1..6}`（依据见 §7.2） |

辅助探针（只读工具，留在 `tools\_scratch\`）：`_phase1_dump_returns.py`、`_phase1_dump_return_source.py`、`_phase1_probe_end_to_end.py`。

---

## 3. ticket 模型与签发

### 3.1 字段（设计 §6 对照）

`ContractTicket`（`ticket.py:97`）字段与 §6 一一对应，差异只有两处（均为人工决定）：

- `capability_id` = `ApiContract.id`（决定 3，不再造 `runtime.image.match_one`）；
- `receiver_type` + `resolution_chain` 来自 Registry 的 `receiver_types`（决定 4），签名器不许猜。

### 3.2 完整性为什么不需要密钥

`integrity = "sha256:" + sha256(规范化 JSON(ticket 字段，除 revoked_*))`（`ticket.py:_digest:91`）。
摘要算法公开，所以**摘要本身不防伪造**；防伪造靠"本地那一行才是权威"：

- `TicketService.verify`（`:309`）**只接受 ticket_id**，必须先 `store.get_ticket()` 取到行；
- 库里没有这一行 ⇒ `API_TICKET_MISSING`（LLM 自己拼的票，哪怕摘要自洽也过不了）；
- 拿真票改字段 ⇒ `presented_matches`（`:316`）逐项对不上 / 摘要对不上 ⇒ `API_TICKET_MISMATCH`；
- `ticket_id` / `integrity` / `revoked_*` 属于身份与本地状态，**不接受**调用方指定（`_PRESENTED_READONLY:68`）。

> 上行：将来若要**离线**校验（不查库），再把摘要换成 HMAC（本地密钥）即可，字段语义不变。
> Phase 1 不做密钥管理：那会引入一份新的本地秘密要保护，而当前威胁模型里库才是边界。

### 3.3 签发输入 / 输出示例（真实探针输出）

输入（V3 侧，真实 `ActionRunner` + `execution_target`）：

```json
{"type": "query", "payload": {"name": "api.get_many",
  "arguments": {"names": ["match_image", "wait_image", "click_image"]}}}
```

执行环境：`runtime_profile=desktop_user_window`、`profile_source=runtime_instance`、
`target_revision=target_rev_1`、`main_turn_id=mt_20260929T000000Z_ABCDEF`。

输出（节选，`tools\_scratch\_phase1_probe_end_to_end.py` 实跑）：

```json
{"runtime_profile": "desktop_user_window", "profile_source": "runtime_instance",
 "receiver_type": "UserWindow",
 "contracts_revision": "sha256:79d5a2f53845740060532c25001867a39726bc96c8cf1114e41ce460a3671b37",
 "ticket_ids": ["ct_f7c84a510e03604d", "ct_589eae6c1551c86a", "ct_0efabd1dbf9b3aa0"],
 "ticket_skipped_reason": "", "missing": [], "rejected": []}
```

落库行（一张）：

```json
{"ticket_id": "ct_f7c84a510e03604d", "schema_version": 1,
 "main_turn_id": "mt_20260929T000000Z_ABCDEF", "view_event_id": "evt_7f4c5084a0e1493d",
 "contract_revision": "sha256:79d5…1b37", "capability_id": "desktop_user_window.match_image",
 "receiver_type": "UserWindow", "requested_method": "match_image", "resolved_owner": "browser",
 "runtime_profile": "desktop_user_window", "profile_source": "runtime_instance",
 "call_kind": "async_method", "resolution_chain": ["UserWindow.match_image"],
 "environment_revision": "target_rev_1", "permission_id": "",
 "parameters_allowed": ["cheap","force_wide","img_path","match_mode","match_select","pixel_tol",
                        "quiet","threshold","use_color_check","use_hotspot_roi","vision_priority"],
 "return_model": {"type": "MatchResult | falsy", "iterable": true, "sized": false,
                  "mapping": false, "nullable": false,
                  "nullable_fields": ["x","y","match_success","width","height","scale"],
                  "identity_check_unsafe": true},
 "integrity": "sha256:cf383351407630753e6fe82afab2510b1f0d4294ab6c97756469ba7826cf879f"}
```

同一轮的时间线（`domain_events`，只引用 id）：

```
contract.viewed   | {"capability_ids": ["desktop_user_window.match_image", …, "…click_image"],
                     "contract_revision": "sha256:79d5…", "runtime_profile": "desktop_user_window",
                     "profile_source": "runtime_instance", "receiver_type": "UserWindow",
                     "missing": [], "rejected": []}
call.authorized   | {"ticket_ids": ["ct_f7c8…", "ct_589e…", "ct_0efa…"], "permission_id": "",
                     "capability_ids": [...], "runtime_profile": "desktop_user_window", ...}
```

校验：

```
ct_f7c84a510e03604d -> ok            （3 张都 ok）
ct_ffffffffffffffff -> missing  API_TICKET_MISSING   （伪造 id）
```

---

## 4. 存储与 migration

### 4.1 表结构

`stores/schema.sql:250` 建 `contract_tickets`（24 列，`ticket_id` 主键），
`:291-294` 建 `ix_tickets_turn(main_turn_id, issued_at)` 与
`ix_tickets_capability(capability_id, runtime_profile)`。

设计取舍：

- **可空列写空串必须落 NULL**（`sqlite_store._NULLABLE_TICKET_COLUMNS:93`）：
  否则 `revoked_at IS NULL` 这类判定会静默失效 —— 这正是"字段缺失 ≠ 字段为空"那个老坑；
- **失效是粘性的**：`put_ticket` 的 upsert 用 `COALESCE(excluded.revoked_at, 现有值)`
  （`sqlite_store.py:628` 起），重复签发同一张票不会把撤销状态抹掉；
- 列清单只有一份（`_TICKET_COLUMNS:64`），避免两个写入口慢慢漂移。

### 4.2 migration 6

`SqliteDomainStore.initialize()`（`sqlite_store.py:175`）：表与索引由 `schema.sql` 的
`CREATE … IF NOT EXISTS` 建（老库同样生效），migration 块只登记版本号 6。
`initialize()` 幂等，可重复执行。

### 4.3 回滚策略

回滚代码即可，**不需要**动数据：

```sql
-- 可选：清掉凭据表与版本号（不回滚也不影响旧代码：旧代码不读这张表、不读版本 6）
DROP TABLE IF EXISTS contract_tickets;
DELETE FROM schema_migrations WHERE version = 6;
```

- 旧代码（无 ticket 支持）遇到遗留的 `contract_tickets` 表与版本 6 行**不会报错**：
  `initialize()` 只做 INSERT/ALTER，从不校验最大版本；
- ticket 数据是**本地审计事实**，不是用户数据：删表只丢"当时签过哪些票"，不影响任务/脚本/事件；
- 唯一要注意的是：回滚后 `tests/test_collab_v3_sqlite_store.py` 的 `{1..6}` 断言需要一起回滚。

---

## 5. V2 / V3 如何证明共用同一服务

```
                 ┌──────────────────────────────────────────────┐
V3  api.get ────▶│  queries._ticket_service(ctx)  (:485)        │
                 │   ├─ 若进程默认服务就绑在同一个库 → 复用它      │
                 │   └─ 否则 service_for_store(ctx.store)        │
                 ├──────────────────────────────────────────────┤
V2  lookup_api ─▶│  api_catalog.record_contract_view()          │
                 │   └─ ticket_service.default_service()         │
                 └───────────────┬──────────────────────────────┘
                                 ▼
        api_contracts.ticket_service.TicketService.view_contracts()   ← 唯一签发实现
                                 ▼
   SqliteTicketStore ─▶ contract_tickets 表 ｜ EventStore.emit ─▶ domain_events
```

三条测试证据：

1. `test_api_contract_ticket_service.py::test_v2_lookup_and_v3_query_share_one_service`：
   对同一个服务实例挂 spy，先调 V2 `lookup_api(..., service=service)`、再调 V3
   `api.get` 执行器，断言 `view_contracts` 被调用 **2 次**，且两次落库的
   `capability_id` 集合相同、`contract_revision` 相同；
2. `test_api_contract_ticket_v2_lookup.py::test_bootstrap_registers_the_default_service`：
   `bootstrap.initialize_v3()` 之后，V2 的 `lookup_api` 写进的是**同一个库**；
3. `test_api_contract_ticket_v2_lookup.py::test_agent_tool_dispatch_goes_through_the_shared_service`：
   V2 的工具分发点（`agent._tool_result_text`）也必须留痕。

接线位置：V3 = `bootstrap.initialize_v3`（生产由 `adapters/gui_bridge.py:154` 调用）；
V2 = `agent._ensure_ticket_service()`（`generate_script` / `revise_script` 入口各调一次）。

---

## 6. Registry 新增字段的真实来源

| 新字段 | 真实来源 | 说明 |
| --- | --- | --- |
| `ApiContract.receiver_types` | `ProfileContracts.target_class`（`desktop_user_window.py:524`=`UserWindow`、`browser_user_browser.py:532`=`UserBrowser`），在 `registry._stamp_receiver_types:164` 盖章 | 这个事实**本来就被核验**：`verify_runtime` 用 `target_module` + `target_class` 对真实类做 `inspect.signature` 比对；没有新造第二张表。portable 交集盖两个候选（`registry.py:243-248`） |
| `ReturnContract.iterable` | 真实类是否实现 `__iter__`（或序列） | `core/match/match_result.py:17` 有 `__iter__` ⇒ `match_image` `iterable=True` |
| `ReturnContract.sized` | 真实类是否实现 `__len__` | 同上文件**没有** `__len__` ⇒ `sized=False`（设计 §10 的 `len(MatchResult)` 场景） |
| `ReturnContract.mapping` | 是否 Mapping（`dict` 类） | `dict | None` ⇒ True；`MatchResult` 有部分 `__getitem__` 但**不是** Mapping ⇒ False |
| `ReturnContract.nullable` / `nullable_fields` | 返回类型里的 `| None` 与字段的 `Optional[...]` | `update_frame -> frame | None` ⇒ `nullable=True`；`MatchResult` 对象永不为 None，但 `x/y/match_success/width/height/scale` 可为 None ⇒ `nullable=False` + `nullable_fields=[…]` |
| `Any` 类型 | **保持未知**（四个字段全 `None`） | 未知不等于"确定不是"：`release_fps`/`request_fps`/`goto`/`dmm_login`/`move`/`mouse_move` 都是 `Any`，不许登记成假事实 |

三态语义写进了 `schema.py:73-131` 的文档，并由
`test_every_registered_return_model_is_coherent`（覆盖 54 条契约）强制自洽：
不许三态混用、未知不许带 `nullable_fields`、`None` 类型必须四项全 False、
`list[...]`/`dict...` 必须可迭代可 len、`bool` 必须不可迭代不可 len。

portable 的合并规则（`registry._merge_return:69`）：类型已由 `_merge_portable` 确认相同，
机器事实**只在两端一致时保留**（不一致落回 `None`=未知），`nullable_fields` 取交集 ——
不猜单端行为，但也不留空白。

### ⚠️ 连带影响（需要你知道）

`receiver_types` 与返回事实都进了 `contract_semantic_payload`（`schema.py:309`），
所以 **`contracts_revision()` 变了**。这是设计本意（§11：语义变化 ⇒ 凭据失效），
但有一个真实后果：

> **升级前签发的 `ValidationReceipt` 会失效**：`runner.py:2096-2105` 会把
> `api_contract_revision` 不符列为 reason，`finish` 会被拒（`COMPLETION_NOT_PROVEN`
> + "请重新 validate_script"）。已完成的旧交付不受影响（那是历史事实），
> 但**升级瞬间正在进行的会话**需要重新校验一次脚本。

如果不希望旧 receipt 失效，唯一的替代是**不把新事实纳入语义载荷** —— 那会让
"返回事实改了但票据仍有效"，与 §11 和决定 5 直接冲突。建议保留现状。

---

## 7. 测试结果

### 7.1 先红后绿

```
# 先写测试（8 个文件，93 例）—— 首次运行
4 个文件收集失败（ModuleNotFoundError: api_contracts.ticket 等，功能尚未实现）
其余 4 个文件：39 failed, 3 passed      ← 3 个是通过的既有护栏（target_class 已存在等）
# 实现完成后
93 passed ｜ 8 个新文件全绿
```

红灯原因都是"功能还没实现"（`ModuleNotFoundError: api_contracts.ticket` 等）与
事实缺失（`sized is None is not False`），没有一个是测试自身写错后被我改掉预期的。

### 7.2 需要修改的两个既有测试（依据）

1. `tests/test_collab_v3_sqlite_store.py:135` / `:196`：断言迁移版本集合 `== {1,2,3,4,5}`。
   新增 migration 6 后集合必然变成 `{1..6}` —— 这是**迁移清单护栏**，必须同步加入 6
   （已加注释指向 `test_collab_v3_ticket_store.py`）。这不是"改测试迁就实现"，
   而是护栏本来就要求"每加一个迁移就登记"。
2. `tests/test_api_contract_return_facts.py`（我自己写的测试）里有一条断言写错了：
   我最初写 `if nullable_fields: assert nullable is True`，但 `MatchResult` 正是
   "对象不为 None、字段可为 None" 的组合。已改为只断言 `nullable_fields ⊆ fields`，
   并在 `schema.py` 文档里把两者**相互独立**写清楚。属于测试规格修正，不是放宽实现。

### 7.3 回归

| 套件 | 结果 |
| --- | --- |
| `tests/test_api_contract_*.py`（全部） | **154 passed** |
| `tests/test_collab_*.py`（83 文件） | **982 passed / 1 skipped** |
| 上述两者合并（94 文件，最终状态复跑） | **1136 passed / 1 skipped** |
| `tests/`（全量） | **1329 passed / 41 failed / 28 skipped** |

那 41 个 failed 全部来自两个**既有、与本方案无关**的文件，根因是测试脚本期望的属性在
`scripts/yys/刷关卡.py` 里不存在（`AttributeError: module 'sgk_刷关卡' has no attribute '_percentile'`、
`module 'sgk_speed' has no attribute '_CHALLENGE_CLICK_POINT'`），文件 mtime 2026-09-21，
与本次改动无交集（Phase 0 前就已失败）。

---

## 8. 本次改动引发的一个真问题（已修）

加两个查询后 `query.catalog` 的载荷超过了默认上限（6000 B），而
`clip_summary` 的阶梯会**把列表砍到 20 条** —— 被砍掉的查询名，模型就以为它不存在
（`provider_model` 里那条"以前这里只列了最早的 13 个"的注释就是这个坑）。
已给 `query.catalog` 单独放宽到 `CATALOG_CHARS = 16000`（`queries.py:50`、`:1295`）。
这不是顺手修别的缺陷，而是**我的改动直接造成的**回归。

---

## 9. 尚未进入 Phase 2 的事项（本 Phase 明确未做）

| 项 | 现状 |
| --- | --- |
| AST 调用点提取与对账报告 | 未做。`validator` 一行未改（只加了 3 个码） |
| `CALL_EXTRACTED` 记录 | 未做（event 类型也没登记） |
| MRO / Mixin / 子类覆盖解析 | 未做 |
| 代理链解析（`resolution_chain` 多段） | 未做；当前 ticket 的链只有一段 `Receiver.method` |
| 调用点 → ticket 映射 | 未做（receipt 未扩展、无新表） |
| 交付拒绝（硬闸） | 未做：`verify_artifact` / `finish` 路径未接 ticket；`API_TICKET_*` 目前只由服务返回 |
| 返回值数据流检查（`TYPE_NOT_SIZED` 等） | 未做（事实已备好，闸门在 Phase 3） |
| receipt 扩展 | 未做 |
| GUI 三层状态 | 未做（`gui/` 一行未动） |
| `bundled` 基准与注入块行段信息 | 未做（materializer 未改） |

---

## 10. 已知限制 / 需要你拍板

1. **V2 生产上目前只记 VIEW、不签发 ticket**：V2 生成路径没有权威 profile
   （Phase 0 Q5 已确认：`agent.py:870` 的 prompt 替换不传 profile，`ScriptGenState`
   里也没有 profile 键）。管线已通（`call_llm_with_tools(profile=…)` →
   `lookup_api(profile=…)` → 同一个服务），一旦调用方给出 profile 立刻能签发。
   **要不要在 V2 生成入口补一次执行目标解析**（与 V3 的 `execution_target` 同源）？
   这是 Phase 2/3 之前必须定的事，否则硬闸启用后 V2 会因"没有 ticket"全拒。
2. **`resolved_owner` 仍是 `browser`（占位）**：`ApiContract.owner` 默认 `"browser"`、
   全仓无覆写（Phase 0 D1）。ticket 如实复制它，没有用 `receiver_type` 顶替。
   若希望 `resolved_owner` 变成真实类名，需要在契约里补事实 —— Phase 1 未动。
3. **`receiver_hint` 目前没有生产调用方**：参数已就位（`view_contracts(receiver_hint=…)`），
   但 V3 只有在 workspace 有具体 profile 时才走单一候选，所以暂时用不到它。
   如果将来要让 portable 会话也能签发，需要由调用方给出"本次执行目标是 UserWindow 还是
   UserBrowser" —— 那必须来自真实执行目标，不能由模型自报。
4. **ticket 与权限的联动只有"引用 + 撤销后失效"**（`evaluate_ticket(permission_revoked=…)`），
   还没有从 `permission_grants` 自动读撤销状态的生产调用方（Phase 3 接线）。
5. **`integrity` 用内容摘要而非 HMAC**（理由见 §3.2）。若你要"离线也能校验凭据"，
   需要本地密钥管理，请明确是否要做。

---

**Phase 1 到此停止，等待确认后再进入 Phase 2。**
