# 本地 API 契约凭据 —— 决定 1-4 处理 + Phase 2 报告

- **状态**：决定 1-4 已处理；Phase 2（AST 调用点 / 真实定义类解析 / 只读对账报告）已实施并跑完语料统计
- **未进入 Phase 3**：无生产交付硬拒绝、无 GUI 展示、无 receipt 扩展、无权限自动联动、无完整数据流分析、无 Writer DSL
- **只读扫描**：不改任何业务文件、不签发凭据、不写库（`tools\_scratch\phase2_corpus_scan.py`）

---

## 1. 决定 2：不升级 HMAC（已写入设计说明）

落点：`api_contracts/ticket.py` 模块 docstring（新增"为什么不现在上 HMAC"一段）。

> 当前信任边界 = 本地 SQLite。校验一律按 ``ticket_id`` 读权威记录并**重新计算摘要**，
> 从不信任 LLM 提交的载荷；随机 id + 只读字段 + 库内唯一行 + 内容摘要已覆盖"模型编造/篡改凭据"。
> 只有出现**导出凭据 / 脱离数据库校验 / 跨设备传递**时才引入 HMAC 与密钥管理。

代码行为不变（`integrity` 仍是内容摘要），只是把决策与边界写成文档，避免以后有人"顺手加个密钥"。
测试侧已有两条锁死这个边界：`test_verify_rejects_a_forged_field_set`（伪造字段）、
`test_tampering_with_the_digest_itself_is_detected`（自算摘要）。

---

## 2. 决定 3：`resolved_owner` 不再写占位 owner

| 项 | 改动 |
| --- | --- |
| `ticket.build_ticket` | 新增 `resolved_owner` 参数，**默认空串**；不再 `resolved_owner=contract.owner` |
| `TicketService` | 签发具体 profile 的凭据时用 `verify_runtime.resolve_method()` 填**真实定义类**；证明不了就留空 |
| `resolution_chain` | 证明得了 → 完整路径（含 Mixin/基类/代理链）；证明不了 → 只保留"接收者→属性"一跳；portable → 空 |
| `ApiContract.owner` | **保留**（兼容），但任何真实性裁决都不再读它 |

实测（真实类）：

```
desktop_user_window.match_image -> owner=UserWindow        chain=('UserWindow','match_image')
desktop_user_window.move        -> owner=Win32Target       chain=('UserWindow','Win32Target','move')   (登记代理)
browser_user_browser.match_image-> owner=UserBrowser       chain=('UserBrowser','match_image')
browser_user_browser.goto       -> owner=''                chain=('UserBrowser.goto',)                 (证明不了)
portable_common.match_image     -> owner=''                chain=()                                    (交集)
```

---

## 3. 决定 4：MatchResult 契约与方案示例

**Registry 事实**（`ReturnContract` 新增 `indexable` / `collection` / `subscript_keys`）：

| 契约 | type | iterable | sized | indexable | collection | subscript_keys |
| --- | --- | --- | --- | --- | --- | --- |
| `match_image`（两端） | `MatchResult \| falsy` | **true** | **false** | **true** | **false** | x / y / score / max_val |
| `match_image_multi` | `list[dict]` | true | true | true | **true** | — |
| `finish_pseudo_record` | `dict \| None` | true | true | false（mapping=true） | false | — |
| `wait_image` / `bool` 类 | `bool` | false | false | false | false | — |
| `Any` 类 | `Any` | 未知 | 未知 | 未知 | 未知 | — |

新增纯判定（Phase 3 直接用，现在只被测试与报告调用，不是死代码）：

- `validator.len_conflict(returns)` → `TYPE_NOT_SIZED`（`sized is False` 才报，未知不报）；
- `validator.multi_target_conflict(returns)` → `TYPE_NOT_MULTI_TARGET`（"可迭代但**不是**多结果集合"）；
- `ReturnContract.iteration_semantics()` → `multi_target` / `single_result_parts` / `not_iterable` / `unknown`。

**五项测试**（`tests/test_api_contract_return_interface.py`，21 例）逐条对应你的要求：

1. `x, y, score = result` 允许 —— 先用真实类跑通解包，再断言事实是 `iterable=true`；
2. `result["x"]`、`result[0]` 与真实类一致（键 x/y/score/max_val，索引 0/1/2）；
3. `len(result)` 判成 `TYPE_NOT_SIZED`（`len_conflict == "TYPE_NOT_SIZED"`），而 `list[dict]`/`dict` 不报；
4. 迭代 MatchResult ≠ 遍历多个目标（`iteration_semantics()=="single_result_parts"`，
   且 `multi_target_conflict == "TYPE_NOT_MULTI_TARGET"`）；
5. 需要"命中数量"必须用返回 Sized 集合的多目标能力（`collection&sized` 只有 `match_image_multi`）。

**方案书示例修正**：按你的指示改了 `design_local_api_contract_ticket.md` §6 里写死的
`"iterable": false`（那与真实类矛盾），现在是与事实一致的版本（`iterable/ sized/ indexable/ subscript_keys/ collection`）。
这是本次唯一一处改动方案书的地方。

---

## 4. 决定 1：V2 接入权威执行目标 + portable 凭据语义

**唯一的取目标入口**（V2/V3 共用，不再各写一套判断）：

```
gui 的 _execution_target_payload(account, controller)      ← GUI 里唯一的取目标点
        │  execution_profile.resolve_execution_target(...)
        ▼
execution_target 事实  {runtime_profile, profile_source, target_revision, account_ref, confidence}
        │  execution_profile.profile_from_target(...)      ← V2/V3 唯一的解释函数
        ▼
V3: queries._contract_profile(workspace)      V2: api_catalog.record_contract_view(execution_target=…)
        └──────────────► 同一个 TicketService.view_contracts ◄──────────────┘
```

落地清单：

| 要求 | 落点 |
| --- | --- |
| V2 生成/修订带权威目标 | `mixin_generate` 参数加 `execution_target`；`agent.generate_script/revise_script(execution_target=…)` → `call_llm_with_tools(execution_target=…)` → `_tool_result_text(execution_target=…)` → `lookup_api(execution_target=…)` |
| 不制造默认 profile | `profile_from_target`：缺失/`unresolved`/未知 → `(None, "unresolved", rev)`；`resolve_execution_target` 拿不到账号就返回 `unresolved` |
| portable 只给交集能力签 portable ticket | `TicketService.view_contracts`：`effective == portable_common` 时 `receiver_type=""`，`build_ticket` **只允许** `runtime_profile==portable_common` 的契约这样签（否则 `ValueError`） |
| profile 专属能力 → `PROFILE_REQUIRED` | 服务产出 `ContractViewResult.profile_required`（单端能力判定只认 Registry）；V3 `api.get` 直接以 `registry.PROFILE_REQUIRED_CODE` 失败；`api.get_many` 在载荷里列出；V2 的 `lookup_text` 原本就有结构化 `PROFILE_REQUIRED` |
| 目标确定后核验/重签 | `TicketService.reconcile_for_target` / `reconcile_portable_for_turn`：portable → 具体 = **重签 + 旧票本地失效（保留行）**；目标里没有该契约 = `kept_portable` + `PROFILE_REQUIRED`（不猜） |
| 接线时机 | ① `runner._apply_execution_target`（Runtime 目标一确定就核验/重签本主轮）；② V2 入口（`_reconcile_tickets_for_target`）；③ GUI 试运行前（`mixin_runtime._reconcile_tickets_for_target`，即"试运行前最后一道解析"） |
| 一个 resolver | GUI 里 `resolve_execution_profile(` 出现 **0** 次、`resolve_execution_target(` **1** 次；`agent/api_catalog/queries` 不得出现 `resolve_execution_profile(`（有测试扫源码锁死） |

`_fail()` 现在接受 `ErrorCode | str`：`PROFILE_REQUIRED` 的字面量归 `api_contracts.registry` 所有，
在 V3 再定义一个枚举成员就又成了同义双码（决定 7 的同一原则）。

---

## 5. Phase 2 实施内容与文件

### 新增（实现 2 个）

| 文件 | 内容 |
| --- | --- |
| `api_contracts/callsite.py` | AST 调用点提取：稳定 `node_id`（`cs_<sha1(path|span|method)[:16]>`）+ 行列 span；receiver 六态（entry/alias/reassigned/ambiguous/attribute/other/unresolved）；别名多跳传播、重赋值失效、分支合并（各分支一致才算别名，否则 `ambiguous`）；入口名从 `do_work` 首个参数推断 |
| `api_contracts/reconcile.py` | 只读对账报告：`CallSiteVerdict`（结论/契约/owner/凭据/误报标记）+ `ReconciliationReport`（计数、比率、样本）；**不拒绝、不写库** |

### 修改（含决定 1-4）

`api_contracts/verify_runtime.py`（**新增** `resolve_attribute` / `resolve_method` / `MethodResolution`：MRO→子类覆盖优先、Mixin/基类出现在 chain、登记代理链、`unregistered_proxy` 如实报告）、
`schema.py`（3 个新事实字段 + `iteration_semantics`）、`validator.py`（2 个新码 + 2 个纯判定）、
`ticket.py`（`resolved_owner` 默认空 / `is_portable()` / HMAC 说明）、
`ticket_service.py`（portable 凭据、`profile_required`、`reconcile_*`、真实 owner 解析）、
`registry.py`（portable 合并新事实）、`render.py`（"返回用法"补 indexable/collection）、
`execution_profile.py`（`resolve_execution_target` / `profile_from_target`）、
`api_catalog.py`、`agent.py`、`collaborator_v3/runtime/queries.py`、`collaborator_v3/runtime/runner.py`、
两个 profile 文件（新事实）、`design_local_api_contract_ticket.md`（§6 示例）、
GUI 三处接线（`mixin_collab` / `mixin_generate` / `mixin_runtime`，均非展示改动）。

### 新增测试（3 个文件 / 68 例）

| 文件 | 例数 | 覆盖 |
| --- | --- | --- |
| `tests/test_api_contract_return_interface.py` | 21 | 决定 4 的五项要求 + 真实类核对 |
| `tests/test_api_contract_portable_ticket.py` | 19 | 决定 1 全部子要求（含"一个 resolver"源码扫描） |
| `tests/test_api_contract_reconcile.py` | 28 | 提取 / 别名 / 重赋值 / 分支 / MRO·Mixin·子类覆盖·代理链 / 只读对账 |

### 既有测试的更新（5 处，均为**规格变更**而非迁就实现）

Phase 1 写的这 5 条断言"portable 视图不签发凭据"，与决定 1 的
"portable 只能为交集能力签发 portable ticket"直接冲突，已按新规格改写：
`test_unresolved_profile_issues_a_portable_ticket`、
`test_lookup_without_profile_records_the_view_and_a_portable_ticket`、
`test_agent_tool_dispatch_goes_through_the_shared_service`（现在断言 portable + 具体两类凭据）、
`test_api_get_with_unresolved_profile_issues_a_portable_ticket`、
`test_api_get_many_issues_portable_tickets_when_unresolved`。

---

## 6. 语料统计（Phase 2 的交付物）

命令（可复现）：

```powershell
python\python311\Scripts\python.exe tools\_scratch\phase2_corpus_scan.py | Set-Content -Encoding utf8 tools\_scratch\_phase2_scan.json
python\python311\Scripts\python.exe tools\_scratch\_phase2_sample_dump.py   # 人读样本
```

两个视图：**declared**（把脚本自己注解推出的 profile 当执行目标）、
**portable**（执行目标未确定 = 上闸前最常见的真实状态）。

### 6.1 真实历史脚本（`scripts/**/*.py`）

- 77 个文件，48 个真有 `do_work`；注解分布：`unresolved` 19 / `browser` 19 / `desktop` 10
- 方法调用点 **2302** 个；只按方法名匹配会命中 **1600** 个；其中接收者可证明 **1596**

| 指标 | declared 视图 | portable 视图 |
| --- | --- | --- |
| 可确定匹配率（determinable / local_api_sites） | **100.0%**（1596/1596） | **98.25%**（1568/1596） |
| receiver unresolved 比例 | **0.25%**（4/1596） | 0.25% |
| **未经过 ticket 签发流程的调用点比例**（旧称"缺票率"，口径见 §6.4） | **80.8%**（1290/1596） | **100%**（历史脚本没有凭据） |
| profile 不一致比例 | 0 | 见下（29 个单端调用） |
| owner 可证明率 | 80.8%（1290/1596） | 0（合成 profile 无真实类） |
| 误报候选率（名字命中但接收者与入口无关） | **0.17%**（4/1600） | 0.17% |

- 未登记的 701 个调用点**全是标准库/自建 helper**（`asyncio.gather`、`dict.get`、`str.endswith`、
  `time.time`…）⇒ 提取器没有把它们误判成 API（**这是反误报的关键证据**）
- portable 视图下的 29 个"单端调用"就是 `profile 不一致` 的可操作形态：
  `goto` 16 / `dmm_login` 7 / `match_image_multi` 5
- 唯一的 `not_approved`：`scripts\新斗\新斗记本.py:151` 调了 `click_until_gone`（desktop 侧 approved=False）

### 6.2 测试语料（`tests/**` 内嵌脚本文本，含 `async def do_work`）

- 122 段内嵌脚本；注解分布：`desktop` 43 / `unresolved` 76 / `browser` 3
- 调用点 110；名字命中 103；接收者可证明 96

| 指标 | declared 视图 | portable 视图 |
| --- | --- | --- |
| 可确定匹配率 | 96.9%（93/96） | 96.8%（92/95） |
| receiver unresolved 比例 | 7.3%（7/96） | 7.4%（7/95） |
| ticket 缺失比例 | 78.5%（73/93） | 100% |
| owner 可证明率 | 78.5%（73/93） | 0 |
| 误报候选率 | 6.4%（7/103） | 6.4% |

测试语料的 receiver unresolved 偏高，是因为里面**故意**放了反例样本
（`bb = other` 重赋值、分支赋值、`self.win.` 属性链、`self.match_image()`）——它们正是 Phase 2 要有能力区分的东西。

### 6.4 口径（人工决定 二，必须遵守）

- "80.8%" 只能叫 **"未经过 ticket 签发流程的调用点比例"**（工具里字段名 `unticketed_rate`，
  并带 `unticketed_rate_label`）；
  **不得**称为错误率、幻觉率、硬闸拒绝率或需要修复比例。
- 历史脚本**不能补签假 ticket**，也不得为了让统计变绿而批量签票（扫描器是只读的，
  有测试锁死"影子模式不签发"）。
- 是否可启用硬闸**只能**用新流程会话测量（见 Phase 3 报告的统计器），
  历史 80.8% 不得用作生产阈值。

### 6.3 误报样本（真实材料里的具体例子）
| 样本 | 说明 |
| --- | --- |
| `scripts\Deep One\DO日常_v1.py:1486/2116/2119/2120`（`btn.click` / `b.match_image`，裸名字） | 唯一 4 处"名字命中但接收者证明不了"的历史脚本点（占名字命中的 0.17%） |
| `tests\test_api_contract_reconcile.py::L31/114/122/147`（`bb` 重赋值/分支） | 反例样本：确认 `reassigned` / `ambiguous` 不会被误当入口 |
| `tests\test_collab_form.py::L281`、`test_collab_final_artifact_gate.py::L330`（`browser.not_a_real_api()`） | 未登记方法但接收者是入口 → 真报（不是误报） |
| `self.match_image()`（接收者 `self`） | 典型误报：只按名字匹配会当成 API 调用 |

---

## 7. Phase 2 发现的真实缺口（只报告，未修）

1. **`browser_user_browser.goto` 是"批准契约但无法证明定义位置"**：
   `UserBrowser` 没有 `goto`，靠 `__getattr__`（`user_browser.py:222`）代理到 `self._browser`，
   而 profile 里**没有登记** `proxy_target_module/class`（desktop 侧登记了 `Win32Target`）。
   ⇒ `resolve_method` 如实报 `unregistered_proxy` + owner 空。
   **修法（属后续阶段）**：要么给 browser profile 补登记代理目标，要么把 `goto` 降为未批准。
2. **`click_until_gone` 在 desktop 侧 approved=False，但历史脚本在用**（`scripts\新斗\新斗记本.py:151`）。
3. **19/48 历史脚本没有 `do_work` 注解** ⇒ 拿不到具体 profile ⇒ owner 不可证明
   （契约匹配仍可确定，因为交集能力足够）。这不是缺陷，但决定了"上闸时这些脚本只能按 portable 核验"。

---

## 8. 未做的事（严格按范围）

生产交付硬拒绝；GUI 三层状态展示；receipt 扩展；权限自动联动（凭据只存 `permission_id` + 作用域摘要）；
完整返回值数据流分析（只提供事实与 `len_conflict`/`multi_target_conflict` 判定，闸门在 Phase 3）；
Writer DSL；顺手修复无关历史缺陷（含 §7 的三个缺口）。

---

## 9. 测试结果（最终复跑）

| 套件 | 结果 |
| --- | --- |
| 本阶段新增测试（接口 21 + portable 19 + 对账 28） | **68 passed** |
| `tests/test_api_contract_*.py`（全部） | **222 passed** |
| `tests/test_collab_*.py`（83 文件） | **982 passed / 1 skipped** |
| 全量 `tests/` | **1397 passed / 41 failed / 28 skipped** |

那 41 个 failed 与本次改动无关：全部来自既有的 `tests/test_sgk_settle_gate.py` 与
`tests/test_sgk_scan_speed.py`（脚本 `scripts/yys/刷关卡.py` 缺 `_percentile` /
`_CHALLENGE_CLICK_POINT`，mtime 2026-09-21；Phase 0 之前就是红的）。
全量通过数从 Phase 1 的 1329 升到 1397（+68 = 本阶段新增用例），协作套件保持 982/1。**没有新增失败**。

---

**Phase 2 到此停止：只读对账报告与统计已产出，未进入 Phase 3 硬闸，等待确认。**
