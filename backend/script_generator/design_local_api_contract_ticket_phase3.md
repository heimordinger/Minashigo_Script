# 本地 API 契约凭据 —— Phase 2.1 收口 + Phase 3 影子闸报告

- **状态**：Phase 2.1 三项缺口已处理；Phase 3 **影子闸**（只读报告）已实施并跑过一批可复现的新主轮；
  §11 是人工决定 5 的收口检查（样本来源 / readiness 门槛 / 按 profile 统计）
- **未启用生产硬拒绝**：影子阶段只分级、只报告；`enforced` 恒为 `false`，
  且 `hard_gate_readiness` 结构上无法自动开启（§11）
- **未做**（人工决定 三）：不改 GUI、不因缺票拒绝交付、不扩建 Writer DSL、不自动批准历史 API、
  不用方法名匹配替代 receiver 解析、不把 portable owner 不可证明当幻觉、不用历史缺票率作阈值
- **`click_until_gone`（人工决定 1）**：保持未批准，旧脚本迁移属独立任务，本方案不顺手改

---

## 1. Phase 2.1-1：`browser_user_browser.goto` 补登记代理目标

**改动**（`api_contracts/browser_user_browser.py`）：

```python
proxy_target_module="backend.browser.browser",   # 真实 Browser 类型
proxy_target_class="Browser",                    # 由它自己的 MRO 继续解析
```

**解析链**（`verify_runtime.resolve_method`，代理链现在显式包含 `__getattr__` 这一跳）：

```
proxied | owner=NavigationMixin
chain=('UserBrowser', '__getattr__', 'Browser', 'BaseMixin', 'LifecycleMixin',
       'NetOptimizeMixin', 'NavigationMixin', 'goto')
```

- 没有写孤立 Mixin、没有为 `goto` 写特例：`NavigationMixin` 是从 `Browser` 的**真实 MRO** 里解出来的；
- **代理契约不再"免检"**：`verify_contract(BROWSER, goto) == []`，而且改坏默认值会被漂移核验报出来
  （新测试 `test_proxied_contract_signature_is_verified_through_the_proxy`）；
- `desktop.move` 的链也跟着变成 `('UserWindow', '__getattr__', 'Win32Target', 'move')`。

**新增测试**（`tests/test_api_contract_reconcile.py`，8 条）：

| 要求（人工决定 一.1） | 测试 |
| --- | --- |
| owner 解析为 NavigationMixin | `test_browser_goto_owner_is_navigation_mixin` |
| chain 包含 `UserBrowser.__getattr__`、`Browser`、`NavigationMixin.goto` | `test_browser_goto_chain_names_getattr_browser_and_navigation_mixin`（并断言顺序） |
| 真实签名与契约一致 | `test_browser_goto_real_signature_matches_the_contract`（`url` 必填、`retries=3`、async） |
| 代理目标是真实 Browser 类型（不是孤立 Mixin） | `test_browser_profile_declares_the_real_browser_class_as_proxy_target` |
| 未登记的 Browser 内部方法仍不能取得凭据 | `test_unregistered_browser_internal_methods_still_get_no_ticket`（真实存在、可解析，但 `registry.contract` 为 None ⇒ 无 ticket、无事件） |
| "proxied 标注过时"仍会被抓 | `test_directly_resolvable_method_still_flags_a_stale_proxied_flag` |
| 没登记代理目标时必须报"证明不了" | `test_unregistered_proxy_is_reported_when_no_target_is_declared`（monkeypatch 掉代理目标） |

`verify_profile` 两个 profile 均为空问题列表（真实签名全部对得上）。

---

## 2. Phase 2.1-2：`click_until_gone` 只读专项

**独立报告**：`design_local_api_contract_ticket_click_until_gone_audit.md`
（证据脚本 `tools\_scratch\_click_until_gone_audit.py` → `_click_until_gone_audit.md`）

核对结论摘要（八项全查）：

| 核对项 | 结论 |
| --- | --- |
| desktop / browser 真实签名 | 两端一致：`async def click_until_gone(self, img_path, timeout=10)` |
| 返回值语义 | `bool`：图消失 → `True`；超时 → `False`；契约的 `truthiness` 为空（未登记） |
| 任务取消检查 | **两端都有**（desktop `await self._check()`；browser `await self._task_ctrl.check()`），每轮一次 |
| timeout 边界 | 墙钟 `deadline`，`timeout=10`；无下限/上限校验；循环内**没有 sleep** |
| 内部 `click_image` 语义 | 每轮一次真实点击（默认 `expect='none'`）+ 每轮 `ensure_frame(force=True)` ⇒ **副作用随轮数放大** |
| 是否属 portable 交集 | 否（未批准侧被安全交集排除） |
| 等价批准能力 | 有近似但**不等价**：`click_image(expect='gone')` 是"点一次 + 等转场"；它是"反复点直到消失" |
| 历史调用 profile / 用途 | 唯一调用点 `scripts\孤儿\孤儿推本.py:151`，脚本注解 = **browser_user_browser**；用途：点"2_出击"直到消失 |

**建议（未拍板）**：**保留未批准 + 迁移旧脚本**。理由：①语义是循环点击（副作用可放大），
批准它等于批准一种未设计上限/退避的循环行为；②唯一历史调用点在 browser 侧，而 browser 侧**完全没有登记**
（desktop 那张未批准卡在 browser 不生效）⇒ 只批 desktop 解决不了实际问题；
③迁移成本极低（1 处，可用 `click_image(expect='gone')` 重写）；④契约本身不完整（参数表为空），
批准前必须重新登记并让漂移核验通过。报告里给了"若将来要批准"的最小完整登记清单。

---

## 3. Phase 2.1-3：历史无注解脚本不是生产缺陷

**已按人工决定执行**（写进工具与报告的措辞）：

- 离线扫描只有两个视图：**declared**（脚本自己注解推出的 profile）与 **portable**（执行目标未确定）；
- **新生成/新试运行的 profile 必须来自 Runtime 事实**：V3 由 `runner._apply_execution_target`
  写工作区、V2 由 `execution_target ← resolve_execution_target(account, ctrl)`；
- 注解**不得覆盖** Runtime 目标：`profile_from_target` 只在没有目标时退回注解，
  且新增测试 `test_v2_annotation_never_overrides_the_runtime_target` 锁死
  （脚本注解 `UserBrowser` + 目标 `desktop` ⇒ 按 desktop 核验并 `confirmed`，注解单独记为 `annotation_profile`）；
- **不**为了统计好看去逼旧脚本补注解，也**没有**给它们补签任何凭据。

---

## 4. ticket 口径（人工决定 二）

- 工具字段改名：`unticketed_rate` + `unticketed_rate_label`
  = "未经过 ticket 签发流程的调用点比例（不等于错误率或拒绝率）"；
  Phase 2 报告 §6.4 已同步该口径。
- 硬闸判断**只**用新流程指标（见 §6 的统计器）；历史 80.8% 不作阈值。
- 影子/扫描路径**不补签**：测试 `test_shadow_writes_an_event_and_no_tickets`、
  `test_v2_validate_script_local_returns_the_same_errors_with_shadow` 锁死"零 ticket"。

---

## 5. Phase 3 影子闸实现

### 5.1 判断等级（人工决定 四）

`api_contracts/reconcile.py` 现在产出 `CallSiteVerdict.grade`，八级与细分结论的映射：

| 等级 | 触发条件 | 细分 verdict |
| --- | --- | --- |
| `confirmed` | 具体 profile + 接收者可证明 + owner 可证明 + 凭据有效且与调用一致 | `ok` |
| `portable_confirmed` | 执行目标未确定，但调用属 portable 交集且 portable 凭据有效 | `ok`（portable） |
| `needs_profile` | 能力只属于单端，需要 Runtime 目标 | `not_approved` / `unregistered`（单端） |
| `missing_ticket` | 本地调用已确定但本轮没取得契约 | `ticket_missing` |
| `expired_ticket` | 契约/环境/资源/profile 已变化（含被本地撤销） | `ticket_expired` / `ticket_profile_mismatch` |
| `mismatch` | 有票但实际调用与票不符（方法/owner/参数） | `call_mismatch` |
| `unresolved_receiver` | 无法静态证明接收者（或具体 profile 下 owner 证明不了） | `receiver_unresolved` / `owner_unproven` |
| `not_approved` | 真实存在但未进入能力面（含从未登记的方法） | `not_approved` / `unregistered` |

- **只有** `missing_ticket` / `expired_ticket` / `mismatch` / `not_approved` 是未来硬闸的**候选**拒绝等级
  （`REJECTION_CANDIDATE_GRADES`），本阶段只计数；
- `needs_profile` 必须先解析 Runtime 目标；
- **portable owner 不可证明不算"证明不了"**：合成 profile 下 owner 判定被跳过
  （`reconcile` 里 `synthetic` 分支），portable 交集 + 有效 portable 票 = `portable_confirmed`；
- `mismatch` 新增了真实比对：owner（凭据记录的 vs 解析出的）、方法、参数
  （未知关键字、位置参数过多、必填未传 —— 用 Registry 契约的形状判定，不猜）。

八级**全部可被真实场景命中**（`test_every_grade_constant_is_reachable` + 8 条分级测试）。

### 5.2 V2/V3 共用的影子入口

`collaborator_v3/runtime/shadow.py`（V2 与 V3 都走它）：

- **对 bundled 源码**提取调用点：V3 用产物闸门 materialize 出来的 `workspace.bundled_text`
  （新增字段），拿不到才退回提交原文并标 `source_kind="submitted"`；V2 用
  `script_artifact.materialize_trial_script(..., dry_run=True)` 的 `bundled`（同一流水线）；
- 只读：**不签发、不改库、不拒绝**；只写 ①validation 结果的紧凑摘要（`shadow_reconcile`，
  含 `mode/enforced/attempt/budget/grades/rejection_candidates/reasons/sample_sites/hint`）
  与 ②事件 `shadow.reconciled`（完整分级 + 计数 + 逐调用点样本，最多 200 条）；
- 补查预算 `REQUERY_BUDGET = 1`，报告里给出 `hint`：**补查后必须重新提交/重新确认相关调用**；
  `attempt` 由本主轮已有的 `shadow.reconciled` 事件数推出，用于统计"补查一次后仍缺票"。

### 5.3 接线位置（都在生产交付路径，都不改交付结论）

| 路径 | 落点 |
| --- | --- |
| V3 | `runner._validate_script_subturn` → `_shadow_reconcile`：结果进 `RuntimeResult.payload["shadow_reconcile"]`、进 `ScriptValidated` 事件；bundled 文本由 `_run_artifact_gate` **随返回值**给出（第三个返回值），**不**落工作区字段（它不是跨 pause→resume 的状态；这一点被 `test_collab_v3_workspace_snapshot_coverage.py` 的字段覆盖性护栏挡下并已改正） |
| V2 | `agent.validate_script_local(..., shadow_sink=None, execution_target=None, task_id, turn_id)`：**返回值仍是 `list[str]` 且逐字段不变**，摘要写事件、可选填 `shadow_sink` |

### 5.4 新流程统计器

`collaborator_v3/runtime/shadow_stats.py`（只读）：

- `collect_turn(store, main_turn_id)`：契约查询数（`contract.viewed` 的 capability_ids）、
  签发凭据数、交付调用点数、缺票点数、缺票原因、补查后仍缺票、误报率、八级直方图；
- `collect(store, limit=...)`：跨主轮汇总（`still_missing_after_requery_rate` 等）；
- `hard_gate_readiness(stats, min_turns=5)`：**只是判断依据**，样本不足/补查后仍缺票 >20%/误报 >5% 就不建议切闸。

---

## 6. 一次可复现的新主轮证据

命令：

```powershell
python\python311\Scripts\python.exe tools\_scratch\phase3_shadow_evidence.py
# 输出存档：tools\_scratch\_phase3_evidence_output.txt
```

流程（真实生产代码路径：`ActionRunner` + **真实** `V2ScriptValidator` + 真实产物闸门 + 真实凭据）：

1. `api.get_many([match_image])` → 本地签发 1 张凭据 + `contract.viewed`；
2. `validate_script`（脚本含 `match_image` + **没查过的** `click_image`）→
   影子报告 `attempt=1`、`source_kind=bundled`、`call_sites=2`：
   `confirmed=1`、`missing_ticket=1`，并记下缺票原因与两个调用点样本；
3. **补查一次** `api.get(click_image)`（预算 1 次）→ 第 2 张凭据；
4. **重新提交**同一脚本（人工决定 三.6）→ 影子报告 `attempt=2`：`confirmed=2`、`missing_ticket=0`；
5. finish 走完 → `MainTurnCommitted`（**交付行为未变**）。

统计（`collect_turn`）：

```json
{"contracts_viewed": 2, "tickets_issued": 2, "delivery_call_sites": 2,
 "shadow_attempts": 2, "missing_ticket_sites": 0, "still_missing_after_requery": false,
 "grades": {"confirmed": 2, "missing_ticket": 0, "...": 0},
 "false_positive_rate": 0.0, "enforced": false}
```

汇总与就绪度：

```json
{"turns": 1, "tickets_issued": 2, "delivery_call_sites": 2, "missing_ticket_sites": 0,
 "false_positive_rate": 0.0, "enforced": false}
{"ready": false, "blocking_reasons": ["新流程样本不足（1 < 5）"], "enforced": false}
```

**额外证据**：bundled 文本真的被用上了 —— 同一个脚本在提交原文里行号是 `3:…/5:…`，
在 bundled 文本里变成 `4:…/7:…`（materialize 加了头），报告里的 span 是后者。

---

## 7. "影子模式不改变交付结果"的证明（测试）

| 测试 | 断言 |
| --- | --- |
| `test_v3_validate_script_gets_a_shadow_report` | validation 结果里有 `shadow_reconcile`，`enforced=false`、`source_kind=bundled`、事件 `shadow.reconciled` + `ScriptValidated.shadow_reconcile` 都在 |
| `test_v3_shadow_mode_does_not_change_delivery_result` | 同一脚本站两遍：关掉影子（monkeypatch `_shadow_reconcile`）后，**除 `shadow_reconcile` 外的 payload 逐字段相等**，`status` 也相同 |
| `test_v3_shadow_records_but_does_not_reject_missing_tickets` | 缺票时 `validated=true`、`issues=[]` —— 只记录、**不拒绝** |
| `test_v2_validate_script_local_returns_the_same_errors_with_shadow` | 开/关影子，`validate_script_local` 的返回列表**完全相同**，且不产生任何凭据 |
| `test_stats_are_read_only` / `test_shadow_writes_an_event_and_no_tickets` | 统计与影子都不改库（凭据数不变、影子路径零签发） |

---

## 8. 是否达到启用硬闸的条件

**没有达到，本阶段不切闸。** 依据：

1. **新流程样本不足**：`hard_gate_readiness` 要求 ≥5 个带影子报告的主轮，目前只有 1 个可复现主轮
   （`ready=false`，原因"新流程样本不足（1 < 5）"）。历史脚本不能充当新流程样本。
2. **`unresolved_receiver` 的误报数据还没攒够**：人工决定 四要求它"单独看误报数据"，
   而生产数据里这一级的构成（`self.x()` 型误报 vs 真无法解析）需要更多会话才能分开。
3. **`needs_profile` 的处置尚未定**：单端能力在 portable 状态下必须"先解析 Runtime 目标"，
   这条链路在 V2 侧依赖 `execution_target`（现在已接通），但还没有真实多账号会话验证。
4. **`goto` 刚补登记**：代理契约进入签名核验后需要一段真实运行确认没有新的漂移。

**下一步（等确认）**：继续积累新流程会话，用 `shadow_stats.collect()` +
`hard_gate_readiness()` 复测；`missing_ticket` 占比与"补查一次后仍缺票"比例稳定在低位后，
再讨论把 `REJECTION_CANDIDATE_GRADES` 变成拒绝。

---

## 9. 本轮文件与测试

**新增**：`api_contracts/reconcile.py` 的八级 + mismatch 比对（改造）、
`collaborator_v3/runtime/shadow.py`、`collaborator_v3/runtime/shadow_stats.py`、
`tests/test_collab_v3_shadow_gate.py`（23 例）、
`design_local_api_contract_ticket_click_until_gone_audit.md`、
证据/审计脚本 4 个（`tools\_scratch\`）。

**修改**：`api_contracts/browser_user_browser.py`（代理登记 + goto 语义文案）、
`api_contracts/verify_runtime.py`（代理链含 `__getattr__`、`_params_of`、proxied 签名核验）、
`collaborator_v3/events/schema.py`（`shadow.reconciled`）、
`collaborator_v3/runtime/runner.py`（影子接线；`_run_artifact_gate` 第三个返回值给出 bundled 文本）、
`agent.py`（`validate_script_local` 影子接线，返回值不变）、
`tests/test_api_contract_reconcile.py`（goto 代理 8 例 + 链更新）、
`design_local_api_contract_ticket_phase2.md`（§6.4 口径）。

> 过程中被护栏抓到一次：我先把 bundled 文本存成 `TurnWorkspace.bundled_text`，
> `test_collab_v3_workspace_snapshot_coverage.py` 立刻点名"新字段既没进快照也没豁免"
> —— 已改成"随返回值传递"，**没有**去放宽那条护栏。

**测试**：`tests/test_collab_v3_shadow_gate.py` **23 passed**；
`tests/test_api_contract_reconcile.py` **36 passed**（含 goto 代理 8 例）；
全量回归见 §10。

---

## 10. 全量回归（最终复跑）

| 套件 | 结果 |
| --- | --- |
| 本阶段新增/更新（影子 23 + 对账 36） | **59 passed** |
| 全量 `tests/` | **1428 passed / 41 failed / 28 skipped** |

那 41 个 failed 与本次改动无关：全部来自既有的 `tests/test_sgk_settle_gate.py` 与
`tests/test_sgk_scan_speed.py`（脚本 `scripts/yys/刷关卡.py` 缺 `_percentile` /
`_CHALLENGE_CLICK_POINT`，改动前就是红的）。通过数从 Phase 2 的 1397 升到 1428（+31 = 本阶段新增用例），
**没有新增失败**。过程中被 `test_collab_v3_workspace_snapshot_coverage.py` 抓到一次真实问题
（见 §9 末尾），已按护栏要求改正而不是放宽护栏。

---

## 11. 收口检查（人工决定 5）：样本来源 / readiness 门槛

**检查结论：这五项原先**没有**实现，因此按"只补充"的要求加了最小改动；影子模式与交付行为不变。**

| 要求 | 之前 | 现在 |
| --- | --- | --- |
| 统计样本来源字段 | 无 | 影子报告的 validation 摘要与 `shadow.reconciled` 事件都带 `sample_origin`（`real_user`/`test`/`probe`/`replay`/`unknown`）+ `countable_sample` + `runtime_session_id` |
| readiness 只统计真实用户 | 统计全部 | `collect()` 按来源分桶：`real_user` 才进指标，其余进 `excluded_by_origin`；`hard_gate_readiness` 只读 `real_user` 桶 |
| 默认门槛 20 / 50 / 5% | 5 / — / 20% | `DEFAULT_MIN_REAL_TURNS=20`、`DEFAULT_MIN_CALL_SITES=50`、`DEFAULT_MAX_STILL_MISSING_RATE=0.05` |
| `enforced` 不得被 readiness 自动修改 | 只是没人改 | 返回值硬编码 `auto_enable=False` / `enforced=False` / `requires_human_approval=True`，并有测试 `test_readiness_never_enables_anything` 锁死；函数本身只读 stats |
| 按 profile 分别统计 ready | 无 | `collect()["per_profile"]` + `hard_gate_readiness(..., profiles_expected=[...])` → `unsampled_profiles`（未采样 profile 不得一起开闸） |

**门槛里的其余四项也已落地**（都是"读统计 + 报原因"，不写任何配置）：

- **确定性拒绝候选的人工误报必须为 0**：`hard_gate_readiness(manual_false_positives=...)`；
  不提供 → 直接不建议切闸（"人工误报数未提供"）；提供非 0 → 不建议；
- **`needs_profile` 不得在目标已确定后残留**：按主轮统计
  `needs_profile_after_target_turns`（目标 profile 非 portable/unresolved 却出现 needs_profile）>0 即阻塞；
- **至少一次应用重启**：事件里写 `runtime_session_id`（每进程一个 uuid），
  readiness 要求真实样本里出现 **≥2 个不同会话 id**（也顺带证明 ticket/事件/失效能跨重启恢复）；
- **`goto` 至少一次真实 browser 证据**：统计真实样本里 `method=goto` 且 `resolution_status=proxied`
  且 profile=browser 的调用点，为 0 即阻塞；
- **`unresolved_receiver` 不进入拒绝候选**：只出现在 `not_blocking` 里（附误报率），不阻塞其他等级分阶段启用。

**分阶段开启计划已记录但不执行**（`STAGED_ROLLOUT`）：
第一阶段只拒绝 `mismatch` / `not_approved` / `expired_ticket`；
第二阶段才处理 `missing_ticket`（首次缺票 → 一次结构化修复提示 → 允许 `api.get/get_many` 补查 →
必须重新提交 → 仍缺票才拒绝）；`needs_profile` 只要求 Runtime 先给执行目标；
`unresolved_receiver` 继续警告。`auto_switch=False`、`requires_human_approval=True`。

### 11.1 样本来源怎么判定（保守优先）

`shadow.detect_sample_origin()` 的优先级：**测试/探针的客观信号 > 真实入口的显式声明 > 环境变量 > `unknown`**。

- `PYTEST_CURRENT_TEST` 或 `sys.modules` 里有 pytest → `test`（所以测试里调到真实入口也不会被记成真实样本）；
- 主脚本路径在 `tools/_scratch`（或 `tools/`）下 → `probe`；
- `MINASHIGO_SAMPLE_ORIGIN` 环境变量可用于重放脚本（→ `replay`）；
- 真实入口显式声明：`gui_bridge.run_v3_gui_turn`（GUI 触发 V3 主轮的**唯一**真实入口）
  调 `set_sample_origin(SAMPLE_REAL_USER)`；
- **其余一律 `unknown`，并且不计入**（一个普通进程实测返回 `unknown`，不会冒充 real_user）。

### 11.2 已知限制（需要你决定，未擅自改）

**V2 的真实会话目前会记成 `unknown`（因此不计入 readiness）**：V2 的入口
（`agent.generate_script` / `validate_script_local`）既被 GUI 调用也被测试和重放脚本调用，
在**不改 `gui/` 文件**的前提下无法可靠区分——按"分不清就报未知、不得计入"的要求，
这里**没有**给它加自动的 `real_user` 标记（那会把重放脚本也算成真实样本）。
如果你要 V2 也计入，需要的改动是：在 V2 的 GUI 工作线程入口
（`gui/widgets/script_gen_panel/workers.py` 的 `GenerateWorker/ReviseWorker.run`）
加一行 `set_sample_origin(SAMPLE_REAL_USER)`——这属于 `gui/` 改动，等你点头再做。

**方向是安全的**：宁可少算（V2 记 unknown），也不会多算。

### 11.3 本轮验证

只跑了受影响的测试文件（按你的要求不反复跑全量）：

| 文件 | 结果 |
| --- | --- |
| `tests/test_collab_v3_shadow_gate.py` | **35 passed**（新增来源/门槛/per-profile/never-auto-enable/unresolved-不阻塞/goto 证据等 12 例） |
| `tests/test_collab_v3_workspace_snapshot_coverage.py` | passed（字段覆盖性护栏） |
| `tests/test_collab_v3_event_literals.py` | passed（事件字面量登记护栏） |
| `tests/test_api_contract_reconcile.py` | 36 passed |
| 四个文件合计 | **85 passed** |

**改动**：`collaborator_v3/runtime/shadow.py`（来源 + 会话 id）、
`collaborator_v3/runtime/shadow_stats.py`（分桶统计 / 门槛 / per-profile / 分阶段计划）、
`collaborator_v3/adapters/gui_bridge.py`（真实入口声明一行）、
`tests/test_collab_v3_shadow_gate.py`（12 例）。**没有**改 GUI、没有改交付行为、没有引入任何自动开关。

---

**收口完成：影子模式继续保持，硬闸未启用；后续由真实使用产生样本，达到量级后再做只读统计审查。**

