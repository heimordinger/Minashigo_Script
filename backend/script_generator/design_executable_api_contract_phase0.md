# Phase 0 调查报告：可执行 API 契约（只调查，不改行为）

> **状态（2026-09-28 修订）：Phase 0 主体完成；新增漂移调查补充亦已完成。**
> 本文件**不再声称"已完成"同时又列出未确认项**：§10 现分为「本轮已核实」与「仍未确认」两段，
> 只有后者为空时才可宣称 Phase 0 全部完成。**但结论是：Phase 1 不得按"默认 desktop"直接落地**，
> 必须先落实 §13 的锁定决定（profile 由 Runtime 事实决定，禁止静默默认）。
>
> 配套设计文档：`backend/script_generator/design_executable_api_contract.md`
> 日期：2026-09-28
> 纪律：本文只写**已用窄路径复核**的事实，每条给 `文件:符号:行号`；未能复核的一律进 §10「未确认项」，不写猜测句。
> ⚠️ 工具前提：本次实测 **宽范围 grep 会静默漏结果**（对 `backend/` 全量搜 `confirmed` 漏掉 `runner.py:296`）。后续阶段一律窄路径搜索，不依赖宽搜结论。
> ⚠️ 另实测：`Get-Content` 默认按 GBK 解码本仓中文文件，会**合并行并造成约 19 行偏移**（`design_executable_api_contract_phase0.md` 被读成 119 行，真实 198 行）。
> **行号一律以 `read`/`grep`/`Select-String` 为准**，`Get-Content` 只可用于加 `-Encoding utf8` 的场景。
> ⚠️ **并发写告警**：本次调查期间有另一会话在实时修改本仓（时间线见 §14）。本文引用的行号以 2026-09-28 04:16 磁盘状态为准，实施前需重新核对。

---

## 1. 试运行运行链路（设计 §13.1）

**结论：没有模板文件；`scripts/_trial/_gen_trial.py` 是点「试运行」时由三步变换后原子写盘的产物。**

| 步骤 | 位置 |
| --- | --- |
| 协作页把已交付代码推给经典面板 | `gui/widgets/script_gen_panel/mixin_collab.py::CollabChatMixin._push_code_to_host:4813`（调用点 `:3168-3173`） |
| 素材打包（第二套替换） | `mixin_collab.py:4835-4837` → `panel.py::ScriptGenerator._bundle_assets_for_save:1348` → `asset_bundle.py::bundle_script_assets:163` |
| 注入伪录制 + 写盘 | `mixin_runtime.py::_on_trial_run:595` → `_write_trial_file:233-273`（`inject_pseudo_record_for_trial` `pseudo_codegen.py:161`；tmp + `os.replace` 原子写 `:262-264`） |
| 启动任务 | `_facade.start_task(account, _TRIAL_REL):677`（`_TRIAL_REL` = `_trial/_gen_trial.py`，`constants.py:7`） |

伪录制注入会 AST 定位 `do_work` 并包一层 `try/except/finally`（`pseudo_codegen.py:146-157`），逐级降级：完整注入 → 只加 `import os`/`DEBUG_PSEUDO_RECORD` → 原文，每级自检编译（`:171-183`）。其它也会重写该文件的入口：`_sync_trial_code:333`、`_run_local_script_validate:302`。

### 1.1 执行类到底是 UserWindow 还是 UserBrowser

**结论：由脚本 `do_work` 首参注解 + `account["_target"]` 二选一，找不到会「静默降级」到另一个。不是声明式 profile。**

- GUI 先定倾向：`mixin_runtime.py:621-629`（只有 window → `_target="window"`；只有 browser → `"browser"`；都有且无倾向 → `"window"`）。
- 决策：`task_controller.py::_run_async:128` → `_detect_prefer_window(script):377-392`
  （单类型 `UserWindow` → True，`UserBrowser` → False，**Union → None**）。
  - `prefer_window is None`：按 `account["_target"]`，取不到就取另一个（`:140-157`）。
  - 单类型注解：`_target` 可覆盖注解（`:159-162`）。
  - **取不到实例时静默换另一个，只记 WARNING**（`:175-188`）。
- 实例来源：`ctrl.py:263-269` → `_browser_instances[name] = MainLoopProxy(UserBrowser)`（**代理**）；
  `ctrl.py:347-349` → `_window_instances[name] = UserWindow(target=Win32Target.from_hwnd(hwnd), task_ctrl=...)`（**裸实例**）。
- 因此 `runtime_profile` **不能靠猜**：必须记录实际选中的分支（设计 §12.9）。

### 1.2 试运行怎么结束

**结论：运行层没有任何「跑够 N 轮就停」机制。** 只有四类结束：脚本自然 return、用户点停止、异常、StuckGuard 看门狗。

- `TaskStopped` 定义 `task_controller.py:52`；**全仓唯一 raise 点** `task_controller.py::TaskController.check:405-410`。
- 触发路径：每次 API 调用前 `UserWindow._check():321-327`（`__getattr__` 代理的 sync 方法同样先 `_check`：`:314-317`）。
  **import 阶段不经过 `_check`** → 停止在 import 期间不生效。
- stop 入口：`ctrl.py::Controller.stop_task:558-568` → `TaskController.stop:394-397`（`_stopped=True` + `future.cancel()`）→ 收尾 `task_controller.py:256-264`（归档停帧 + 打 `"任务已停止"`，与实况日志一致）。
- 看门狗（抛 `RuntimeError`，走 error 路径而非 TaskStopped）：`backend/automation/stuck_guard.py`，`idle_limit=300s:37`、`state_fail_s=180s:45`、`click_fail_fail=16:60`、`match_ok_fail=200:57`；实例化 `user_window.py:70` / `user_browser.py:62`。

---

## 2. 素材 token：两阶段替换（设计 §13.2）

**结论：两套替换、两个函数、共用一个正则；V3 commit 存的是 raw，真正跑的是 bundle 之后的产物。**

| 阶段 | 函数 | 输出形式 | 调用点 |
| --- | --- | --- | --- |
| ① 归一 | `collab_form.py::substitute_asset_refs:1518`（含 `normalize_collab_script:1643`、`ensure_img_dir:1568`） | `'F:/.../paste_xxx.png'`（`_py_literal:1447`，`\`→`/`） | `engine.py:380`、`engine.py:812-813`、`collab_form.py:1769/1776` |
| ② 打包 | `asset_bundle.py::bundle_script_assets:165`（token 解析 `:291-345`） | `IMG_DIR / '名'` + `IMG_DIR = IMG_PATH / '<stem>'`（`_set_img_dir:137`） | `mixin_collab.py:4837`、`panel.py:1362` |

> 行号漂移提示（2026-09-28 04:15 复核）：`asset_bundle.py` 已被并发会话改动，`bundle_script_assets` 由 `:163` → **`:165`**，
> `_set_img_dir` 由 `:135` → **`:137`**。其余 §1/§2 行号复核后未变。

- 正则（唯一）：`collab_form.py:1431 _ASSET_REF_RE = re.compile(r"(['\"]?)\[\[\s*([^\[\]\s]+?)\s*\]\]\1")` —— **引号可选**。
- 解析：`_find_asset_path:1507` → `find_asset(asset_id=)` 或按 `name` 兜底。
- 物理路径：`IMG_PATH = PROJECT_ROOT/"assets"/"images"`（`core/path.py:38`）；打包产物 `assets/images/brush_level/paste_*.png`；源素材 `screenshots/collab_snips/paste_*.png`。
- **没有 id→文件名表**：映射在会话 JSON `backend/script_generator/corpus/collab_sessions/<sid>.json` 的 `assets[]`（`history.py:15-29`、`session.py:116/165`）；V3 另有 sqlite `resources` 表，`gui_bridge.py:295-346` 单向同步进 V2 session 供打包用。

### 2.1 现状与设计 §8.2 的冲突（重要）

- 裸 `[[asset_x]]` **当前被接受且被正确替换**（实况 commit 原稿就是裸写，最终产物正确）。
- 变量二次套用必报错：`CHALLENGE="[[asset_x]]"` 后写 `[[CHALLENGE]]` → `素材引用没有对应素材: CHALLENGE`（实况 2 次）。
- 残留 token **没有出闸级检查**：`normalize_collab_script:1656` 丢弃 `_unresolved`；`engine.py:384` 调 `collab_script_issues(script)` **漏传 session** → 该处 token 检查静默失效；试运行写入只做 `compile()`（`mixin_runtime.py:249/275`）；bundle 的 unresolved 只进聊天说明。
  → 要禁裸 token，必须**同时改接受面（normalize/bundle）**，只改校验器会自相矛盾。

---

## 3. 脚本加载 / import / 执行入口（设计 §13.3）

| 项 | 位置 |
| --- | --- |
| 模块名 | `task_controller.py::_load_script:315` `"scripts." + task_name.replace("/", ".").replace(".py","")` |
| 缓存清理 | `:317` `importlib.invalidate_caches()`、`:320-330` 删对应 `.pyc`、`:332-333` 删 `sys.modules` |
| 真实加载 | `:336` `importlib.import_module(module_name)` —— **真包导入，顶层代码会执行** |
| 入口检查 | `:343-344` 必须有 `do_work`；`:347` → `_validate_do_work_annotation:351-374` |
| 注解白名单 | `_VALID_DO_WORK_TYPES = {"UserBrowser","UserWindow","Browser"}` `:49`；`_annotation_member_names:22-46` 支持字符串/Union |
| 调用 | `task_controller.py:246` `await script.do_work(user_browser)` |

**对 import smoke 的直接约束**：必须模拟「以 `scripts.<dotted>` 模块名导入」的上下文（包 `scripts/_trial/__init__.py` 存在，`_write_trial_file:254-256` 缺则创建），且**不得触发真实副作用**（不启动浏览器、不点击、不执行 `do_work`）。

---

## 4. 现有 API 事实源清单（设计 §4.1 要收敛的对象）

**共 7 处静态表 + 1 处动态表 + 散文若干，且互相矛盾。**

| # | 位置 | 内容 | 谁读它 |
| --- | --- | --- | --- |
| 1 | `agent.py:1404 ALLOWED_BROWSER_METHODS` | 16 项（**含** `invalidate_frame:1413`） | 校验器 `:8148`；修订 prompt `:9831`；`optimize.py:72,219` |
| 2 | `agent.py:1424 LOGIN_BROWSER_METHODS` | `goto`/`dmm_login` | `api_catalog` 同款语义 |
| 3 | `agent.py:1973` | 5 项子集 | 专用规则 `_none_browser_image_arg_errors` |
| 4 | `agent.py:6278` | 6 项（含**不存在**的 `click_game`） | 专用规则 |
| 5 | `api_catalog.py:12 ALLOWED_METHODS` | 15 项（**缺** `invalidate_frame`）；`_CARDS:37-163` 手写卡片 | `lookup_api:170`、`compact_catalog:194`、多个 prompt |
| 6 | `graph/plan_schema.py:14 _ALLOWED_BROWSER_METHODS` | 15 项（**缺** `invalidate_frame`） | `graph/nodes.py:795,832` |
| 7 | `collab_form.py:1830 _window_api_names()` | **动态** `dir(UserWindow)` 全部 callable | `collab_script_issues:1792,1801` |
| 8 | `provider_model.py:123-126` | 散文规则（唯一提到 `cheap`/`force_wide`） | V3 每个副轮 prompt |

**硬矛盾实例**：`invalidate_frame` 在 `agent.py:1413` 允许、在 `api_catalog.py:12` 与 `plan_schema.py:14` 缺失 → `lookup_api("invalidate_frame")` 回「NOT allowed」而校验器放行。

**已证实的签名漂移**：`api_catalog.py:91-94` 写 `wait_image(path, timeout=seconds, threshold=0.9)`；`user_window.py:969` 是 `wait_image(self, img_path, timeout=0)`（**无 threshold**），`user_browser.py:868` 才是 `(img_path, timeout=0, threshold=0.9)`。`cheap`/`force_wide` 只在 desktop 的 `match_image`（`user_window.py:563-576`）；browser 的没有（`user_browser.py:350-360`）。设计 §2 的漂移例子成立。

### 4.1 校验器现状（设计 §8 要替换的对象）

- `agent.py:7925 validate_script_local` → 转发 `agent.py:7984 validate_generated_code(...) -> list[str]`（`return errors` `:8356`）。**扁平字符串，无 code/line/symbol 结构。**
- 它会检查：语法/AST（`:8005-8014`）、`do_work` 入口与首参标注（`:8034-8050`）、IMG_DIR/素材路径（`:8056-8100`）、FSM 结构（`:8115-8145`）、**browser 方法白名单**（`:8148-8170`）、未定义调用（`:8173-8179`）、未定义名字（`:8182-8203`），外加 ~60 条业务规则（`:8206-8356`）。
- **它不检查**：async/await 一致性、关键字参数名、必填参数、import/Python policy（`iscoroutinefunction|inspect.signature` 在 agent.py 0 命中；`_BUILTINS:1426` 只用于「名字是否已定义」，不是禁用表；`eval/exec/subprocess/__import__` 无拦截）。
- `collab_form.py:1763 collab_script_issues(code, session=None)`：归一 → 素材解析 → `dir(UserWindow)` 接口名 → **真实签名检查** `_browser_call_signature_issues:1852`（未知关键字 `:1890`、位置参数过多 `:1905`、同步方法被 await `:1872`；**只认 `UserWindow`**，`:1862` 硬 import）→ 再叠加 `validate_script_local(..., free_mode=True, require_fsm=False)`（`:1813-1826`）。
- V3 侧：`collaborator_v3/adapters/script_validator.py::V2ScriptValidator:8` 只包一层 `collab_script_issues`；调用点 `runtime/runner.py:1172`，issues 直接进 `RuntimeResult.summary`（`runner.py:1209`）。

**可复用的已有能力（不要重写）**：真签名检查 + await 检查已在 `collab_form._browser_call_signature_issues` 存在，缺的是「按 profile 取契约、覆盖 kwargs 全表、返回结构化 issue、以及参数/import policy」。

### 4.2 `UserWindow.__getattr__` 是隐藏的 API 加宽路径（重要）

- `user_window.py:307-319`：未定义属性代理到 `self._target`（`Win32Target`），callable 一律包成 async。
- `Win32Target`（`backend/automation/win32_target.py:399`）公开方法 **40 个**，含 `move(x, y)`、`mouse_move(x, y)`、`key_combo()`、`close()`、`resize()` 等。
- 这解释了实况里模型为何写 `browser.move` / `browser.mouse_move`：**它们不是纯幻觉，是代理可真实到达的能力**。`move_by` 才是不存在的。
- 结论：契约 registry 必须是**显式批准集**，且测试要保证「代理可达但未登记」的名字一律被拒（设计 §20.3）。

---

## 5. 日志 / 事件 / 遥测写入关系

| 系统 | 位置 | 现状 |
| --- | --- | --- |
| 运行时日志备份 | `core/logging/runtime_backup.py::RuntimeLogBackup:45`，`begin_run:72-112`；唯一出口 `ctrl.py::emit_log:649-673`（内存 deque → `backup.record` → Qt 信号）；`task_controller.py:225` 开启 | 全量落 `logs/runtime/*.jsonl|.log`，12 次轮转 |
| domain_events | `sqlite_store.py::append_event:919-938` → `_append_event:885-912`；**无事件类型枚举**，`event_type` 全为字面量；唯一调用者 `runner.py::_event:587-591` | 只有 `turn_id`/`aggregate_id` 列，**无 subturn/action 列** |
| workspace trace | `gui_bridge.py:241-243 on_subturn` → `session_workspace.record_subturn:163-171` → `_append_jsonl:129-142`；根 `session_workspace.py:34` | `user_data/collab_workspaces/<sid>/trace/{turns,subturns,events}.jsonl` |
| trial registry | `runtime/trial_runs.py`（`ROOT:20`、`start_run:59`、`append_log:82`、`finish_run:95`、`get_run:106`、`latest_run:130`、`get_script:139`、`log_tail:156`） | `user_data/collaborator_v3/trial_runs/<task_id>/run_*.{json,log.jsonl,script.py}`；**只由 GUI 面板写**（`mixin_runtime.py:716-755`），登记先于启动（`:647` 先于 `:677`）→ 首行异常也有 run；但**依赖 `artifacts["v3_task_id"]` 且面板存活** |
| PseudoRecorder | `backend/automation/run_recorder.py::PseudoRecorder:97`；`event:148-160`、`maybe_keyframe:184-228`、`finish:254-284` | `screenshots/pseudo_record/<account>_<ts>_<script>/{timeline.jsonl,frames/*.jpg,summary.json}`；kind 为自由字符串：`session_start/log/keyframe/keyframe_error/black_on/black_off/session_end` + span `capture/sleep/click/match`（`user_window.py:167/437/464/538/623`）；**只由脚本自开**（`user_window.py:enable_pseudo_record:201-234`），无 TaskController 自动开启；`ctrl.py::emit_match_event:675-694` 只发 Qt 信号、不落库（印证设计 §19.1） |
| MINASHIGO_STEP | 常量/解析 `trial_cursor.py:18/27-35`；发送 `user_window.py:note_state:284-297`、`user_browser.py:200-213`（仅状态名**变化**时打） | 读取 `mixin_runtime.py:772-787` → `mixin_collab.py:4212` → `engine.py:921`；落在**3 处互不相通**的存储（runtime jsonl、trial_runs `.log.jsonl`、面板内存） |

---

## 6. 副轮序号重置的位置（设计 §14.1）

- 计数是 `_loop` 的**局部变量**：`runner.py:344 subturns_used = 0`，递增 `:371`，留痕 `:356-368 _record_subturn`。
- 两条入口都会重进 `_loop`：`run():123-141`、`resume():231`→`:322-331`→`_run_contained:143-167`（`:157` 调 `_loop`）→ **恢复后 `subturn` 从 1 重来**（与实测 42→1 一致）。
- `action_id` 由模型回复给定，runner 不重写 → `resume` 后 `action_id` 也从 `a1` 重来（实测第三段）。
- 记录字段：`runner.py:_record_subturn:653-675`（`subturn, turn_id, outcome, user_text, protocol, last_result, model, action{...}, workspace_asked, budget, extra`），落盘加 `at`（`session_workspace.py:170`）。

---

## 7. 查询指纹 / 缓存 / epoch / TOOL_NO_PROGRESS

- 分发：`runner.py::_query_batch:841`；命中回放 `:969-989`。
- 指纹：`queries.py::QuerySpec.cache_key:570-575` = `name:{归一化 json}`；注册表 `:723-753`，`QUERY_NAMES:761`。
- 缓存命中要求同一 `mutation_epoch`：`workspace.py:cached_query:152-156`；字段声明 `:95-99`；`remember_query:158-164`、`mark_mutation:166-168`（epoch+1 且清 `query_counts`）、`mark_plan_change:177-185`（只清 counts，**不动** epoch）；`model_summary:231-236`（只带最近 12 条同 epoch 结果）。
- **`TOOL_NO_PROGRESS` 有三个来源**：(a) 查询命中缓存回放 `:969-989`（实况那 8 次即此类）、(b) act 重复失败熔断 `:1078-1087`、(c) 预算结束分类 `:563-576`。
- `query_budget_exhausted` 写于 `runner.py:801`，挂载 `:435-441`；查询副轮临时 +1 上限 `:427-433`。
- **差距**：`cache_key` 不含 `task_revision / resource_versions / trial_epoch`（设计 §17.2 要求），空结果无 reason（`trial.get_latest_run` 空返回是 `{"run": null, "task_id": ...}` 且 `ok=True`，`queries.py:_exec_trial_latest:516-519`）。

---

## 8. finish 的校验与逻辑确认链

- finish 分支入口 `runner.py:469-503`（熔断 `_FINISH_REFUSAL_LIMIT` 检查 `:470-482`、闸门 `:483-485`、被拒留痕 `:486-503`）；提交成功 `:504-535`。
- 放行闸 `_validate_finish_script:1491-1578`，五段：无正文→用已验证正文（`:1510-1518`）→ 经典出码（`:1528-1537`）→ 逻辑确认闸（`:1546-1548`）→ hash 闸（`:1551-1560`）。
- **经典兜底走不到的原因**：`_generate_script_for_finish:1297-1335` 在 `:1310-1315` 先查 `logic_draft.confirmed`，未确认直接返回 `_logic_confirmation_required_result:1278-1295`。
- 逻辑确认写入点：`runner.py::resume:295-300`（`draft["confirmed"]=True` + `remember_fact` + `mark_mutation`），判定 `_is_affirmative_confirmation:1824-1860`；新草稿登记 `confirmed=False` 在 `:1639`。
- workspace 侧：`remember_validation:193-222`（失败时 `llm_script_fail_count += 1` `:208-209`、清 `finish_refusals` `:221-222`）、`script_is_validated:227-229`。
- 阈值：`runner.py:81 _LLM_SCRIPT_FAIL_BEFORE_CLASSIC = 3`；`max_subturns=24`、`max_extra_query_subturns` 默认取 `max_subturns`（`:90-92`、`:102`）。

---

## 9. 对设计文档的三处必要修正（否则实现会自相矛盾）

1. **§8.2「只接受字符串 token、裸 token 报错」必须同时改接受面**：现行 `_ASSET_REF_RE`（`collab_form.py:1431`）引号可选，normalize 与 bundle 都接受裸 token 且替换正确。只在校验器加规则 → 校验器与替换器互相矛盾。实现须为「严格模式」：校验路径禁裸 token，替换路径保留 legacy 兼容（不重验旧脚本）。
2. **§8.3「artifact gate 校验最终产物」的先决问题是"哪一份算最终产物"**：V3 commit 存 raw（带 `[[asset]]`），运行的是 GUI 侧 bundle 后的 `scripts/_trial/_gen_trial.py`。因此 receipt 必须**同时**绑 `source_sha256`（raw 模型稿）与 `artifact_sha256`（bundle 产物），并且需要一个「构造最终试运行脚本」的**纯函数**（现逻辑散在 `engine.apply_llm_reply` + `mixin_collab._push_code_to_host` + `mixin_runtime._write_trial_file` 三处），否则校验与运行永远不是同一份文本。
3. **§8.4 对齐清单要多带一处既存漏检**：`engine.py:384` 调 `collab_script_issues` 时未传 session（token 检查静默失效）；`normalize_collab_script:1656` 丢弃 `_unresolved`。

---

## 10. 未确认项（2026-09-28 修订：分「本轮已核实」与「仍未确认」两段）

> 原版把 9 条未确认项与「已完成」并列，自相矛盾。现改为：**只有 10.2 为空时才可称 Phase 0 全部完成。**
> 本轮已把原 9 条全部核实（其中第 9 条上一轮已核实），并追加 1 条（生成期 profile 事实，见 §12.3）。
> 逐条证据（含未确认部分标注）见 `tools/_scratch/phase0_residual_verification.md`。

### 10.1 本轮已核实（10/10，均有磁盘证据）

| # | 原未确认项 | 结论 | 关键证据 |
| --- | --- | --- | --- |
| 1 | V3 `view.code` 上游 | V3 无自己的 view 类型，**出码回流 V2**：`final_response["script"]` → `map_view_update_to_v2`（只拷 6 个键 `gui_bridge.py:127-130`）→ `workers.py:275-277` → `mixin_collab.py:3676` → **`:3740` 交给 V2** `engine.apply_llm_reply`（`engine.py:370` 读 script；**`:384` 调 `collab_script_issues` 未传 session**）→ `_apply_view:3119` → `:3168-3173 _push_code_to_host` | `gui_bridge.py:83,127-130`；`workers.py:277`；`mixin_collab.py:3676,3740,3119,3168-3173`；`engine.py:370,384` |
| 2 | V3 `resources` ↔ V2 `session.assets` 同步完备性 | 确为**单向 V2→V3**（3 个调用点）；**丢 `file_name`**（`canvas.py:64-82` 产出 7 键，`session_assets.py` 只读 6 键）；**无删除传播**（`sqlite_store.py:537-539` 只 INSERT；`collaborator_v3/` 内 grep remove/mark/REMOVED 0 命中）；**无反向写回** | `gui_bridge.py:168-173,182-187,276-285`；`canvas.py:64-82`；`session_assets.py`；`sqlite_store.py:537-539` |
| 3 | `get_browser_by_account_name` 返回什么 | **返回原始 `Browser`**（`ctrl.py:716-717`），既非 `UserBrowser` 也非代理；`TaskController.browser` 绑的就是它（`task_controller.py:61`）。**脚本实际拿到的对象**来自 `_browser_instances`(=MainLoopProxy) 或 `_window_instances`(裸 UserWindow) | `ctrl.py:716-717,432-434`；`task_controller.py:61` |
| 4 | `UserWindow` 是否包 `MainLoopProxy` | **不包**：`ctrl.py:344-349` 裸构造裸存，`:367` 同一裸对象还进 TaskFlow 全局池。只有 `UserBrowser` 走 `MainLoopProxy`（`ctrl.py:259,263-264,268-269`） | `ctrl.py:259,263-269,344-349,367` |
| 5 | GUI 未收到 task state 时 run 状态 | **会永久 `running`**：`finish_run` 只被 `_finish_v3_trial_record` 调用，收尾点仅 `:681`(start_task 异常) 与 `:849`(终态事件)；`_on_trial_state` 首行有 `if not self._trial_running: return`(`:814-815`)；`panel.py:130-138` 的 disconnect 只在**换 facade** 时；无面板 closeEvent；无 stale 修复；`trial_runs.latest_run:130-136` 只看 mtime 不看 status | `mixin_runtime.py:681,814-815,849`；`panel.py:130-138`；`trial_runs.py:130-136` |
| 6 | `trial_runs` 轮转/清理 | **无**：`cache_cleanup.py:179-223` 的 7 项里没有它（对照 `runtime_backup.py:154`、`stop_frame_archive.py:73` 都有轮转）。**但 `user_data/collaborator_v3/trial_runs` 当前不存在**（该目录下只有 4.2MB sqlite）→ 第 5 条是**潜在缺陷、尚未发生** | `cache_cleanup.py:179-223`；`runtime_backup.py:154`；`stop_frame_archive.py:73` |
| 7 | 是否存在第 4 份方法清单 | **存在且是活的**：`config/` 目录不存在，清单在 `config.json` 的 `system_prompt_template`（`## Available API (only these methods, do NOT invent others)`，offset 1483-2709，**12 个**方法），**被 `agent.py:830` 真的拼进经典生成器 prompt**。与 `agent.py:1404-1422`(16 项) **差 4 个**：`click_image_effect`/`invalidate_frame`/`enable_pseudo_record`/`finish_pseudo_record` → **`agent.py:1403` 注释「与 config system_prompt 一致」不成立**。反讽：该段 `wait_image(path, timeout=3)` 反而与 desktop 真实签名一致 | `config.json`(offset 1483-2709)；`agent.py:830,1403-1422` |
| 8 | `click_game` | 两个执行类 **AST 级都不存在**；全仓唯一命中 `agent.py:6284`（`_function_has_browser_action` 的 action_methods `:6277-6293`），消费点 `:6308/:6968/:7125` → 死规则 | `agent.py:6277-6293,6284,6308,6968,7125` |
| 9 | `api.search`/`api.get` 是否已注册 | **仍未注册**（以当前磁盘为准）：`queries.py` 849 行、仍 **13** 个查询、无任何 `api.*`。`render.search`/`render.details` 已写好（`render.py:71,156`）但**只被测试消费**（`tests/test_api_contract_registry.py:106,116,119,124`），生产链路未接线 | `queries.py:761`；`render.py:71,156`；`tests/test_api_contract_registry.py:106,116,119,124` |
| 10 | （新增）生成期是否存在执行目标/profile 事实 | **不存在**：`TurnWorkspace`(`workspace.py:75-129`) 无 `runtime_profile`/`execution_target`/`target_kind` 任何字段；整个 `collaborator_v3/` 中 `account` 只出现在 `runtime/trial_runs.py:59`（试运行登记，晚于生成）→ 详见 §12.3 | `workspace.py:75-129`；`trial_runs.py:59` |

### 10.2 仍未确认（实施前必须补齐，禁写猜测句）

> 以下均为**明确未确认**，不得当作已证实使用；括号内标注是「代码级推断」还是「未读」。

1. 各 `cap_v2_*` 能力是否真的新增/移出 rail 素材（只确认了 hook 装配点）——**未确认**。
2. Qt 在接收者销毁后是否仍投递 `QueuedConnection`：**代码级推断**，未做运行时实验。GUI 之外是否有模块补写 `finish_run`：grep 未见。
3. `agent.py:830` 所在函数名及其调用方未定位；`$THRESHOLD` 取值来源未确认（`agent.py` 11319 行，按纪律未整读）。
4. `click_game` 三个消费点各属哪条规则、是否实际改变判定（`agent.py` 未整读）；「影响面仅限启发式」是**推断**。
5. `render.search`/`render.details` 的返回结构是否符合设计 §6（`runtime_profile` / 批量 ids / `contracts_revision`）**未逐字段核对**。
6. `engine.apply_llm_reply` 的 `:400-570` 分支区间未逐行读；`_push_code_to_host` 之后到 `_gen_trial.py` 虽已于本轮复核（见 §12.8），但其间 `collab_form` 归一化的全部触发点未穷举。
7. 另一会话是否仍在实现 `api.search`/`api.get`——无法观测；第 9 条只对上述 mtime 的磁盘负责。

### 10.3 与 Phase 0 原版相矛盾的结论（6 条，实施前必读）

> **前提：Phase 0 已成半旧文档。** 另一会话在 2026-09-27 20:07–20:17 UTC（= 本地 09-28 04:07–04:17）改了 5 个文件。

1. **§4「7 处静态表」清单过期**：新增原版未记录的 `api_contracts/`（10 个 .py，`validator.py` 784 行）、`script_artifact.py`（484 行）、3 个契约测试；`collab_form.py` 已重写为 **2191 行**。
2. **【最严重】迁移半成品，新旧检查在同一函数内打架（已实测复现，非推断）**：新 validator 已按 profile 判定（`validator.profile_for_code` 能正确区分 UserBrowser/UserWindow），但旧的 `_window_api_names() = dir(UserWindow)` 名称检查在交付闸里**无条件执行**（`collab_form.py:1853-1867`；`_window_api_names` 定义 `:1894-1903`，实测 24 个名字，**不含 `match_image_multi`、不含 `goto`**）。实测对一份合法浏览器脚本：
   - `collab_contract_issues(..., profile="browser_user_browser")` → **0 条**（正确放行）
   - 同一脚本过交付闸 `collab_script_issues()` → **仍报「不存在的接口 browser.match_image_multi」**

   关键细节：同函数内的 `_browser_call_signature_issues` **已经**被门控（`:1868-1875` + `if not any(item.code.startswith("API_") ...)`），**唯独名称检查缺这道闸**。后果：占多数的浏览器脚本（242:14）在 V3 交付闸上仍会被拒。原版只说"校验器只认 UserWindow"，**没有**指出新 validator 已接入且两者并存冲突。
3. **§2 表格措辞方向写反**：`gui_bridge.py:295-346` 实际是「构造 `session_asset_ids` 并把 V2 素材 id **传进** V3」（`:295-299`/`:339-347`），方向仍是 V2→V3。§10.2 的"单向"结论正确，但 §2 那句「单向同步进 V2 session 供打包用」与自己的结论相反。**已按 §2 下方漂移提示修订。**
4. **两处默认 profile 方向相反**：`api_catalog.py:20 RUNTIME_PROFILE = "browser_user_browser"`，而 `collab_form.py:1765 _CONTRACT_PROFILE = None` → 默认 desktop（`:1779` 文档串）。叠加第 2 条（旧名称检查事实上强制 desktop），设计 §4.2 的"默认 desktop"在实践中变成**硬约束**。→ **需决策，不是实现细节；已由 §13.1 锁定为「不得静默默认」。**
5. §10.9 结论仍成立（`api.search`/`api.get` 未注册），补：render 层已写好且只被测试用。
6. **复核未变**的条目：`wait_image` desktop/browser 签名差异；`match_image_multi` 不在 `UserWindow`；`goto` 仅在 `navigation.py:7` 经 `UserBrowser.__getattr__:222-235` 代理可达；`ctrl.py:716` 返回原始 `Browser`；`UserWindow` 未包代理。

### 10.4 状态结论

Phase 0 主体 + 本轮补充调查均已完成；**10.2 仍有 7 条未确认**（都是"未读/推断"，不是"没查"）。
因此 Phase 0 的准确状态是：

> **Phase 0 主体完成；漂移补充调查完成。可以进入 Phase 1，但必须先落实 §13 的锁定决定，
> 且 10.2 的条目在触及相应代码前必须当场核实，不得引用为事实。**

---

## 11. Phase 0 完成后的直接动作（已锁定）

1. Phase 1 以 `UserWindow` 真实签名为准建 registry；`api_catalog` 降级为 facade（保持旧输出兼容）。
2. Phase 2 校验器从 registry 取「允许方法 / 参数 / await 属性」，并新增 Python policy、素材 token 专项、artifact gate、import smoke、`ValidationReceipt`。
3. Phase 3 新建事件类型枚举 + `EventEnvelope` + 层级 ID（现表无 subturn/action 列、无枚举）。
4. Phase 4 补 `trial.get_latest_run` 空返回 §17.1 形状；`cache_key` 扩到「任务/资源/trial epoch」。
5. Phase 5 让 TaskController 自动 start/finish run（现只在 GUI 面板且依赖 `v3_task_id`），并接 match/click/frame 结构化遥测。

> ⚠️ 本节（§11）写于 2026-09-28 早，**其 1/2 条已被并发会话部分执行**（见 §15）。
> 以 §13 的锁定决定为准；§11 仅作历史记录。

---

## 12. 本轮新增漂移调查（2026-09-28 补充，全部窄路径复核）

### 12.1 交付脚本的真实入口注解分布（决定 profile 的前提）

- **V3 实况**：脚本正文落在 `user_data/collaborator_v3/collaborator_v3.sqlite3` → `commits.result_json.final_response.script`（V3 **没有 scripts 表**）。13 条 commit 中 **8 条**含脚本正文，注解分布：

  | `do_work` 首参注解 | 条数 |
  | --- | --- |
  | `UserWindow` | 3 |
  | `Union(UserBrowser \| UserWindow)` | 3 |
  | 无注解（`async def do_work(browser)`） | 2 |
  | **`UserBrowser`** | **0** |

- **真正可运行脚本** `scripts/`（47 个含 `do_work`）：`UserBrowser` 20 / Union 16 / `UserWindow` 10 / 无注解 1。
- **corpus**（240）：`UserBrowser` 196 / Union 29 / `UserWindow` 14 / 无注解 1。
- 当前 `scripts/_trial/_gen_trial.py` 是 `Union(UserBrowser | UserWindow)`。
- 复现脚本：`tools/_scratch/phase0_probe_v3_facts.py`。

**结论**：协议 `provider_model.py:122` 写死 `async def do_work(browser: UserWindow)`，V3 实况**从未产出 `UserBrowser` 注解**（模型要么照抄 UserWindow，要么改写成 Union 对冲，要么干脆不写注解）；而真实脚本里 browser 侧占多数。
→ 与决定 §13.1 一致：**profile 既不能由协议写死，也不能由 corpus 数量决定**；无注解那 2 条更是**零 profile 信号**，只能落到 `unresolved`。

> 统计注意：`commit_c86723b0b9d54dd` 的 `final_response.text` 也含 `do_work` 字样（说明文字），做统计时不要把它当脚本。

### 12.2 执行目标选择链与静默 fallback

权威事实在 **Controller**，不在 V3：

- `ctrl.py:52-53`：`_browser_instances` / `_window_instances`（name → 实例）。
- 写入点：`ctrl.py:269`（`MainLoopProxy(UserBrowser)`）、`ctrl.py:323-349 register_window_target`（**裸** `UserWindow`）。
- GUI 派生 `_target` 的两条路径，**依据的字典不同**：
  - `mixin_runtime.py:621-629`（**试运行**时）：`has_win and not has_br → "window"`；`has_br and not has_win → "browser"`；两者都有且无倾向 → `"window"`。检查的是 **`ctrl._browser_instances`**（`:622`）。
  - `mixin_collab.py:4745-4754`（协作「用当前窗口」抓图时）：同款逻辑，但检查的是 **`ctrl._browsers`**（`:4748`）。
  → **两个 GUI 路径可能给出不同倾向**，是既存缺陷（Phase 1 修 profile 时不要沿用它们作为事实源）。
- 决策：`task_controller.py::_run_async:128-205`
  - 注解优先：`_detect_prefer_window`（单类型 → True/False，Union → None，**无注解 → None**）。
  - `prefer_window is None`：按 `_target` 取，取不到再取另一个（`:140-157`）。
  - 单类型注解：`_target` **可覆盖**注解（`:161-163`）。
  - **静默 fallback**：主实例缺失时改用另一个，**只记 WARNING**「脚本注解偏向X，但仅Y就绪，已自动改用Y」（`:176-188`）。
  - 两者都没有才报错（`:189-203`）。

**结论**：`_target` 是**试运行时刻**由 GUI 写入 account 字典的**派生值**，不是生成期事实。

### 12.3 生成期是否存在执行目标事实（回答"尤其要确认"那条）

**不存在。**

- `TurnWorkspace`（`workspace.py:75-129`）字段全集：`turn_id` / `task_id` / `viewport_snapshot_id` / `acquired` / `facts` / `hypotheses` / `failures` / `current_plan` / `invalidated_keys` / `task_kind` / `pending_evidence` / `pending_resources` / `query_results` / `query_counts` / `mutation_epoch` / `meaningful_actions` / `repeated_queries` / `initial_user_text` / `derivation_attempted` / `session_asset_ids` / `validated_script_hash` / `validated_script_name` / `validation_issues` / `validated_script_text` / `artifact_gate_available` / `validated_artifact_hash` / `validation_receipt_id` / `script_repair` / `codegen_cache` / `finish_refusals` / `llm_script_fail_count` / `prefer_classic_codegen`。
  **无 `runtime_profile` / `execution_target` / `target_kind` 任何字段。**
- 整个 `collaborator_v3/` 中 `account` 只出现在 `runtime/trial_runs.py:59 start_run(..., account: str)`（试运行登记，**晚于**生成）。
- 唯一提到执行类的是 `adapters/provider_model.py:122-123` 的**写死散文**。

→ 按决定 §13.1：**生成期必须为 `unresolved`**（除非 Runtime 先注入该事实）。**不得静默 desktop。**
→ 可行的注入源是 `ctrl._browser_instances` / `ctrl._window_instances`（账号级、生成前通常已就绪），**不是** `account["_target"]`（试运行才写）。

### 12.4 单端能力跨 profile 泄漏（本轮最严重发现）

- `match_image_multi` 在 desktop 侧**不存在**：实测 `in dir(UserWindow) = False`；`backend/automation/` 全目录 grep 无定义；只在 `UserBrowser:459` 与 `ImageMatchingMixin:175`。
- 但**旧的两套 profile-无关白名单**都放行它：`agent.py:1406`、旧 `api_catalog.py:14`（现已是 facade），且旧卡片（原 `:47-61`）**主动教模型用它**（含 `m['y']` 用法）。
- 并发实现已部分修正：`api_contracts/desktop_user_window.py:426-448` 把 `match_image_multi` 与 `goto` 登记为 `approved=False` 的 `REJECTED`（仅用于给"最接近的合法替代"报错）——与决定 §13.4 一致。
- **但 prompt 侧仍有残留**：`render.py:314-319 contracts_block()` **无条件前置** `return_type_banner()`，而 `render.py:28-39 _RETURN_TYPE_BANNER` 是 **profile 无关**文案，仍在教
  `match_image_multi -> list[dict]` 与 `then browser.click(best['x'], best['y'])`。
  → **registry 禁止、prompt 宣传**，等于新包内部又出现**第二事实源**，违反设计 §4.1。
- 另 `desktop_user_window.py:198`（`click` 的 `coordinate_space`）也写「坐标应来自 `match_image/match_image_multi` 的结果」（该处未加 profile 限定）。

### 12.5 `goto` / `dmm_login`

- `goto` 只在 `backend/browser/mixins/navigation.py:7`：`async def goto(self, url: str, retries: int = 3)`。`UserBrowser` 类体**无** `goto`，经 `__getattr__:222-235` → `self._browser`（`Browser` 组合了 `NavigationMixin`）才可达 → **`dir(UserBrowser)` 里也没有**。
- `dmm_login` 在 `UserBrowser` 类体 `:701`：`async def dmm_login(self, *, timeout=30_000, eager=False)` → **`eager` 有默认值**，故旧卡片 `dmm_login(timeout=30_000)` 不会 `TypeError`（原 §10 的疑虑解除）。
- 两者在 desktop 均不可达。

### 12.6 两套校验器的 API 裁决实测不一致（决定 §13.5 的直接证据）

同一份内存脚本（含 5 类 API 错误）实测，脚本：`tools/_scratch/phase0_probe_validators.py`。

| 注入的错误 | `collab_script_issues` | `agent.validate_script_local` |
| --- | --- | --- |
| `match_image_multi`（desktop 不存在） | **抓** | 漏 |
| `await browser.note_state()`（sync 被 await） | **抓** | 漏 |
| `wait_image(..., threshold=)`（无此参数） | **抓** | 漏 |
| `b_sleep(lo=, hi=)`（无此参数 + 缺必填 `seconds`） | **抓**（3 条） | 漏 |
| `click(10, 20, pianyi=(1,2))`（合法） | 通过 | 通过 |

→ `agent` 侧只报「缺少 IMG_DIR 赋值」与「未定义的名字 UserWindow」，**API 契约类错误 0 命中**。
→ 与决定 §13.5「两入口 API 错误输出必须一致」**相差最远**，必须先写回归测试锁定。

### 12.7 `click` 的解析优先级已按正确签名登记（决定 §13.3 已落实）

`api_contracts/desktop_user_window.py:181-206`：`click` 参数恰为 `x / y / down_time / pianyi`，全文件无 `button` / `send_mode`
（`Win32Target.click(x, y, button='left', send_mode='send')` 未泄漏）。

### 12.8 raw → artifact 的文本变换链（决定 §13.6 的边界）

实际有 **4 段**可变文本，其中 **2 段是条件性的**：

| # | 变换 | 位置 | 条件 |
| --- | --- | --- | --- |
| a | 素材 token → 路径字面量 | `collab_form.py::substitute_asset_refs` / `normalize_collab_script`（`:1656`） | 总是 |
| b | **前置 `ban_script_snippet`** | `mixin_collab.py:4827-4829` | 仅当 `session.artifacts["ban_script_snippet"]` 非空且正文未含 `_COLLAB_BAN_ZONES` |
| c | 素材打包 + `IMG_DIR` + 路径改写 | `mixin_collab.py:4835-4838` → `panel.py:1348 _bundle_assets_for_save` → `asset_bundle.py:165 bundle_script_assets` | 总是 |
| d | 注入伪录制 | `mixin_runtime.py:233-264 _write_trial_file` → `pseudo_codegen.py:161 inject_pseudo_record_for_trial` | 总是；**注入版编译失败则回退原文**（`:244-248`，`mode="raw-fallback"`） |

- 载体是 GUI 实例属性 `host._generated_code`（`mixin_collab.py:4838`）→ `_write_trial_file` 读它（`:234`）。
  `materialize_trial_script()` **必须吸收 a–d 全部**，否则 receipt 的 `artifact_sha256` **不等于**真正运行的文本。
- `inject_pseudo_record_for_trial` 是**确定性**的（`pseudo_codegen.py` 内无 `time`/`datetime`/`now`/`uuid`/`random`/`strftime`）。
  但注入文本与 `IMG_DIR` 都依赖 **`script_name`**（`_set_img_dir(code, stem)`、`inject(..., script_name=)`）
  → **receipt 必须一并绑定 `script_name`**；否则改名会让 hash 变化（或更糟：hash 未覆盖真正的差异）。
- `_write_trial_file` 已双归档 `code_at_trial_raw.py` / `code_at_trial_injected.py`（`:265-271`），artifact gate 可复用。
- 注意命名冲突：`_write_trial_file` 里的局部变量 `raw` **不是** receipt 的 `source_sha256` 对象
  ——它已是 (a)(b)(c) 之后的文本。三个阶段的正确命名：**source**（带 `[[asset]]`）/ **bundled**（`_generated_code`）/ **artifact**（实际运行的 `_gen_trial.py`）。

---

## 13. 已锁定决定（实施不得改题）

> 以下为 2026-09-28 用户拍板结果，视为需求，不需要重新讨论。

**13.1 profile 是 Runtime 事实，禁止静默默认。** 优先级：
(1) 当前试运行所选账号/目标的**真实执行类型** →
(2) 当前任务**明确绑定**的执行目标 →
(3) 「按某脚本修改」时，**基线脚本的 profile** 作为 provisional →
(4) 以上都不存在 → **`unresolved`，禁止静默猜 desktop/browser**。

- `242 UserBrowser : 14 UserWindow` 只说明历史脚本分布，**不能**决定当前任务的运行环境。
- `unresolved` 且脚本**只使用两端共同且签名一致的 API** 时，可用 **`portable_common`**：
  它**不是两个 profile 的并集，而是经过核实的安全交集**；脚本必须**分别通过 desktop/browser 两套契约校验**；
  receipt 记 `{"validated_profiles": ["desktop_user_window", "browser_user_browser"]}`。
- 一旦使用 `match_image_multi`、`goto` 等**单端能力**，就必须先确定具体 profile。
- **不得**为此询问用户「选 UserWindow 还是 UserBrowser」；Runtime 应从账号与执行目标自动判断；
  **只有根本没有运行目标、脚本又必须用单端能力时**，才提示用户选择运行账号/环境。
- `provider_model.py:122` 写死的 `async def do_work(browser: UserWindow)` **必须改为由 profile 决定**。

**13.2 `__getattr__` 可达 ≠ 受支持 API。** 必须区分「Python 运行时可达 ≠ 稳定脚本 API ≠ LLM 被授权使用」。
registry 只登记**明确批准、愿长期维护**的能力。确实要开放的代理方法必须：
标明 `resolution_source = UserWindow.__getattr__ → Win32Target`；记录**代理后的真实同步/异步形态**与签名；加**运行解析测试**。
**未登记的方法即使运行时可达，也必须被脚本闸门拒绝。** **不要**把 `Win32Target` 的 48 个公开方法自动并入契约。

**13.3 同名方法按 Python 真实解析顺序**：① `UserWindow` 类自身属性 → ② 实例属性 → ③ `UserWindow.__getattr__` 代理到 `Win32Target`。
因此 `click` 必须用 `UserWindow.click(x, y, down_time=0.12, pianyi=(0, 0))`；
**不得**因 `Win32Target.click` 也可见就放行 `button=` / `send_mode=`。
registry 记录**最终对脚本可见的 resolved contract**，不是机械收集所有同名方法。

**13.4 `match_image_multi`**：`browser_user_browser` **允许** / `desktop_user_window` **禁止** / `portable_common` **禁止**。
现有无 profile 的白名单与卡片属**错误事实源**：先写回归测试锁定，再迁移 facade。
**不要**给 `UserWindow` 补一个空壳 `match_image_multi` 迎合旧文档（除非以后单独实现且有真实测试）。

**13.5 两套校验器最终只能有一套契约判断核心。**
`agent.validate_script_local` 与 `collab_form.collab_script_issues` 保留为**兼容入口**，
但二者**必须调用同一个 contract validator，并显式传入 profile**；不得继续各维护一套白名单或反射逻辑。
迁移期先写测试证明：**同一脚本 + 同一 profile + 两个入口 → API 契约类错误输出一致**
（方法 / 参数 / await / 返回值判断必须相同；其他非 API 业务检查可暂时不同）。

**13.6 最终产物同时绑定两个 hash，但出闸的决定性对象是 `artifact_sha256`**（实际运行文本）：
`source_sha256`（带 `[[asset]]` 的模型交付源码）、`artifact_sha256`（解析/bundle/路径替换后**实际写入 `_gen_trial.py` 并运行**的文本）、
`materializer_version`、`asset_versions`。
必须抽出**唯一纯函数** `materialize_trial_script(raw_script, assets, runtime_profile) -> MaterializedScript`，
保存页 / validate / commit / 试运行**共用**；**禁止 GUI 保留一套私有替换逻辑、validator 再猜一套**。
流程：`raw 校验 → materialize → artifact 校验 → import/Fake Runtime smoke → receipt → 试运行前重算 artifact hash → 不一致则拒绝或重新校验`。

**13.7 Phase 1 范围限制**：只做 ① 新增 `api_contracts/` ② 新增契约测试 ③ 最小修改 `api_catalog.py`（facade）。
**不要**同时改 runner、事件系统、GUI；**不提前**实现事件系统、receipt、trial 升级、GUI materializer 迁移
——只需把它们依赖的 **profile 与双 hash 边界**设计正确。

**13.8 工作树安全**：不清理 / 不 reset / 不覆盖既有改动；不因 `collaborator_v3/` 未跟踪就重新生成。
开工前记录 `git status --short --untracked-files=all`、本阶段计划修改的精确文件、这些文件的 hash 或副本、
哪些是既有用户改动、哪些是本阶段新文件。

**13.9 测试命令（PowerShell 下必须显式展开）**：

```powershell
$tests = Get-ChildItem tests -Filter 'test_collab_*.py' | ForEach-Object FullName
python\python311\Scripts\python.exe -m pytest $tests -q
```

原写法 `pytest tests\test_collab_*.py` 在 PowerShell 下**不展开通配符**（pytest 收到字面量 → `file or directory not found`），
报告中不得写成"按原命令执行成功"。
**基线**（2026-09-28，`api_contracts` 落地前）：**536 passed, 1 skipped, 91s**；`api_contracts` 落地后**须重测**。

---

## 14. 并发写告警与工作树安全基线（2026-09-28 04:16 实测）

调查期间**另一会话正在本仓实时实现 Phase 1/2**。实测时间线（本地时间 UTC+8；UTC = 本地 − 8）：

| 本地时间 | 事件 |
| --- | --- |
| 04:04:24 | `design_executable_api_contract_phase0.md`（本文，Phase 0 主体） |
| 04:05:55 | **新建 `backend/script_generator/api_contracts/`**；04:05:56 `python_policy.py` |
| 04:07:36 | `registry.py` |
| 04:08:40-42 | `browser_user_browser.py` / `schema.py` / `desktop_user_window.py` |
| 04:08:46 / 04:08:50 | `verify_runtime.py` / `render.py` |
| 04:09:08 | **`api_catalog.py` 被重写（12529 → 5021 字节，facade 化）** |
| 04:09:22 / 04:09:54 | 新增 `tests/test_api_contract_registry.py` / `test_api_contract_runtime_drift.py` |
| 04:13:24 | `tests/test_collab_contract_validator.py` |
| 04:14:36 | **`collaborator/collab_form.py` 被改**（已接 registry，报错带「（改法：…）」） |
| 04:15:18 / 04:15:38 | `validator.py` / `receipt.py` + `collaborator_v3/runtime/validation_store.py` |
| 04:15:54 / 04:16:18 / 04:16:56 | `asset_bundle.py` / `script_artifact.py`（484 行） |

`git status --short --untracked-files=all` 实测：**401 条**（**54 modified + 347 untracked**）。
先前口头提到的"24 个改动"是 `--porcelain` 输出被截断造成的**低估**，本文件以 401 为准。
`api_contracts/`、`collaborator_v3/`、`script_artifact.py`、`tests/test_*contract*.py` 等**均为未跟踪**。

**对本任务的约束**：Phase 1 的阶段目标已被并发会话**部分完成**，且其
`registry.py:19-20 DEFAULT_PROFILE = "desktop_user_window"` 与决定 §13.1 **直接冲突**（`validator.py:784` 会静默回退到它），
另有 §12.4 的 prompt 侧 banner 泄漏、§10.3 第 2 条的交付闸名称检查未门控。
→ 实施前必须与该并发改动**对齐**，不要重复实现或覆盖；本文行号需重新核对。

**本阶段（调查）新增文件**（均为新增，未改任何既有文件）：
`tools/_scratch/phase0_probe_v3_facts.py`、`tools/_scratch/phase0_probe_validators.py`、
`tools/_scratch/phase0_residual_verification.md`。
**本阶段唯一修改的既有文件**：`backend/script_generator/design_executable_api_contract_phase0.md`
（修改前 SHA256 `62E3B1BC2E0702B8FAA0A22594612D635A6539597807C9DB5727055EB706683F`，198 行 / 20661 字节）。
> 本文自身每次编辑后 hash/行数都会变（存在自引用问题），**不要引用本文的"改后 hash"**；
> 需要冻结值时以 `tools/_scratch/phase1_freeze_manifest.txt` 为准。

### 14.1 并发产物的完整冻结清单（2026-09-28 04:30 实测）

- 并发会话写入窗口：**04:05:55 – 04:27:02**；最后一次写入是 `collaborator_v3/runtime/workspace.py`。
- 静默验证：04:28:54 → 04:30:24 **无任何新写入**（90s），判为已停止。
- 窗口内被写入的 `backend/`+`tests/` 文件共 **32 个**（不止先前报告的 17 个），完整 hash/size/mtime 清单：
  `tools/_scratch/phase1_freeze_manifest.txt`；`git status --short -uall` 快照：`tools/_scratch/phase1_git_status_freeze.txt`（**408 条** = 54 modified + 354 untracked）。
- **并发范围已超出"只做 Phase 1"**：除 `api_contracts/`(11 文件) 与 `script_artifact.py` 外，还出现
  `collaborator_v3/events/{schema.py,store.py}`（`EVENT_SCHEMA_VERSION` / `EventType` / `EventEnvelope`，属 **Phase 3**）、
  `collaborator_v3/runtime/ids.py`（层级 ID，属 **Phase 3**）、`collaborator_v3/runtime/validation_store.py`、
  `collaborator_v3/runtime/runner.py`(94818B)、`stores/sqlite_store.py`(50329B)、`domain/models.py`、
  `adapters/script_validator.py` + `ports/script_validator.py`，
  以及测试 `test_collab_v3_ids.py` / `test_collab_final_artifact_gate.py` / `test_collab_v3_runner.py`(80575B) / `test_collab_v3_sqlite_store.py`。
  → **审计范围必须扩展到 Phase 3 骨架**，不能只审 Phase 1/2。
- 两个相反的默认 profile **确认仍在**（并发已停，值已固定）：
  - `api_catalog.py:20 RUNTIME_PROFILE = "browser_user_browser"`，并被 `:22,32,42,46,57` 当作**模块级固定 profile** 使用
    → 旧 facade **硬编码 browser**，调用方无法覆盖。
  - `collab_form.py:1765 _CONTRACT_PROFILE: Optional[str] = None` → `:1825` 传给 `collab_contract_issues(profile=None)` → registry 静默落 desktop。
  → **facade=browser、交付闸=desktop，两处反方向硬编码**，正是决定 §13.1 要清除的对象。

---

## 15. 并发实现与锁定决定的对照（便于 Phase 1 收口）

| 锁定决定 | 现状（04:16 磁盘） | 证据 |
| --- | --- | --- |
| §13.1 profile 由事实决定 / 禁止静默默认 | ❌ **未落实**：只有两个 profile，`DEFAULT_PROFILE="desktop_user_window"`，`get_profile(None)` 静默回退；**无** `portable_common`、`unresolved`、provisional | `registry.py:19-20,42`；`validator.py:784` |
| §13.2 代理可达 ≠ 批准 | ✅ 已落实：`proxied` 字段 + `proxy_target_class` + 「代理可达 ≠ 已批准」文案 + `verify_runtime` 校验「proxied 必须不是类直接属性」 | `schema.py:116-118,189,253,267`；`desktop_user_window.py:476-481,511-514`；`verify_runtime.py:6` |
| §13.3 解析优先级 / `click` 签名 | ✅ 已落实 | `desktop_user_window.py:181-206` |
| §13.4 match_image_multi 三 profile 裁决 | ⚠️ registry 侧已落实（`REJECTED`/`approved=False`），**prompt 侧仍泄漏** | `desktop_user_window.py:426-448` vs `render.py:28-39,314-319` |
| §13.5 两入口 API 输出一致 | ❌ 未落实：实测 collab 抓 5 类、agent 抓 **0** 类；且交付闸内新旧检查并存冲突 | §12.6、§10.3 第 2 条 |
| §13.6 双 hash + 唯一 materialize 纯函数 | ⚠️ 进行中：`script_artifact.py` 已有 `build_artifact:96` / `import_smoke:276` / `fake_runtime_smoke:325` / `verify_artifact:411`；`asset_bundle.py` 04:15:54 被改；workspace 已加 `artifact_gate_available` / `validated_artifact_hash` / `validation_receipt_id` | `script_artifact.py`；`workspace.py:113-116` |

**需要你决策的两点（子代理明确提出）**：
1. `api_catalog.py:20 RUNTIME_PROFILE = "browser_user_browser"` 与 `collab_form.py:1765 _CONTRACT_PROFILE = None`（默认 desktop）**方向相反**
   —— 叠加 §10.3 第 2 条（旧名称检查事实上强制 desktop），设计 §4.2 的"默认 desktop"在实践中成为**硬约束**。
   已由 §13.1 覆盖（改为不得静默默认），但**两处相反默认本身仍需在 Phase 1 一并清除**。
2. 交付闸内的旧 `_window_api_names()` 名称检查是**删掉**还是**加 profile 门控**（子代理建议后者：与同函数内 `_browser_call_signature_issues` 一致地按 `API_*` 门控）。

---

## 16. 本文档的阅读顺序（给实施模型）

1. §13 锁定决定（**需求**，不得改题）；
2. §10.3 与 Phase 0 原版矛盾的 6 条（原版已成半旧文档）；
3. §12 本轮漂移调查（profile 选择链 / 生成期无 profile 事实 / 双 hash 边界）；
4. §15 对照表（哪些已落实、哪些没有）；
5. §1–§9 Phase 0 主体结论（行号除 §2 标注外未变）；
6. §10.2 仍未确认清单（触及相应代码前必须当场核实）。

---

## 17. 并发实现审计结论与红灯基线（2026-09-28 04:35，冻结后）

> 审计产物：`tools/_scratch/audit_api_contracts.md`（507 行）、`tools/_scratch/audit_consumers.md`（347 行）。
> 规格测试：`tests/test_api_contract_profile_policy.py`（**9 failed / 2 passed，刻意红**）。
> 冻结有效性已复验：04:28 之后**无任何** `backend/`+`tests/` 的 `.py` 被写入。

### 17.1 审计判定汇总（12 个焦点）

| 区域 | 焦点 | 判定 | 关键证据 |
| --- | --- | --- | --- |
| api_contracts | profile 默认与静默 fallback | ❌ 不满足 | `registry.py:20,41-47`；`validator.py:835-849`（只有恰好 `{UserBrowser}` 才判 browser，**Union/无注解一律落 desktop**）；包内 24 处入口继承 |
| api_contracts | desktop/browser 契约分离 | ✅ **满足** | 六方法逐项与真实签名一致（含 `update_frame` desktop `*, priority` vs browser 位置参数 `crop_game_canvas`）；`verify_all()` 两侧 0 漂移 |
| api_contracts | `portable_common` / `unresolved` | ❌ 不满足 | 全仓零命中，`has_profile` 皆 False，无安全交集逻辑 |
| api_contracts | prompt/render 泄漏单端能力 | ❌ 不满足（**活的**） | `render.py:28-39` banner + `:316` 无条件前置；活调用点 `agent.py:827-828,9849,10592,10662,10779`、`optimize.py:81,188` |
| api_contracts | `approved` 语义 | ⚠️ 部分 | `search`/`compact_catalog`/`lookup_text` 都过滤，`API_NOT_APPROVED` 有触发；**例外**：`detail/details` 不过滤（`render.py:112,166`）、banner 绕过 registry |
| api_contracts | validator 能力 | ⚠️ 部分 | 21 个 code 中 20 个有触发点（`ENTRY_NOT_CALLABLE` 死常量）；结构化字段与设计 §7 对齐。**「收集未定义名字」完全没实现**；`target_names` 只认 `browser`/`win`/入口首参 → **别名 `bb = browser; await bb.xxx()` 全绕过 API 检查**；`MODULE_SIDE_EFFECT` 漏 `X = f()` |
| api_contracts | `receipt` 绑定项 | ❌ 不满足 | 缺 `bundled_sha256` / `script_name` / `materializer_version` / `validated_profiles` / `transforms`（全仓零命中）；生产还漏传 `profile=`（`adapters/script_validator.py:32-36`）与 `run_id`/`subturn_id`/`script_name`（`runner.py:1279-1283`） |
| api_contracts | `verify_runtime` | ✅ 满足 | 真用 `inspect.signature:86-93` + `iscoroutinefunction:100`；`proxied=True` 跳签名但要求非类直接属性（`:143-148`）；`getattr_static` 列未登记真实方法（desktop 8 / browser 8） |
| api_contracts | `python_policy` | ⚠️ 部分（**有实质漏洞**） | `open/eval/exec/compile/__import__/subprocess/socket/shutil` 全禁（已实测）；**但 `os` 与 `pathlib` 放行** → 实测 `module_policy('os.system')` → True，`os.remove`/`os.popen`/`Path.write_text` 全不报，与设计 §4.3「任意文件写入默认禁止」冲突 |
| 消费者 | 两个相反默认 | ❌ 不满足（**被测试锁死**） | `api_catalog.py:20`=browser（被 `:25,28,32,42,46,57` 读）；`collab_form.py:1765`=None→desktop；锁定断言在 `test_api_contract_registry.py:23-24` 与 `:216` → **改默认必须同步改测试** |
| 消费者 | 交付闸 `_window_api_names()` | ❌ 不满足（**生产误拒，已复现**） | 名称裁决无条件：`collab_form.py:1853,1855-1867`；同函数签名裁决**已门控**：`:1870`。合法 browser 脚本 → 契约 0 条 vs 交付闸 2 条 |
| 消费者 | 两入口一致性 | ⚠️ 部分 | `collab_script_issues:1817` → `collab_contract_issues:1825`，后者有 `profile=`，**前者没有**；实测 API 错误数 **6 vs 0** |
| 消费者 | materializer 四段流水线 | ⚠️ 部分（2/4） | ✅(a)`script_artifact.py:121,126`、✅(c)`:137-143`（含 `script_stem=script_name:140`）；❌(b) `ban_script_snippet` 零命中、❌(d) 伪录制注入与 raw-fallback 零命中 → **receipt 的 `artifact_sha256` ≠ 真正运行的文本**。已确认接入生产（`adapters/script_validator.py:26-36` → `runner.py:1248-1291`） |

### 17.2 规格测试红灯矩阵（`tests/test_api_contract_profile_policy.py`）

| 目标测试 | 现状 | 失败原因 |
| --- | --- | --- |
| ① profile 缺失不得静默默认 | 🔴 | `get_profile(None)` 返回 desktop |
| ①b `unresolved` 存在 | 🔴 | 无此 profile |
| ② `portable_common` 且拒绝单端能力 | 🔴 | 无此 profile |
| ③ browser 允许 `match_image_multi` | 🟢 | — |
| ④ desktop 拒绝 `match_image_multi` | 🟢 | — |
| ⑤ 合法 browser 脚本不被交付闸误拒 | 🔴 | 名称裁决未门控 |
| ⑥ 两入口 API 结论一致 | 🔴 | agent 侧 0 条 |
| ⑥b 交付闸可显式指定 profile | 🔴 | `collab_script_issues` 无该形参 |
| ⑦ facade 返回 `PROFILE_REQUIRED` | 🔴 | `lookup_api` 无 `profile` 形参 |
| ⑦b 清除模块级默认 | 🔴 | `RUNTIME_PROFILE` 仍在 |
| ⑧ desktop prompt 不教单端能力 | 🔴 | banner 仍无条件前置 |

命令：`python\python311\Scripts\python.exe -m pytest tests\test_api_contract_profile_policy.py -q`
→ **9 failed, 2 passed**（与审计预测一致）。

### 17.3 新基线（并发落地后，覆盖旧 536）

```powershell
$tests = Get-ChildItem tests -Filter 'test_collab_*.py' | ForEach-Object FullName
python\python311\Scripts\python.exe -m pytest $tests -q
```
→ **49 个文件 / 613 passed, 1 skipped（80.5s）**。并发自带的 4 个契约测试单跑 → **96 passed**。
**绿测试 + 错行为**：上述缺陷全都不在它们的覆盖里（且 `test_api_contract_registry.py:236-242` 反向要求那份 profile 无关 banner 必须存在）。

### 17.4 待决策（审计提出，影响后续测试与修法）

1. `render.detail/details` **不过滤** `approved=False`（`search` 过滤而它不过滤，不对称）——有意还是疏漏？
2. `python_policy` 放行 `os` / `pathlib`——保留还是收紧？这决定设计 §4.3「任意文件写入默认禁止」是不是仅文档声明。
3. 修默认 profile 会**打破两条现有测试**（`test_api_contract_registry.py:23-24`、`:216`），确认可同步修改这两条断言。
4. `config.json` 的 `system_prompt_template`（12 个方法，唯一消费点 `agent.py:830`）仍在 prompt 里 —— 纳入迁移清单还是另行处理？

### 17.5 最小修正顺序（用户 2026-09-28 指定，不得合并成大改）

1. 清除两处相反默认（含同步改那两条锁定测试）；
2. 加 `unresolved` / `portable_common`；
3. 去掉交付闸里旧 `_window_api_names()` 的 API 名称裁决；
4. 统一两校验入口（`collab_script_issues` 加 `profile` 形参并透传）；
5. 修 prompt 的 profile 泄漏（banner 按 profile 过滤或删除）；
6. 补齐真实 materializer（ban gate + 注入 + raw-fallback）与 receipt 绑定项；
7. 跑回归（46→49 个协作测试 + 契约专项 + `tests/test_validator_false_positives.py`）。

---

## 18. 第一步（profile 政策）完成记录 —— 2026-09-28

> 用户指定顺序的第 1 步，**单独完成、单独报告，未进入第 2 步**。

### 18.1 改动清单（11 个文件）

| 文件 | 改动 |
| --- | --- |
| `api_contracts/registry.py` | 新增 `UNRESOLVED_PROFILE` / `PORTABLE_PROFILE` / `PROFILE_REQUIRED_CODE` / `ProfileRequiredError` / `effective_profile()` / `verifiable_profile_names()` / `diagnostic_contracts()`；**删除 `DEFAULT_PROFILE`**；`get_profile(None/"")` 改为抛错；`contract()` 默认不再返回未批准条目（需 `include_unapproved=True`）；按安全交集构造 `portable_common` |
| `api_contracts/__init__.py` | 导出同步（去 `DEFAULT_PROFILE`，加新符号） |
| `api_contracts/render.py` | 新增 `_spec()`（缺失/未确定 → portable）；7 处 `get_profile(profile)` 改走它；`lookup_text` 在 profile 未知时对单端能力返回结构化 `PROFILE_REQUIRED` |
| `api_contracts/validator.py` | `validate_source` 走 `effective_profile`；`profile_for_code` 重写（Union/无注解/无法解析 → `unresolved`，不再落 desktop）；新增 `profile_source_for_code`（explicit/annotation/unresolved） |
| `api_contracts/verify_runtime.py` | 4 个入口走 `effective_profile`；合成 profile 跳过真实类核验（`verify_profile` 对无 `target_module` 者返回 `[]`） |
| `api_catalog.py` | **删除模块级 `RUNTIME_PROFILE`**；`lookup_api`/`compact_catalog`/`api_contracts_block`/`generate_tool_hint` 新增 `profile=`；模块常量改为安全交集 |
| `script_artifact.py` | `FakeTarget` / `verify_artifact` 走 `effective_profile`；`ArtifactVerdict` 新增 `profile_source` |
| `collaborator/collab_form.py` | `collab_script_issues` 新增 `profile=` 形参并透传 |
| `collaborator_v3/runtime/runner.py` | receipt 比对改用 `effective_profile`（此前会用 `unresolved` 与凭证里记录的 profile 不符而误判失效） |
| `tests/test_api_contract_registry.py` | 用户批准修改的两条锁定断言（`:23-24`、`:216`）+ 3 条连带断言 |
| `tests/test_api_contract_runtime_drift.py` | 3 条测试改为只核验绑定真实类的 profile／用 `include_unapproved=True` 读拒绝条目 |

### 18.2 那"24 个继承默认"的调用点分类（用户要求先分类再改）

| 类别 | 调用点 |
| --- | --- |
| **A. 能够取得真实 profile（必须显式传入）** | `adapters/script_validator.py:32-36`（`verify_artifact` 仍漏传 profile，属第 6 步）、`runner.py:1348/1357`（注解 provisional → 已改为 effective）、GUI 试运行链（`mixin_runtime.py:621-629` 已有真实目标，尚未接线，属第 6 步） |
| **B. 只能使用 `portable_common`** | `render.search/index_text/detail/details/compact_catalog/contracts_block/lookup_text/rejection_text`（`profile=None` 时）、`api_catalog` 全部旧通路（经典生成/修订/优化 prompt）、`validator.validate_source/validate_artifact`、`script_artifact.FakeTarget/verify_artifact`、`verify_runtime.target_class/proxy_target_class/verify_profile/unregistered_real_methods` |
| **C. 必须暂时返回 `unresolved`** | `validator.profile_for_code` 的 Union / 无注解 / 无法解析分支；`registry.get_profile(None/""/未知)` → `ProfileRequiredError` |
| **D. 仅测试/开发调用** | `registry.diagnostic_contracts(include_unapproved=True)`、`registry.profile_revision`/`revision_summary`、`verify_runtime.verify_all`、`tests/*` |

> 没有机械地把所有 `None` 换成 `portable_common`：C 类保留 `unresolved` 语义，
> A 类标注为仍需接线（第 6 步），D 类保留诊断入口。

### 18.3 验证结果

| 命令 | 结果 |
| --- | --- |
| `pytest tests/test_api_contract_profile_policy.py tests/test_api_contract_registry.py tests/test_api_contract_runtime_drift.py tests/test_collab_contract_validator.py tests/test_collab_final_artifact_gate.py tests/test_validator_false_positives.py -q` | **111 passed, 3 failed** |
| 49 个协作测试 | **613 passed, 1 skipped**（与改前基线一致 → **零回归**） |

剩下的 3 条红灯是**刻意留给后续步骤**的：`test_05`（交付闸 `_window_api_names`，第 3 步）、
`test_06`（两入口一致，第 3 步）、`test_08`（profile 无关 banner，第 2 步）。

### 18.4 运行时可观测行为（实测）

```text
profile_names      : ('browser_user_browser','desktop_user_window','portable_common','unresolved')
portable approved  : 15 个方法（= desktop 15 ∩ browser 16）
get_profile(None)  : ProfileRequiredError
effective_profile  : None/""/unresolved → portable_common
portable has multi : False      portable has goto : False
注解 UserBrowser   → browser_user_browser
注解 UserWindow    → desktop_user_window
注解 Union         → unresolved     无注解 → unresolved
facade lookup_api('match_image_multi')          → PROFILE_REQUIRED
facade lookup_api('match_image', profile=...)   → ## match_image
ALLOWED_METHODS    : 15 项，不含 match_image_multi
```

### 18.5 第 2 步待办（未开始）

按用户顺序：修 renderer —— 过滤 `approved=False`（`detail/details` 目前不过滤）、
去除 profile 无关 banner（`render.py:28-39` + `:316`）、迁移 `config.json` 那 12 个方法的活清单。
（`test_08` 即为此步的验收。）

---

## 19. 第 2–7 步实施记录（2026-09-28，自主执行）

> 起点：第 1 步完成（§18）。每一步单独记录文件、行为变化与测试。
> **未回滚任何未知来源改动，未清理脏工作树，未删除/弱化断言**（旧断言要么修正为正确要求，要么保留）。

### 19.1 第 2 步：修 renderer

**文件**：`api_contracts/render.py`、`api_contracts/__init__.py`、`api_catalog.py`、`agent.py`、
`tests/test_api_contract_registry.py`

**行为变化**

| 项 | 改法 |
| --- | --- |
| `detail` / `details` 过滤 `approved=False` | 默认**读不到**未批准条目；`details` 把它们放进 `rejected` 并带 `API_NOT_APPROVED`；`include_unapproved=True` 仅供本地诊断 |
| profile 无关 banner | **删除** `_RETURN_TYPE_BANNER` / `return_type_banner()`，并从 `contracts_block` 移除无条件前置；`api_catalog.api_return_types_banner()` 降级为返回 `""`（兼容旧调用方） |
| config.json 活清单 | 新增 `agent._replace_available_api_section()`：把模板里 `## Available API (only these methods…)` 整段换成 registry 渲染的契约；`agent.py` 不再把该字段当能力事实源，也不再插 banner |
| 旧断言 | `test_api_catalog_block_and_banner_still_render` 改为断言"不再有 banner"；新增 `test_desktop_contracts_block_never_teaches_single_sided_capability`、`test_config_method_list_cannot_grant_api_capability`、`test_details_rejects_unapproved_contract_ids` |

**测试**：`test_08` 转绿；专项 115 passed；**49 个协作测试 613 passed / 1 skipped（无回归）**。

### 19.2 第 3 步：修交付闸

**文件**：`collaborator/collab_form.py`、`agent.py`、`tests/test_api_contract_profile_policy.py`

**行为变化**

- **删除** `_window_api_names()`（`dir(UserWindow)` 名称表）与 `_browser_call_signature_issues()`
  （UserWindow 专属签名检查）——两份可漂移的第二事实源，会误拒合法浏览器脚本。
  方法/参数/await/返回值一律由 `api_contracts.validator` 按 profile 判定。
- `agent.validate_script_local()` 新增 `profile=` 形参，并合并
  `_contract_api_issue_strings()`（把结构化 issue 转成带 `[API_xxx]` 前缀的旧字符串）；
  profile 解析与交付闸**同一套**（`profile_for_code` → 否则 portable），
  否则同一脚本在两个入口会被判成不同 profile，给出 `API_NOT_APPROVED` vs `API_UNKNOWN`。

**测试**：`test_05` / `test_06` / `test_06b` 转绿；**8 条目标测试全部转绿**；
`test_collab_v3_runner.py` 有 1 处旧构造需修（见 19.6）；协作套件 613 passed（无回归）。

### 19.3 第 4 步：修 Python 能力策略

**文件**：`api_contracts/python_policy.py`、`api_contracts/validator.py`、
`tests/test_api_contract_python_policy.py`（新）、`tests/test_api_contract_registry.py`

**行为变化**

- **`os` 从白名单移入禁用表**：`os` / `os.path` / 任何 `os.*` 一律拒绝（含 system/popen/remove/
  unlink/rename/replace/mkdir/makedirs）。
- **`pathlib` 只留纯路径能力**：构造与属性读取允许；分两级禁用 ——
  `write_text/write_bytes/touch/mkdir/rmdir/unlink/symlink_to/hardlink_to/lchmod/read_text/
  read_bytes/glob/rglob/iterdir` **按名字即拒**（非路径对象上几乎不出现）；
  `replace/rename/open/remove/chmod/walk/makedirs/removedirs` **只在接收者解析为 Path 时拒**
  —— 否则 `"a-b".replace(...)` 会被误伤（已加回归测试 `test_str_replace_is_not_mistaken_for_file_operation`）。
- **import alias**：新增 `import_alias_map()`（`import os as system_os` / `from pathlib import Path as P`），
  新增 `POLICY_NOT_ALLOWED` code 与 `_policy_issues_for_call()` 接线。

**测试**：新增 10 条全部通过；协作套件 613 passed（无回归）。
旧断言 `test_python_policy_allows_language_basics_...` 里把 `os` 列为允许 → 按决定修正。

### 19.4 第 5 步：修 validator（别名 + 未定义名称）

**文件**：`api_contracts/validator.py`、`tests/test_api_contract_validator_aliases.py`（新）

**行为变化（别名绕过是阻断项）**

- `_receiver_aliases()`：跟踪 `bb = browser` 与 `cc = bb` 链式简单赋值；
  **重新赋值即失效**（`alias` 清除、执行器参数本身被赋值则从 target 集合移除）；
  tuple unpack 与属性赋值**不解析**（不猜）。
- `_resolve_receiver()`：把接收者解析回执行器根名，报错用根名（`browser.match_image_multi`），
  便于模型定位。
- `_receiver_unverified_issues()`：接收者无法归属时**不把"无法分析"说成"已验证"** ——
  方法名是**单端**契约能力 → `API_RECEIVER_UNVERIFIED`（**error**，阻断）；
  共有能力名 → 同 code 但 `warning`（避免误伤恰好同名的普通方法）。
- **补未定义名称检查** `undefined_name_issues()`（设计 §8.1，此前**完全缺失**）：
  覆盖注解里的类型名（`def do_work(browser: UserWindow)` 缺 import 会在定义时 NameError）；
  `from x import *` 时放弃该检查（不猜）；可用 `check_undefined_names=False` 关闭。

**实测**：`bb = browser; await bb.match_image_multi([])` → `API_NOT_APPROVED`（阻断）；
链式别名同样；`bb = object()` 后 → `API_RECEIVER_UNVERIFIED`(error)；合法调用 0 issue。

**测试**：新增 11 条全部通过；协作套件 613 passed（无回归）。

### 19.5 第 6 步：补齐 materializer 与 receipt

**文件**：`script_artifact.py`、`api_contracts/receipt.py`、`backend/script_generator/execution_profile.py`（新）、
`gui/widgets/script_gen_panel/mixin_runtime.py`、`gui/widgets/script_gen_panel/mixin_collab.py`、
`tests/test_api_contract_materializer.py`（新）、`tests/test_execution_profile.py`（新）、
`tests/test_collab_v3_runner.py`

**行为变化**

- `ArtifactBuild` 拆成**三段**：`source`（带 token 原稿）/ `bundled`（解析+打包后、注入前）/
  `artifact`（**实际运行**文本），各自一个 hash；新增 `transforms`、`script_name`、
  `materializer_version`、`ban_blocked`。
- 新增唯一入口 `materialize_trial_script(source, …, ban_snippet=, with_pseudo_record=)`：
  **ban gate（阻断，不改文本）→ asset bundle → 伪录制注入 + 编译失败回退**。
- `verify_artifact` 改为调用它；检查分工：AST/契约/策略跑 `bundled`（模型可写部分），
  import / fake-runtime 冒烟跑 `artifact`（真正运行文本）—— 否则会把我们自己的
  注入 `import os` 误报成越权（已加测试 `test_policy_checks_do_not_flag_our_own_injection`）。
- `receipt` 新增并绑定 `bundled_sha256` / `script_name` / `materializer_version` /
  `transforms` / `validated_profiles`，`mismatches()`/`is_valid_for()` 同步支持。
- GUI `_write_trial_file` 改为调用同一个 `materialize_trial_script`
  （`already_bundled=True, dry_run=False`），删掉自己那份注入+回退实现；
  禁用区不再被 `_push_code_to_host` 自动前置（改为 gate）。
- **新增唯一 profile 解析入口** `execution_profile.resolve_execution_profile(account, controller)`：
  任务绑定 → Controller 真实实例 → 基线 provisional → `unresolved`；
  `mixin_runtime` 与 `mixin_collab` **两处 GUI 都改调它**（此前一个查 `_browsers`、
  另一个查 `_browser_instances`，可能相反）。

**测试**：materializer 13 条 + resolver 8 条全部通过；协作套件修 1 处旧构造后 613 passed。

### 19.6 第 7 步：Phase 3 骨架审计（只标记，不扩建）

| 组件 | 状态 | 证据 |
| --- | --- | --- |
| `collaborator_v3/events/{schema,store}.py` | ❌ **纯骨架，零引用**（连测试都没有） | 全仓 grep 只有本包内部自引用；生产事件仍走 `runner.py::_event` → `sqlite_store.append_event` |
| `collaborator_v3/runtime/ids.py` | ❌ **未接入生产** | 只有 `tests/test_collab_v3_ids.py` 引用；`runner.py`/`sqlite_store.py` 都不 import 它 |
| `runtime/validation_store.py` | ✅ **已接入且完整** | `runner.py` 用来存/取凭证；`to_json/from_json` 自动带上新增字段 |
| `workspace.py` 的 `artifact_gate_available` / `validated_artifact_hash` / `validation_receipt_id` | ✅ 已接入 | `runner._run_artifact_gate` / finish 凭证闸 |
| `stores/sqlite_store.py` 的 `subturns` 表 + `max_subturn_seq()` | ⚠️ **半接入** | 表与查询存在，但 `runner.py:344` 仍用**局部** `subturns_used = 0` → 主轮恢复后序号仍从 0 重来（设计 §14.2 要求单调递增） |

已按用户要求给未接入的两个骨架加**明确标记**（`events/schema.py`、`events/store.py`、`runtime/ids.py`
的模块 docstring 顶部写明"未接入生产路径 / 本轮不接线"）。
**未修** subturn 序号问题：它需要把"序号语义"与"每轮预算"分开设计（现在同一个变量），
属 Phase 3 范畴，本轮按约束不扩建。

### 19.7 最终测试结果

| 测试集 | 结果 |
| --- | --- |
| profile-policy + 契约专项 + validator 一致性 + materializer/receipt + false_positives（10 文件） | **158 passed** |
| 49 个协作测试 | **613 passed, 1 skipped** |
| 仍失败 | **无** |

### 19.8 仍存在的问题（未解决，不得隐瞒）

1. **重复事实源**（本轮已消除 3 处，仍有 4 处）：
   - 已消除：`_window_api_names()` 名称表、`_browser_call_signature_issues()` 签名检查、profile 无关 banner。
   - 仍存在：① `agent.ALLOWED_BROWSER_METHODS`（16 项，与 registry 并行的白名单，`validate_generated_code` 仍在用）；
     ② `graph/plan_schema._ALLOWED_BROWSER_METHODS`（15 项）；③ `config.json` 模板里那段清单**文本仍在文件里**
     （已被整段替换、不再生效，但没删，属"deprecated/ignored"）；
     ④ `agent.py` 自带的"未定义名字"检查与新的 `undefined_name_issues()` 语义重叠（文案不同）。
2. **未接线的 profile 来源**：`execution_profile` 已建好且两处 GUI 已调用，但
   **V3 runner / adapter** 仍未拿到权威执行目标 —— `adapters/script_validator.py` 调 `verify_artifact`
   时不传 `profile`，`runner.py` 用 `profile_for_code()`（注解 provisional）。
   即：目前生产的 profile 仍是"注解 + 安全交集"，不是"Runtime 事实"。
3. **`os` 收紧的连带影响**：伪录制注入自己会生成 `import os` + `os.getenv`，
   因此**任何对注入后文本做策略检查的调用方都会误报**；本轮的规避方式是"策略只查 `bundled`"。
   若以后有人对 `artifact` 直接跑 `validate_source`，会踩这个坑（已在 `verify_artifact` 里注释说明）。
4. `ENTRY_NOT_CALLABLE`（validator）、`NEEDS_CAPABILITY_CONTRACT`、`policy_summary` 仍是无触发点的数据，
   按用户决定**只登记、不删除**。
5. `python_policy` 对 `pathlib` 的 `exists()/is_file()/stat()` 等只读元数据查询仍放行（未在禁用清单里），
   属有意保守（宁少误伤）；如需更严需单独决策。
6. Phase 3 的 subturn 序号单调性、事件统一入口（`events/`）、层级 ID 接入均未做。

### 19.9 下一步建议（按优先级）

1. **把 profile 接到 Runtime 事实**：`runner.py` / `adapters/script_validator.py` 传入
   `execution_profile.resolve_execution_profile()` 的结果，并让凭证记录 `profile_source`（现在只记 profile）。
2. **清理剩余白名单**：`agent.ALLOWED_BROWSER_METHODS`、`graph/plan_schema` 改为从 registry 派生；
   `config.json` 那段文本标记 deprecated。
3. **补端到端一致性测试**：真实 GUI 流程下断言 `sha256(scripts/_trial/_gen_trial.py) == receipt.artifact_sha256`。
4. **Phase 3**：subturn 序号与 run 预算分离；`events/` 接入 `runner::_event`；层级 ID 替换 `turn.id`。

---

## 20. 收尾冻结（本轮停止实施）

> 用户 2026-09-28 认可第 2–7 步结果后，明确要求**暂停实现**、保留工作区、不进入剩余架构阶段。
> 本节是这一轮的收尾记录：清单、测试、以及**三处刻意未接入**的部分。

### 20.1 最终测试结果（收尾时实跑）

| 测试集 | 命令要点 | 结果 |
| --- | --- | --- |
| 专项 10 文件（profile-policy / 契约专项 / validator 一致性 / materializer+receipt / false_positives） | `pytest tests\test_api_contract_profile_policy.py tests\test_api_contract_registry.py tests\test_api_contract_runtime_drift.py tests\test_api_contract_python_policy.py tests\test_api_contract_validator_aliases.py tests\test_api_contract_materializer.py tests\test_collab_contract_validator.py tests\test_collab_final_artifact_gate.py tests\test_execution_profile.py tests\test_validator_false_positives.py -q` | **160 passed** |
| 49 个协作测试 | `$tests = Get-ChildItem tests -Filter 'test_collab_*.py' \| % FullName; pytest $tests -q` | **613 passed, 1 skipped** |
| 仍失败 | — | **无** |

> ⚠️ "测试全绿"只代表**当前实施范围**完成，**不代表**完整新架构已落地 —— 见 §20.3 的三处刻意未接入。

### 20.2 本轮改动冻结清单

完整 SHA256 / 大小 / mtime 见 **`tools/_scratch/round2_final_manifest.txt`**（100 行）。摘要：

- **A. 冻结清单（32 文件）内的变化项 18 个**：`api_catalog.py`、`api_contracts/{__init__,python_policy,receipt,registry,render,validator,verify_runtime}.py`、
  `collaborator/collab_form.py`、`collaborator_v3/events/{schema,store}.py`、`collaborator_v3/runtime/{ids,runner}.py`、
  `script_artifact.py`、本文档、`tests/{test_api_contract_registry,test_api_contract_runtime_drift,test_collab_v3_runner}.py`
- **B. 新增文件 7 个**：`execution_profile.py`、`agent.py`（本会话首次修改，故不在 32 文件冻结集内）、
  `tests/test_api_contract_profile_policy.py`、`test_api_contract_python_policy.py`、
  `test_api_contract_validator_aliases.py`、`test_api_contract_materializer.py`、`test_execution_profile.py`
- **C. scratch 产物**：`audit_api_contracts.md`、`audit_consumers.md`、`phase0_residual_verification.md`、
  `phase0_probe_v3_facts.py`、`phase0_probe_validators.py`、`phase1_freeze_manifest.txt`、
  `phase1_git_status_freeze.txt`、`phase1_hash_manifest.txt`，以及遗留临时文件 `_tmp_probe_merge.py`（未删，按"停止"要求不动工作区）

**并发**：04:27:02（并发会话最后一次写入）之后**无外来写入**，本轮多次复核最近写入全部来自本会话。
**遵守**：未 reset/checkout/清理 408 项脏工作树；未删除任何旧兼容字段；未回滚未知来源改动；未弱化/删除断言。

### 20.3 三处刻意未接入（本轮不实现，等下一次明确任务）

| # | 未接入项 | 当前状态 | 接入需要做什么 |
| --- | --- | --- | --- |
| 1 | **Runtime 权威 profile** | `execution_profile.resolve_execution_profile()` **已实现**，`mixin_runtime.py` / `mixin_collab.py` 两处 GUI 已改调它；但 **V3 runner / `adapters/script_validator.py` 仍未拿到权威执行目标** → 生产 profile 仍是「入口注解 provisional + portable 安全交集」 | GUI 选择账号/执行目标 → `resolve` → MainTurn/Task workspace → V3 runner → validator/materializer → receipt；receipt 记录 `{runtime_profile, profile_source, target_revision, account_ref}`；**执行目标变化后旧 receipt 必须失效**；注解只能作 `profile_source="annotation"` 的 provisional |
| 2 | **恢复后的全局副轮序号** | `sqlite_store` 已有 `subturns` 表与 `max_subturn_seq()`；但 **`runner.py:344` 仍用局部 `subturns_used = 0`** → 主轮恢复后序号从 0 重来（实况「42 后又从 1 开始」的根因） | 把 **`subturn_seq`（主轮内持续递增，取自 store）** 与 **`budget_used`（本次执行段预算）** 彻底分成两个量；恢复只重置预算，不重置序号 |
| 3 | **本地事件系统** | `collaborator_v3/events/{schema,store}.py` 与 `runtime/ids.py` **全仓零生产引用**（`ids.py` 仅被自己的测试引用）；已在三个模块 docstring 顶部标记「未接入生产路径 / 本轮不接线」 | 等前两项完成后再统一设计：`EventEnvelope`、层级 ID、trial/match/click 事件、projection + epoch、LLM 证据查询。**禁止现在半接线** |

### 20.4 下一阶段优先顺序（用户给定，记录备用）

1. Runtime 真实 profile 接线（当前最重要缺口）
2. 真实落盘文件 hash 端到端测试：`sha256(scripts/_trial/_gen_trial.py) == receipt.artifact_sha256`，
   覆盖 asset bundle / pseudo-record injection / raw fallback / `script_name` 影响 / atomic replace 后内容
3. 清理剩余白名单（`agent.ALLOWED_BROWSER_METHODS`、`graph/plan_schema._ALLOWED_BROWSER_METHODS` 改为从 registry 派生；
   `config.json` 旧文本保留但标 deprecated 且不参与能力判断；`agent.py` 旧未定义名称检查与新检查先做**差异测试**再决定合并）
4. 修复恢复后的副轮序号（同 §20.3 第 2 项）
5. 最后才接事件系统

### 20.5 停止声明

本轮**到此停止**：不新建实现、不清理脏工作树、不删除旧兼容字段、不半接线事件系统。
下一轮从 §20.4 的第 1 项开始。

---

## 21. 阶段 1 实施记录：补完代码闸门的剩余连接（2026-09-28）

> 用户批准继续后按七阶段实施。本节记录**阶段 1**（其余阶段见后续章节）。
> 未改已提交原方案书；未回滚未知来源改动；未清理 408 项脏工作树。

### 21.1 交付内容

| # | 要求 | 实现 |
| --- | --- | --- |
| 1 | 真实 profile 写入 Task/MainTurn workspace | `TurnWorkspace` 新增 `runtime_profile` / `profile_source` / `target_revision` / `account_ref` / `run_id`；新增 `ActionRunner._apply_execution_target()` |
| 2 | 贯穿 runner → validator → materializer → receipt | `runner._validation_profile()` + `_validate_with_profile()` + `_run_artifact_gate(profile=…)`；`V2ScriptValidator.validate/verify_artifact` 新增 profile 与目标参数；`verify_artifact(profile_source=…, target_revision=…, account_ref=…)`；`ValidationReceipt` 新增同名字段 |
| 3 | 记录 `profile_source` 与目标版本；目标变化使旧 receipt 失效 | 新增 `execution_profile.target_revision_for()`（`tgt_<hash16>`，组成 = profile+source+account+实例指纹）；`receipt.mismatches/is_valid_for` 新增 `profile_source` / `target_revision` 比对 |
| 4 | 真实落盘文件断言 | 新增 `script_artifact.write_materialized()`（原子写 + 返回落盘 sha256），GUI `_write_trial_file` 改调它；新增 4 条落盘测试 |
| 5 | 恢复后副轮序号不重置 | `_loop` 把 `budget_used`（执行段预算）与 `subturn_seq`（主轮内单调）**拆成两个量**；新增 `_starting_subturn_seq()` / `_persist_subturn()` / `_begin_run_segment()`；副轮身份真正写进 SQLite `subturns` 表（此前只写 trace，表是空的） |
| 6 | 两个白名单改由 registry 派生 | `agent.ALLOWED_BROWSER_METHODS` / `LOGIN_BROWSER_METHODS` 与 `graph/plan_schema._ALLOWED_BROWSER_METHODS` 改为 `_registry_derived_*()`；实测两侧与 registry 并集**完全一致**（18 项） |
| 7 | 旧/新未定义名称检查差异测试后合并 | 新增 `tests/test_undefined_name_check_parity.py`：逐样本比对旧（`agent._find_undefined_calls` / `_find_undefined_name_loads`）与新（`validator.undefined_name_issues`）；**新检查补齐了旧检查的 dunder 跳过**，并证明新检查无覆盖缺口、且更广（模块级/类体/注解） |

**GUI 链路**：`mixin_collab._execution_target_payload()`（`host._selected_account()` + `facade.controller` → `resolve_execution_profile().to_dict()`）→ `_start_llm_turn` payload → `workers.CollabV3Worker` → `gui_bridge.run_v3_gui_turn` → `facade.send_message/resume(execution_target=…)` → `runner`。

**兼容性**：主轮 id 不是层级形状（老记录 / 兼容测试用 `turn_1`）时，`run_id`/`subturn_id` 降级为显式 `run_legacy_*` / `st_legacy_*`，**不伪造成新式可信 ID**（设计 §28）。

### 21.2 新增/修改文件

- 新增：`tests/test_collab_v3_execution_target.py`（12 条）、`tests/test_undefined_name_check_parity.py`（5 条）
- 修改：`execution_profile.py`、`collaborator_v3/runtime/{workspace,runner}.py`、`collaborator_v3/interaction/facade.py`、`collaborator_v3/adapters/{gui_bridge,script_validator}.py`、`script_artifact.py`、`api_contracts/{receipt,validator}.py`、`agent.py`、`graph/plan_schema.py`、`gui/.../mixin_collab.py`、`gui/.../mixin_runtime.py`、`tests/test_api_contract_materializer.py`、`tests/test_api_contract_registry.py`、`tests/test_execution_profile.py`、`tests/test_collab_v3_runner.py`

### 21.3 测试结果（阶段 1 收尾实跑）

| 测试集 | 结果 |
| --- | --- |
| 专项 12 文件（含阶段 1 新增 3 个） | **181 passed** |
| 49+2 个协作测试 | **625 passed, 1 skipped** |
| 仍失败 | 无 |

修过的旧断言 2 处：`test_execution_profile.py::test_no_facts_is_unresolved`（记录键名按用户指定改为 `profile_source`）、`test_collab_v3_runner.py` 的 budget 全等断言（新增 `subturn_seq` 字段）。均为**接口有意变更**，未删测试、未弱化其余校验。

### 21.4 过程中发现并处理的问题

1. **`subturns` 表此前是空的**：runner 只把副轮写进 trace，从未调用 `store.put_subturn`。阶段 1 才真正落库；否则 `max_subturn_seq()` 永远是 0，"恢复不归零"无从实现。
2. **层级 ID 形状校验会拒绝老轮次 id**：`ids.new_run_id("turn_1", …)` 抛 `ValueError`，直接让 63 个 V3 集成测试失败。按设计 §28 改为降级 legacy id，而不是放宽形状校验。
3. **`verify_artifact` 的 profile 来源**：显式传入的 `profile_source` 优先，否则按注解判定 —— 避免把权威来源误写成 annotation。
4. exFAT 上出现一次瞬时写失败（Win32 1175），独占打开与目录写探针均正常、无并发写入，重试即成功。

### 21.5 端到端实况复现情况

- **已复现**：`sha256(落盘 _gen_trial.py) == receipt.artifact_sha256`（含 bundle / 注入 / raw-fallback / script_name 变化 / 原子替换五类覆盖）—— 用真实文件与真实 `verify_artifact` 跑通。
- **未复现**：真实 GUI 点击「试运行」的完整链路（需要 Qt 面板与真实账号/窗口），目前用同一条 `write_materialized` + 同一 `materialize_trial_script` 覆盖其逻辑等价路径。

---

## 22. 阶段 2 实施记录：接入本地事件系统与层级 ID（部分完成）

> 状态：**核心已接入并测试通过；两个子项未做**（见 §22.3）。阶段 3–7 未开始。

### 22.1 已接入生产的部分

| 要求 | 实现 | 证据 |
| --- | --- | --- |
| 统一 `EventEnvelope` + 单一写入口 | runner 新增 `_emit_event()`（`EventStore` + `EventEnvelope`，带 task/main_turn/run/subturn/action 层级 ID）；`_event()`（按 turn）与 `_ws_event()`（按 workspace）都转发到它；写失败退回旧 `append_event`，再失败丢弃（留痕是加固不是闸门） | `runner._emit_event/_event/_ws_event` |
| 所有可信 ID 由 Runtime 生成 | run/subturn 由 `runtime/ids.py` 生成（`new_run_id` / `new_subturn_id`）；**主轮 id 形状不合法时降级为显式 `run_legacy_*` / `st_legacy_*`**，不伪造成新式可信 ID（设计 §28） | `runner._segment_run_id/_subturn_id` |
| 接入 runner（关键低频事件） | 已发：`MainTurnStarted`、`MainTurnPaused`、`MainTurnFailed`、**`MainTurnResumed`（新）**、`ActionRequested`、`ActionSucceeded`/`ActionFailed`、`InteractionRequested`、**`QueryBatchCompleted`（新）**、**`QueryCacheReplayed`（新）**、**`ScriptCandidateCreated`（新）**、**`ScriptValidated`（新）**、**`ScriptCommitted`（新）**、**`ValidationReceiptIssued`（新）** | runner 内 `_event/_ws_event` 调用点 |
| 事件名兼容 | 仍写既有字面量（观测性测试/GUI/日志按它读），规范名由 `EventType` + `LEGACY_EVENT_ALIASES` 映射（`canonical_event_type`） | `events/schema.py` |
| 最小投影（6 张） | 新增 `events/projections.py`：`current_task` / `current_resources` / `latest_script` / `latest_validation` / `latest_trial` / `outstanding_blockers`；**只读**既有表，可重建幂等；老记录标 `legacy=True`；空试运行给 `reason` 码（`NO_RUN_FOR_CURRENT_TASK`）而不是只有 `run: null` | `events/projections.py` + 11 条测试 |
| 全部本地 SQLite | 事件写既有 `domain_events`（含层级 ID 列）；投影只读既有表 + 凭证文件 + trial 记录；无网络服务 | — |
| 老记录可读不伪造 | `EventStore` 保留 legacy 行原样；`SubturnRecord` 用 `INSERT OR IGNORE`（首次记录为准）；legacy id 显式命名 | `events/store.py`、`sqlite_store.put_subturn` |

### 22.2 测试结果（阶段 2 后实跑）

| 测试集 | 结果 |
| --- | --- |
| 阶段 2 新增（`test_collab_v3_events_projections.py` 11 条 + 阶段 1 的 12 条） | **23 passed** |
| 专项 11 文件 | **169 passed** |
| 51 个协作测试 | **636 passed, 1 skipped** |
| 仍失败 | 无 |

### 22.3 未做的两个子项（明确登记，不假装完成）

1. **主轮 id 仍是 legacy 形状**：`interaction/facade.py` 仍用 `turn_<uuid>`（`new_main_turn_id()` 未接）。
   影响：新主轮不会得到 `mt_<UTCstamp>_<hex>` 形状的 ID，`run_legacy_*` 降级路径成为常态。
   接入前必须处理老轮次的兼容读取（`store.get_turn("turn_…")`）。
2. **试运行事件未接入**：`trial.started/finished/runtime.exception` 目前只在 GUI 侧写 trial 记录
   （`runtime/trial_runs.py`），没有走 `EventStore`。设计 §19.6 要求由 TaskController 自动登记，
   属阶段 5。
3. 另外：投影尚未接进 query gateway（`query.catalog` 里还看不到这 6 张投影），
   属阶段 3「按需查询投影」。

### 22.4 阶段 3–7 状态

| 阶段 | 状态 |
| --- | --- |
| 3 主轮空白 / 查询指纹 / 空结果原因码 / fact-user_claim-hypothesis | **未开始**（`latest_trial` 投影已含空结果原因码，是其中一小块） |
| 4 副轮 Orient 与证据约束 | **未开始** |
| 5 试运行与伪录制 2.0 | **未开始** |
| 6 自主调参与最小修改 | **未开始** |
| 7 用户个人词典 | **未开始** |

---

## 23. 阶段 2 收尾 + 阶段 3 部分实施（2026-09-28 续）

### 23.1 阶段 2 现已完成（两个尾巴补上）

| 尾巴 | 实现 | 证据 |
| --- | --- | --- |
| 试运行事件未接入 | `trial_runs` 新增事件出口 `set_event_sink()`；`start_run` 发 `trial.started`、`finish_run` 发 `trial.finished`，失败时额外发 `runtime.exception`；`bootstrap.initialize_v3()` **自动接线**到 `EventStore`，脚本不需要主动调用任何录制 API（设计 §19.6）。`start_run` 新增可选 `main_turn_id` | `runtime/trial_runs.py`、`bootstrap.py`、4 条新测试 |
| 主轮 id 仍 legacy | `facade._new_turn_id()` 改用 `ids.new_main_turn_id()`（`mt_<UTCstamp>_<hex>`），失败退回旧 `turn_<uuid>`；实测 `is_trusted_main_turn_id=True`、`id_kind=main_turn`；无测试锁定旧形状、facade 是唯一生成点 | `interaction/facade.py` |

**阶段 2 至此：7 项要求全部接入生产**（Runtime 生成 ID / EventEnvelope / 关键低频事件 / 6 张投影 / 本地 SQLite / 老记录可读不伪造 / runner 单一写入口）。

### 23.2 阶段 3 已实现的部分

| 要求 | 实现 |
| --- | --- |
| 规范化查询指纹 | `TurnWorkspace.query_key()` = `name` + 归一化参数（JSON 键序规范化）+ **任务修订** + **相关资源版本** + **变更 epoch**；新增 `note_task_revision()` / `note_resource_version()`；`acquire()` 自动把取得的资源版本记进指纹；`cached_query()` 增加**指纹二次核对**（手工塞旧 key 也不回放） |
| 缓存含空结果 | `remember_query()` 记录空结果；`cached_query()` 命中即回放（含空）——已有 + 新测试锁定 |
| 空试运行原因码 | `queries._exec_trial_latest` 改为结构化：`status="empty"` + `reason`（`NO_RUN_FOR_CURRENT_TASK` / `QUERY_SCOPE_MISMATCH`）+ `task_id` + `trial_epoch` + `recommended_next_action`；非空时给 `status` 与 `run`。**只描述现象，不宣布根因** |

### 23.3 阶段 3 仍未做（明确登记）

1. **fact / user_claim / hypothesis 三分层**：表结构已在（`claims` / `claim_evidence` / `evidence` / `task_facts`），`ClaimStatus` 枚举也在，但**尚未接入** propose_claim 的写入与晋升规则。
2. **`propose_claim` 的证据校验**：`evidence_refs` 是否真实存在、是否在允许作用域——**未实现**。
3. **主轮空白 + 短索引**：`projections.projection_index()` 已实现并测试，但**未接进主轮上下文**（主轮仍是既有 workspace 摘要；投影也未接进 query gateway，模型暂时查不到这 6 张投影）。
4. 阶段 4–7 仍未开始。

### 23.4 测试结果（本节收尾实跑）

| 测试集 | 结果 |
| --- | --- |
| 专项 11 文件 | **169 passed** |
| 52 个协作测试（新增 `test_collab_v3_query_fingerprint.py`） | **652 passed, 1 skipped** |
| 仍失败 | 无 |

### 23.5 过程中的一次真实事故与修复（如实记录）

新增的试运行事件测试**把 run 写进了真实 `user_data/collaborator_v3/trial_runs/task_1/`**（6 个文件，05:36:13–05:36:30），并因此污染了同一文件里"空试运行"的断言（`latest_run("task_1")` 变得非空）。

- 核对：目录在阶段 0 已确认原本不存在，6 个文件时间戳与内容（`demo.py`、`task_1`）全部来自我的测试 → **属我自己的测试产物**，不是用户数据。
- 处理：删除该 `task_1` 目录（目录回到空态），并给测试加 **autouse fixture**：隔离 `trial_runs.ROOT` 到 `tmp_path`、每个用例前后重置全局事件出口。
- 复验：测试全绿，真实 `user_data/.../trial_runs` 文件数回到 **0**。

---

## 24. 阶段 3 完成 + 阶段 4 实施记录（2026-09-28 续）

### 24.1 阶段 3：现已 4/4 完成

| 要求 | 实现 | 证据 |
| --- | --- | --- |
| 主轮只获短索引、按需查询投影 | 新增封闭查询 **`context.index`**（任务 id / 修订 / 目标摘要 + 可用投影名，**不夹带正文**）与 **`context.projection`**（按需取一张投影，未知名字给可用清单） | `queries.py` 新 `QuerySpec` + 3 条测试 |
| 规范化查询指纹 | `query_key = name + 归一化参数 + 任务修订 + 资源版本 + mutation_epoch`；`acquire()` 自动记资源版本；`cached_query()` 追加指纹二次核对 | `workspace.py` + 12 条测试 |
| 缓存含空结果 | 空结果进缓存并回放；`mark_plan_change()` 不清事实缓存（防回归） | 同上 |
| 空试运行原因码 | `trial.get_latest_run` 返回 `status/reason/trial_epoch/recommended_next_action`（`NO_RUN_FOR_CURRENT_TASK` / `QUERY_SCOPE_MISMATCH`） | `queries._exec_trial_latest` + 3 条测试 |
| fact / user_claim / hypothesis 分层 | `TurnWorkspace` 新增 `user_claims` 与 `claim_layer()`；`runner._propose_claim()` 统一分层：用户目标/偏好/授权/选择 → `fact`；用户对外部现实的说法 → `user_claim`；模型推测 → `hypothesis`；事实不被后续主张覆盖 | `workspace.py` / `runner.py` + 12 条测试 |
| propose_claim 证据约束 | `evidence_refs` 必须**真实存在**（工作区待验证证据 id 或已取得资源 id），否则拒绝并列出缺失引用；不静默降级 | 同上 |

### 24.2 阶段 4：已实现部分

| 要求 | 实现 |
| --- | --- |
| 同一次响应含 `orientation` + `action`，**不额外调用模型** | `parse_action_envelope` 放开并校验 `orientation`；`ActionEnvelope` 新增该字段 |
| 不保存/不展示自由思维链 | `orientation` **只允许 8 个约定键**（`ORIENTATION_KEYS`），任何额外字段（如 `reasoning`）直接 `MODEL_PROTOCOL_ERROR`；列表字段归一化；自由文本既不落库也不上屏 |
| 重大歧义先问用户 | 新增 `runner._orientation_gate()`：`material_ambiguities` 非空时，`act`/`validate_script`/`finish` 被拒（`INTERACTION_NOT_JUSTIFIED` + 歧义清单 + "先查或先问"），只放行 `query`/`request_interaction`/`update_plan`/`propose_claim` |
| 低风险细节自主决定 | 闸门只对**非空**歧义生效；无歧义时 `act`/`finish` 照常放行（有测试锁定，防止过度拦截） |
| 推测标 hypothesis 并给验证方法 | `_propose_claim` 对 hypothesis 追加要求：必须有 `verification_plan` 或 `missing_evidence`，否则拒绝 |
| 事实结论带 evidence_refs | 阶段 3 已实现；阶段 4 补齐验证方法要求 |
| 协议文本同步 | `provider_model._SYSTEM_PROTOCOL` 新增 orientation 契约、歧义闸门规则、证据/推测规则、"未复测不得说已修复" |

### 24.3 阶段 4 仍未做（明确登记）

1. **"未复测不得说已修复"只在协议文本里，没有硬闸门**。要真正拦住，需要在 finish 时检查
   "本次交付之后是否有新的试运行证据（trial 事件 / 新 run）"，而试运行由 GUI 侧登记、
   与主轮的关联（`main_turn_id`）本轮才刚打通（`trial_runs.start_run` 新增可选 `main_turn_id`，
   但 GUI 还没传）。**接入前不宜做文本关键词闸门**（误伤风险高）。
2. `orientation` 目前只用于闸门，**未落进事件/投影**（设计 §18.2 未要求保存，但若要审计
   "当时怎么理解需求"，需要一个结构化投影项）。

### 24.4 测试结果（本轮收尾实跑）

| 测试集 | 结果 |
| --- | --- |
| 专项 11 文件 | **169 passed** |
| 54 个协作测试（新增 `test_collab_v3_claim_layers.py`、`test_collab_v3_orientation.py`） | **673 passed, 1 skipped** |
| 仍失败 | 无 |

修过的旧断言 2 处（`test_collab_v3_claim_layers.py` 的两条）：推测现在必须给验证方法 —— 属**有意收紧**，未删测试。

---

## 25. 阶段 7 实施记录：用户个人术语词典（2026-09-28 续）

### 25.1 交付内容

新增 `backend/script_generator/user_terminology.py`（单用户、本地 JSON、无网络）：

| 要求 | 实现 |
| --- | --- |
| 本地只有一个用户，不建多用户系统 | 词典落 `user_data/collaborator_v3/user_terminology.json`，作用域固定 `local_user`；无账号/租户维度 |
| "抖动"默认 = `click_offset` | `default_store()` 首次调用种入本用户偏好：`preferred_meaning="click_offset"`、定义"点击时增加小幅随机坐标偏移"、**confidence 0.9**、`source=local_profile_default` |
| 任务上下文可覆盖 | `resolve()` 按设计 §23.3 顺序：① 本句已明确 → ② 参考脚本已定义 → ③ 任务上下文 → ④ 用户词典默认 → ⑤ 仍有实质歧义则**询问**（返回 `requires_confirmation` + 现成的三选一问句） |
| 滑动轨迹/页面抖动等例外 | 默认条目的 `exceptions` 明确写出 `gesture_path_jitter` / `retry_delay_jitter` / `viewport_motion` / `cursor_wiggle` 的适用场景 |
| **不能授予 API 权限** | `grants_api_permission()` **恒返回 False**；解析结果里没有 permission 字段；测试断言解析前后 registry 批准状态完全不变 |
| **不进全局提示词** | 只提供 `terminology.resolve` **封闭查询**（按需查）；`_SYSTEM_PROTOCOL` **未加入**任何词典内容（这是刻意的，符合设计 §23.2） |
| 用户明确改口 → 新版本 + 审计 | `record_user_definition()` 版本 +1、旧版本进 `history` 且 `active=False`；LLM 自行归纳只能 `propose_meaning()`（confidence ≤ 0.5），**不能覆盖**用户明确偏好 |

### 25.2 测试结果

| 测试集 | 结果 |
| --- | --- |
| `tests/test_user_terminology.py`（含按需查询 3 条） | **17 passed** |
| 专项 11 文件 | **169 passed** |
| 54 个协作测试 | **673 passed, 1 skipped** |
| 仍失败 | 无 |

### 25.3 仍未做

1. 术语解析**未接进 Orient 自动流程**：模型需要自己调 `terminology.resolve`；
   刚接入的 `_orientation_gate` 只看模型自报的 `material_ambiguities`，
   不会因为用户句子里出现词典词而提醒。
2. 「词义偏好不能授予 API 权限」目前靠**恒 False 的函数 + 不写进提示词**保证；
   没有做成"若某处据词义放行能力就测试失败"的全局断言。

---

## 26. 七阶段总体状态（本轮结束）

| 阶段 | 状态 | 说明 |
| --- | --- | --- |
| ① 补完闸门剩余连接 | ✅ 完成 | 7/7 |
| ② 事件系统与层级 ID | ✅ 完成 | 7/7（含试运行事件自动发生、主轮层级 ID） |
| ③ 主轮空白/查询指纹/事实分层 | ✅ 完成 | 4/4（投影按需查询、指纹含任务/资源/epoch、空结果原因码、fact/user_claim/hypothesis） |
| ④ 副轮 Orient 与证据约束 | 🟡 6/7 | "未复测不得说已修复"仅入协议文本，**无硬闸门**（缺 trial↔主轮关联） |
| ⑤ 试运行与伪录制 2.0 | ⬜ 未开始 | 6 个诊断查询、match/click 结构化遥测、诊断码均未做 |
| ⑥ 自主调参与最小修改 | ⬜ 未开始 | AST/语义 diff、安全范围、复测闸门均未做 |
| ⑦ 用户个人词典 | ✅ 完成 | 词典 + 解析顺序 + 不授权 + 修订审计 + 按需查询 |

---

## 27. 阶段 5 实施记录：试运行与伪录制 2.0（2026-09-28 续）

### 27.1 交付内容

**结构化遥测摄取层**（`runtime/trial_runs.py`，append-only，与文本日志分开存放）：

| 函数 | 作用 |
| --- | --- |
| `append_trial_event()` | 写一行结构化 JSON 到 `<run>.events.jsonl`；**封闭事件类型集合** `EVENT_KINDS`（vision.match_completed / input.click_attempted / input.click_completed / frame.captured / runtime.exception / script.note），未知类型直接拒绝 |
| `record_match()` | §19.2 全字段：template(asset_id/version/hash/name/path_ref/size)、frame_id/frame_size、search_region、threshold、best_score、second_best_score、**score_margin（本地算）**、matched、best_bbox、match_mode、force_wide、cheap、latency_ms、error。**取不到的写 None 并保留键，不伪造** |
| `record_click()` | §19.3 全字段：source_event_id、source、raw_xy、click_offset、**final_xy（本地算）**、delivered、frame_before/after、**visual_effect_observed 与 expected_scene_reached 分开记**、confirm_policy、latency_ms、failed_stage、error |
| `record_frame()` | 关键帧**引用**（图像存文件、事件只放引用）；同一帧去重（§19.4） |
| `record_exception()` | 异常结构化事件（脚本第一行炸也有） |
| `list_trial_events()` / `get_trial_event()` / `get_trial_frame()` | 分页/下钻；frame 只返回**句柄**（路径/尺寸），不塞 base64 |
| `bind_active_run()` / `clear_active_run()` / `ingest_match_hud()` | 把真实执行器的 HUD 事件**自动**归位到活动 run；执行器不知道的字段留 None |

**诊断层**（`runtime/trial_diagnosis.py`）：

- `aggregate_consecutive()`：连续相同事件折叠（带 `count` / `first_seq` / `last_seq` / `score_range`），**原始事件完整保留**；
- `summarize_vision()`：按模板给 attempts/matches/threshold/best_score_range/`stable_best_location`/`possible_causes`/`evidence_event_ids`；
- `click_chain()`：从点击追到产生坐标的 match 事件（含前后关键帧、失败阶段）；
- `diagnosis_codes()`：8 个稳定码 `TRIAL_NO_RUN` / `VISION_THRESHOLD_NEAR_MISS` / `VISION_LOCATION_UNSTABLE` / `CLICK_NOT_ATTEMPTED` / `CLICK_EFFECT_NOT_OBSERVED` / `EXPECTED_SCENE_NOT_REACHED` / `SCRIPT_HASH_MISMATCH` / `RUNTIME_EXCEPTION`；**只描述现象**，摘要里明确写"不要直接当成结论"；
- `diagnosis_summary()`：状态/异常/最后进度/失败最多模板/临界阈值/点击链/聚合/codes。

**6 个诊断查询**（`queries.py`，封闭集合从 16 → 22）：`trial.get_diagnosis_summary`、`trial.get_vision_events`、`trial.get_action_events`、`trial.get_event`、`trial.get_frame`、`trial.get_pseudo_record_summary`（后者给 `telemetry_quality=structured|legacy_text_only`）。

**生产接线**：

- `Controller.emit_match_event()`（`controller/ctrl.py`）内部调用 `trial_runs.ingest_match_hud(...)` —— 真实执行器的 match/click 事件自动落成结构化遥测，**脚本不需要调用任何录制 API**；异常被吞掉，遥测失败绝不影响执行。
- GUI `_begin_v3_trial_record()` 在 `start_run` 成功后 `bind_active_run(account, task, run)`；`_finish_v3_trial_record()` 里 `clear_active_run()`。
- `bootstrap.initialize_v3()` 自动把 `trial.started/finished/runtime.exception` 写进本地事件表（阶段 2 已做）。

### 27.2 测试结果

| 测试集 | 结果 |
| --- | --- |
| `tests/test_collab_v3_trial_telemetry.py` | **26 passed** |
| 专项 12 文件 | **186 passed** |
| 55 个协作测试 | **699 passed, 1 skipped** |
| 仍失败 | 无 |

### 27.3 阶段 5 仍未做（明确登记）

1. **自动登记仍在 GUI 面板**（`_begin_v3_trial_record`），未下沉到 `TaskController`。可观测要求已满足（登记先于 `start_task`，import 第一行异常也有 run），但"谁负责登记"没变。
2. **`frame.captured` 与"区域视觉变化 / 预期场景"没有真实产点**：执行器 HUD 只有 img_path/status/score/x/y，不知道关键帧路径、`click_image_effect` 的视觉判定、`expect='appear'` 的场景确认。这些字段目前只能靠 `record_click(...)` 的显式调用（还没有调用方），因此 `CLICK_EFFECT_NOT_OBSERVED` / `EXPECTED_SCENE_NOT_REACHED` 在真实运行里暂时不会自动出现。
3. 伪录制器 `PseudoRecorder` 的时间线**尚未导入**结构化事件表（两套记录并存）。

---

## 28. 阶段 6 实施记录：自主调参与最小修改（2026-09-28 续）

### 28.1 交付内容

**最小修改约束**（新增 `backend/script_generator/script_diff.py`）：

| 能力 | 说明 |
| --- | --- |
| 基线绑定 | `BaselineBinding.of(code, policy, source)`：源码 + `sha256`（hash 前 strip，尾部空白不算改动）+ 变更政策 |
| 三种政策 | `asset_replacement_only`（只允许改字面量/调用参数）、`local_repair`（可改函数体，不得增删函数；控制流变化需 `justification`）、`free`（不限制，仍汇报 diff） |
| AST/语义 diff | `diff_scripts()` 给出：函数增删/改动、**控制流形状变化**（按函数比对 AST 控制流节点序列）、字面量增删、import 增删、调用增删 |
| 政策判定 | `enforce_change_policy()` → `PolicyVerdict`（ok / violations / `requires_justification` / `repair_hint` / diff 摘要） |
| 中文别名 | `normalize_policy("只替换资源")` → `asset_replacement_only`；`"局部修复"` → `local_repair` |

**自主调参安全边界**（新增 `backend/script_generator/tuning.py`）：

| 能力 | 说明 |
| --- | --- |
| 安全范围 | `PARAM_RANGES`：threshold/nav_threshold/icon_threshold/effect_threshold/timeout/confirm_timeout/stable_ms/retries/max_delay/sleep 各自的 `(floor, ceiling, max_step)`；**阈值下限刻意不低**（0.55 起） |
| 单次最大变化 | 超过 `max_step` 直接拒，并提示"分步调整、每步复测" |
| 最大尝试次数 | `DEFAULT_MAX_ATTEMPTS=4`；到顶后拒并提示"不要再盲试，改用诊断查询或问用户" |
| 一步一个参数 | `MAX_PARAMS_PER_STEP=2`，避免多参数同时动导致无法归因 |
| 防"靠降阈值凑命中" | `lower_threshold_is_unsafe()`：分数间隔 < 0.05（模板区分度不足）或已到安全下限 → 判定不安全并说明理由 |
| 无复测不得称修复 | `requires_retest_before_claiming_fixed()` → "只能说「已修改并通过静态校验，等待复测」" |
| 留痕 | `TuningState`（每参数尝试计数 + `TuningAttempt` 历史，含 reason_code/evidence_refs/verified）；`tuning_summary()` 供排障 |

**接入 runner（越界即拦）**：`TurnWorkspace` 新增 `baseline_script` / `change_policy` / `baseline_source` 与 `bind_baseline()` / `baseline_binding()`；
`_validate_script_subturn` 在写结论前跑 `enforce_change_policy`，**越界时把违规写进 issues → 校验不通过 → `ok=False`**；
校验事件（`ScriptValidated`）里带上 `change_policy` 判定摘要。

### 28.2 测试结果

| 测试集 | 结果 |
| --- | --- |
| `tests/test_script_diff_and_tuning.py` | **24 passed**（含 2 条 runner 端到端） |
| 专项 13 文件 | **210 passed** |
| 55 个协作测试 | **699 passed, 1 skipped** |
| 仍失败 | 无 |

### 28.3 阶段 6 仍未做（明确登记）

1. **基线/政策还没从 GUI 或协议传进来**：`run()/resume()` 没有 `change_policy`/`baseline_script` 参数，
   facade 也未透传；目前只能由 runtime 侧调用 `workspace.bind_baseline()`（测试里就是这样做的）。
   要真正生效，需要在"用户说只替换资源"时由 Runtime 绑定基线脚本（例如取上一次 commit 的脚本）。
2. **`tuning.py` 还没接进模型协议/查询**：模型目前不会自动调用它；没有 `tuning.propose` 之类的封闭查询。
3. **"没有复测不能说已修复"仍只是 helper + 协议文本**，未变成 finish 硬闸门（缺 trial↔主轮关联）。

---

## 29. 打通 trial ↔ 主轮关联 + 「无复测不得称已修复」硬闸门（2026-09-28 续）

> 这一项同时闭合**阶段 4 的第 7 项**与**阶段 6 的第 3 项**（两者同一根因）。

### 29.1 交付内容

| 环节 | 实现 |
| --- | --- |
| 试运行记主轮 | GUI `_begin_v3_trial_record()` 把 `session.artifacts["v3_turn_id"]` 作为 `main_turn_id` 传给 `start_run()` |
| 按主轮反查 | `trial_runs.list_runs_for_turn(task_id, main_turn_id)` |
| 复测证据判定 | `trial_runs.has_retest_for_turn(task_id, main_turn_id, script_hash=...)` → `(是否有, 原因)`；原因码 `NO_TRIAL_FOR_THIS_TURN` / `SCRIPT_HASH_MISMATCH` |
| finish 硬闸门 | `runner._fix_claim_gate()`：交付文本含"已修复/已解决/修复完成"等结论词，**且**本主轮没有覆盖当前脚本 hash 的试运行证据 → 拒绝，返回 `allowed_wording="已修改并通过静态校验，等待复测"`、`retry_policy="reword_or_retest"` |
| 闸门位置 | `_validate_finish_script()` 的**第一步**（早于脚本/凭证闸），因此改措辞是最省的出路 |

**为什么现在才做**：之前不做文本闸门是因为 trial 与主轮没有可靠关联，关键词判定会误伤。现在关联可靠（`main_turn_id` 落盘 + 按 hash 判定），闸门只在"**根本没有覆盖当前交付物的运行证据**"时触发。

### 29.2 测试结果

| 测试集 | 结果 |
| --- | --- |
| `test_collab_v3_trial_telemetry.py`（新增 7 条：按主轮反查 / hash 匹配 / 闸门四向） | **33 passed** |
| 专项 13 文件 | **210 passed** |
| 55 个协作测试 | **706 passed, 1 skipped** |
| 仍失败 | 无 |

修过的旧断言 1 处：`test_collab_v3_runner.py` 里某条 finish 的回复文本原本写 `"脚本已修复"` —— 在新规则下这是**不被允许的措辞**（本轮无复测），改为 `"脚本已修改并通过静态校验，等待复测"`。属协议有意收紧，未删测试。

### 29.3 该闸门的已知边界

1. 判据是**结论词 + 证据缺失**，不做语义理解：换一种说法（"搞定了"）不会触发。
   真正的语义级约束仍要靠协议与证据协议，不在本闸门职责内。
2. 试运行证据只有 `script_hash` 这一条绑定；**没有**绑定"这次试运行是否成功"——
   跑失败也算"有复测证据"。若要更严（要求复测成功），需要 trial 侧的成功判定，属阶段 5 尾巴。

---

## 30. 阶段 5/6 尾巴：伪录制导入 + 政策识别与基线贯通（2026-09-28 续）

### 30.1 交付内容

**① 伪录制时间线导入统一事件**（新增 `runtime/pseudo_import.py`）：

- `import_pseudo_record(task_id, run_id, record_dir)`：读既有 `timeline.jsonl`，按 kind 映射成结构化事件 ——
  `match*` → `vision.match_completed`（score/threshold/hit/x/y 尽力映射，**取不到的留 None**）、
  `click*` → `input.click_completed`、`keyframe*/frame*` → `frame.captured`（走 `record_frame` 的**相同帧去重**）、
  其余（`session_start` / `log` / `black_on` / `black_off` / `session_end`）→ `script.note`（原文照放，不猜语义）；
- 对坏行、缺目录、重复导入都容错；返回 `imported/skipped/by_kind` 统计。
- 意义：**旧的伪录制数据立刻能被新增的 6 个诊断查询用上**，不用等执行器改造。

**② 变更政策识别 + 基线贯通**（`runner`）：

- `run()/resume()` 新增 `baseline_script=` 与 `change_policy=` 参数（此前只有 workspace 内部 API，外部无法设置）；
- 新增 `runner._bind_change_policy()`：政策优先取显式参数，否则从**用户措辞**识别
  （"只替换/只换/仅替换/只改素材/只改图片" → `asset_replacement_only`；"局部修复/只修/仅修/只改这一处/修一下" → `local_repair`）；
- **没有基线就不虚构基线**：识别到政策但没给基线时，只记 `change_policy`，`baseline_binding()` 返回 None，
  闸门如实记为"未做最小修改约束"。

### 30.2 测试结果

| 测试集 | 结果 |
| --- | --- |
| `test_collab_v3_trial_telemetry.py`（新增 5 条导入测试） | **38 passed** |
| `test_script_diff_and_tuning.py`（新增 4 条政策识别测试） | **28 passed** |
| 专项 13 文件 | **214 passed** |
| 55 个协作测试 | **711 passed, 1 skipped** |
| 仍失败 | 无 |

### 30.3 仍未做

1. **自动取"上次交付脚本"作基线**：store 没有列 commits / 取脚本的访问器（只有 `record_commit` / `apply_commit`），
   因此现在只能由调用方显式传 `baseline_script`。下一轮需要加一个只读访问器
   （`latest_committed_script(task_id)`），再在 `run()` 里自动取。
2. **facade/GUI 还没透传** `baseline_script` / `change_policy`（runner 已就绪）。
3. **`frame.captured` / 视觉变化 / 预期场景仍无真实产点**（阶段 5 尾巴 2 未动）。
4. **TaskController 侧自动登记**（阶段 5 尾巴 1）未动：面板已"先登记后启动"，可观测要求满足。

---

## 31. 阶段 6 尾巴收口：自动绑定基线（2026-09-28 续）

### 31.1 交付内容

| 环节 | 实现 |
| --- | --- |
| 只读访问器 | `SqliteDomainStore.latest_committed_script(task_id) -> (code, commit_id)`：按 `created_at DESC, rowid DESC` 扫最近 20 条 commit，从 `result_json.final_response.script` 取第一条非空脚本。**只读**、老库/缺表/坏 JSON 一律返回空，不抛 |
| 自动绑定 | `runner._bind_change_policy()`：显式基线 > **上次提交脚本** > 不绑。用户说"只替换资源/局部修复"且库里有上次交付脚本时，自动 `bind_baseline(..., source="last_commit:<commit_id>")` |
| 不虚构 | 库里没有可用脚本时只记 `change_policy`，`baseline_binding()` 返回 None，闸门如实记为"未做最小修改约束" |

**效果**：以前"只替换资源"政策只能靠调用方手动传基线；现在**从用户措辞 + 任务历史就能自动生效**，最小修改闸门（阶段 6）在生产路径上真正可用了。

### 31.2 测试结果

| 测试集 | 结果 |
| --- | --- |
| `test_script_diff_and_tuning.py`（新增 3 条） | **31 passed** |
| 专项 13 文件 | **217 passed** |
| 55 个协作测试 | **711 passed, 1 skipped** |
| 仍失败 | 无 |

### 31.3 阶段 6 收口判定

| 要求 | 状态 |
| --- | --- |
| 自主调整阈值/超时/重试（不抛给用户） | ✅ `tuning.py`（安全范围 + 单步上限 + 最大尝试 + 留痕） |
| 每次调整产生新版本并复测 | ⚠️ 判定与措辞已实现；**"必须复测"的写入侧（谁在调整后强制再跑一次）未做** |
| 安全范围 / 单次最大变化 / 最大尝试次数 | ✅ |
| 防无限降阈值制造误命中 | ✅（下限钳制 + 分数间隔护栏） |
| "只替换资源/局部修复"绑定基线 + AST/语义 diff 限变更 | ✅ 基线**自动**绑定 + 越界即拒 |
| 控制流非必要变化拒绝提交或要求说明证据 | ✅（runner 校验闸 + `requires_justification`） |

### 31.4 本轮一次可疑但已排除的告警（如实记录）

并发检查报出 `runner.py` / 两个测试文件在 06:06 被写，晚于上一轮收尾检查（06:05:23）看到的时间。
逐项核对后确认**属于本会话**：我上一轮写入的标记（`_bind_change_policy` / `_POLICY_HINTS` /
`test_pseudo_record_timeline_is_imported_into_structured_events` / `test_policy_is_detected_from_user_wording`）
全部在位，且无任何外来标记（TODO/FIXME/冲突标记/凭据），全量测试仍全绿。判定为**时间戳噪声**，非并发写入。
（第 9 轮开头再次出现同样现象：`sqlite_store.py` / `runner.py` / 测试文件的时间戳晚于收尾检查，
同样核对为本人上一轮写入。）

---

## 32. 阶段 6 补完：调参变化检测接进闸门（2026-09-28 续）

### 32.1 交付内容

| 环节 | 实现 |
| --- | --- |
| 调参变化检测 | `script_diff.tuning_deltas(baseline, candidate)`：收集**关键字参数**里的数值 + **`CFG = {'threshold': 0.8}` 这类字典字面量**里的数值（本项目脚本最常见写法，只看调用关键字会漏），按参数名比较取值集合，给出 `{parameter, before, after, direction}` |
| 闸门说清"复测什么" | `_fix_claim_gate()` 拒绝"已修复"时，附上本次动过的调参（`tuning_deltas`：`threshold 0.8→0.7` 等），不再只给一句原则 |

**意义**：这一条把"每次调整必须产生新版本并复测"从**措辞规则**变成**有据可查的提示** ——
模型能看到自己改了哪个参数，用户/排障能看到"改了但没复测"的具体内容。

### 32.2 测试结果

| 测试集 | 结果 |
| --- | --- |
| `test_script_diff_and_tuning.py`（新增 4 条，含 CFG 字典写法） | **35 passed** |
| 专项 13 文件 | **221 passed** |
| 55 个协作测试 | **711 passed, 1 skipped** |
| 仍失败 | 无 |

### 32.3 阶段 6 最终判定

| 要求 | 状态 |
| --- | --- |
| 自主调参不抛给用户 | ✅ |
| 每次调整产生新版本 | ⚠️ 参数变化**可检测、可上报**；"新版本"由交付脚本本身承载（每次 finish 一份），未做独立版本号 |
| 必须复测 | ⚠️ 措辞闸门 + 变化清单已生效；**"强制再跑一次"的写入侧仍未做**（需 GUI 触发试运行） |
| 安全范围 / 单步上限 / 最大尝试 / 防无限降阈值 | ✅ |
| 基线绑定 + AST/语义 diff 限变更 | ✅（自动绑定） |
| 控制流非必要变化拒绝或要求证据 | ✅ |

---

## 33. 阶段 6 收口之三：`tuning.propose` 封闭查询（2026-09-28 续）

### 33.1 交付内容

之前 `tuning.py` 只是**库**：模型侧完全够不着，等于"安全边界写好了但没人走"。本轮把它接成第 23 个封闭查询：

| 环节 | 实现 |
| --- | --- |
| 查询 | `tuning.propose`：入参 `parameter` / `target` / 可选 `current` / `reason_code` / `evidence_refs` / `params_changed_this_step` |
| 输出 | `{ok, value, clamped, reasons, requires_retest, message, safe_range}` —— 走完安全范围钳制、单步上限、最大尝试、一步多参数限制 |
| 留痕 | 通过时把 `TuningAttempt` 记进 `workspace.tuning_state`（每参数尝试计数 + 历史），后续调用据此算"第几次尝试" |
| 失败语义分两类 | **提问无效**（缺 parameter/target、参数未登记）→ 查询级失败并附可调参数清单；**判定拒绝**（超单步 `STEP_TOO_LARGE`、超尝试 `MAX_ATTEMPTS_REACHED`）→ 查询成功但 payload 里 `ok=false`（可操作信息） |
| 协议文本 | `_SYSTEM_PROTOCOL` 增加"调参不要自己拍数字，先查 tuning.propose；requires_retest=true 就说明改完必须复测；被拒就按 reasons 分步或停止盲试" |

### 33.2 测试结果

| 测试集 | 结果 |
| --- | --- |
| `test_script_diff_and_tuning.py`（新增 6 条） | **41 passed** |
| 专项 13 文件 | **227 passed** |
| 55 个协作测试 | **711 passed, 1 skipped** |
| 仍失败 | 无 |

### 33.3 阶段 6 现在真正收口

| 要求 | 状态 |
| --- | --- |
| LLM 自主调整阈值/超时/重试，不抛给用户 | ✅ **可调用**（`tuning.propose`） |
| 安全范围 / 单次最大变化 / 最大尝试次数 | ✅ 查询内强制 |
| 防止无限降低阈值制造误命中 | ✅（下限钳制 + 分数间隔护栏 + 尝试上限） |
| 每次调整产生新版本并复测 | ⚠️ 参数变化可检测可上报、`requires_retest` 明示；**"强制再跑一次试运行"的写入侧仍未做**（需 GUI 触发） |
| 基线绑定 + AST/语义 diff 限变更 | ✅ 自动绑定 + 越界即拒 |
| 控制流非必要变化拒绝提交或要求说明 | ✅ |

**封闭查询总数**：13（原有）→ 23（现有）。新增的 10 个：`context.index`、`context.projection`、
`terminology.resolve`、`tuning.propose`、`trial.get_diagnosis_summary`、`trial.get_vision_events`、
`trial.get_action_events`、`trial.get_event`、`trial.get_frame`、`trial.get_pseudo_record_summary`。

## 34. 阶段 5 尾巴收口：关键帧 / 视觉变化 / 预期场景的**真实生产者**（2026-09-28 续）

### 34.1 这一轮修的缺口

阶段 5 把**消费侧**做完了：8 个稳定诊断码、6 个 `trial.*` 查询、连续事件聚合都上线了。
但**生产侧**只到 HUD 的五个基础字段（`img_path` / `status` / `score` / `x` / `y`），于是：

| 诊断码 | 触发条件 | 真实试运行里 |
| --- | --- | --- |
| `CLICK_EFFECT_NOT_OBSERVED` | `delivered and visual_effect_observed is False` | **永远不出现**（字段恒为 `None`） |
| `EXPECTED_SCENE_NOT_REACHED` | `delivered and expected_scene_reached is False` | **永远不出现** |
| `frame.captured` | 关键帧登记 | 只有测试造过，真实运行 0 条 |

也就是说"现象码写好了，但没人喂数据"。这一轮把这三样接上。

### 34.2 交付内容

| 环节 | 实现 |
| --- | --- |
| 补充事实透传 | `Controller.emit_match_event(..., detail=)` → `trial_runs.ingest_match_hud(..., detail=)`；`detail` 只接受 `_CLICK_DETAIL_KEYS` 里**登记过**的 12 个键，未登记的一律丢弃（执行器多给字段不炸、也不悄悄混进事件） |
| `visual_effect_observed` | `click_image_effect()` 里"点击后原目标区域是否变化"的实测结果（`score < effect_threshold`）直接落成该字段——这正是 `click_image_effect` 的定义 |
| `expected_scene_reached` | `click_image(expect=gone/appear)` 的 `confirm_after_click` 结果落成该字段；`confirm_policy` 记 `gone`/`appear`/`effect`，`failed_stage` 记 `match`/`no_hit_size`/`confirm`/`effect` |
| 一次点击一条记录 | 带 `expect` 时把点击 HUD **推迟到确认之后**再发（`_match_and_click(emit_click_hud=False)`、轮询分支同改），否则同一次点击会落成两条 `input.click_completed`，`click_chain` 会把点击数翻倍 |
| 关键帧生产者 | 新增 `runtime/trial_frames.py`：`save_key_frame(account, frame)` 把帧编码成 PNG 写进 `<run>.frames/<frame_id>.png` 并登记 `frame.captured`；`frame_id` 用内容 sha256 前 16 位（天然去重），并只允许 `[A-Za-z0-9_-]`（`..` 会走出目录） |
| 关键帧接进点击事件 | 视觉变化**未观察到**时存下对照区域并作为 `frame_after` 挂在点击事件上 → 诊断侧 `trial.get_frame` 能换回可看的文件 |
| 两套执行器都接 | `desktop_user_window`（`UserWindow.click_image` / `click_image_effect`）、`browser_user_browser`（`UserBrowser.click_image` / `click_image_effect`、底层 `ImageMatchingMixin.click_image`） |
| 老控制器兼容 | HUD 出口对不接受 `detail` 的 `Controller` 撤回退调用（`except TypeError`）：遥测降级成基础字段，**不影响执行** |

### 34.3 不伪造的边界（本轮新增测试锁的就是这条）

* 没有 `detail` → `visual_effect_observed` / `expected_scene_reached` / `confirm_policy` 留 `None`/`""`，
  绝不因为"点了"就推断"生效了"；
* 确认过程**抛异常** → `expected_scene_reached` 留 `None`（"没观察到" ≠ "没达成"），
  并且**不改**各执行器原有的异常传播/返回值语义（`UserWindow` 仍返回 `False`；`UserBrowser`
  与 `ImageMatchingMixin` 仍按老行为继续，只有明确的 `False` 才判失败）；
* 关键帧：拿不到活动 run / 帧编不出 PNG / 写盘失败 / 事件登记失败 → 一律返回 `""`（=「这次没有关键帧」），
  **绝不抛异常打断脚本**；
* 字节流关键帧的尺寸无从得知 → `frame_size` 留 `None`，不猜。

### 34.4 测试结果

| 测试集 | 结果 |
| --- | --- |
| `test_collab_v3_trial_hud_producers.py`（**新增 27 条**） | **27 passed** |
| 专项 16 文件（含上表新增文件与 `test_click_image_effect.py`） | **251 passed** |
| 56 个协作测试 | **738 passed, 1 skipped** |
| 全量 `tests/`（排除 2 个陈旧文件，见下） | **1017 passed, 28 skipped** |
| 仍失败 | 无（与本轮无关的既有失败见下） |

新增测试覆盖：透传与封闭集合（5）、诊断码真的会被喂到（3）、关键帧落盘/去重/降级/路径穿越/
句柄可回查（7）、`UserBrowser.click_image_effect` 端到端生产者（4）、`UserWindow` HUD 出口与
老控制器回退（5）、`expect` 点击恰好一条记录（3）。

**与本轮无关的既有失败（未处理，避免动别人的脏工作树）**：`tests/test_sgk_scan_speed.py`
（未跟踪的新文件）与 `tests/test_sgk_settle_gate.py` 全部失败，原因是
`scripts/yys/刷关卡.py`（同样处于已修改状态）里当前**没有** `EmptyFrameWait`。
这三个文件的 mtime 都是 **2026-09-21**，比本轮（09-28）早一周，属陈旧状态，不是并发写入。

### 34.5 阶段 5 状态更新

| 要求 | 改动前 | 现在 |
| --- | --- | --- |
| TaskController 自动登记试运行 | ✅ | ✅ |
| match/click 结构化遥测（不靠脚本主动调用） | ✅ 但只有 5 个基础字段 | ✅ **含执行器实测的视觉变化/预期场景/失败阶段** |
| 关键帧引用（`frame.captured`） | ⚠️ 只有测试造的 | ✅ **真实生产者（未观察到效果时自动存对照帧）** |
| 6 个 `trial.*` 诊断查询 | ✅ | ✅ |
| 连续事件本地聚合（原始事件仍保留） | ✅ | ✅ |
| 稳定诊断码只描述现象 | ✅ | ✅ **不再有空转的诊断码（8 个都能被真实数据触发）** |

### 34.6 七阶段总体状态（截至本轮；§26 保留为当时的快照）

| 阶段 | 状态 | 说明 |
| --- | --- | --- |
| ① 补完闸门剩余连接 | ✅ 完成 | 7/7 |
| ② 事件系统与层级 ID | ✅ 完成 | 7/7 |
| ③ 主轮空白/查询指纹/事实分层 | ✅ 完成 | 4/4 |
| ④ 副轮 Orient 与证据约束 | ✅ 完成 | 7/7（§29 补上 trial↔主轮关联 + 无复测不得称已修复的硬闸门） |
| ⑤ 试运行与伪录制 2.0 | ✅ 完成 | 5/5（§34 补上关键帧/视觉变化/预期场景的真实生产者） |
| ⑥ 自主调参与最小修改 | ✅ 完成 | 7/7（§35 补上"动了调参必须交代等待复测"的写入侧闸门） |
| ⑦ 用户个人词典 | ✅ 完成 | 词典 + 解析顺序 + 不授权 + 修订审计 + 按需查询 |

## 35. 阶段 6 写入侧收口：动了调参就必须交代"等待复测"（2026-09-28 续）

### 35.1 这一轮修的缺口

阶段 6 原来只有**一半**闭环：`_fix_claim_gate` 管的是"**多说**"——没复测却说「已修复」→ 拦。
但反过来"**少说**"没人管：模型把阈值从 0.8 改成 0.7，然后交付一句「阈值已调整为 0.7」
就结束了，用户会以为改完就好了。设计要求是"每次调整产生新版本并复测"（§6.2），
而本轮**拿不出复测证据**时，至少必须把"还要复测"这件事说出来。

### 35.2 交付内容

| 环节 | 实现 |
| --- | --- |
| 新闸门 | `ActionRunner._retest_obligation_gate(workspace, action_id, final_response)`：拿**基线脚本**与**本次交付脚本**算 `script_diff.tuning_deltas`，非空且无复测证据时拒绝 finish |
| 放行条件（任一） | ①相对基线没有调参变化（只换素材/改文案不算）；②本轮有覆盖当前交付物 hash 的试运行证据（`has_retest_for_turn`）；③话术里已明确"等待/需要复测"，或 `final_response.needs_user_retest=true` |
| 拒绝形状 | `retest_pending_without_evidence=true`、`tuning_deltas=[…]`、`allowed_wording="已修改并通过静态校验，等待复测"`、`must_repair_script=False`、`error_code=COMPLETION_NOT_PROVEN`、`recoverable=true`、`retry_policy="reword_or_retest"` |
| 不是死循环 | 拒绝只是话术问题：补一句话**或**一个字段即可放行（两条路径都有测试）。拒绝信息里列出具体改了哪些参数（`threshold 0.8→0.7`），让模型知道"复测什么" |
| 无基线不加负担 | 没有基线绑定（首次交付、自由改写）时闸门直接不生效 |
| 协议文本 | `_SYSTEM_PROTOCOL` 增加该规则；并修掉一处**自相矛盾**：原文第 70 行写的是「已**修复**并通过静态校验，等待复测」，而 `_FIX_CLAIM_RE` 正好匹配「已修复」——照抄会被自己的闸门拦下。现统一为「已**修改**并通过静态校验，等待复测」并加了显式提醒 |

### 35.3 测试结果

| 测试集 | 结果 |
| --- | --- |
| `test_collab_v3_trial_telemetry.py`（新增 10 条 → 38 条） | **48 passed** |
| 专项 16 文件 | **261 passed** |
| 56 个协作测试 | **748 passed, 1 skipped** |
| 全量 `tests/`（排除 §34.4 说明的 2 个陈旧文件） | **1027 passed, 28 skipped** |
| 仍失败 | 无 |

新增测试覆盖：静默调参被拒（含 `tuning_deltas`/放行措辞/可修复性）、显式措辞与
`needs_user_retest` 两条放行路径、非调参变化不触发、真实复测证据放行、**过期**
复测证据仍被拒（hash 不符）、无基线不生效、finish 省略 `script` 时读工作区校验原文、
以及 finish 放行路径的接线检查（拒绝→改措辞→放行）。

### 35.4 阶段 6 现在真正收口

| 要求 | 状态 |
| --- | --- |
| LLM 自主调整阈值/超时/重试 | ✅ `tuning.propose` |
| 安全范围 / 单步上限 / 最大尝试 | ✅ 查询内强制 |
| 防无限降阈值制造误命中 | ✅ 下限钳制 + 分数间隔护栏 |
| 每次调整产生新版本并复测 | ✅ **检测 + 硬闸门 + 两条放行路径**（真正跑一次试运行仍由用户在试运行页触发——模型没有"启动试运行"这个动作，这是设计边界而非缺口） |
| 基线绑定 + AST/语义 diff 限变更 | ✅ 自动绑定 + 越界即拒 |
| 控制流非必要变化拒绝或要求说明 | ✅ |

## 36. 阶段 5 最后一处字面缺口：`TaskController` 自动登记（2026-09-28 续）

### 36.1 为什么还要动它

设计原文（与用户给的阶段 ⑤ 措辞）是「**TaskController** 自动登记」。之前实际登记发生在
GUI 面板 `_begin_v3_trial_record`（在 `start_task` **之前**），因为面板要立刻拿到
`run_id` 来写试运行日志、收尾停帧。功能上"脚本不需要主动调用录制 API"已经成立，
但**字面要求那个角色**没落到 `TaskController` 上，而且面板那次登记一旦抛异常，
（`except` 里只 print）整次试运行就完全没有记录。

### 36.2 交付内容（两条路，绝不重复登记）

| 环节 | 实现 |
| --- | --- |
| 上下文约定 | 面板把**与它自己传给 `start_run` 完全相同**的一份输入放进 `account["_collab_trial"]`：`task_id` / `session_id` / `script_name` / `code` / `main_turn_id` |
| TaskController 登记 | `begin_collab_trial()`：没有活动 run 就用那份上下文 `start_run` + `bind_active_run`（**补登记**）；已有活动 run 就**认领**它，不再造第二条 |
| 收尾 | `end_collab_trial(status)`：只有**自己登记**的那次才 `finish_run` + `clear_active_run`；认领来的留给面板收尾（不抢它的活）。两种情况都会清掉 `account["_collab_trial"]`，避免下一次 start 复用上一次的归属 |
| 调用点 | `_run_async` 最前面（连"脚本加载就炸"的一次也留记录）→ `finally` 里收尾，状态用与面板一致的词表 `finished` / `stopped` / `error` |
| hash 不分叉 | 兜底与主路径**输入逐字节相同**，所以 `script_hash` 相同，「复测证据 ↔ 主轮」的比对不会被兜底路径破坏（有专门的测试锁这条） |
| 失败不拦执行 | 登记/收尾里任何异常只 print，绝不冒泡进脚本执行 |

### 36.3 测试结果

| 测试集 | 结果 |
| --- | --- |
| `test_collab_v3_trial_autoregister.py`（**新增 11 条**） | **11 passed** |
| 专项 17 文件 | **272 passed** |
| 57 个协作测试（本轮 56 → 57） | **759 passed, 1 skipped** |
| 全量 `tests/`（排除 §34.4 说明的 2 个陈旧文件） | **1038 passed, 28 skipped** |
| 仍失败 | 无 |

新增测试覆盖：从账号上下文登记、**认领已有 run 而不重复**、两路 `script_hash` 一致、
重复 `begin` 幂等、自己登记的 run 被 finish 且解绑、面板登记的 run 不被抢收尾、
四种终态落盘、无/不完整上下文一律不登记、收尾失败与 begin 失败都不抛、
以及端到端（兜底登记的 run 能被诊断码与复测闸门正常看见）。

### 36.4 位置约定（避免以后又分叉）

| 角色 | 职责 |
| --- | --- |
| GUI 面板 | **主登记**：立刻拿到 `run_id` 写试运行日志、停帧、游标收尾；并把同一份上下文交给 TaskController |
| `TaskController` | **自动登记/认领 + 兜底收尾**：面板那次失败或换了启动路径时仍然留下完整记录 |

两者写入的 `script_hash` 恒等；`_active_runs` 只有一个绑定，HUD 事件不会归错 run。

## 37. 阶段 6 补完：基线绑定的生命周期与"对比基线"（2026-09-28 续）

### 37.1 这一轮发现的两个真实缺口

**缺口 A（更严重）：基线绑定跨不过 `pause → resume`。**
`TurnWorkspace.runtime_snapshot()` 一直没存 `baseline_script` / `change_policy`。
于是流程一旦是「用户提问 → 模型停下问一句 → 用户回答 → resume」，
恢复出来的工作区 `baseline_binding()` 就是 `None`：
`enforce_change_policy`（最小修改约束）与 `_retest_obligation_gate`（§35 的复测闸门）
**一起静默消失** —— 不是"没触发"，是**根本没绑**。

**缺口 B：复测闸门被"政策识别"顺带关掉了。**
`_bind_change_policy` 原来是"先解析政策，解析不出来就 `return`"，连基线也不记。
可 §6.2 的"每次调整 → 复测"不依赖用户说"只替换资源"。结果是：
**只有用户碰巧说了那几句话的会话，§35 的复测闸门才生效**。

### 37.2 交付内容

| 环节 | 实现 |
| --- | --- |
| 快照持久化 | `runtime_snapshot()` / `restore_runtime_snapshot()` 增加 `baseline_script` / `change_policy` / `baseline_source` / `comparison_script` / `comparison_source`；**老检查点读到空串 → 如实"没有基线"，不假装有** |
| 新增"对比基线" | `workspace.note_comparison_baseline(code, source=)` / `comparison_baseline()`：本轮"和哪一版比"。与约束政策**分开**：政策只在用户明确要求时才绑，对比基线任何时候都记 |
| `_bind_change_policy` 拆开两件事 | 先记对比基线（显式 `baseline_script` 优先，否则 `latest_committed_script`），再解析政策。**没有政策时不再提前 return 掉基线** |
| 复测闸门改用对比基线 | `_retest_obligation_gate` 依次取 `comparison_script` → `baseline_script`（只绑了政策的老工作区，政策基线同样是准确的"上一版"）→ 现取 `latest_committed_script`；**三处都拿不到才放弃**（没有可比的上一版，不凭空拦人） |
| `resume` 接受显式基线 | `ActionRunner.resume(..., baseline_script=, change_policy=)`：调用方给了新事实就以新事实为准重新绑；没给且检查点里也没有（老检查点）→ 按本轮原始用户措辞补绑一次 |
| facade 透传 | `V3RuntimeFacade.send_message(..., baseline_script=, change_policy=)` / `resume(...)` 转发给 runner（`runner.run` 本来就收这两个参数，只是以前没有可达路径） |
| 文档修正 | `_bind_change_policy` 的 docstring 原文还写着"store 还没有'取上次交付脚本'的访问器"——§31 之后已经不成立了，一并修掉 |

### 37.3 测试结果

| 测试集 | 结果 |
| --- | --- |
| `test_collab_v3_baseline_lifecycle.py`（**新增 12 条**） | **12 passed** |
| `test_collab_v3_runner.py`（**新增 3 条** → 53 条） | **53 passed** |
| 58 个协作测试（本轮 57 → 58） | **774 passed, 1 skipped** |
| 全量 `tests/`（排除 §34.4 说明的 2 个陈旧文件） | **1053 passed, 28 skipped** |
| 仍失败 | 无 |

新增测试覆盖：快照往返保住绑定、恢复后**控制流重写仍被拒**（这就是以前丢的行为）、
老检查点不虚构绑定、**没提"只替换"也会记对比基线**、政策与对比基线各自独立、
显式基线优先于上次提交、没有上一版时不记也不拦、只看对比基线时复测闸门也生效、
只有政策绑定时同样生效、facade 两条路径的透传与默认不虚构，
以及 **runner 端到端**：`run(baseline_script=…)` 暂停后检查点里确实带着绑定、
`resume` 后工作区里仍带着绑定、调用方可在 resume 时覆盖。

### 37.4 与 §35 的关系（读的时候别混）

| 闸门 | 管什么 | 用的基线 |
| --- | --- | --- |
| `_fix_claim_gate` | **多说**：没复测却称"已修复" | 政策绑定（只影响提示文案） |
| `_retest_obligation_gate` | **少说**：动了调参却一句不吭 | **对比基线**（`comparison_script`） |
| `enforce_change_policy` | 越界改控制流/新增调用 | 政策绑定（`baseline_script` + `change_policy`） |

三者现在都能跨 `pause → resume` 存活。

### 37.5 ⚠️ 顺带发现的仓库卫生问题（**未处理，等用户决定**）

`.gitignore:37` 的 `runtime/`（本意是"运行期数据目录"）会**递归匹配任意深度的
`runtime` 目录**，于是整个 V3 运行时包都被 git 忽略：

```
git check-ignore -v backend/script_generator/collaborator_v3/runtime/runner.py
→ .gitignore:37:runtime/
```

受影响的是本实施记录里改动最密集的那一批文件：
`runtime/{runner,workspace,trial_runs,trial_frames,trial_diagnosis,pseudo_import,queries,ids,checkpoint}.py`、
`events/`、`stores/`、`adapters/` 里的部分文件等。**它们现在都不在版本控制里**，
只存在于工作区磁盘上。

没有自行修改 `.gitignore`，原因有二：

1. 改它会让一大批文件**突然**变成"已跟踪/未跟踪"的新增项，属于对仓库基线的大改动，
   不在"逐阶段实施"的授权范围内；
2. `.gitignore` 本身已经在脏工作树里被别的会话改过（`M .gitignore`），
   再去动容易和对方的工作撞车。

建议的修法（需用户确认后执行）：把 `runtime/` 收紧成只匹配仓库根的那一个，
例如 `/runtime/`；或显式否定 `!backend/script_generator/collaborator_v3/runtime/`。

## 38. 阶段 5 的"最后一公里"：伪录制 → 结构化遥测的生产桥（2026-09-28 续）

### 38.1 缺口：链路两头都做好了，中间没人接

审计"试运行与伪录制 2.0"时发现一个典型的"看起来做完了"：

| 环节 | 状态 |
| --- | --- |
| `PseudoRecorder` 写 `screenshots/pseudo_record/<账号_时间_脚本>/timeline.jsonl` + `frames/*.jpg` | ✅ 一直在跑 |
| 执行器在 capture/click 时真的调了 `maybe_keyframe`（`user_window:491/550`、`user_browser:286/318`） | ✅ |
| `pseudo_import.import_pseudo_record`（timeline → 结构化事件） | ✅ 写好了，也有 5 条测试 |
| **生产代码里调用它** | ❌ **一处都没有** |

后果：设计 §19.4 的关键帧、伪录制里的匹配分数，`trial.*` 诊断查询**一条都看不到**。
`VISION_THRESHOLD_NEAR_MISS`（"差一点点"——最该被诊断出来的现象）在真实运行里
**永远不可能出现**，因为阈值/分数只在伪录制目录里，而没人读它。

### 38.2 交付内容

| 环节 | 实现 |
| --- | --- |
| 归属校验 | `find_record_for_run(run_record, script_hint=, account_hint=, root=)`：先按脚本名/账号过滤候选，再用 `timeline.jsonl` 的 **mtime 与 run 开始时刻**比对；早于本次 run 的目录（允许 5s 时钟余量）**一律拒绝**。宁可少导入，也不能把上一跑的分数/关键帧混进这一跑的证据里 |
| 导入入口 | `import_pseudo_record_for_run(task_id, run_id, ...)`：`NO_SUCH_RUN` / `NO_TIMELINE` / `NO_PSEUDO_RECORD_FOR_THIS_RUN` / `ALREADY_IMPORTED` / `IMPORT_FAILED:<异常类>`，**任何失败都不抛异常**（收尾阶段不能被它打断） |
| 幂等 | `match`/`click` 在 `import_pseudo_record` 里是**追加**语义（重复导入会翻倍，只有 `frame.captured` 有内容去重）→ 新增 `trial_runs.mark_pseudo_imported()` / `pseudo_imported_dirs()`，去重标记落在 **run 记录**上，不靠"调用方记得只导一次" |
| 可见性 | 导入完写一条 `script.note`（前缀 `【伪录制导入】`，含目录名、条数、按类型明细）：让"诊断结果为什么变了"本身可追，不用猜 |
| 两条收尾路径都接 | GUI 面板 `_finish_v3_trial_record`（在 `finish_run` **之前**导入）与 `TaskController.end_collab_trial`（兜底登记那次，按 `account_hint` 找目录） |
| 不重复登记 | 面板是主登记、TaskController 是兜底（§36）；两边都在自己收尾时各导入一次，靠 `ALREADY_IMPORTED` 保证不会翻倍 |

### 38.3 测试结果

| 测试集 | 结果 |
| --- | --- |
| `test_collab_v3_pseudo_bridge.py`（**新增 14 条**） | **14 passed** |
| 59 个协作测试（本轮 58 → 59） | **788 passed, 1 skipped** |
| 全量 `tests/`（排除 §34.4 说明的 2 个陈旧文件） | **1067 passed, 28 skipped** |
| 仍失败 | 无 |

新增测试覆盖：本次 run 的目录能被找到、**早于 run 的目录被拒**、多个候选时取新的、
没有目录/没有 run/没有 timeline / 坏行 各自的 reason、显式目录跳过归属校验、
二次导入 `ALREADY_IMPORTED` 且事件数不变、run 记录上留下标记、
导入失败不抛、导入说明可见（含目录名），以及**端到端**：
伪录制里的一次 near-miss 真的变成 `VISION_THRESHOLD_NEAR_MISS` + 关键帧句柄可回查，
外加 `TaskController` 兜底收尾路径的接线检查。

### 38.4 仍然依赖既有录制器的部分（如实说明，不夸口）

设计 §19.4 列了 8 类关键帧触发。桥修好之后**录制器写下的**都能被诊断看到，但
"写下多少"取决于既有 `PseudoRecorder` 的调用点，现状是：

| §19.4 触发 | 现状 |
| --- | --- |
| 点击前/后 | ✅ `reason="click"`（`user_window` / `user_browser` capture 路径） |
| match 连续失败且接近阈值 / match 成功 | 🟡 由脚本或录制器按 `reason="diag"` 决定，不是自动判定的 |
| 页面黑屏边沿 | 🟡 `note_black_frame` 只写 `black_on`/`black_off` **事件**，不存帧 |
| trial 起点 / 状态切换 / 异常与停止 / 用户手动标记 | ❌ 还没有调用点（`step`/`error`/`manual` 只在间隔豁免白名单里） |

这部分属于"录制器侧的采样策略"，它**不影响**本轮的结论：链路已经通了，
以后补调用点会自动进入诊断，不需要再动遥测层。

## 39. 阶段 3 补完：空结果原因码真的可达 + 旧版本证据必须被标出来（2026-09-28 续）

### 39.1 两处"枚举写好了、没人产生"的死码

| 位置 | 症状 |
| --- | --- |
| `trial.get_latest_run` 的 `other_runs_exist` | **写死的 `False`** —— 别的任务跑过时等于告诉模型"你从来没跑过" |
| `trial.get_latest_run` 的 `reason` | 只有 `NO_RUN_FOR_CURRENT_TASK` / `QUERY_SCOPE_MISMATCH`；设计 §17.1 建议枚举里的 **`RUN_VERSION_MISMATCH` 从来没出现过** |
| `trial_diagnosis.SCRIPT_HASH_MISMATCH` | 需要 `current_script_hash`，而 `trial.get_diagnosis_summary` **从来没传**过 → **死码**。后果很重：模型会把**旧版本**脚本的运行证据当成当前交付物的复测结论 |
| `diagnosis_summary` 的 `evidence_usable_for_current_script` | 不存在：模型无法判断"这份证据还能不能用" |

### 39.2 交付内容

| 环节 | 实现 |
| --- | --- |
| 注册表级事实 | `trial_runs.any_runs(exclude_task_id=)` / `trial_runs.registry_active()`：真去扫目录，**不再写死** |
| `other_runs_exist` | 空结果时如实返回"别的任务有没有跑过" |
| `RUN_VERSION_MISMATCH` | 最近一次试运行存在、且它的 `script_hash` ≠ **当前交付脚本** hash 时给出，附 `run_script_hash` / `current_script_hash` / `evidence_usable_for_current_script=False` / "请对当前版本重新试运行"；**仍然把 run 记录给出去**（让模型自己看，不是藏起来） |
| 当前版本 hash 的来源 | 新增 `queries._current_script_hash(ctx)`：本主轮校验通过的正文 → 上次提交交付的脚本；**两者都拿不到就返回空串**，此时 `evidence_usable_for_current_script=None` + `reason="UNKNOWN"`，既不谎报可用也不谎报不可用 |
| `SCRIPT_HASH_MISMATCH` 接线 | `trial.get_diagnosis_summary` 现在把 `current_script_hash` 传进 `diagnosis_summary`；版本一致/未知时**不**报这个码（不反过来指控"证据是旧的"） |
| **故意不产生**的两个码 | `TRIAL_REGISTRY_NOT_ACTIVE`：同一条信息已由 `registry_active` 布尔说清，而**改既有 reason 的取值属于载荷兼容性变更**（老调用方按值分支会失配），不值得为它换；`RUN_RECORD_EXPIRED`：注册表还没有轮转/过期机制，造一个永远不会发生的码只会误导模型。两条都写进 docstring，需要时读布尔字段 |

### 39.3 测试结果

| 测试集 | 结果 |
| --- | --- |
| `test_collab_v3_trial_empty_reasons.py`（**新增 12 条**） | **12 passed** |
| 60 个协作测试（本轮 59 → 60） | **800 passed, 1 skipped** |
| 全量 `tests/`（排除 §34.4 说明的 2 个陈旧文件） | **1079 passed, 28 skipped** |
| 仍失败 | 无 |
| 改动的锁定测试 | **0 个**（`test_collab_v3_query_fingerprint` / `test_collab_v3_events_projections` 里那两条断言空结果 reason 的测试原样通过） |

新增测试覆盖：注册表未启用时 `registry_active=False` 且 reason 不变、
别的任务跑过时 `other_runs_exist=True`、无任务时 `QUERY_SCOPE_MISMATCH`、
旧版本证据被标出（含两个 hash 与"旧版本"建议）、版本一致时 `usable=True` 且无 reason、
版本未知时 `usable=None` + `UNKNOWN`、当前版本回退到"上次提交交付的脚本"、
以及诊断摘要里 `SCRIPT_HASH_MISMATCH` 现在**真的会出现** / 一致与未知时不出现。

### 39.4 与 §35/§37 的关系

至此"这份试运行证据能不能用来支持当前交付物"这件事在**三处**口径一致：

| 位置 | 判据 |
| --- | --- |
| `_retest_obligation_gate`（§35） | `has_retest_for_turn(script_hash=workspace.validated_script_hash)` |
| `trial.get_latest_run`（本轮） | `run.script_hash` vs `_current_script_hash(ctx)` |
| `trial.get_diagnosis_summary`（本轮） | 同上 → `SCRIPT_HASH_MISMATCH` |

三者都用 `trial_runs.script_hash()`（sha256 of stripped text），**不存在两套哈希口径**。

## 40. 阶段 6 安全补完 + 三处过期"未接线"标记（2026-09-28 续）

### 40.1 缺口：调参尝试计数可以被"回答一次提问"绕开

`tuning.propose`（§33）把尝试计数放在 `workspace.tuning_state` 上，但这个字段：

* **既没在 `TurnWorkspace` 里声明**（靠 Python 允许动态属性才"能跑"）；
* **也没进检查点快照**。

后果：每次 `pause → resume` 都是新的工作区对象 → 计数从 0 开始 →
`MAX_ATTEMPTS_REACHED`（"不要再盲试"）**可以被"回答一次提问再继续"无限绕开**。
也就是说设计 §6 里"防止无限降低阈值制造误命中"这条安全边界是**可绕过的**。

这和 §37 的基线绑定是同一类问题（**安全相关状态没进快照**），修法也一样。

### 40.2 交付内容

| 环节 | 实现 |
| --- | --- |
| 序列化 | `TuningState.to_dict()` / `TuningState.from_dict(payload)`；`from_dict` **坏数据一律跳过**（非 dict、坏计数、坏历史行都不抛），绝不因为一条脏记录炸掉恢复 |
| 声明成真字段 | `TurnWorkspace.tuning_state: Any = None`（**不 import tuning**：workspace 不依赖同包顶层模块，序列化在方法里惰性 import，与 `baseline_script` 同一处理方式） |
| 进快照 | `runtime_snapshot()` 带 `tuning_state`；`restore_runtime_snapshot()` 惰性 import 还原 |
| 老检查点语义 | 没有这个键 / 空 dict → `tuning_state is None`（= "一次都没试过"），**不是**"试过很多次"，不凭空造计数 |

### 40.3 测试结果

| 测试集 | 结果 |
| --- | --- |
| `test_collab_v3_tuning_lifecycle.py`（**新增 8 条**） | **8 passed** |
| 61 个协作测试（本轮 60 → 61） | **808 passed, 1 skipped** |
| 全量 `tests/`（排除 §34.4 说明的 2 个陈旧文件） | **1087 passed, 28 skipped** |
| 仍失败 | 无 |
| 改动的锁定测试 | **0 个** |

新增测试覆盖：`to_dict/from_dict` 往返（含 evidence_refs / note）、**坏数据只跳过不炸**、
工作区快照往返保住计数与历史、老检查点/空 dict → 无状态、
**跑满 4 次后恢复出来的工作区继续拒绝**（本轮修的核心）、
以及反面对照（没有计数时第一次必须放行；只用了 1 次则恢复后还剩 3 次而不是重置成 4 次）。

### 40.4 顺带更正三处过期的"未接线"标记（纯文档，但会误导人）

审计时被这三个标记误导过，逐一复核后确认**它们描述的都是接线之前的状态**：

| 文件 | 原标记 | 实测 |
| --- | --- | --- |
| `runtime/ids.py` | "尚未接入生产路径，全仓只有 `test_collab_v3_ids.py` 引用" | ❌ 过期：`runner._segment_run_id`/`_subturn_id` 用 `new_run_id`/`new_subturn_id`，`facade._new_turn_id` 用 `new_main_turn_id`；`_starting_subturn_seq` 走 `store.max_subturn_seq()`，副轮序号跨恢复**不归零** |
| `events/schema.py` | "全仓（含 tests）零 import" | ❌ 过期：`runner._emit_event`（唯一写入口）与 `trial_runs._emit`（bootstrap 装的 sink）都在用 |
| `events/store.py` | "除本包内部外全仓零 import"，并称生产事件仍绕过它 | ❌ 过期：`runner._emit_event` 与 `bootstrap.initialize_v3` 都走它 |

三个标记已改成**当前真实状态**，并写清仍然是真的那部分：

* 读侧 `EventStore.for_turn / for_subturn / for_run` **目前只有测试在用**，
  生产读取仍走 `sqlite_store.list_events*`（有意保留，不是遗漏）；
* 设计 §18.1 里的 `bus.py`（本进程发布/订阅）**没有做** —— 现在只有"写入 + 按需读取"，
  没有订阅者。

> 教训记一笔：**"未接线"这类断言必须带日期并且要复核**。它们本来是用来防止
> "以为做了其实没做"的，结果反过来变成了"以为没做其实做了"，同样浪费时间。

## 41. 阶段 3 补完：查询指纹的 `task_revision` 成分是死的（2026-09-28 续）

### 41.1 缺口：`note_task_revision` 在生产里零调用 → 指纹少一个成分

`TurnWorkspace.query_key` 的指纹按 §17.2 由
``name + 归一化参数 + task_revision + resource_versions + mutation_epoch`` 组成，
`cached_query` 还会拿 `_domain_fingerprint()` 再核对一遍（防手工塞旧 key）。

但审计发现 **`note_task_revision()` 只有定义、生产代码一次都没调用** →
`task_revision` **恒为 0** → 指纹里的 ``rev=`` 成分等于不存在。

真实会踩到的序列（已用测试复现，见下）：

1. 还没有 Task 时查 `task.get_current` → 缓存里放进一份"**没有任务**"；
2. `update_plan` 带 `task_draft` → `establish_task_for_turn` 建出 Task（修订号 0 → 1），
   而它走的是 `mark_plan_change()`（**故意不 bump mutation_epoch**，见 §内注释）；
3. 再查 `task.get_current` → 指纹一个成分都没变 → **回放"没有任务"**，
   模型于是认为任务根本没建起来，并收到 `TOOL_NO_PROGRESS`。

也就是说 §17.2 里"任务修订变化即失效"这条**在生产中不成立**。

### 41.2 交付内容

| 环节 | 实现 |
| --- | --- |
| 新helper | `ActionRunner._refresh_task_revision(workspace)`：从 `store.get_task()` 读修订号写进工作区；没任务/读失败 → 0（不猜、也不留旧值） |
| 三个调用点 | `run()` 建工作区后、`resume()` 恢复后（恢复期间修订可能已变）、**`establish_task_for_turn` 之后**（就是上面那个序列的关键点） |
| 域版本成分进快照 | `runtime_snapshot()` / `restore_runtime_snapshot()` 增加 `task_revision` + `resource_versions`：丢了它们，恢复后 `_domain_fingerprint()` 会变，**所有缓存条目被判失效**——安全，但等于每次恢复都把 §17.2 的缓存语义清空；而且 `cached_query` 的第二道核对会永远比不上 |
| 坏数据 | `task_revision` 非数字 → 0；`resource_versions` 里坏值逐条跳过；都不抛 |

### 41.3 测试结果

| 测试集 | 结果 |
| --- | --- |
| `test_collab_v3_runner.py`（**新增 1 条** → 54 条） | **54 passed** |
| `test_collab_v3_query_fingerprint.py`（**新增 3 条**） | **15 passed** |
| 61 个协作测试 | **812 passed, 1 skipped** |
| 全量 `tests/`（排除 §34.4 说明的 2 个陈旧文件） | **1091 passed, 28 skipped** |
| 仍失败 / 改动锁定测试 / 删除文件 | 无 / 0 / 0 |

**这条测试是做了"去掉修复就会失败"验证的**（不是只测"现在通过"）：

```text
暂时移除 establish_task_for_turn 后的 _refresh_task_revision 调用 →
test_creating_a_task_invalidates_the_cached_empty_task_query
E  AssertionError: assert 'TOOL_NO_PROGRESS' != 'TOOL_NO_PROGRESS'
（即：第二次查询确实被当成缓存回放，模型拿到"没有任务"）
恢复调用 → 1 passed
```

新增测试：端到端"建任务后缓存空结果必须失效"（含 Task 真的建出来了）、
指纹两个域版本成分的快照往返（含 `_domain_fingerprint()` 前后一致、
恢复后缓存条目仍可回放）、坏数据容错。

### 41.4 这一类问题的清单（三处，都已修）

| 轮次 | 状态 | 后果 |
| --- | --- | --- |
| §37 | 基线绑定 / 变更政策 | 恢复后最小修改约束与复测闸门**静默消失** |
| §40 | `tuning_state` 尝试计数 | 恢复后"最大尝试次数"可被无限绕开 |
| §41 | `task_revision` / `resource_versions` | 恢复后指纹成分丢失；且**从未被喂过值**，任务建立后回放"没有任务" |

共同点：**"必须跨 `pause → resume` 存活的状态"没有被系统性地看待**——
每次都是单独发现的。建议后续把"快照字段清单 vs `TurnWorkspace` 字段清单"
做成一条回归测试（谁新增了字段却没进快照，测试直接失败），而不是靠审计。

## 42. 阶段 3/5 交界：续跑后提交失败（待写证据丢失）+ 快照覆盖性回归（2026-09-28 续）

### 42.1 缺口：`pending_evidence` 不进快照 → "校验完 → 暂停 → 继续 → 提交"直接失败

`TurnWorkspace.pending_evidence` / `pending_resources` 是**本主轮还没提交的待写内容**，
`_finish` 与 `Committer` 直接取它们：

* `_finish`：`acceptance_proofs` 里的字面量 `"latest_tool_evidence"` **只在这个列表非空时**
  才会被换成真实证据 id；
* `Committer.commit`：`resources_to_create` / `evidence_to_create` 直接来自这两个列表，
  并校验 `acceptance_proofs` 的每个值必须"待写入 or 库里已存在"。

它们都没进检查点快照。于是这条**最常见的续跑路径**会失败：

```
恢复 → validate_script（产生证据）→ 因预算/提问暂停 → 用户回答继续 → finish
  → pending_evidence 是空的 → "latest_tool_evidence" 没被替换
  → Committer: EVIDENCE_INSUFFICIENT "acceptance proof does not exist"
  → 提交失败（任务停在 active），模型再试还是失败
```

**已用测试复现**（先写失败测试再修）：

```text
修复前：test_evidence_created_before_a_pause_survives_into_the_commit
E  AssertionError: assert 'active' == 'completed'
修复后：1 passed
```

### 42.2 交付内容

| 环节 | 实现 |
| --- | --- |
| 记录序列化 | `ResourceRecord.to_dict()/from_dict()`、`EvidenceRecord.to_dict()/from_dict()`（枚举取 `.value`，`ResourceRef` 子对象拆成 `{id, version}`） |
| 进快照 | `runtime_snapshot()` / `restore_runtime_snapshot()` 增加 `pending_evidence` / `pending_resources` |
| 坏行容错 | 恢复时**逐条** try/except：脏记录跳过，绝不让整轮恢复失败；`version=0` 这类过不了 `__post_init__` 校验的自然被跳过 |
| 顺手补一个同类 | `user_claims`（用户对外部现实的说法）也进快照：它刻意不进 `fact_snapshot`（那是**事实**），但丢了之后同一句话在恢复后的分层报告里会变成"没有落成任何一层" |
| 覆盖性回归 | 新增 `tests/test_collab_v3_workspace_snapshot_coverage.py`：`TurnWorkspace` 的**每个**字段必须**要么**在 `runtime_snapshot()` 里、**要么**在带理由的排除表里；不满足就失败并点名。反向也查（排除表里的幽灵字段、快照里的幽灵键） |

### 42.3 那条覆盖性测试立刻就抓到了 6 个字段

写完就红，点名 `account_ref` / `profile_source` / `run_id` / `runtime_profile` /
`target_revision` / `user_claims`。逐个判断后：

| 字段 | 处置 | 理由 |
| --- | --- | --- |
| `user_claims` | **持久化** | 见上（分层报告用） |
| `run_id` | 排除 | 每次开始/恢复都由 `_begin_run_segment` 重新签发（恢复=新执行段，设计 §14.2） |
| `runtime_profile` / `profile_source` / `target_revision` / `account_ref` | 排除 | 执行目标事实由 GUI 每次 resume **重新解析**写入；而 `restore` 发生在 `_apply_execution_target` **之后**，存进快照反而会把新事实**覆盖成旧事实**。真的没给目标时按设计 §1 落 `unresolved` 并退回注解 provisional —— 不靠旧值假装权威 |

> 最后一行是个**刻意保留的边界**，也值得记下来：`resume` 里 `restore` 与
> `_apply_execution_target` 的先后顺序决定了"哪些事实可以被持久化"。
> 如果以后要持久化执行目标，必须先把 `restore` 提到 apply 之前。

### 42.4 测试结果

| 测试集 | 结果 |
| --- | --- |
| `test_collab_v3_runner.py`（**新增 1 条** → 55 条） | **55 passed** |
| `test_collab_v3_workspace_snapshot_coverage.py`（**新增 8 条**） | **8 passed** |
| 62 个协作测试（本轮 61 → 62） | **821 passed, 1 skipped** |
| 全量 `tests/`（排除 §34.4 说明的 2 个陈旧文件） | **1100 passed, 28 skipped** |
| 仍失败 / 改动锁定测试 / 删除文件 | 无 / 0 / 0 |

新增测试：续跑后提交成功（含"任务确实变成 completed"）、记录往返（evidence / resource 各一条）、
待写内容过快照、坏行跳过（含 `version=0` 被 `__post_init__` 拒的情形）、缺键=空、
快照覆盖性与反向幽灵检查、**持久化字段的往返无损**。

### 42.5 这一类的清单（四处，都已修 + 现在有护栏）

| 轮次 | 状态 | 后果 |
| --- | --- | --- |
| §37 | 基线绑定 / 变更政策 | 恢复后最小修改约束与复测闸门**静默消失** |
| §40 | `tuning_state` 尝试计数 | 恢复后"最大尝试次数"可被无限绕开 |
| §41 | `task_revision` / `resource_versions` | 恢复后指纹成分丢失；且**从未被喂过值** |
| §42 | `pending_evidence` / `pending_resources` / `user_claims` | 续跑后**提交直接失败** |

前三次都是靠人工审计发现的；第四次之后加了 §42.2 那条覆盖性回归 ——
**以后新增字段忘了进快照会直接红**，不再依赖审计运气。

## 43. 阶段 2 补完：模型看到的层级序号是假的 + 死码扫描成工具（2026-09-28 续）

### 43.1 换了个方法：扫"定义了但全仓没人调用"

前几轮靠人工猜哪里可能"中间没接上"。这一轮改成机械扫描：

```powershell
python\python311\Scripts\python.exe tools\_scratch\audit_v3_dead_code.py
```

做法：对 `collaborator_v3` 包里**每个公开函数/方法**，统计它的名字在
`backend/ core/ controller/ gui/ tests/ tools/` 全仓出现次数；只有定义处出现 = 零调用。
名字以字符串出现也算命中，所以 `getattr(obj, "name")` 这类动态用法**不会被误报**。

结果只剩 6 条（去掉重复报的其实是 3 处）：

| 位置 | 结论 |
| --- | --- |
| `events/schema.py::EventEnvelope.is_legacy` | 信封 API 的一部分；读侧本来就没被生产消费（§40.4 已记），**保留** |
| `workspace.py::next_run_seq` / `next_subturn_seq` | **真的没人调用**，而且与权威归属矛盾 → 见下 |

### 43.2 缺口：`hierarchy_summary()` 里的 `run_seq` / `subturn_seq` 恒为 0

`model_summary()["hierarchy"]` 是每轮交给模型的上下文之一，里面有
`run_id` / `run_seq` / `subturn_seq`。而工作区这两个字段**没有任何人写**：

* `next_run_seq` / `next_subturn_seq` 从未被调用；
* 真正的权威在 **store**：run 序号来自 run 记录计数，副轮序号来自
  `SqliteDomainStore.max_subturn_seq()`（runner 用局部变量 `subturn_seq` 走它）。

于是模型每轮看到的是 `run_seq: 0 / subturn_seq: 0` —— 而层级 ID 正是设计 §14.1
要求它知道的事实。**喂假值比不喂更糟**：模型会以为这是"第 0 个副轮"。

### 43.3 交付内容

| 环节 | 实现 |
| --- | --- |
| 镜像权威值 | `_begin_run_segment` 写完 `workspace.run_id` 后一并写 `workspace.run_seq = seq`；副轮循环里每推进一轮写 `workspace.subturn_seq = subturn_seq`（进循环前也写一次） |
| 删掉误导性助手 | `next_run_seq` / `next_subturn_seq` 从未被调用，且暗示"工作区持有权威序号"，与 store 的事实矛盾 → **删除**；`hierarchy_summary` 的 docstring 写明"镜像、只读、不参与 ID 生成、不持久化" |
| 更正文档 | `ids.py` 原文写"序号由调用方（`TurnWorkspace.subturn_seq`）持有并持久化" —— 事实是 store 持有、工作区只是镜像，已改正 |
| 工具留档 | `tools/_scratch/audit_v3_dead_code.py`（顺手修了两处工具自身的小毛病：UTF-8 BOM 读法、方法被重复报两次） |

### 43.4 测试结果

| 测试集 | 结果 |
| --- | --- |
| `test_collab_v3_runner.py`（**新增 2 条** → 57 条） | **57 passed** |
| 62 个协作测试 | **823 passed, 1 skipped** |
| 全量 `tests/`（排除 §34.4 说明的 2 个陈旧文件） | **1102 passed, 28 skipped** |
| 仍失败 / 改动锁定测试 / 删除文件 | 无 / 0 / 0 |

两条新测试同样做了**"去掉修复就会失败"**验证：

```text
暂时移除两个镜像赋值 →
test_hierarchy_summary_shows_the_real_subturn_sequence     FAILED
test_resumed_turn_keeps_counting_up_in_the_hierarchy_report
E  assert [1, 1] == [2, 3]     （应为 [0,0]/[1,1]，取决于哪一处镜像被去掉）
恢复 → 2 passed
```

覆盖：单段主轮里模型依次看到 `subturn_seq = 1, 2`、`run_seq = 1`、`run_id` 形如 `run_*`；
恢复后**接着数**（`2, 3`）且 `run_seq = 2`（恢复 = 新执行段，设计 §14.2）。

## 44. 阶段 5 补完：trial 事件查询"先切片再筛选"→ 模型永远拿不到证据（2026-09-28 续）

### 44.1 缺口

两个面向模型的查询都在**切片之后**才筛选，于是"想要的证据在后面"时会返回空页，
而 `total` 报的还是**没筛过**的总数：

| 查询 | 旧行为 | 后果 |
| --- | --- | --- |
| `trial.get_action_events`（不带 `action`） | 先取前 `limit` 条**任意类型**事件，再内存里筛出点击/异常/关键帧 | 一次以匹配为主的试运行里前 100 条可能全是 `vision.match_completed` → **返回空**，但点击明明在后面 |
| `trial.get_vision_events(template_id=…/outcome=…)` | 先取前 100 条匹配事件，再按模板/命中与否过滤 | 问模板 B 只拿到模板 A 的前 100 条 → **返回空**；`total` 报成 151 |

这正是模型最容易得出错误结论的地方："这次运行一次点击都没有"／"这个模板从来没匹配过"。

### 44.2 交付内容

| 环节 | 实现 |
| --- | --- |
| 存储层 | `trial_runs.list_trial_events` 新增 `kinds=(…)`（类型集合）与 `predicate=callable`（payload 级自定义筛选）；**筛选全部发生在 `total`/`cursor`/`next_cursor`/切片之前**；筛选器抛异常按"不匹配"处理，不把查询打成 500 |
| `trial.get_action_events` | 不给 `action` 时传 `kinds=("input.click_completed","input.click_attempted","runtime.exception","frame.captured")`，不再内存过滤 |
| `trial.get_vision_events` | 模板/命中条件下沉成 `predicate`；返回的 `total`/`returned`/`next_cursor` 现在都是**筛过之后**的语义 |
| 兼容 | `kind=`（单值）与 `outcome=` 参数保持原样；无筛选时行为完全不变（有测试锁） |

### 44.3 测试结果

| 测试集 | 结果 |
| --- | --- |
| `test_collab_v3_trial_event_paging.py`（**新增 8 条**） | **8 passed** |
| 63 个协作测试（本轮 62 → 63） | **831 passed, 1 skipped** |
| 全量 `tests/`（排除 §34.4 说明的 2 个陈旧文件） | **1110 passed, 28 skipped** |
| 仍失败 / 改动锁定测试 / 删除文件 | 无 / 0 / 0 |

这次是**先写测试确认红**（6 failed）再改的：`kinds` 与 `predicate` 的分页正确性
（`total`/`next_cursor`）、150 条匹配之后的那一次点击仍能被 `get_action_events` 找到、
第 151 条匹配（模板 B / matched=true）仍能被 `get_vision_events` 选中、
`outcome=missed` 的 `total=150` 与默认 limit 截断、以及**无筛选时行为不变**。

### 44.4 这一类问题的清单（五处）

| 轮次 | 症状 |
| --- | --- |
| §39 | `RUN_VERSION_MISMATCH` / `SCRIPT_HASH_MISMATCH` 是死码 → 拿旧版本证据当结论 |
| §40 | `tuning_state` 未持久化 → 安全计数可绕过 |
| §41 | `task_revision` 从未被喂值 → 建任务后回放"没有任务" |
| §43 | `hierarchy` 的 `run_seq`/`subturn_seq` 恒为 0 → 喂假数据 |
| §44 | 事件查询先切片再筛选 → **永远返回空**（本次） |

共同特征：**"消费者存在的代码路径从来没被真正的数据走过"**。
本轮起这类审计可以机械做（§43.1 的死码扫描 + 本轮的"筛选顺序"检查），
不必再靠读代码猜。

## 45. 阶段 5 补完：命中位置稳定性是**凭空捏造**的结论（2026-09-28 续）

### 45.1 缺口（两个问题叠在一起）

`trial_diagnosis.summarize_vision` 的 `stable_best_location` 与
`VISION_LOCATION_UNSTABLE` 依赖命中位置坐标，但：

1. **生产里没有任何地方写位置**：它只读 `best_bbox`，而
   * 执行器 HUD **明明知道命中点**（`x` / `y`，`_match_and_click` 就在发），
     但 `ingest_match_hud` 的 match 分支**把这两个坐标丢了**（只传了分数）；
   * `PseudoRecorder` 也不写 bbox。
   于是坐标样本恒为空。
2. **更糟**：`location_stable` 的门槛是 `matches >= 2`，而 `_stddev([]) == 0.0`
   → 跑过两次匹配的运行会得到 **`stable_best_location: True`**：
   一条**没有任何数据支撑**的结论，而 `VISION_LOCATION_UNSTABLE` **永远不会出现**。

"喂假值比不喂更糟"这条在本轮又出现了一次：模型会拿"命中位置稳定"当事实。

### 45.2 交付内容

| 环节 | 实现 |
| --- | --- |
| 记录执行器已知的命中点 | `record_match(..., best_center=(x, y))`，payload 里新增 `best_center`；`ingest_match_hud` 的 match 分支把 HUD 的 `x/y` 传进来 |
| **不伪造 bbox** | 中心点是二元组、bbox 是四元组：从中心点编一个框就是伪造。两者分开存，`best_center` 只在执行器真的给了坐标时才有值 |
| 取值收敛 | 新增 `trial_diagnosis._hit_center(payload)`：**优先 bbox 中心，其次 best_center**，都没有 → `None` |
| 判稳定必须有样本 | `location_stable` 的门槛从 `matches >= 2` 改成 `location_samples >= 2`；**0 个样本 → `None`（未知）而不是 `True`（稳定）** |
| 让模型看得见样本数 | 摘要新增 `location_samples`（判稳定性用了几个坐标），0 就说明这次运行没记到命中点 |
| 查询说明 | `trial.get_vision_events` 的 catalog 文案改为"命中位置（`best_bbox` 框中心或 `best_center` 命中点，二者只有一个有值）"，并补上§44 的"筛选在分页之前" |

### 45.3 测试结果

| 测试集 | 结果 |
| --- | --- |
| `test_collab_v3_vision_location.py`（**新增 8 条**） | **8 passed** |
| 64 个协作测试（本轮 63 → 64） | **839 passed, 1 skipped** |
| 全量 `tests/`（排除 §34.4 说明的 2 个陈旧文件） | **1118 passed, 28 skipped** |
| 仍失败 / 改动锁定测试 / 删除文件 | 无 / 0 / 0 |

同样是**先写测试确认红**（8 failed）再改。覆盖：HUD 匹配记下 `best_center`
且 `best_bbox` 保持 `None`（不伪造）、没坐标时两者都是 `None`、
`record_match` 接受显式中心点、**没有样本 → `stable_best_location is None`**
（这条直接回归"凭空捏造 True"）、三次一致中心 → `True`、
三次散开中心 → `False` **且 `VISION_LOCATION_UNSTABLE` 真的出现**（以前不可能）、
bbox 与 center 同时存在时 bbox 优先、单样本仍判不了稳定。

### 45.4 至此 8 个诊断码全部可达

| 诊断码 | 生产数据的来源 |
| --- | --- |
| `TRIAL_NO_RUN` | 没有 run（§17.1 的原因码） |
| `RUNTIME_EXCEPTION` | `record_exception` / 运行期异常 |
| `VISION_THRESHOLD_NEAR_MISS` | 伪录制导入的分数+阈值（§38） |
| `VISION_LOCATION_UNSTABLE` | HUD 命中点 / bbox（**本轮**） |
| `CLICK_NOT_ATTEMPTED` | HUD 匹配但无点击 |
| `CLICK_EFFECT_NOT_OBSERVED` | `click_image_effect` 实测（§34） |
| `EXPECTED_SCENE_NOT_REACHED` | `click_image(expect=…)` 确认结果（§34） |
| `SCRIPT_HASH_MISMATCH` | 诊断查询传 `current_script_hash`（§39） |

## 46. 阶段 5 补完：点击追不回它的匹配（设计 §19.3/§19.5）（2026-09-28 续）

### 46.1 缺口

§19.3 要求每次点击记录「**生成坐标的 match event id**」，§19.5 的 `click_chain`
就是靠它把点击追回产生坐标的那次匹配。但生产里：

* `ingest_match_hud` 的 click 分支**从来不填** `source_event_id`；
* 只有 `pseudo_import`（伪录制时间线自带该字段时）才会填；
* 而 HUD 的 match 与 click 是**同一条代码路径先后发出的**（`_match_and_click`／
  `click_image_effect` 里先发 match 再发 click）—— 也就是说这条关联本来就在手边，
  只是被丢掉了。

后果：`click_chain` 给出的是一串**没有来源**的点击，模型无法回答
"这次点击用的是哪个分数？坐标是哪次匹配算出来的？"。

### 46.2 交付内容

| 环节 | 实现 |
| --- | --- |
| 记下"最近一次成功匹配" | `trial_runs._last_match_events[(账号, 模板路径)] = (run_id, match_event_id)`；**只记命中**的匹配（失败的匹配没有坐标可给） |
| 接到随后的点击上 | click HUD 没显式给 `source_event_id` 时补上它，并标 `source_event_link = "hud_adjacent_match"` |
| 标明这是**推导** | `record_click` 新增 `source_event_link` 字段；`click_chain` 一并输出。露出"显式 vs 相邻推出"是**刻意的**：推导出的关联可用，但不该被当成硬证据 |
| 不串 run | 双重保障：①记忆里带 `run_id`，不匹配就不用；②`start_run` 与两条收尾路径（面板 / `TaskController`）都调 `clear_match_links()` |
| 不伪造 | 没有匹配来源就保持空串；显式给了 `source_event_id` 就一直用显式的（伪录制导入的链接不会被覆盖） |

### 46.3 测试结果

| 测试集 | 结果 |
| --- | --- |
| `test_collab_v3_click_match_link.py`（**新增 9 条**） | **9 passed** |
| 65 个协作测试（本轮 64 → 65） | **848 passed, 1 skipped** |
| 全量 `tests/`（排除 §34.4 说明的 2 个陈旧文件） | **1127 passed, 28 skipped** |
| 仍失败 / 改动锁定测试 / 删除文件 | 无 / 0 / 0 |

同样是**先写测试确认红**（6 failed）再改。覆盖：点击指回产生坐标的匹配（含
`source_event_link` 标记）、没有匹配来源时保持空、失败匹配不算来源、
按模板各自记忆、后来的匹配覆盖先前的、显式 `source_event_id` 优先、
**跨 run 不串**、`click_chain` 能顺着 id 在同一次运行里查到那条 match 事件
（并读到它的分数）、伪录制自带链接不被"相邻匹配"覆盖。

### 46.4 仍未做的部分（如实说明）

§19.3 还要求点击记录 **frame id**（`frame_before` / `frame_after` 用的那种 id）。
HUD 侧**不掌握** frame id（`FrameObserver` 的帧没有向执行器暴露句柄），
所以这一项保持为空 —— **不猜**。要补的话需要让 observer 把 frame 句柄交给执行器，
那属于执行器/观察器接口改动，不在本轮范围。§19.4 的 `frame.captured` 事件
（§34/§38 已接）仍然提供了"事后能看哪些帧"的能力。

## 47. 阶段 5 补完：三件坐标只发了一件 + `EVENT_KINDS` 幽灵类型（2026-09-28 续）

### 47.1 缺口 A：§19.3 的三件坐标，发了三件但其实只有一件

§19.3 要求记「原始坐标、抖动/偏移量、缩放后的实际客户区坐标」。执行器**三件都知道**
（`_match_and_click` / 轮询分支 / `click_image_effect` 里都有 `match.x/y`、`pianyi`、
`final = match + pianyi`），但 HUD 只发了最终点，而且：

* `ingest_match_hud` 把它同时当成 `raw_xy` → **"原始坐标"其实是最终坐标**；
* `click_offset` 恒为空。

于是模型无法回答"模板命中的是哪一点？抖动把它挪了多少？"——而 §19.3 正是为此存在的。

**交付内容**：`raw_xy` 加进 `_CLICK_DETAIL_KEYS`；click 分支优先用显式 `raw_xy`，
否则退回 HUD 坐标（老行为不变）。**6 处生产者**全部补齐三件坐标：
`user_window._match_and_click` / `click_image` 轮询分支 / `click_image_effect`、
`user_browser.click_image` 轮询分支 / `click_image_effect`、
`browser/mixins/image_matching.click_image`。

### 47.2 缺口 B：`input.click_attempted` 是幽灵类型

`EVENT_KINDS` 的注释写着"封闭集合；新增必须在这里登记"，但**反过来没人管**：
登记了却没有任何地方产生。而且 `trial.get_action_events` 的"动作类事件"集合里
还带着它 —— 等于让查询去筛一个永远不存在的类型。
（"尝试了但没投递"其实一直是 `input.click_completed` + `delivered=false` +
`failed_stage`，诊断码也是这么读的。）

**交付内容**：从 `EVENT_KINDS` 与查询集合里去掉；`events.schema.EventType`
**保留**该成员（历史记录若真有那个字面量仍要可读 —— §28 的"老记录可读不伪造"）。
并新增护栏测试：**`EVENT_KINDS` 里每一种 kind 的字面量必须在包内出现 ≥2 次**
（一次登记 + 一次生产），否则测试失败并点名。

### 47.3 缺口 C：`outcome` 把"已知没投递"记成 `unknown`

```python
"outcome": "delivered" if delivered else ("failed" if error else "unknown")
```

"匹配没命中 → 根本没点下去"（HUD 明确发 `delivered=false, failed_stage=match`、
无异常）被记成 **`unknown`**，而 `failed_stage` 明明写清了失败在哪一步。

**交付内容**：抽出 `_click_outcome(delivered, error, failed_stage)`：
`True→delivered`、`False→failed`、`None→unknown`（执行器没给状态时不猜）。
排查确认：没有任何生产调用方传 `outcome=` 过滤、也没有测试断言旧取值，所以这是纯修正。

### 47.4 测试结果

| 测试集 | 结果 |
| --- | --- |
| `test_collab_v3_trial_event_kinds.py`（**新增 4 条**） | **4 passed** |
| `test_collab_v3_click_match_link.py`（**+6 条** → 14 条） | **14 passed** |
| `test_collab_v3_trial_hud_producers.py`（**+1 条** → 28 条） | **28 passed** |
| 66 个协作测试（本轮 65 → 66） | **858 passed, 1 skipped** |
| 全量 `tests/`（排除 §34.4 说明的 2 个陈旧文件） | **1137 passed, 28 skipped** |
| 仍失败 / 改动锁定测试 / 删除文件 | 无 / 0 / 0 |

先写测试确认红（幽灵类型 1 条 + `outcome` 1 条 + 三件坐标 2 条）再改。
其中三件坐标那条走的是**真实执行器路径**（`UserBrowser.click_image_effect` →
`_emit_click_hud` → `ingest_match_hud`），断言
`raw_xy=[50,30]`（命中点）、`click_offset=[3,-2]`（pianyi）、`final_xy=[53,28]`（实际点）。

### 47.5 这一族的护栏现在有两道

| 护栏 | 防的是什么 |
| --- | --- |
| `test_collab_v3_workspace_snapshot_coverage.py`（§42） | `TurnWorkspace` 新字段忘了进检查点快照 |
| `test_collab_v3_trial_event_kinds.py`（本轮） | `EVENT_KINDS` 登记了却没有生产者 |

## 48. 阶段 3/5 的关键发现：提示词自称"封闭集合"，却漏掉了 10 个已注册查询（2026-09-28 续）

### 48.1 缺口：§19.5 的 6 个诊断查询**模型根本不知道存在**

`provider_model._SYSTEM_PROTOCOL` 里写着：

```
可用 name（封闭集合，不许自造）：
query.catalog、task.get_current、viewport.get_current、resource.get、resource.search、
capability.search、capability.get、script.search、script.get、trial.get_latest_run、
trial.get_run、trial.get_log_tail、trial.get_script。
```

这是最早那 **13** 个查询的名单。而注册表已经有 **23** 个 —— 后加的 10 个
（`context.index`、`context.projection`、`terminology.resolve`、`tuning.propose`、
以及 **6 个 `trial.*` 诊断查询**）**一个都没写进去**。

而且措辞是「**封闭集合，不许自造**」：模型会认为那 10 个查询**不存在**。
也就是说 §19.5（6 个诊断查询）、§38（伪录制导入后的下钻）、
§43（短索引/投影）、§33（调参查询，只有它恰好在别处被提到过）这些实现，
在真实对话里**模型没有理由去调用**——除非它恰好先调了 `query.catalog`
（提示词只把它当作"参数不确定时"的兜底）。

这是同一族问题里影响面最大的一次：前面几轮是"数据没接上"，
这次是**能力没被告知**。

### 48.2 交付内容

| 环节 | 实现 |
| --- | --- |
| 名单补全 | 改为按用途分组的**全部 23 个**（通用 / 资源·能力·脚本 / 试运行 / 其他），并自报总数 |
| 诊断用法 | 明确写下：诊断试运行**先看 `trial.get_diagnosis_summary`**（本地已聚合好模板、位置稳定性、点击链、稳定诊断码），**不要自己吞全量日志**；下钻用 `trial.get_vision_events` / `trial.get_action_events` / `trial.get_event`；看图用 `trial.get_frame`（返回文件句柄，不是 base64） |
| 防止再漂移 | 新增 `tests/test_collab_v3_prompt_query_coverage.py`：**每个注册查询名都必须作为完整标识符出现在提示词里**；反向也查（提示词里出现的查询名必须已注册） |

护栏的匹配用 `(?<![\w.])名字(?![\w.])` 而不是 `name in text` ——
这一点是**被自己的验证过程纠正的**：第一版用子串判断，
把 `trial.get_frame` 篡改成 `trial.get_frameX` 时**没被发现**
（`"trial.get_frame" in "trial.get_frameX"` 为真）。改成标识符边界后，
两个方向都能抓到漂移（已实测：篡改 → 反向测试红；彻底移除 → 正向测试红）。

### 48.3 测试结果

| 测试集 | 结果 |
| --- | --- |
| `test_collab_v3_prompt_query_coverage.py`（**新增 5 条**） | **5 passed** |
| 66 个协作测试 | **863 passed, 1 skipped** |
| 全量 `tests/`（排除 §34.4 说明的 2 个陈旧文件） | **1142 passed, 28 skipped** |
| 仍失败 / 改动锁定测试 / 删除文件 | 无 / 0 / 0 |

覆盖：23 个注册名全部在提示词里、提示词不出现未注册的查询名、自报总数与注册表一致、
**§19.5 的 6 个诊断查询可被按名字叫出**、提示词明确要求先用本地聚合摘要。

### 48.4 护栏清单（三道）

| 护栏 | 防的是什么 |
| --- | --- |
| `workspace_snapshot_coverage`（§42） | 工作区新字段忘了进检查点快照 |
| `trial_event_kinds`（§47） | `EVENT_KINDS` 登记了却没有生产者 |
| `prompt_query_coverage`（本轮） | 注册了查询却没写进提示词（模型不知道它存在） |

三条的共同点：**把"两处必须一致"的手工约定变成会失败的测试**。
本轮之前，这三处不一致都只能靠人读出来。

## 49. 阶段 2 补完：7 个事件字面量翻译不出规范名（2026-09-28 续）

### 49.1 缺口：枚举有名字、映射缺一条 = 静默变"未知类型"

延续 §48 的思路，把"两处必须一致"的审计扩到事件层。新增工具
`tools/_scratch/audit_event_literals.py`：扫生产代码里调用
`_emit_event` / `_ws_event` / `_event` / `_emit` / `append_trial_event` 时传的
**字符串字面量**，再逐个问 `canonical_event_type()`。

结果：生产里共发出 **21 种**字面量，其中 **7 种翻译不出规范名**：

| 字面量 | 原因 | 处置 |
| --- | --- | --- |
| `QueryCacheReplayed` / `ScriptCandidateCreated` / `ScriptValidated` / `ScriptCommitted` | **枚举里早就有规范名，只是 `LEGACY_EVENT_ALIASES` 缺这一条** | 补别名（4 条） |
| `ClaimRecorded` | 枚举与映射都没有 | 新增 `claim.recorded` + 别名 |
| `frame.captured` / `script.note` | Runtime 自己写的试运行遥测类型，没有规范名 | 新增两个 `EventType` 成员 |

后果按设计是"**未知保持未知，不猜**"——所以不会报错，只会让投影/诊断
把这些事件当未知类型分类。而其中 4 条属于"看起来做了"：枚举里明明有名字。

### 49.2 交付内容

| 环节 | 实现 |
| --- | --- |
| 枚举 | 新增 `EventType.FRAME_CAPTURED` / `SCRIPT_NOTE` / `CLAIM_RECORDED` |
| 映射 | 补齐 4 条"枚举有、映射缺"的别名 + `ClaimRecorded` |
| 护栏 | `tests/test_collab_v3_event_literals.py`：**真的去 AST 扫生产代码**里的事件字面量，任何一个翻译不出 → 失败并点名（附出处文件）；另外钉住这 7 个、钉住"规范名可原样接受"、"未知保持未知"、"别名目标必须是真成员"、"别名左边不许撞规范名" |
| 工具留档 | `tools/_scratch/audit_event_literals.py`（退出码 1 = 有不可翻译的字面量，可直接进 CI） |

### 49.3 测试结果

| 测试集 | 结果 |
| --- | --- |
| `test_collab_v3_event_literals.py`（**新增 6 条**） | **6 passed** |
| 68 个协作测试（本轮 66 → 68） | **869 passed, 1 skipped** |
| 全量 `tests/`（排除 §34.4 说明的 2 个陈旧文件） | **1148 passed, 28 skipped** |
| 仍失败 / 改动锁定测试 / 删除文件 | 无 / 0 / 0 |

护栏的**有效性也实测过**：临时删掉 `ScriptValidated` 那条别名 →
`test_every_emitted_event_literal_maps_to_a_canonical_type` 与
`test_the_previously_broken_literals_are_registered` 双双变红；恢复后 6 passed。

过程中被自己的护栏纠了一次：第一版把 `PKG_ROOT` 算成
`backend/script_generator`（`parents[2]`），于是扫到了 V2 的
`collaborator/llm_turn.py`，把它的一堆**进度文案**当事件类型报了出来
（19 个假阳性）。改成 `parents[1]`（= `collaborator_v3`）后干净。
—— 这也说明"先看护栏自己扫了哪些文件"是必要的一步，否则很容易被噪声劝退。

### 49.4 护栏清单（四道）

| 护栏 | 防的是什么 |
| --- | --- |
| `workspace_snapshot_coverage`（§42） | 工作区新字段忘了进检查点快照 |
| `trial_event_kinds`（§47） | `EVENT_KINDS` 登记了却没有生产者 |
| `prompt_query_coverage`（§48） | 注册了查询却没写进提示词 |
| `event_literals`（本轮） | 生产发出的事件字面量没登记规范名映射 |

## 50. 阶段 4 补完：`request_interaction` 的策略要求没写进提示词（2026-09-28 续）

### 50.1 缺口：三个 reason 需要的字段名，模型只能靠被拒一次去猜

`PolicyEngine.may_request_interaction` 对每个 `reason_code` 都有硬性要求：

| reason | 要求 |
| --- | --- |
| `PERMISSION_REQUIRED` | `permission_checked` + `lower_risk_alternatives_exhausted` |
| `REQUIREMENT_AMBIGUOUS` | 四个 ambiguity 标志**任一**为真 |
| `REALITY_CONFLICT` | `conflict_evidence_refs` + `closest_alternative` |
| `RESOURCE_REQUIRED` | 五个"查过了"标志全真 |
| `LOGIC_DRAFT_CONFIRMATION` | `logic_draft_ref` |

其中 `RESOURCE_REQUIRED` 的五个由 Runtime 从**实际执行过的查询**推导
（`_observed_interaction_flags`，模型自报不被采信），**不用模型填**。
剩下三个 reason 要的字段必须模型自己给 —— 而提示词里只有一个字面省略号
`"policy_context":{...}` 和一句 `logic_draft_ref`，**字段名一个都没写**。

后果：模型第一次 `request_interaction` 必被拒（`INTERACTION_NOT_JUSTIFIED` +
`missing_policy_context`），只能按返回的缺口猜回来。**每次交互白烧一个副轮**；
而交互正是"重大歧义先问"的出口（设计 §16），是脚本类任务里常见的一步。

### 50.2 交付内容

| 环节 | 实现 |
| --- | --- |
| 提示词 | 在 `request_interaction` 段落补上**逐 reason 的字段清单**，并显式写明 `RESOURCE_REQUIRED` 那五项"**不用你填**、由 Runtime 按实际查询自动推导"（否则模型会硬编一遍自报值，而自报**不被采信**） |
| 护栏 | `tests/test_collab_v3_interaction_policy_prompt.py`（17 条）：①每个 reason 的空上下文必须被拒；②按 `missing_policy_context` 补齐后**一定**能通过（自愈指引的**充分性**）；③**凡是"报告出来的缺口里、Runtime 不自动推导的字段"，提示词必须点名**；④五个 reason 都在提示词里；⑤提示词要说明哪些字段是自动推导的 |

第 ③ 条是核心：它把"策略要求"与"提示词"这两处必须一致的东西变成会失败的测试。
**要求是从策略引擎实际行为里取的**（`_missing_interaction_context` 的返回值），
不是在测试里再抄一份表 —— 所以改策略不改提示词会被抓到。

### 50.3 测试结果

| 测试集 | 结果 |
| --- | --- |
| `test_collab_v3_interaction_policy_prompt.py`（**新增 17 条**） | **17 passed** |
| 69 个协作测试（本轮 68 → 69） | **886 passed, 1 skipped** |
| 全量 `tests/`（排除 §34.4 说明的 2 个陈旧文件） | **1165 passed, 28 skipped** |
| 仍失败 / 改动锁定测试 / 删除文件 | 无 / 0 / 0 |

先写测试确认红（3 个 reason 缺字段名 → 3 条失败）再改。护栏有效性实测：
把提示词里的 `conflict_evidence_refs` 改成 `evidence_refs` →
`test_prompt_names_every_flag_the_model_must_supply[REALITY_CONFLICT]` 立刻变红并点名；
恢复后 17 passed。

### 50.4 护栏清单（五道）

| 护栏 | 防的是什么 |
| --- | --- |
| `workspace_snapshot_coverage`（§42） | 工作区新字段忘了进检查点快照 |
| `trial_event_kinds`（§47） | `EVENT_KINDS` 登记了却没有生产者 |
| `prompt_query_coverage`（§48） | 注册了查询却没写进提示词 |
| `event_literals`（§49） | 生产事件字面量没登记规范名映射 |
| `interaction_policy_prompt`（本轮） | 策略要求的字段没写进提示词（模型只能靠被拒一次去猜） |

## 51. 阶段 5 补完：伪录制摘要按"前 500 条"统计（§44 的第三种形态）（2026-09-28 续）

### 51.1 缺口

`trial.get_pseudo_record_summary` 的实现是：

```python
items = list(list_trial_events(task_id, run_id, limit=500).get("items") or [])
counts = Counter(kind for e in items)
```

也就是**先取前 500 条、再统计**。§44 修的是"筛选发生在切片之前"，
这里换了个形态：**统计**发生在切片之后。以匹配为主的运行里，点击/关键帧都在后面，
于是摘要会报 **"0 次点击、0 个关键帧"**（实测：600 条匹配 + 3 次点击 + 1 个关键帧
→ 只统计到 500 条匹配，`event_counts` 里点击 0、`keyframe_count` 0）。

模型拿这个摘要就会得出"这次试运行根本没点过"的相反结论 ——
而这个查询正是设计 §19.5 里给模型的**总览入口**。

### 51.2 交付内容

| 环节 | 实现 |
| --- | --- |
| 新聚合函数 | `trial_runs.summarize_trial_events(task_id, run_id)`：按类型统计**全部**事件，**不做分页截断**；返回 `event_total` / `event_counts` / `keyframe_count` / `keyframes` |
| 展示与计数分开 | `keyframes` 是**展示用**截断列表（默认 20），`keyframe_count` 才是真实数量；查询里额外给 `keyframes_truncated`，避免把截断当总量 |
| 查询改造 | `trial.get_pseudo_record_summary` 改用该函数，并新增 `event_total`；`recording_enabled` / `telemetry_quality` 也按真实总量判断（以前按"有没有前 500 条"判断） |

### 51.3 测试结果

| 测试集 | 结果 |
| --- | --- |
| `test_collab_v3_trial_event_paging.py`（**新增 3 条** → 11 条） | **11 passed** |
| 69 个协作测试 | **889 passed, 1 skipped** |
| 全量 `tests/`（排除 §34.4 说明的 2 个陈旧文件） | **1168 passed, 28 skipped** |
| 仍失败 / 改动锁定测试 / 删除文件 | 无 / 0 / 0 |

先写测试确认红（`assert 500 == 600`、`KeyError: 'event_total'`）再改。
覆盖：600 匹配 + 3 点击 + 1 关键帧的统计**全部**正确、25 个关键帧时
`keyframe_count=25` 而展示列表 20 条（计数不被展示截断影响）、大运行报出 `event_total`。

### 51.4 "先切片再处理"这一族的三次形态

| 轮次 | 形态 | 后果 |
| --- | --- | --- |
| §44 | 先切片再**筛选** | `get_action_events` / `get_vision_events` 返回空页 |
| §51 | 先切片再**统计** | 伪录制摘要报"0 次点击、0 个关键帧" |

共同修法：**把筛选/统计下沉到分页之前**，`total`/`cursor`/计数一律针对完整集合。

## 52. 阶段 5 补完：伪录制导入的关键帧引用指不着文件（2026-09-28 续）

### 52.1 缺口

伪录制时间线里 `keyframe` 行的 `path` 是**相对录制目录**的
（`frames/0001_diag.jpg`，见 `run_recorder.maybe_keyframe` 的 `rel`）。
`pseudo_import` 导入时**原样**存进事件的 `path`，于是：

* `trial.get_frame` 返回的句柄就是一个相对路径（`frames/0001_diag.jpg`）；
* 它既不在运行目录下、也不在素材区 → 模型拿到句柄**打不开**；
* 而设计 §19.4/§19.5 要求"图像作为文件保存，事件只放**引用**"，
  引用指不着文件的话，"只放引用"这个设计就落空了。

这和 §38 是同一个功能的两端：§38 把伪录制接进了遥测，本轮让**帧引用真的可用**。

### 52.2 交付内容

| 环节 | 实现 |
| --- | --- |
| 新增 | `pseudo_import._resolve_frame_ref(record_dir, raw)` |
| 解析规则 | 已是绝对路径 → 原样；`录制目录/相对路径` 存在 → 返回其绝对路径；`录制目录/frames/文件名` 存在 → 同上；**都不成立 → 原样返回**（不编一个不存在的路径） |
| 接入 | `pseudo_import` 的 `keyframe` 分支用解析后的路径写 `frame.captured`；`frame_id` **仍用时间线里的原值**（去重与 `trial.get_frame` 的键都靠它，不能改成路径） |

### 52.3 测试结果

| 测试集 | 结果 |
| --- | --- |
| `test_collab_v3_pseudo_frame_handle.py`（**新增 6 条**） | **6 passed** |
| 70 个协作测试（本轮 69 → 70） | **895 passed, 1 skipped** |
| 全量 `tests/`（排除 §34.4 说明的 2 个陈旧文件） | **1174 passed, 28 skipped** |
| 仍失败 / 改动锁定测试 / 删除文件 | 无 / 0 / 0 |

先写测试确认红（3 条）再改。覆盖：相对路径被解析成真实绝对路径、
查询返回的句柄指向真实文件、**文件不在时原样保留不伪造**、绝对路径不被改动、
`frame_id` 仍用时间线原值、**收尾（`finish_run`）之后句柄依然可用**。

### 52.4 关键帧这条链现在的样子

| 环节 | 谁负责 | 状态 |
| --- | --- | --- |
| 采集 | `PseudoRecorder.maybe_keyframe`（click / capture / diag） | 既有 |
| 落盘 | `screenshots/pseudo_record/<run>/frames/*.jpg` | 既有 |
| 接进遥测 | `import_pseudo_record_for_run`（§38） | 已接（面板 + TaskController 两条收尾路径） |
| 引用可解析 | `_resolve_frame_ref`（本轮） | 已修 |
| 同进程内的关键帧 | `trial_frames.save_key_frame`（§34，内容 hash 命名、去重） | 已接 |
| 模型读取 | `trial.get_frame` → 文件句柄（不是 base64） | 可用 |

## 53. 阶段 1 补完：校验侧没跑禁用区闸门 → 凭证在交付那一刻就是假的（2026-09-28 续）

### 53.1 缺口：同一条闸门，两条路径不一致

`materialize_trial_script(ban_snippet=...)` 是**阻断式**闸门：`ban_snippet` 非空
而源码里缺 `_COLLAB_BAN_ZONES` 声明时 `ban_blocked=True`（不自动补写）。
两条使用路径对它的处理并不一致：

| 路径 | 传了 ban_snippet？ | 结果 |
| --- | --- | --- |
| 试运行写盘（GUI `_write_trial_file`） | ✅ 传了 | `raise RuntimeError(ban_message)`，**直接拦住** |
| V3 校验侧（`V2ScriptValidator.verify_artifact` → `script_artifact.verify_artifact`） | ❌ **没传** | 禁用区缺失的脚本**照样过校验、拿到 ValidationReceipt** |

后果很具体：模型交出一个"已校验、已签凭证、已交付"的脚本，用户点试运行时才被告知
"缺少禁用区声明"。**凭证在交付那一刻就是假的** —— 而设计 §4.5/§8.3 的产物闸门
本意就是"要跑的那一份能跑"。

### 53.2 交付内容

| 环节 | 实现 |
| --- | --- |
| 统一来源 | 校验适配器新增 `_ban_snippet()`，读**与写盘侧同一个键**（`session.artifacts["ban_script_snippet"]`） |
| 传进闸门 | `verify_artifact(..., ban_snippet=self._ban_snippet())` → `BAN_GATE_BLOCKED` 变成阻塞 issue → **不签凭证** |
| 老会话容错 | `artifacts` 为 None / 非 dict → 空串（= 没有禁用区要求），不炸 |

### 53.3 测试结果

| 测试集 | 结果 |
| --- | --- |
| `test_collab_v3_validation_ban_gate.py`（**新增 5 条**） | **5 passed** |
| 71 个协作测试（本轮 70 → 71） | **900 passed, 1 skipped** |
| 全量 `tests/`（排除 §34.4 说明的 2 个陈旧文件） | **1179 passed, 28 skipped** |
| 仍失败 / 改动锁定测试 / 删除文件 | 无 / 0 / 0 |

先写测试确认红（1 条）再改。覆盖：会话声明了禁用区而脚本没写 → **校验被拦**
（`BAN_GATE_BLOCKED`，`ok=False`）、写了声明 → 不拦、会话没声明 → 闸门不生效
（不误伤普通脚本）、`artifacts` 缺失不炸、标记常量本身被钉住。

### 53.4 教训：别用"我 grep 过了"当结论

这条差点被漏掉：我第一次 grep `verify_artifact` 的调用方时，PowerShell 的
`backend\script_generator\**\*.py` **没有递归到 `collaborator_v3/adapters/`**，
于是"没有生产调用方"的结论是错的（`adapters/script_validator.py:38` 就是调用方）。
后面改用显式目录列举 + `Select-String` 复核才发现真实情况是
"有调用方、但少传了一个参数"——**这两种情况的危害完全不同**。
后续所有"零调用/零生产方"的判断都要像 §43 那样用 AST 扫，不靠 glob。

## 54. 阶段 6 补完：分数间隔护栏写好了却零调用（2026-09-28 续）

### 54.1 新工具：扫"声明了但函数体里没用过的参数"

`tools/_scratch/audit_unused_params.py`：对关键目录里每个函数，收集它声明的参数，
再看函数体里出现过哪些标识符；报出**从来没用过**的参数。
（第一版把 `ast.arg` 也算成"用过"→ 结果永远"零发现"；去掉那一条才有效。）

结果 26 条，绝大多数是良性（Ports 协议里的 `...` 空实现、按接口统一而保留的
`ctx`/`_args`）。但顺着这张表发现了一个**真缺口**：

### 54.2 缺口：`lower_threshold_is_unsafe()` 生产里零调用

`tuning.lower_threshold_is_unsafe()` 早就写好了、也有单测：

> best 与 second-best 只差 0.0xx：模板区分度不足，降阈值会制造误命中。
> 先修模板/搜索区域，不要继续降阈值

但 `tuning.propose` 只调 `propose_tuning`（安全范围 / 单步 / 尝试上限），
**从来不问"分数间隔够不够"**。于是设计 §6.4 这条**证据型**护栏形同虚设：
模型可以一路贴着地板往下磨阈值，只要不越步长、不超次数就都能过。

这跟 §40 是同一个安全故事的两半：一半是"试太多次"（已修），
一半是"试的理由不成立"（本轮）。

### 54.3 交付内容

| 环节 | 实现 |
| --- | --- |
| 新入参 | `tuning.propose` 增加 `score_margin`（schema + normalize + 例子） |
| 接线 | 其它检查都通过后，若给了 `score_margin` 且判定 `lower_threshold_is_unsafe` → 返回 `ok=false` + `reasons=["LOWERING_THRESHOLD_WITH_TINY_SCORE_MARGIN"]` + 可操作 message |
| 不计数 | 被证据型护栏拒掉**不记尝试**（这次什么都没改）——否则拒一次就白烧一次配额，等于变相惩罚模型求助 |
| 不加负担 | 不给 `score_margin` 时行为完全不变（老调用方与既有测试不受影响） |
| 提示词 | 写明"降低 threshold 要带 score_margin（从诊断摘要或匹配事件的 `score_margin` 拿）"与这条例外的含义 |
| 护栏 | 测试里钉住"`lower_threshold_is_unsafe` 必须出现在生产代码里"（防它再次变成死码） |

### 54.4 测试结果

| 测试集 | 结果 |
| --- | --- |
| `test_collab_v3_tuning_margin_guard.py`（**新增 9 条**） | **9 passed** |
| 72 个协作测试（本轮 71 → 72） | **909 passed, 1 skipped** |
| 全量 `tests/`（排除 §34.4 说明的 2 个陈旧文件） | **1188 passed, 28 skipped** |
| 仍失败 / 改动锁定测试 / 删除文件 | 无 / 0 / 0 |

先写测试确认红（5 条）再改。覆盖：间隔 0.02 时降阈值被拒且理由正确、
间隔 0.30 时放行、**被拒不烧尝试配额**、抬高阈值与间隔无关、不给间隔行为不变、
超单步仍按 `STEP_TOO_LARGE` 拒（两条护栏互不干扰）、非阈值参数忽略间隔、
生产代码确实引用了该护栏、提示词写明 `score_margin`。

### 54.5 与 §40 合起来看

| 半边 | 机制 | 状态 |
| --- | --- | --- |
| 试太多次 | `TuningState` 尝试计数 + `MAX_ATTEMPTS_REACHED`（**跨 pause→resume 存活**） | §40 已修 |
| 理由不成立 | `lower_threshold_is_unsafe`（分数间隔）+ 地板钳制 + 单步上限 | 本轮已接 |

## 55. 阶段 3 补完：查询失败的解释被丢掉了（2026-09-28 续）

### 55.1 缺口

`queries._fail(action_id, code, message, *, retry, summary)` 的实现是：

```python
return RuntimeResult(False, action_id, dict(summary or {}), error_code=..., recoverable=True, ...)
```

`RuntimeResult` 没有 message 字段，而 `message` **也没被塞进 summary** →
**17 处调用点**写好的中文解释全部没到模型手里。模型只看到
`error_code` + `retry_policy`，只能猜"为什么被拒、该改成什么"。

这不是"少一条文案"：失败的**可操作性**就靠这句话。典型如

* `trial.get_event` 找不到事件 → 模型不知道是 run_id 错了还是 event_id 错了；
* `tuning.propose` 参数没登记 → 解释里带着"不能自主调"，而 summary 只有参数清单；
* `script.get` 找不到 → 解释里带着"先用 script.search 找 id"。

（这条是从 §54 那个"未使用参数"扫描里顺出来的：当时它报的是
`_fail(): message` —— 参数没用，恰恰说明它没生效。）

### 55.2 交付内容

| 环节 | 实现 |
| --- | --- |
| 修 `_fail` | `payload = dict(summary or {})` + `payload.setdefault("message", message)`；调用方自带 `message` 时不覆盖 |
| 不破坏既有断言 | 已核对：**没有任何测试**断言查询失败载荷的精确形状（都是按 key 断言），所以这是纯补信息 |
| 可见性链路 | 既有机制会把 `summary` 放进给模型的 `result_summary`（其它测试已在用 `summary["already_available"]`），所以无需再改传递层 |

### 55.3 测试结果

| 测试集 | 结果 |
| --- | --- |
| `test_collab_v3_query_failure_message.py`（**新增 6 条**） | **6 passed** |
| 73 个协作测试（本轮 72 → 73） | **915 passed, 1 skipped** |
| 全量 `tests/`（排除 §34.4 说明的 2 个陈旧文件） | **1194 passed, 28 skipped** |
| 仍失败 / 改动锁定测试 / 删除文件 | 无 / 0 / 0 |

先写测试确认红（5 条）再改。覆盖：`_fail` 把 message 放进载荷、保留调用方 summary、
不覆盖调用方自带的 message、`trial.get_event` 失败的 explanation、
`tuning.propose` 失败时 explanation + 参数清单都在，以及**端到端**：
真实主轮里模型那一侧的 `result_summary["summary"]["message"]` 真的带上了解释。

### 55.4 两个 AST 工具已经收回成本

| 工具 | 找到的东西 |
| --- | --- |
| `audit_v3_dead_code.py`（§43） | `next_run_seq`/`next_subturn_seq` 零调用 → 层级序号喂假值 |
| `audit_event_literals.py`（§49） | 7 个事件字面量翻译不出规范名 |
| `audit_unused_params.py`（§54） | `lower_threshold_is_unsafe` 零调用（§54）+ `_fail().message` 没生效（本轮） |

## 56. 阶段 6 补完：调参历史的 `verified` 是死字段（2026-09-28 续）

### 56.1 缺口

`TuningAttempt.verified` 全仓**没有任何地方设为 True**（只有 `to_dict`/`from_dict`/
`tuning_summary` 在读它）。后果：

* `tuning_summary()` 永远报 `verified: false` —— 哪怕这一版**明明已经复测过**，
  审计轨迹是错的；
* `requires_retest_before_claiming_fixed()`（纯函数版）靠它判定，
  于是永远认为"还有未复测的调整"。

生产上的复测闸门确实是 §35 的 `_retest_obligation_gate`（按脚本 hash 查
`has_retest_for_turn`），但**闸门通过的那一刻**正是"这版调参已复测"的权威时刻 ——
不顺手记下来，历史就永远是假的。

### 56.2 交付内容

| 环节 | 实现 |
| --- | --- |
| 状态机 | `TuningState.mark_verified() -> int`：把未复测的调整标成已复测（`dataclasses.replace`），**返回本次标记条数**，幂等 |
| **不动安全计数器** | `attempts` 计数不因为"复测成功"而回退 —— 它是"试了几次"的配额，不是信用分 |
| 接线 | `ActionRunner._mark_tuning_verified(workspace)`，在 `_retest_obligation_gate` 与 `_fix_claim_gate` **判定有复测证据**时调用；`tuning_state` 缺失或标记失败都不影响闸门结论 |
| 持久化 | 走 §40 已接的快照（`tuning_state.to_dict()`），因此"已复测"跨 `pause → resume` 存活 |

### 56.3 测试结果

| 测试集 | 结果 |
| --- | --- |
| `test_collab_v3_tuning_verified.py`（**新增 9 条**） | **9 passed** |
| 74 个协作测试（本轮 73 → 74） | **924 passed, 1 skipped** |
| 全量 `tests/`（排除 §34.4 说明的 2 个陈旧文件） | **1203 passed, 28 skipped** |
| 仍失败 / 改动锁定测试 / 删除文件 | 无 / 0 / 0 |

先写测试确认红（8 条）再改。覆盖：只翻转未复测的、幂等、**计数不受影响**、
跨快照往返后仍为已复测、摘要报 `verified: true`、纯函数助手随之改口、
两个闸门在有证据时标记、没证据时不标记、`tuning_state` 缺失不炸。

过程中自己的测试写错过两次并修正（都是"前置条件没给全"）：
闸门需要**对比基线**（`comparison_script`）与**交付正文**（`validated_script_text`），
只给 hash 会让它在"没有可比的上一版"处提前返回 —— 这两点已写进测试注释，
免得下一个人重踩。

### 56.4 复测这条规则现在的样子

| 实现 | 位置 | 状态 |
| --- | --- | --- |
| **生产闸门**（按脚本 hash 查 `has_retest_for_turn`） | `ActionRunner._retest_obligation_gate` / `_fix_claim_gate` | 生效，且通过时打 `verified` 标记 |
| 纯函数版判定 | `tuning.requires_retest_before_claiming_fixed` | 目前只有测试在用，但**语义现在是对的**（不再永远返回"必须复测"） |
| 排障摘要 | `tuning.tuning_summary` | `history[].verified` 现在是真的 |

## 57. 阶段 5 补完：`runtime.exception` 生产里零调用 + 一个盖住原异常的隐患（2026-09-28 续）

### 57.1 缺口 A：验收场景 11 不成立

设计 §31 的验收场景 11 是「脚本首行异常：仍能查询 run、script hash、exception 和关键上下文」。
但 `trial_runs.record_exception()` **生产里零调用**（只有定义 + 测试）：

* 脚本第一行炸掉时，run 里只有一条"结束"状态，**没有任何异常事件**；
* 于是 `RUNTIME_EXCEPTION` 这个诊断码在真实运行里永远不可能出现。

> ⚠️ 顺带更正 §45.4 那张表：里面写 `RUNTIME_EXCEPTION` 的来源是
> "`record_exception` / 运行期异常"，**那是错的** —— 当时只看了函数存在，
> 没查它有没有调用方。八个诊断码里实际上只有七个可达，本轮补齐第八个。

### 57.2 缺口 B：`user_browser` 晚初始化会把原异常盖成 `UnboundLocalError`

`_run_async` 里 `user_browser = None` 原来写在 `try` **内部**（在 `_load_script()` 之后）。
`_load_script()` 抛异常时，`finally` 里的 `for tgt in (run_target, user_browser)`
会抛 `UnboundLocalError: cannot access local variable 'user_browser'`，
**把真正的错误信息埋掉**（也正是"脚本首行异常"这条路径）。

### 57.3 交付内容

| 环节 | 实现 |
| --- | --- |
| 修初始化 | `user_browser = None` 提到 `try` 之前 |
| 阶段标记 | 新增局部 `trial_phase`（`import` → `target` → `run` → `finish`），异常时写进事件 |
| TaskController 侧 | `_record_trial_exception(phase, message)`；在 `except Exception`（`phase` 用当前值）与 `except (TaskStopped, CancelledError)`（`phase="stopped"`）里调用 |
| 面板侧 | 同名 `_record_trial_exception()`；"启动试运行失败"时以 `phase="start"` 记录，且在 `_finish_v3_trial_record` **之前** |
| 顺序 | 事件写在 `finally → end_collab_trial` 收尾之前（收尾会把 run 标记结束） |

### 57.4 测试结果

| 测试集 | 结果 |
| --- | --- |
| `test_collab_v3_runtime_exception_event.py`（**新增 7 条**） | **7 passed** |
| 75 个协作测试（本轮 74 → 75） | **931 passed, 1 skipped** |
| 全量 `tests/`（排除 §34.4 说明的 2 个陈旧文件） | **1210 passed, 28 skipped** |
| 仍失败 / 改动锁定测试 / 删除文件 | 无（见 57.5） / 0 / 0 |

覆盖：helper 写入正确 phase 与 message、没有 run 时静默、
**端到端：`_load_script` 抛异常后 run 存在 + `script_hash` 正确 + `runtime.exception` 事件 +
`RUNTIME_EXCEPTION` 诊断码可达**、原异常不被 `NameError` 盖掉、停止也留事件、
面板侧同名 helper（有/无 id 两种情况）。

**两条关键测试都做了"去掉修复就会失败"验证**：

```text
移除 user_browser 的提前初始化 →
test_import_failure_still_leaves_run_and_exception            FAILED
test_import_failure_does_not_get_masked_by_a_name_error       FAILED
E  UnboundLocalError: cannot access local variable 'user_browser' ...   ← 原异常被盖掉
恢复 → 7 passed
```

### 57.5 教训：端到端测试动了进程级全局，会污染别的测试

新增的 `_run_async` 端到端测试会在 `finally` 里把**冷定标日志静音**
（`set_cold_calibration_muted(True)`，进程级全局）。单跑新文件全绿，
但全量套件里 `test_scale_cache_policy.py` 的两条限流断言变红 ——
因为静音开关被留在了 `True`。

修法：新测试文件加 autouse fixture，用**公开读写接口**保存/恢复该开关
（`is_cold_calibration_muted` / `set_cold_calibration_muted`），不直接碰私有全局。

> 记一笔：**"单跑绿、全量红"基本都是全局状态泄漏**。
> 凡是驱动真实执行路径（而不是纯函数）的测试，都要问一句"它动了哪些进程级状态"。

## 58. 阶段 3 补完：`latest_script` 投影永远报"没有提交"（设计 §21 / 验收场景 5）（2026-09-28 续）

### 58.1 缺口：跨主轮恢复交付脚本的读路径是死的

验收场景 5：「新主轮修复：没有旧聊天上下文，通过 task index 找到实际交付脚本和素材」。
`context.index` 会把 `latest_script` 作为可用投影告诉模型，但它的实现有两处硬伤，
**两处都静默**给出"没有交付脚本"的错误结论：

| # | 硬伤 | 后果 |
| --- | --- | --- |
| 1 | `store.list_commits(...)` —— **这个访问器根本不存在**，`hasattr` 守卫把缺失吃成空列表 | 投影**永远**返回 `{"available": False, "reason": "NO_COMMIT"}` |
| 2 | 即使有 commits，读的是 `result["script"]`，而提交记录的形状是 `{"commit_id","turn_id","final_response","memory_summary"}`（脚本在 `result["final_response"]["script"]`，`latest_committed_script` 就是这么读的） | `has_script` 永远 False、`script_length` 永远 0 |

也就是说：新主轮里模型问"上次交付的脚本是什么"，拿到的是"没有提交" —— 与事实相反。

### 58.2 交付内容

| 环节 | 实现 |
| --- | --- |
| 补访问器 | `SqliteDomainStore.list_commits(task_id, *, limit=20) -> list[dict]`：`{"id","turn_id","created_at","result"}`，按 `created_at DESC, rowid DESC`；按任务隔离；读不出返回空列表（不猜） |
| 修路径 | 投影改读 `result["final_response"]["script"]`，与 `latest_committed_script` 口径一致 |
| 补可用信息 | 新增 `script_name` 与 `script_sha256`（**由正文算出**，供与凭证/试运行证据比对）；`legacy` 仍表示"提交行本身没带 hash"（老式记录不冒充新式凭证） |
| 顺带补齐提交记录 | `_finish` 里用**本主轮校验过的**脚本名补齐 `final_response.script_name`（模型常常不回填）——"上次交付的脚本叫什么"是跨主轮恢复与排障都要用的事实 |

### 58.3 测试结果

| 测试集 | 结果 |
| --- | --- |
| `test_collab_v3_latest_script_projection.py`（**新增 6 条**） | **6 passed** |
| 76 个协作测试（本轮 75 → 76） | **937 passed, 1 skipped** |
| 全量 `tests/`（排除 §34.4 说明的 2 个陈旧文件） | **1216 passed, 28 skipped** |
| 仍失败 / 改动锁定测试 / 删除文件 | 无 / 0 / 0 |

先写测试确认红（4 条）再改。覆盖：`list_commits` 新→旧且按任务隔离、
投影读出交付脚本（内容/长度/名字/hash/commit_id）、没有提交时仍如实 `NO_COMMIT`、
**端到端：新主轮通过 `context.index` + `context.projection` 找回上次交付的脚本**、
blockers 投影不再被带歪。

写测试时自己也踩了一次：最初直接 `run()` 一次 finish，被
`LOGIC_CONFIRMATION_REQUIRED` 拦下（**finish 之前必须先确认逻辑草稿**），
于是 helper 改成"先问一次 → resume 时确认 → 校验 → finish"的既有路径，
并加了一句前置断言（`assert store.list_commits(...)`），
免得以后 helper 悄悄不产生提交却让测试"看起来在测别的"。

### 58.4 验收场景 5 现在成立

| 环节 | 状态 |
| --- | --- |
| 新主轮（无旧聊天上下文）拿到短索引 | `context.index` ✅ |
| 按需读"上次交付的脚本" | `context.projection(name="latest_script")` ✅（本轮修） |
| 素材 | `current_resources` 投影 + `resource.search` ✅ |
| 与凭证/试运行对账 | `script_sha256` 现在真的给得出来 ✅ |

## 59. 验收场景 10 核实通过 + 一个**未实现**的结构性缺口（2026-09-28 续）

### 59.1 核实：分层确实挡住了跨主轮污染

验收场景 10：「模型胡扯：无 evidence 的事实结论被协议/闸门拒绝，
**不能进入下一主轮 task facts**」。

工作区级分层早有测试，但"下一主轮"这一半只在文档里写着。本轮把它变成可执行事实
（`tests/test_collab_v3_claim_cross_turn.py`，3 条）：跑完一轮之后查
`store.get_task_facts()`：

| 层 | key | 结果 |
| --- | --- | --- |
| `fact`（用户明确表达的目标） | `goal_click_claim` | ✅ 入库，成为下一主轮可见的事实 |
| `hypothesis`（模型推测 + verification_plan） | `img_hp_bar` | ✅ **不入库** |
| `user_claim`（用户对外部现实的说法） | `img_boss_hp` | ✅ **不入库** |
| 被拒（无真实 evidence_refs） | `no_evidence_claim` | ✅ 被拒且不留痕 |

测试同时断言**三层都真的发生过**（否则这条测试等于没测），并钉住
"无证据的结论必须被拒、错误码与解释都要给出来"。

证据引用用的是**已取得的资源 id**（先 query `task.get_current`，
它会 acquire 任务）—— 这正是提示词允许的"工作区里已有的证据 id / 已取得的资源 id"
那条路径，顺带把它也覆盖了。

### 59.2 未实现：`ClaimRecord` 从来没有落库，"晋升"也就无从谈起

核实过程中发现的**结构性缺口**（本轮**不修**，如实记录）：

| 事实 | 位置 |
| --- | --- |
| `CommitProposal.claims_to_create` 从来没有被填充 | `runner._build_proposal` 只填了 resources / evidence / memory_summary / acceptance_proofs / final_response |
| 于是 `claims` 与 `claim_evidence` 两张表**没有任何写入方** | `sqlite_store.apply_commit` 里的插入分支永远不会执行 |
| `validate_claim_for_assertion`（要求 `status=verified` + 证据）在生产里也不会被调用 | `domain/validate.py` |
| 因此 §31 场景 7 的「调整后复测再**晋升**」没有实现路径 | 推测只活在当轮工作区，轮末即消失 |

为什么**不**顺手补：真要补必须先解决一个**会让提交整体失败**的坑 ——

```sql
CREATE TABLE claim_evidence (
    claim_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    FOREIGN KEY(evidence_id) REFERENCES evidence(id)   -- ← 这里
);
```

而 store 里 `PRAGMA foreign_keys = ON`。分层的语义里 `evidence_refs` **允许是
"已取得的资源 id"**（本轮测试用的 `task_1` 就是），那种引用在 `evidence` 表里
**不存在** —— 直接把它们写进 `claim_evidence` 会触发外键错误，
**整笔提交失败**（不是丢一条 claim，是把这一轮交付也带崩）。

所以要做的正确实现是：

1. `workspace.pending_claims`（新字段 → 按 §42 的护栏必须同时进快照）；
2. `_propose_claim` 把 `evidence_refs` **拆开**：能对上 `evidence` 行的进
   `evidence_ids`，资源引用另存（`scope` 或单列），并在事件里标注拆法；
3. `_build_proposal` 传 `claims_to_create=tuple(workspace.pending_claims)`；
4. "晋升"另起一轮：候选 claim + 覆盖当前交付物的复测证据 → `status=verified`
   （`validate_claim_for_assertion` 会要求证据齐全）。

这是**下一步的候选工作**，不是本轮交付；写在这里是为了不让"分层做了"被误读成
"主张落库与晋升也做了"。

## 60. 阶段 3/4 补完：候选主张落库（并绕开那个会带崩提交的外键）（2026-09-28 续）

### 60.1 做了什么

§59.2 记下的结构性缺口：`CommitProposal.claims_to_create` 从来没被填充，
`claims` / `claim_evidence` 两张表**没有写入方** —— 分层只活在当轮工作区。
本轮把它接上，并且**先解决那个会带崩整笔提交的外键**。

| 环节 | 实现 |
| --- | --- |
| 新字段 | `TurnWorkspace.pending_claims: list[ClaimRecord]`；按 §42 的护栏**同时进检查点快照**（`ClaimRecord.to_dict/from_dict`） |
| 构造 | `runner._build_claim_record(...)`：**一律 `status=candidate`**（晋升要另起一轮拿复测证据）；`scope` 里带 `layer` / `subject_class` / `proposer` / `resource_refs` |
| **拆引用** | `runner._split_claim_refs(workspace, refs)`：真证据（本主轮待写证据 ∪ 库里已有证据）→ `evidence_ids`；其余 → `scope["resource_refs"]`。**资源引用绝不进 `claim_evidence`** |
| 落库 | `_build_proposal` 传 `claims_to_create=tuple(workspace.pending_claims)`（Committer 与 `sqlite_store` 早就准备好了） |
| 留痕 | `ClaimRecorded` 事件里补上 `evidence_ids` 与 `resource_refs`，让"拆法"可审计 |
| 事实层不变 | `subject_class` 属用户目标/偏好/授权/选择时仍走 `task_facts`，**不**额外写 candidate 行（那是事实，不是候选主张） |

### 60.2 为什么必须先拆引用

```sql
CREATE TABLE claim_evidence (
    claim_id TEXT, evidence_id TEXT,
    FOREIGN KEY(evidence_id) REFERENCES evidence(id)   -- store 里 PRAGMA foreign_keys = ON
);
```

分层的语义**允许** `evidence_refs` 是"已取得的**资源** id"（提示词明确这么写，
§59 的测试用的 `task_1` 就是）。那种 id 在 `evidence` 表里**不存在** ——
直接把它写进 `claim_evidence` 会触发外键错误，**丢的不是一条 claim，
而是这一轮整笔交付**。所以引用必须在进库前拆开，且拆法要留痕。

### 60.3 测试结果

| 测试集 | 结果 |
| --- | --- |
| `test_collab_v3_claim_persistence.py`（**新增 5 条**） | **5 passed** |
| `test_collab_v3_workspace_snapshot_coverage.py`（§42 护栏） | **通过**（新字段进了快照，没走豁免表） |
| 78 个协作测试（本轮 77 → 78） | **945 passed, 1 skipped** |
| 全量 `tests/`（排除 §34.4 说明的 2 个陈旧文件） | **1224 passed, 28 skipped** |
| 仍失败 / 改动锁定测试 / 删除文件 | 无 / 0 / 0 |

先写测试确认红（4 条）再改。覆盖：

* **资源引用当证据 → 提交成功**（本轮关键回归：`claim_evidence` 为空、
  `scope_json` 里能找到那个资源 id）；
* 真证据（本主轮 `validate_script` 产生的 id）→ 落进 `claim_evidence`；
* 候选主张落库之后，**`get_task_facts` 里依然没有它**（§59 的跨主轮保证不被破坏）；
* 事实层不产生 candidate 行；
* `pending_claims` 跨 `pause → resume` 存活（`ClaimRecord` 往返 + 状态枚举）。

### 60.4 仍未做：晋升（§31 场景 7 的后半）

`status` 现在**只会是 `candidate`** —— "调整后复测再晋升"仍缺实现。
原因不是懒：晋升需要一个**跨主轮的判定**（"这条候选主张被之后的复测证据支持"），
而那要求先有"按 key 找历史候选主张"的读取路径（现在只有写入）。
最小可行做法已经清楚：

1. 加一个只读访问器 `list_claims(task_id, predicate=…)`（新→旧）；
2. 复测证据到齐时（`_retest_obligation_gate` 已经知道这个时刻），
   把匹配的候选主张标成 `verified`（`ClaimRecord` 的校验会要求 `evidence_ids` 齐全）；
3. 晋升本身要留事件与 revision（设计 §23.4：**不覆盖审计历史**）。

这条留到下一轮，不在本轮宣称完成。

## 61. 阶段 3 补完：`hypothesis` 投影（§24.6）+ 主张绑资源版本（§24.5）（2026-09-28 续）

### 61.1 缺口：下一主轮读不到未验证主张

设计 §24.6 明确列出新主轮要查的投影，其中一条是
**`hypothesis` projection：尚未验证的模型判断（明确标记）**。但 `PROJECTION_NAMES`
里只有 6 个，**没有它** —— 于是 §24.8 的**测试 10**
（"新主轮查询投影时能区分 fact、user_claim 和 hypothesis"）不成立：
上一轮刚落库的候选主张，下一主轮根本读不回来。

另一半：§24.5 对视觉/数量/坐标类主张的要求是「工具/视觉证据**及资源版本**」，
`ClaimRecord.subject` 就是放资源版本的地方，但落库时**从来没设过**。

### 61.2 交付内容

| 环节 | 实现 |
| --- | --- |
| 存储读取 | `SqliteDomainStore.list_claims(task_id, *, layer="", status="", limit=20)`：新→旧，含 `predicate`/`value`/`layer`/`verified`/`subject`/`resource_refs`。⚠️ claims 表**没有 task_id 列** → 靠 `created_in_turn` JOIN `main_turns` 定作用域（不引入第二条真相） |
| 两个投影 | `hypothesis`（layer=hypothesis）与 `user_claims`（layer=user_claim），共用 `_claims_projection`；只列 **candidate**，每条带 `verified: false`，载荷里明写"**不是事实**"（设计 §24.6 的"明确标记"） |
| 资源版本绑定 | `_claim_subject(workspace, resource_refs)`：版本取自**本轮实际取到的句柄**（`workspace.acquired`）—— 那才是模型真正看过的版本；取不到就**不绑**（`get_resource` 要精确 (id, version)，拿不到版本时**不编一个**） |

第一版实现里我用 `get_resource(ResourceRef(id, 1))` 去查，被自己的测试挡下：
那个访问器要求精确版本匹配，猜 `1` 只会查不到 → 静默返回 None。
改成从 `acquired` 取版本才是正确的来源。

### 61.3 测试结果

| 测试集 | 结果 |
| --- | --- |
| `test_collab_v3_claim_projections.py`（**新增 8 条**） | **8 passed** |
| 79 个协作测试（本轮 78 → 79） | **953 passed, 1 skipped** |
| 全量 `tests/`（排除 §34.4 说明的 2 个陈旧文件） | **1232 passed, 28 skipped** |
| 仍失败 / 改动锁定测试 / 删除文件 | 无 / 0 / 0 |

先写测试确认红（8 条）再改。覆盖：`list_claims` 按任务隔离且可按 layer 过滤、
**主张绑到资源版本**（subject = {id, version:3}）、没有资源引用时 subject 为 None、
`hypothesis` / `user_claims` 两个投影可读且带"不是事实"提示、**§24.8 测试 10 端到端**
（新主轮里 fact / hypothesis / user_claim 三者分得开）、空结果如实报 `NO_CLAIMS`。

### 61.4 设计 §24.6 的投影清单对照

| 设计的投影 | 现状 |
| --- | --- |
| `task.get_current`（用户目标/约束/未决问题） | ✅ |
| resource projection（素材及版本） | ✅ `current_resources` |
| latest script projection | ✅（§58 修好） |
| latest trial projection | ✅ |
| **hypothesis projection（未验证判断，明确标记）** | ✅ **本轮补上**（并额外给了 `user_claims`，让 §24.8 测试 10 真正可判） |

### 61.5 仍未做（§24.7 的封闭命令缺三个）

§24.7 列的命令集里，`record_user_preference` / `link_evidence` / `invalidate_claim`
**都还没有**（现有 `ActionType` 只有 query/act/update_plan/propose_claim/
request_interaction/validate_script/finish）。目前的替代路径是：

* 用户偏好 → `propose_claim(subject_class=preference)`（能成为事实）；
* 补充证据 → 重新 `propose_claim`（会新增一条候选，而不是给旧的那条 link 证据）；
* 失效 → 无（`invalidated_keys` 只在工作区内部用于视口/计划失效）。

这是与"主张晋升"同一族的后续工作，记在这里以免被误读成已实现。

## 62. 完成报告（按原方案书 §30 的模板，缺一项即未完成）

> 说明：本节是**交付报告**，不是新的改动记录。数据截至第 36 轮。
> 凡"未做/未验证"的，一律写在 §62.11，不隐瞒。

### 62.1 已完成 Phase

| 阶段 | 状态 | 依据 |
| --- | --- | --- |
| ① 补完闸门剩余连接 | ✅ | §18–§24、§53（校验侧禁用区闸门） |
| ② 本地事件系统与层级 ID | ✅ | §23、§43（层级序号镜像）、§49（事件字面量↔规范名） |
| ③ 主轮空白/指纹/缓存/分层 | ✅ | §24、§39（空结果原因码）、§41（指纹成分）、§55（失败解释）、§58（跨主轮找回脚本）、§60/§61（主张落库与投影） |
| ④ 副轮 Orient 与证据约束 | ✅ | §24、§35（无复测只说已修改）、§50（交互策略字段写进提示词） |
| ⑤ 试运行与伪录制 2.0 | ✅ | §27、§34（真实生产者）、§36（TaskController 自动登记）、§38（伪录制桥）、§44/§51（分页语义）、§45（位置稳定性）、§46（点击链）、§52（帧引用）、§57（异常事件） |
| ⑥ 自主调参与最小修改 | ✅ | §28、§31（自动基线）、§32、§33（`tuning.propose`）、§37（基线生命周期）、§40（尝试计数持久化）、§54（分数间隔护栏）、§56（`verified` 标记） |
| ⑦ 用户个人词典 | ✅ | §25（词典 + 解析顺序 + 不授权 + 按需查询） |

### 62.2 新增/修改文件（按职责分组，均为本地系统改动）

| 区域 | 文件 |
| --- | --- |
| 唯一事实源 | `api_contracts/{registry,render,validator,receipt,python_policy,verify_runtime,schema}.py`、`api_catalog.py`、`script_artifact.py`、`execution_profile.py` |
| V3 运行时 | `collaborator_v3/runtime/{runner,workspace,queries,ids,checkpoint,policy,committer,trial_runs,trial_frames,trial_diagnosis,pseudo_import,validation_store}.py` |
| V3 事件/存储/适配 | `collaborator_v3/events/{schema,store,projections}.py`、`stores/{sqlite_store.py,schema.sql}`、`domain/models.py`、`adapters/{provider_model,model_callable,script_validator,v2_capabilities,gui_bridge}.py`、`bootstrap.py`、`interaction/facade.py` |
| 执行器与遥测落点 | `controller/{task_controller,ctrl}.py`、`backend/automation/user_window.py`、`backend/browser/user_browser.py`、`backend/browser/mixins/image_matching.py` |
| GUI | `gui/widgets/script_gen_panel/{mixin_runtime,mixin_collab}.py` |
| 调参/词典 | `tuning.py`、`script_diff.py`、`user_terminology.py` |
| 测试（新增约 28 个文件） | `tests/test_api_contract_{profile_policy,python_policy,validator_aliases,materializer}.py`、`tests/test_execution_profile.py`、`tests/test_undefined_name_check_parity.py`、`tests/test_collab_v3_{execution_target,events_projections,query_fingerprint,claim_layers,claim_cross_turn,claim_persistence,claim_projections,orientation,trial_telemetry,trial_hud_producers,trial_autoregister,trial_event_paging,trial_empty_reasons,trial_event_kinds,pseudo_bridge,pseudo_frame_handle,vision_location,click_match_link,event_literals,prompt_query_coverage,interaction_policy_prompt,workspace_snapshot_coverage,tuning_lifecycle,tuning_margin_guard,tuning_verified,runtime_exception_event,latest_script_projection}.py`、`tests/test_user_terminology.py`、`tests/test_script_diff_and_tuning.py` |
| 审计工具（可复用） | `tools/_scratch/{audit_v3_dead_code,audit_event_literals,audit_unused_params}.py` |
| 实施记录 | 本文件（§1–§62） |

### 62.3 唯一事实源位置

`backend/script_generator/api_contracts/`：
`registry.py` 是唯一登记处（`ApiContract`），`render.py`（文档/卡片）、`validator.py`（AST/契约校验）、
`python_policy.py`（允许的 Python 能力）、`receipt.py`（凭证结构）、`verify_runtime.py`（与真实实现的一致性测试）
**全部从它派生**。旧入口 `api_catalog.py` 只是 facade。

### 62.4 真实 runtime profile 调用链

```text
GUI 账号/执行目标（mixin_runtime._execution_target_payload / mixin_collab）
  → facade.send_message|resume(execution_target=…)
  → runner._apply_execution_target
  → workspace.runtime_profile / profile_source / target_revision / account_ref
  → runner._validation_profile（权威 > 注解 provisional > portable 安全交集）
  → validator.validate(script, profile=…) 与 script_artifact.verify_artifact(profile=, profile_source=, target_revision=, account_ref=)
  → ValidationReceipt（绑定上列事实）
  → runner._receipt_gate 逐项比对（任一项变化即失效）
  → trial_runs.start_run(profile 由试运行脚本实际执行目标决定)
```

### 62.5 旧接口兼容方式

* `api_catalog` facade 保留旧函数名（`lookup_api` / 卡片渲染 / 白名单），内部读 registry；
* `agent.ALLOWED_BROWSER_METHODS` / `LOGIN_BROWSER_METHODS`、`graph.plan_schema._ALLOWED_BROWSER_METHODS`
  改为 `_registry_derived_*()`；
* V2 交付闸 `collab_form.collab_script_issues` **包装** V3 契约校验器（`collab_contract_issues`
  → `api_contracts.validator`），不是第二份规则；`optimize.py` 读派生白名单。

### 62.6 新增 SQLite migration

**无新增**。`schema.sql` + `initialize()` 的 migration 1–5 都是既有实现：
1 建表；2–4 checkpoint 的 `facts_json` / `runtime_json` 等列；5 层级 ID 列
（`main_turns.run_id`、`domain_events.run_id/subturn_id/action_id` + 两个索引）。
本轮只**新增只读访问器**（`list_commits`、`list_claims`）与新的**字段级**持久化
（检查点 JSON 里增加 `baseline_*` / `comparison_*` / `tuning_state` / `pending_claims` 等），
**没有** DDL 变更、没有数据迁移、没有删除。

### 62.7 新增事件/投影/查询

| 类别 | 数量与内容 |
| --- | --- |
| 规范事件名 `EventType` | 23 个（本轮新增 `frame.captured` / `script.note` / `claim.recorded`） |
| 既有字面量→规范名 `LEGACY_EVENT_ALIASES` | 19 条（补 `QueryCacheReplayed` / `ScriptCandidateCreated` / `ScriptValidated` / `ScriptCommitted` / `ClaimRecorded`） |
| 试运行结构化事件类型 `EVENT_KINDS` | 5 个（去掉幽灵 `input.click_attempted`） |
| 投影 `PROJECTION_NAMES` | 8 个：`current_task` / `current_resources` / `latest_script` / `latest_validation` / `latest_trial` / `outstanding_blockers` / **`hypothesis`** / **`user_claims`** |
| 封闭查询 `QUERY_NAMES` | 23 个（原 13 + `context.index`、`context.projection`、`terminology.resolve`、`tuning.propose`、6 个 `trial.*`） |

### 62.8 验证凭证绑定项

`source_sha256`、`artifact_sha256`、`bundled_sha256`、`script_name`、`materializer_version`、
`transforms`、`runtime_profile`、`profile_source`、`target_revision`、`account_ref`、
`api_contract_revision`、`task_id`、`main_turn_id`。任一变化 → `_receipt_gate` 拒绝交付。

### 62.9 试运行遥测字段

| 事件 | 字段 |
| --- | --- |
| `vision.match_completed` | template(path_ref/asset_id/name)、frame_id、frame_size、search_region、threshold、best_score、second_best_score、score_margin、matched、best_bbox、**best_center**、match_mode、force_wide、cheap、latency_ms、error |
| `input.click_completed` | **source_event_id**、**source_event_link**、source、**raw_xy**、**click_offset**、**final_xy**、delivered、frame_id、frame_before、frame_after、**visual_effect_observed**、**expected_scene_reached**、confirm_policy、latency_ms、failed_stage、error、outcome |
| `frame.captured` | frame_id、path、frame_size、source（图像落文件，事件只放引用） |
| `runtime.exception` | phase（import/target/run/finish/stopped/start）、message |

加粗的是本轮补上的（此前恒为 `None`/空）。

### 62.10 仍存在的重复事实源（不隐瞒）

| # | 重复项 | 影响 | 处置 |
| --- | --- | --- | --- |
| 1 | `agent.py` 两处**图像方法名清单**（`:2009` 5 个、`:6947` 4 个），与 registry 内容一致但无同步保证 | 窄：AST 辅助检查（首参为 None 的报错、从调用取模板 stem） | **未改**（本轮做到一半，见 §62.11 第 8 条）；可像 §21 那样改成 registry 派生 |
| 2 | `render.detail_text(profile, …)` / `card_text(profile, …)` / `registry.portable_reason(profile, …)` 的 `profile` 参数**冗余**（不参与渲染） | 无功能影响，只是误导读者 | 未改（改动面大、收益低） |
| 3 | V2 `collaborator/` 与 `collaborator_v3/` 两套会话/表单并存 | 迁移期现实：GUI 现在走 V2 入口，V3 是目标实现 | 有意保留（设计 §22「尽量只改本地系统」） |
| 4 | 复测规则有两处实现：`ActionRunner._retest_obligation_gate`（生产）与 `tuning.requires_retest_before_claiming_fixed`（纯函数，仅测试用） | 语义已一致（§56 打通 `verified` 标记），但仍有两条路径 | 未合并 |

### 62.11 仍未解决的问题（不得隐瞒）

1. **没有真机验证**：从未在真实 GUI 上跑过一次试运行。§31 场景里"真实数据"的部分
   由测试与合成数据支撑；这是当前最大的未知。
2. **无 git 回滚**：`.gitignore:37` 的 `runtime/` 把整个 V3 运行时包排除在版本控制外
   （`git check-ignore` 已证实）。同一规则可能还影响 `events/`、`stores/`、`adapters/` 的部分文件。
   建议改成 `/runtime/` 或加 `!backend/script_generator/collaborator_v3/runtime/`。
3. **主张晋升未实现**：`status` 只会是 `candidate`；§31 场景 7 的"调整后复测再晋升"没有路径（§60.4）。
4. **§24.7 三个封闭命令缺失**：`link_evidence` / `invalidate_claim` / `record_user_preference`（§61.5）。
5. **点击记录缺 `frame_id`**：HUD 拿不到帧句柄（§46.4）；`frame_before/after` 只有"未观察到效果"时才有一侧。
6. **§19.4 的 8 类关键帧触发只落了部分**：点击前后 ✅、未生效对照帧 ✅；
   黑屏边沿只有事件不存帧；trial 起点/状态切换/异常停止/用户手动标记**没有调用点**。
7. `tuning.propose` 的 `score_margin` 要模型自己提供，Runtime 不会自动从诊断摘要取（§54）。
8. 本轮想做的 `agent.py` 图像方法名去重**没做完**（见 §62.10 第 1 条）。
9. 真机会话里的预算/查询额度参数未校准（`max_subturns`、查询额度、trial 保留策略）。

### 62.12 测试命令与结果

```powershell
# 协作测试（本仓库 79 个文件；用户最初说的"49 个"是当时的数量）
python\python311\Scripts\python.exe -m pytest tests\test_collab_*.py -q
#   → 953 passed, 1 skipped

# 专项（本轮扩到 41 个文件：契约/profile/材料化/试运行遥测/调参/词典/点击等）
python\python311\Scripts\python.exe -m pytest <41 files> -q
#   → 542 passed

# 全量
python\python311\Scripts\python.exe -m pytest tests -q --ignore=tests/test_sgk_scan_speed.py --ignore=tests/test_sgk_settle_gate.py
#   → 1232 passed, 28 skipped
```

**与本轮无关的既有失败**：`tests/test_sgk_scan_speed.py`（未跟踪的新文件）与
`tests/test_sgk_settle_gate.py` 全部失败 —— `scripts/yys/刷关卡.py` 当前没有
`EmptyFrameWait`；这三个文件 mtime 是 2026-09-21（比本轮早一周），属陈旧状态、非并发写入。

### 62.13 手工复现场景与结果

**必须写清楚：以下没有一条是在真实 GUI 上手工跑的**，全部由测试驱动（`ActionRunner`
真实主轮 + FakeExecutor/FakeLogicModel + 真实 SQLite），或由 `/simplify` 级别的检查覆盖。

| §31 场景 | 覆盖方式 | 结果 |
| --- | --- | --- |
| 1 新建脚本（契约查询→生成→素材解析→产物校验→receipt→finish） | 既有 + `test_collab_final_artifact_gate` | ✅ 测试通过 |
| 2 资源替换（baseline + 只改允许字段） | `test_script_diff_and_tuning` | ✅ |
| 3 API 写错一次校验结构化返回 | `test_collab_contract_validator` | ✅ |
| 4 主轮恢复（副轮序号继续、原确认有效） | `test_collab_v3_execution_target`、§43 层级测试 | ✅ |
| 5 新主轮靠 task index 找回交付脚本 | **§58 新修** + `test_collab_v3_latest_script_projection` | ✅（此前是死的） |
| 6 trial 为空只报缺证据 + 相同查询回放 | `test_collab_v3_query_fingerprint`、§39 | ✅ |
| 7 near-miss → hypothesis → 调整后复测再晋升 | 前半 ✅（§38/§45/§54/§56）；**"晋升"未实现** | 🟡 半 |
| 8 点击链（trial→click→match→frame→模板版本） | §46（click→match ✅）、§52（frame ✅）；`frame_id` 仍缺 | 🟡 半 |
| 9 用户术语（抖动=click_offset，他人不继承，上下文覆盖） | `test_user_terminology` | ✅ |
| 10 无 evidence 的结论被拒且不进下一主轮 task facts | **§59 新修** + `test_collab_v3_claim_cross_turn` | ✅ |
| 11 脚本首行异常仍能查 run/script hash/exception | **§57 新修** + `test_collab_v3_runtime_exception_event` | ✅ |
| 12 兼容旧会话（可展示可查询、不伪造新凭证） | §23、§49（legacy 字面量仍可读）、§28 | ✅ |

### 62.14 回滚方式

**当前没有 git 级回滚**（见 §62.11 第 2 条）。可行的替代：

1. 用 §17 的**冻结清单**（32 个文件）与 `tools/_scratch/phase1_freeze_manifest.txt`
   做人工比对，把要撤的文件恢复到冻结版本；
2. 本轮以来所有改动都是**增量**：新增函数/字段/测试/记录，**无 DDL 变更、无数据迁移、无删除文件**，
   因此回滚不需要动数据库；
3. 数据库侧唯一新增的是检查点 JSON 里的新键 —— 老代码读到时忽略（`runtime_snapshot` /
   `restore_runtime_snapshot` 对缺键都有默认值），**前向/后向都兼容**；
4. 建议**先修 `.gitignore`** 再继续后续轮次，否则回滚始终依赖人工清单。


















