# 协作脚本：可执行 API 契约与受限生成方案

> 状态：待实施方案（供后续模型直接执行）  
> 日期：2026-09-28  
> 目标：让逻辑模型稳定生成“能够通过闸门、能够进入试运行、不会因虚构 API 立即异常”的脚本。

## 1. 先读：这次要解决的究竟是什么

最近两个主轮共发生 26 次 `validate_script`：17 次通过、9 次失败。9 次失败中没有
Python `SyntaxError`；主要问题是不存在的方法、错误参数、同步方法被 `await`、错误素材引用。
此外至少有一份虽被判定通过、实际仍可能因未定义名称或素材表达错误而失败的脚本。

因此本方案不把重点放在“教模型更多 Python 语法”，而是建立一套**可执行 API 契约**：

1. 模型写代码前可以准确查询当前运行环境真正支持的函数；
2. 模型只能使用已批准的 Python 能力和契约中的项目 API；
3. 文档、RAG 查询、模型提示、静态校验共用同一份事实源；
4. 契约与真实实现漂移时，测试必须立即失败；
5. 出闸前校验的是素材替换后的最终脚本，而不只是模型原稿。

这不是普通“API 文档 RAG”。普通 RAG 只能降低犯错概率；本方案要求同一份结构化契约同时
驱动检索和本地强制校验，让文档外的能力无法通过。

## 2. 当前事实与已发现的漂移

现有相关入口：

- `backend/script_generator/api_catalog.py`：手写 API 卡片；生成器会把全文放进部分 prompt。
- `backend/script_generator/agent.py::validate_script_local`：现有本地脚本校验主入口。
- `backend/script_generator/collaborator/collab_form.py::collab_script_issues`：协作脚本校验包装。
- `backend/script_generator/collaborator_v3/adapters/script_validator.py::V2ScriptValidator`：V3 复用 V2 校验。
- `backend/script_generator/collaborator_v3/adapters/provider_model.py::_SYSTEM_PROTOCOL`：V3 模型协议。
- 真实桌面执行类：`backend/automation/user_window.py::UserWindow`。
- 浏览器执行类：`backend/browser/user_browser.py::UserBrowser`。

不要把 `UserWindow` 与 `UserBrowser` 的签名混成一张表。已经发现的真实漂移示例：

- `api_catalog.py` 写着 `wait_image(..., threshold=0.9)`；
- `UserWindow.wait_image(self, img_path, timeout=0)` 并不接受 `threshold`；
- `UserBrowser.wait_image` 才接受 `threshold`。

这也是为什么“把完整文档塞给模型”仍可能教错模型。新实现必须先确定脚本的 runtime profile，
再返回对应契约。

## 3. 不在本阶段解决的事项

本阶段只解决 API 认知与代码可执行性，不顺手重构整个协作架构。以下问题另立任务：

- “只替换资源”却大幅重写：需要基于 AST/diff 的最小修改约束；
- 没有试运行证据仍猜测：需要证据声明与诊断链约束；
- 重复查询空结果：需要查询缓存与 mutation epoch；
- 已确认逻辑在新主轮丢失：需要任务事实与完成闸门正确继承；
- 图片模板、阈值和业务控制流虽然可运行但效果不好：需要试运行与视觉证据。

不要声称本方案完成后这些问题也自然消失。

## 4. 核心设计原则

### 4.1 单一事实源

每个 API 只允许有一份结构化契约。以下内容都从它派生：

- `api.search` / `api.get` 查询结果；
- 给 LLM 的短能力索引；
- API 详细文档；
- AST 调用白名单；
- 同步/异步检查；
- 参数名、必填参数和常量类型检查；
- 返回值字段检查；
- 契约—真实实现一致性测试。

禁止再维护一套 prompt 文本、一套校验白名单和另一套文档签名。

### 4.2 runtime profile 明确分离

至少定义：

- `desktop_user_window`：当前脚本生成/试运行默认目标，绑定 `UserWindow`；
- `browser_user_browser`：确实运行在浏览器脚本环境时使用，绑定 `UserBrowser`。

任务创建或运行上下文必须明确 `runtime_profile`。没有明确证据时，协作脚本默认
`desktop_user_window`，不能把两个 profile 的方法取并集。

### 4.3 模型能力收窄但不错误收窄

允许三类能力：

1. Python 语言基础：控制流、函数、类（必要时）、容器、推导式、异常处理等；
2. 明确批准的标准库模块/符号；
3. 当前 runtime profile 契约中登记的项目 API。

“Python 自带函数全部允许”过宽。`open`、`eval`、`exec`、`compile`、`__import__`、
`subprocess`、`socket`、任意文件写入和动态加载默认禁止。是否开放必须进入单独的能力契约，
不能因为它属于标准库就自动通过。

### 4.4 查询按需，校验强制

模型不需要每轮吞下全部文档。每轮只提供紧凑索引，写代码前按需查询具体 API。
但“模型是否查过”不应成为唯一安全条件；最终代码仍必须由本地校验器检查完整契约。

### 4.5 最终产物校验

模型源码通过不等于最终脚本通过。正确顺序：

`模型源码 → 解析素材引用 → 打包/替换为最终路径 → AST/契约校验 → import 冒烟 → 出闸`

## 5. 建议的数据结构

新增独立包，避免继续把结构化事实塞进 `api_catalog.py` 的字符串：

```text
backend/script_generator/api_contracts/
  __init__.py
  schema.py              # dataclass / TypedDict 与序列化
  registry.py            # profile → contract registry
  desktop_user_window.py # UserWindow 契约
  browser_user_browser.py# UserBrowser 契约
  python_policy.py       # 标准库/内建白名单与禁用项
  render.py              # 面向 LLM/人的摘要和详情
  verify_runtime.py      # 与 inspect.signature 的一致性校验
```

每个方法的契约至少包含：

```python
ApiContract(
    id="desktop_user_window.click_image",
    owner="browser",
    name="click_image",
    runtime_profile="desktop_user_window",
    call_kind="async_method",          # sync_method / async_method
    parameters=[
        Parameter(name="img_path", kind="positional_or_keyword",
                  type="str | Path", required=True),
        Parameter(name="threshold", type="float", required=False, default=...),
    ],
    return_contract=ReturnContract(
        type="bool",
        truthiness="True 表示……；False 表示……",
        fields=[],
        lifetime="调用结束后仍有效/下一次刷新后失效",
    ),
    semantics=[...],
    side_effects=[...],
    preconditions=[...],
    errors=[...],
    examples_valid=[...],
    examples_invalid=[...],
    tags=["click", "vision"],
)
```

必须明确记录而不能只写自然语言的字段：

- `call_kind`：是否必须 `await`；
- 完整参数列表、参数 kind、默认值；
- 是否接受 `**kwargs`；
- 返回类型、真假语义、合法字段；
- 坐标系与尺度；
- 是否刷新/使帧失效；
- 结果有效期；
- 外部副作用；
- runtime profile。

注意：`inspect.signature` 只能提供形状，不能可靠提供真假语义、坐标系、副作用。因此采用
“真实签名自动核验 + 人工语义覆盖”，而不是试图完全自动生成契约。

## 6. 对 LLM 暴露的查询 API

在 V3 查询器中新增两个封闭查询，名称可按现有 query registry 风格调整，但语义不可缩水。

### 6.1 `api.search`

输入：

```json
{
  "runtime_profile": "desktop_user_window",
  "query": "识图 点击 等待",
  "limit": 20
}
```

输出只给索引：

```json
{
  "runtime_profile": "desktop_user_window",
  "contracts_revision": "sha256:...",
  "items": [
    {
      "id": "desktop_user_window.match_image",
      "name": "browser.match_image",
      "summary": "匹配单个模板，返回 MatchResult 或假值",
      "call_kind": "async_method",
      "signature": "(img_path, threshold=..., ...)"
    }
  ]
}
```

### 6.2 `api.get`

输入支持批量，避免一个函数浪费一个副轮：

```json
{
  "runtime_profile": "desktop_user_window",
  "ids": [
    "desktop_user_window.match_image",
    "desktop_user_window.click_image",
    "desktop_user_window.update_frame"
  ]
}
```

输出完整结构化契约，带 `contracts_revision`。模型应在一次查询里取得本次生成需要的全部函数。

### 6.3 查询缓存与主轮规则

- 结果进入当前主轮 `runtime_workspace.query_results`；
- 同 profile、同 contract revision、同参数的重复查询直接回放缓存；
- 契约 revision 没变时，副轮之间不重复查询；
- 新主轮遵守既定架构，模型不自动继承查询结果，重新按需查询；
- 如果脚本参考资源自身携带已解析的契约 refs，可作为查询建议，但不能绕过当前版本核验。

## 7. 给模型的生成协议如何修改

不要再把完整手写 `api_contracts_block()` 塞入所有 prompt。每轮只给：

```text
runtime_profile=desktop_user_window
允许能力：Python 基础 + python_policy 白名单 + 当前 profile API 契约。
写脚本前先批量 api.get 本次会调用的方法；不得调用契约外方法。
不确定参数、返回值、同步/异步或坐标语义时必须查询，不得猜测。
validate_script 失败后只按结构化 issue 修正，不得用另一个虚构方法替代。
```

`validate_script` 结果中的每个 issue 应结构化返回：

```json
{
  "code": "API_UNKNOWN_KEYWORD",
  "line": 35,
  "symbol": "browser.click_image",
  "message": "不支持参数 force_wide",
  "contract_id": "desktop_user_window.click_image",
  "allowed_parameters": ["img_path", "threshold", "confirm_timeout", "stable_ms"],
  "repair_hint": "force_wide 属于 match_image；不要传给 click_image"
}
```

保留面向用户/旧调用方的字符串 issues，但内部和 V3 优先消费结构化 issues。

## 8. 本地强制校验

在现有 `validate_script_local` 基础上逐步抽出新的 contract validator，不要一次删除旧校验。

### 8.1 AST 能力白名单

至少检查：

- `browser.<method>` 必须属于当前 profile；
- async 方法必须 `await`，sync 方法不得 `await`；
- 关键字参数必须存在；
- 必填参数必须提供；
- 明确常量的类型可静态判断时必须匹配；
- 返回对象只允许访问契约登记的字段；
- 禁止未知 import、动态 import、`eval`、`exec`；
- 禁止未批准的文件、进程、网络能力；
- 入口必须符合当前执行器要求；
- 收集未定义名字，不能只检查一部分表达式节点。

不要试图实现完整 Python 类型检查器。变量传播无法可靠判断时留给 import smoke 和运行期；
但所有能够确定的违约必须阻断出闸。

### 8.2 素材引用

规定唯一规范形式，建议模型源码中只接受字符串 token：

```python
challenge_img = "[[asset_fa260dcd]]"
```

裸 `[[asset_fa260dcd]]` 必须报专门错误，不能因为它恰巧是合法 AST 列表表达式而通过。
校验内容：

- token 语法正确；
- asset id 在本主轮资源范围存在；
- 素材类型适合作为图片；
- 替换后路径存在；
- 最终脚本不再包含未解析 token。

### 8.3 两阶段校验

`validate_script` 返回通过至少需要：

1. **source gate**：模型源码 AST、契约、素材 token 检查通过；
2. **artifact gate**：生成与试运行实际相同的最终脚本，再次解析和校验；
3. **import smoke**：在隔离命名空间加载最终模块，确认注解、import、顶层代码不抛异常；
4. 确认存在可调用入口。

import smoke 不能真的启动浏览器、点击或执行 `do_work`。若现有模块顶层带副作用，应先明确禁止，
而不是在校验阶段执行副作用。

### 8.4 契约漂移测试

对每个 profile：

- 用 `inspect.signature` 比对真实类方法；
- 方法不存在、同步/异步不符、参数增删、默认值变化时测试失败；
- 每个允许方法必须有语义卡；
- 每张语义卡必须对应真实方法；
- `ALLOWED_BROWSER_METHODS`、graph 白名单和旧 catalog 在迁移完成前必须与 registry 对齐；
- 迁移完成后删除重复白名单，不能永久保留多份事实源。

## 9. 推荐实施顺序

### 阶段 A：建事实源，不改变线上行为

1. 建 `api_contracts` schema、registry 和两个 runtime profile；
2. 先录入当前协作脚本真正用到的全部 `UserWindow` 方法；
3. 加签名漂移测试；
4. 让旧 `api_catalog.lookup_api` 从 registry 渲染，保持旧接口兼容；
5. 此阶段不要改出闸策略。

### 阶段 B：接入 V3 查询

1. 在 query catalog/registry 注册 `api.search`、`api.get`；
2. 返回结构化契约与 revision；
3. provider protocol 改为短索引 + 按需批量查询；
4. 保留同查询缓存规则；
5. 增加查询器与上下文测试。

### 阶段 C：校验器消费同一契约

1. AST 校验从 registry 取得允许方法、参数和 async 属性；
2. 增加 Python policy；
3. 增加素材 token 专项检查；
4. 返回结构化 issues；
5. 保持旧字符串错误兼容。

### 阶段 D：最终产物与冒烟闸门

1. 提取“构造最终试运行脚本”的纯函数；保存页与校验页共用；
2. 对最终产物运行 artifact gate；
3. 隔离做 import smoke；
4. commit/finish 只能引用同一 hash 的完整通过记录；
5. 任意后处理改变代码 hash 后，旧校验立即失效。

### 阶段 E：清理重复事实源

只有全部调用点和测试迁移后，才删除散落的白名单与手写签名。不要提前删除
`api_catalog.py`，先把它变成兼容 facade。

## 10. 必须覆盖的测试

建议新增：

```text
tests/test_api_contract_registry.py
tests/test_api_contract_runtime_drift.py
tests/test_collab_v3_api_query.py
tests/test_collab_contract_validator.py
tests/test_collab_final_artifact_gate.py
```

最低用例：

1. `UserWindow.wait_image(..., threshold=...)` 在 desktop profile 被拒；
2. 同一调用在 browser profile 若真实支持则通过；
3. `await browser.note_state(...)` 被拒，非 await 通过；
4. `browser.move(...)` 被拒并返回最接近的合法能力，但不能自动替换；
5. `click_image(force_wide=True)` 被拒；
6. `match_image(force_wide=True)` 按真实签名通过；
7. 裸 `[[asset_id]]` 被拒；字符串 token 只有资源存在时通过；
8. 未定义的注解名在 import smoke 被捕获；
9. 未批准 import、`eval`、`exec`、动态 import 被拒；
10. 模型源码通过但最终替换产物语法损坏时，artifact gate 拒绝；
11. 校验后代码改变一个字符，finish 不得复用旧 validation hash；
12. registry 与真实方法签名漂移时测试失败；
13. `api.get` 支持一次查询多个方法，重复查询回放缓存；
14. 文档渲染和校验器对同一方法给出相同签名；
15. 原有 `tests/test_collab_*.py` 全部通过。

按工作区约定运行：

```powershell
python\python311\Scripts\python.exe -m pytest tests\test_collab_*.py -q
```

另单跑新契约测试与 `tests/test_validator_false_positives.py`。

## 11. 验收标准

全部满足才算完成：

- API 文档、RAG 查询、AST 校验来自同一个 registry；
- desktop/browser profile 不再混用签名；
- 真实 API 签名变化会使 CI/本地测试失败；
- 未登记的 `browser.*` 无法通过；
- 同步/异步和参数错误无法通过；
- 素材 token 在最终产物中全部解析，裸 token 无法通过；
- 最终脚本完成 import smoke；
- finish 只能提交与通过记录 hash 相同的脚本；
- 旧生成器/优化器仍可通过兼容 facade 读取契约；
- 全量协作测试无回归。

目标不是承诺脚本业务行为 100% 正确，而是保证：**凡是系统声称“静态校验通过并可试运行”
的脚本，不应再因已知 API、参数、await、素材占位符、未定义顶层名称等问题立即异常。**

## 12. 给实施模型的工作纪律

1. 先读本文件和根目录 `AGENTS.md`；遵守 3.11 venv 与搜索范围限制。
2. `agent.py`、`llm_turn.py` 等超过 500 行的文件先取符号大纲，只读相关区间。
3. 先写契约漂移测试，再录入契约；以真实 `UserWindow`/`UserBrowser` 签名为准，不以旧文档为准。
4. 不要用一份新的 JSON/YAML 再加一份 Python 白名单；registry 必须是唯一事实源。
5. 不要只改 prompt。没有本地强制校验不能算完成。
6. 不要为了通过测试删掉现有安全检查或放宽完成闸门。
7. 不要在此任务顺便修最小 diff、试运行诊断、确认继承等其他架构问题。
8. 保持兼容迁移：先 facade，后清理；每阶段都应能独立测试。
9. 若真实执行器因账号/页面而选择不同 runtime profile，要把选择依据结构化保存，禁止猜 profile。
10. 最终报告列出：新增事实源、迁移的旧调用点、仍存在的重复事实源、测试结果和未解决范围。

## 13. 实施前仍需核实的三个事实

实施模型必须通过代码确认，不能按本方案猜答案：

1. 当前协作试运行到底实例化 `UserWindow`、`UserBrowser`，还是根据运行模式二选一；
2. 素材 token 从模型源码到最终脚本的实际替换函数与发生时点；
3. import smoke 应使用哪个模块上下文，才能与真实脚本加载器一致而不触发外部副作用。

核实后若与本文假设不同，更新 runtime profile 或流水线，但不要放弃“单一事实源、最终产物校验、
profile 分离”三个核心原则。

## 14. 主轮—副轮层级 ID 与验证凭证

### 14.1 为什么必须补

实况中同一个主轮暂停恢复后，副轮序号曾从 `42` 重新变成 `1`。因此当前的
`turn_id + subturn number` 不能稳定唯一定位一次模型决策，也不能可靠串起生成、校验、提交、
试运行与日志。

新增层级：

```text
Task
└─ MainTurn
   ├─ Run / Continuation（一次开始或恢复执行）
   │  └─ Subturn
   │     └─ Action / Attempt
   └─ ValidationReceipt
```

### 14.2 ID 规则

推荐使用带类型前缀的 ULID，或“UTC 时间戳 + 随机尾缀”。示例只表达可读性，不要求照搬
字符串格式：

```text
main_turn_id = mt_20260928T031522Z_7K4M2P
run_id       = run_<main_turn_id>_0003
subturn_id   = st_<main_turn_id>_000014
action_id    = act_<subturn_id>_01
attempt_id   = att_<action_id>_02
```

规则：

- 所有可信 ID 只由后端 Runtime 生成；前端和 LLM 只能引用，不能自行签发；
- 主轮暂停恢复时 `main_turn_id` 不变，创建新 `run_id`；
- `subturn_seq` 在整个主轮内单调递增，恢复后不能归零；
- `subturn_id`、`action_id`、`attempt_id` 在数据库设置唯一约束；
- 时间另存标准 `created_at` 字段，业务逻辑不得解析 ID 中的时间；
- 重试是否复用 `action_id` 取决于幂等语义，但每次实际尝试必须有唯一 `attempt_id`；
- 兼容旧记录时可派生 legacy correlation key，但不得伪造成新式可信 ID。

### 14.3 全链路携带

以下记录统一携带相关层级 ID：

- 模型请求与回复；
- query 及其缓存项；
- capability 调用与返回；
- 权限授予和用户确认；
- 计划变更与候选代码；
- 脚本校验、commit、finish；
- 试运行、日志、异常、截图和伪录制；
- 前端进度事件。

这样试运行反馈可以反向定位：

`trial_run → validation receipt → artifact hash → validation subturn → generation subturn → main turn`。

### 14.4 闸门认“凭证绑定”，不只认 ID

每次完整校验通过后，由 Runtime 生成不可由模型伪造的 `ValidationReceipt`：

```json
{
  "validation_id": "val_...",
  "task_id": "task_...",
  "main_turn_id": "mt_...",
  "run_id": "run_...",
  "subturn_id": "st_...",
  "action_id": "act_...",
  "runtime_profile": "desktop_user_window",
  "source_sha256": "...",
  "artifact_sha256": "...",
  "api_contract_revision": "sha256:...",
  "asset_versions": {"asset_x": 2},
  "validator_version": "2",
  "checks": {
    "source_gate": true,
    "artifact_gate": true,
    "import_smoke": true,
    "fake_runtime_smoke": true
  }
}
```

`finish` 必须提交 `validation_id`。Runtime 校验：凭证真实存在、属于正确任务、产物 hash 未变、
素材版本未变、runtime profile 相同、契约 revision 兼容、要求的 checks 全部通过。

禁止实现成“某副轮曾经校验通过，所以该主轮任意代码都可出闸”。正确语义是：

> 该副轮对这一份确定的源码、最终产物、素材版本、运行环境和契约版本完成了检验。

任一绑定项变化，旧凭证失效并重新校验。

### 14.5 ID 不取代作用域和状态

- 逻辑确认可以跨主轮继承，条件是其 `logic_sha256` 未变化；不能因为确认来自旧主轮就失效；
- 查询能否重做取决于查询参数与 `mutation_epoch`，不能因为换了副轮 ID 就绕开缓存；
- 权限有效期仍按任务级、阶段级、批次级或单次级作用域判断；
- ID 用于身份、关联、幂等和审计，不负责替代证据、版本与授权规则。

逻辑确认应有独立凭证，例如：

```json
{
  "confirmation_id": "confirm_...",
  "task_id": "task_...",
  "logic_sha256": "...",
  "scope": "task",
  "valid_until_logic_changes": true
}
```

### 14.6 新增测试

1. 同一主轮暂停恢复后，`run_id` 变化且 `subturn_seq` 继续递增；
2. 数据库拒绝重复 `subturn_id` / `action_id` / `attempt_id`；
3. 前端或模型伪造的 ID 不能成为可信记录；
4. 校验通过后修改一个字符，旧 validation receipt 失效；
5. 素材版本、profile、contract revision 改变时按规则失效；
6. 逻辑 hash 未变时确认凭证可跨主轮使用；变化时必须重新确认；
7. 换副轮 ID 但 mutation epoch 未变时，相同空查询仍回放缓存；
8. 试运行日志能通过 ID 链准确找到交付和校验来源。

## 15. 本方案落地后的剩余问题地图

可执行 API 契约、最终产物闸门和层级 ID 完成后，代码身份和 API 合法性会明显稳定，但以下
问题仍需独立设计，不能假定已被本方案解决：

1. **最小修改约束**：用户要求资源替换或局部修复时，模型仍可能大幅重写控制流；需要
   AST/语义 diff、允许变更集合及基线脚本绑定。
2. **试运行综合诊断链**：`trial.get_latest_run` 为空时还要区分未运行、未登记、版本过旧、
   条件不匹配，并统一查询日志、实际运行脚本、事件、截图与伪录制。
3. **证据约束与反猜测**：模型回复需要结构化区分 observed facts、hypotheses、missing evidence；
   无证据根因不能作为事实出闸。
4. **查询空转治理**：相同查询、相同参数、相同 mutation epoch 的空结果应直接回放；模型不能
   通过改写等价参数绕开。
5. **确认与任务事实继承**：任务级逻辑确认、用户偏好、授权和选择要按 hash/scope 继承；
   查询得到的确认必须能被完成闸门读取。
6. **计划收束语义**：修复已完成时计划必须能从 doing 收束到 done；`done_when` 与计划互斥、
   自动续轮和预算暂停仍需统一规则。
7. **需求边界执行**：模型是否真正理解“只换资源”“结算不点击”等约束，需要结构化变更政策
   和行为验收，API 合法不等于满足用户需求。
8. **业务级动态验证**：Fake Runtime 只能证明不立即异常；模板质量、阈值、场景转换、点击是否
   生效仍需真实试运行证据。
9. **视觉证据可靠性**：素材选择、共同视口、裁剪坐标、模型自报尺寸与标注范围仍有独立的
   视觉 grounding 风险。
10. **成本与上下文治理**：契约查询、脚本参考、日志和视觉结果需要按需加载、压缩和版本化，
    避免为了“理解架构”把全部内容塞入每个副轮。

这些问题应按“证据诊断 → 状态继承/收束 → 最小修改 → 动态验证 → 视觉与成本”分批处理，
不要重新回到遇到一条报错就往 system prompt 追加一句的修补方式。

## 16. 副轮定向思考（Orient）与需求讨论闸门

每个副轮保留一次简短、结构化的内部定向思考，但不额外向用户展示推理独白，也不要求再发起
一次模型调用。模型同一次响应先给 `orientation`，再给一个 action：

```json
{
  "orientation": {
    "interpreted_goal": "只替换参考脚本的素材，并取消框外点击",
    "requested_changes": ["素材引用", "框外点击行为"],
    "must_preserve": ["原控制流", "重试与转场确认"],
    "known_facts": [],
    "unknowns": [],
    "material_ambiguities": [],
    "can_act_now": true,
    "next_action_reason": "先读取基线脚本"
  },
  "action": {"type": "query", "payload": {}}
}
```

Runtime 只校验必要字段和一致性，不保存或展示自由形式思维链。作用是强迫模型在行动前明确：

- 它理解的目标是什么；
- 本轮允许改什么、必须保留什么；
- 当前事实与未知项；
- 是否存在会显著改变结果的歧义；
- 下一动作为什么必要。

若 `material_ambiguities` 非空，且该歧义无法通过资源/API/参考脚本查询消除，模型必须
`request_interaction`，不能先生成再让用户纠正。低风险、局部、可撤销且不改变用户目标的细节
仍采用合理默认值，避免把小思考变成每步都问用户。

小思考只帮助模型识别需求边界，不能替代最小 diff 闸门、API 契约和证据校验。

## 17. 空查询与同资源重复查询治理

### 17.1 空试运行的正确语义

`trial.get_latest_run` 返回空时，模型不能推断“为什么点击没执行”。它仍可读取当前交付脚本、
做静态检查、核对资源和版本，但所有运行时根因必须标记为未知，并向用户说明需要重新试运行。

空结果不得只有 `run: null`，至少返回：

```json
{
  "status": "empty",
  "reason": "NO_RUN_FOR_CURRENT_TASK",
  "task_id": "task_...",
  "trial_epoch": 7,
  "other_runs_exist": false,
  "recommended_next_action": "请用户重新试运行以采集运行证据"
}
```

建议 reason 枚举：`NO_RUN_FOR_CURRENT_TASK`、`TRIAL_REGISTRY_NOT_ACTIVE`、
`RUN_VERSION_MISMATCH`、`RUN_RECORD_EXPIRED`、`QUERY_SCOPE_MISMATCH`、`UNKNOWN`。

### 17.2 查询指纹与失效

不能只按“同一资源 ID”禁查，否则资源更新后模型拿不到新结果。查询缓存 key：

```text
query_name
+ normalized_arguments
+ task_revision
+ referenced_resource_versions
+ relevant_mutation_epoch
```

相同指纹第二次查询直接回放缓存，包括空结果；不得消耗真实查询额度。只有相关事件导致 epoch
变化才允许重查，例如 `trial.started/finished` 改变 `trial_epoch`，资源换版改变
`resource_epoch`。改变 JSON 字段顺序、默认参数写法或等价别名不能绕过指纹。

## 18. 本地领域事件与投影系统

本系统只在本地 Runtime 内实现，不引入 Kafka、网络服务或全仓 Event Sourcing。优先扩展现有
SQLite `domain_events`、trial registry、runtime log 和 workspace trace，建立薄的统一层。

### 18.1 建议模块边界

```text
backend/script_generator/collaborator_v3/events/
  schema.py          # EventEnvelope、枚举、版本
  store.py           # SQLite append/read；只追加
  bus.py             # 本进程发布与订阅
  projectors.py      # 当前状态投影
  epochs.py          # task/resource/trial/viewport/contract epoch
  queries.py         # 给 V3 query gateway 的摘要与下钻
  migration.py       # 兼容现有 domain_events / trial records
```

若为避免目录扩张，也可放入现有 `runtime/`，但只能有一个事件 schema 和一个写入入口，禁止各模块
继续自造格式。

### 18.2 统一事件信封

```json
{
  "schema_version": 1,
  "event_id": "evt_...",
  "event_type": "vision.match_completed",
  "occurred_at": "2026-09-28T03:15:22.123+08:00",
  "task_id": "task_...",
  "main_turn_id": "mt_...",
  "run_id": "run_...",
  "subturn_id": "st_...",
  "action_id": "act_...",
  "trial_run_id": "trial_...",
  "subject": {"type": "asset", "id": "asset_x", "version": 2},
  "payload": {},
  "evidence_refs": []
}
```

事件类型先覆盖关键领域动作，不记录无意义的 GUI hover/滚动：

- `user.message_received`、`user.supplement_received`；
- `task.created/updated`、`plan.updated`、`logic.confirmed/invalidated`；
- `resource.added/versioned/removed/focused`；
- `query.completed/cache_replayed`；
- `script.candidate_created/validated/committed`；
- `trial.started/finished`、`runtime.exception`；
- `vision.match_started/completed`；
- `input.click_attempted/completed`；
- `frame.captured/keyframe_saved`；
- `turn.paused/resumed/finished`。

### 18.3 投影而非让模型重放历史

事件写入后，本地投影器维护：

- `CurrentTaskProjection`：目标、约束、已确认逻辑 hash；
- `CurrentResourceProjection`：有效素材 id/version/hash/role/来源；
- `LatestScriptProjection`：候选、已验证、已提交版本；
- `LatestTrialProjection`：运行状态、脚本 hash、异常与摘要；
- `OutstandingBlockersProjection`：仍缺的资源/证据/权限/确认；
- `EpochProjection`：不同查询域的失效版本。

主轮开始时只给模型一张很短的 task index，不自动发送全部投影。模型按需查询投影或下钻事件。
这样保持“主轮空白”，又能精确恢复上一主轮已经落盘的事实。

### 18.4 本地一致性与容量

- SQLite 事务中先写领域状态和 outbox/event，避免状态已改但事件丢失；
- 事件只存结构化元数据，大图、完整脚本、长日志存文件/资源库并保存引用；
- 对高频匹配事件按 trial 分区/轮转，保留摘要，原始事件按配置保留最近 N 次；
- 日志脱敏沿用现有规则；API key/cookie/password 永不进事件 payload；
- 旧 `domain_events` 通过 schema version 兼容，不要求一次迁移全部历史；
- projector 可从事件重建，并有幂等测试；
- 事件写入失败不能静默冒充成功，关键证据缺失要在 trial quality flags 中体现。

## 19. 试运行与伪录制 2.0：面向 LLM 的诊断遥测

### 19.1 当前不足

当前 V3 trial 查询能读取运行概要、文本日志尾部、脚本快照与停帧引用；底层
`PseudoRecorder` 已记录部分 match/click/span/keyframe，但丰富数据没有完整接入 V3 查询。
`Controller.emit_match_event` 主要发 GUI 信号，trial registry 仍以文本行为主。因此当前日志不足以
让 LLM 稳定判断阈值、搜索区域、模板尺度或点击事务发生在哪一步。

### 19.2 每次匹配至少记录

```json
{
  "event_type": "vision.match_completed",
  "template": {
    "asset_id": "asset_x",
    "version": 2,
    "sha256": "...",
    "name": "挑战",
    "path_ref": "resource://asset_x@2",
    "size": [84, 30]
  },
  "frame_id": "frame_...",
  "frame_size": [1920, 1080],
  "search_region": [0, 0, 1920, 1080],
  "threshold": 0.80,
  "best_score": 0.76,
  "second_best_score": 0.51,
  "score_margin": 0.25,
  "matched": false,
  "best_bbox": [820, 430, 904, 460],
  "match_mode": "image",
  "force_wide": true,
  "cheap": false,
  "latency_ms": 38,
  "error": null
}
```

如果底层算法暂时拿不到 second-best 或 bbox，允许字段为 null，但 schema 保留；不得伪造数值。

### 19.3 每次点击至少记录

- 输入目标：模板命中、固定坐标或 multi-match 选择；
- 原始坐标、抖动/偏移量、缩放后的实际客户区坐标；
- frame id 与生成坐标的 match event id；
- down/up 是否成功投递；
- 点击前后关键帧；
- 使用的确认策略（gone/appear/none）；
- 确认结果与耗时；
- 失败阶段和异常。

严禁只记录“点击成功”而不说明成功指 API 调用完成、消息投递完成，还是业务页面发生变化。

### 19.4 关键帧策略

保存而不是无限录像：

- trial 起点；
- match 连续失败但 best score 接近阈值时；
- match 成功；
- 点击前、点击后；
- 页面黑屏边沿；
- 状态切换；
- 异常与停止；
- 用户手动标记。

关键帧绑定原始尺寸、存储尺寸、缩放比例和 frame hash。相同帧去重；图像作为文件保存，事件只放
引用。现有 `keyframe_min_interval_s` 对 click/error/step 等关键事件继续允许绕过间隔。

### 19.5 面向模型的查询

新增本地查询：

- `trial.get_diagnosis_summary(run_id)`：状态、异常、最后进度、失败最多的模板、临界阈值、点击链；
- `trial.get_vision_events(run_id, template_id?, outcome?, cursor?, limit?)`；
- `trial.get_action_events(run_id, action?, cursor?, limit?)`；
- `trial.get_event(run_id, event_id)`；
- `trial.get_frame(run_id, frame_id)`：返回资源句柄，不把 base64 塞入文本；
- `trial.get_pseudo_record_summary(run_id)`。

摘要应本地计算而不是让 LLM吞完整日志，例如：

```json
{
  "template_id": "asset_x",
  "attempts": 12,
  "matches": 0,
  "threshold": 0.80,
  "best_score_range": [0.74, 0.78],
  "stable_best_location": true,
  "possible_causes": ["THRESHOLD_NEAR_MISS"],
  "evidence_event_ids": ["evt_1", "evt_2"]
}
```

`possible_causes` 只是本地诊断提示，不是事实结论。LLM必须结合关键帧、分数间隔、位置稳定性、
搜索区域和复测结果再判断。降低阈值属于可撤销修改，但仍要重新试运行；没有复测只能说“已调整，
等待验证”。

### 19.6 试运行登记必须自动发生

不要要求模型生成的脚本主动调用 `enable_pseudo_record` 才有基本证据。任务控制器在试运行开始时
自动登记 run 并启用基础事件；脚本内显式调用只用于提高采样或附加业务标记。即使脚本在 import
或第一行就异常，也必须存在 trial run、脚本 hash 和异常事件。

## 20. “抖动”的系统语义

### 20.1 当前结论

系统目前没有一份统一、对 LLM公开的“抖动”领域定义：

- `UserWindow/UserBrowser.human_offset(pianyi)` 定义了点击偏移：`None` 为 ±6 均匀随机，整数为
  ±radius 均匀随机，二元组为固定偏移；
- `scripts/yys/刷关卡.py` 等脚本自己定义 Gaussian `human_jitter()`；
- 滑动代码的 `jitter` 又表示轨迹随机扰动；
- 页面滚动注释中的“抖动”表示视觉晃动；
- 最近会话里模型甚至把“抖动”理解成不存在的 `browser.move/mouse_move/move_by`。

所以“改为抖动”是实质歧义，不能由模型自行选一种含义。

### 20.2 统一术语

在 API 契约和需求 schema 中拆成：

- `click_offset`：对已验证点击点增加小偏移后点击；
- `cursor_wiggle`：只移动鼠标，不点击；当前 desktop 脚本 API 若未开放则不可用；
- `retry_delay_jitter`：重试等待时间随机化；
- `gesture_path_jitter`：拖拽/滑动路径扰动；
- `viewport_motion`：页面/窗口视觉晃动，不属于输入策略。

禁止模型把自然语言“抖动”直接映射成某个 API。Orient 阶段若上下文不能唯一确定，必须询问：
“你说的抖动是点击坐标小偏移、只移动鼠标，还是等待时间随机化？”

若用户引用的参考脚本已经明确定义且要求“照它做”，模型可从脚本证据确定语义，并在逻辑草稿中
写明采用哪一种，不必再次询问。

### 20.3 能力与遥测

如确需 `cursor_wiggle`，不要让脚本绕过 `UserWindow` 访问 `_target.mouse_move`；先在契约中增加
受控 API，限定范围、次数与禁区，并记录起止坐标和实际执行结果。`click_offset` 继续通过
`pianyi` 或未来明确命名的参数实现，同时在 click event 记录 base coordinate、offset 和 final
coordinate。

## 21. 主轮空白与跨主轮事实恢复：完整解法

“主轮重置”指 LLM不自动继承上一主轮上下文，不代表 Runtime 删除上一主轮已经确认和持久化的
事实。解决问题需要四层，而不只是给素材加 ID：

### 21.1 资源身份层

每个素材绑定 `resource_id + version + sha256 + role + source + dimensions`。裁剪资源额外绑定
`parent_resource + parent_version + bbox + coordinate_space`；标注图绑定 overlay，不伪装成原图。

它解决“到底是哪张图、哪个版本、从哪里裁的”，不解决图片内容是否识别正确。

### 21.2 任务事实层

用户确认过的目标、偏好、选择和逻辑存为结构化 task facts；每条事实带来源、作用域、版本和
失效条件。图片内容等外部现实仍为 claim，必须有视觉/工具证据才能晋升为 fact。

### 21.3 主轮索引层

第二主轮开始时 Runtime 只提供短索引，例如：

```json
{
  "task_id": "task_x",
  "task_revision": 9,
  "objective_summary": "替换刷关卡素材并取消框外点击",
  "available_projections": [
    "confirmed_logic", "current_resources", "latest_script",
    "latest_validation", "latest_trial", "outstanding_blockers"
  ]
}
```

模型仍然是空白的，但知道去哪里查询。它不应自动读取上一主轮所有副轮对话。

### 21.4 按需恢复层

模型根据用户新消息查询：

- “点击没执行” → latest trial、实际运行脚本、诊断摘要；
- “把这张图换掉” → viewport snapshot、current resources、对应素材版本；
- “继续刚才的逻辑” → confirmed logic、current plan、outstanding blockers；
- “还是用上一张挑战图” → task fact 中的角色绑定，再 resource.get 精确版本。

所有查询结果带事件/资源/确认凭证引用。模型取得的是已整理的事实，不是依靠自然语言聊天记忆。

### 21.5 能解决与不能解决

这四层能解决：旧素材混用、同名覆盖、跨主轮不知道使用过哪个版本、标注图冒充原图、确认状态
丢失、日志无法关联交付脚本。不能解决：裁剪本身错误、模板低信息量、视觉模型误认、阈值不合适。
后者由试运行遥测、关键帧和复测闭环解决。

因此完整闭环为：

```text
主轮空白
→ Runtime 给任务索引
→ LLM 按需查投影
→ 投影返回稳定 ID/version/hash 与证据引用
→ LLM 下钻资源/事件
→ 做出带证据的动作
→ 新事实和运行证据写回本地事件系统
→ 下一主轮重新按需恢复
```

## 22. 统一实施边界：尽量只改本地系统

为避免后续修改零散，优先集中在以下本地边界：

1. `api_contracts/`：能力单一事实源、Python policy、验证；
2. V3 `events/` 或 `runtime/events_*`：事件、投影、epoch、查询缓存；
3. `trial_runs.py + run_recorder.py`：试运行与伪录制统一 run/event/frame 引用；
4. V3 query gateway：只新增结构化查询，不让 provider 直接读文件；
5. V3 runner/provider protocol：Orient、证据引用和动作协议；
6. finish/committer：验证凭证、确认凭证和 hash/revision 闸门。

GUI 只需要：展示已有状态、进入试运行、把用户补充形成新主轮快照；不要把推理、事件归并或证据判断
放进 GUI。真实执行类只负责发出标准化 match/click/frame 事件，不负责决定 LLM 下一步。

实施时应先建立 schema 和适配层，把现有日志/信号接进去，再逐步替换读取方；禁止在
`provider_model.py`、GUI、trial registry、伪录制器各自新增一套相似但不兼容的数据结构。

## 23. 用户个性化术语：保留用户习惯，但不污染系统语义

### 23.1 “抖动”的当前用户含义

当前用户明确说明，其日常所说的“抖动”通常是：

> 点击时增加小幅度的随机坐标偏移，使点击位置产生自然变化，避免每次落在完全相同的像素点。

系统内部标准术语为 `click_offset`。这是一条**用户术语偏好**，不是所有用户共享的全局定义，
也不是无条件覆盖上下文的硬规则。系统不得承诺该变化一定规避任何平台检测；只能准确描述其
可观察行为是“增加点击位置变化”。

### 23.2 三层语义

1. **系统标准语义**：API、事件、校验和代码使用 `click_offset`、`cursor_wiggle`、
   `retry_delay_jitter`、`gesture_path_jitter` 等无歧义标识；
2. **用户个人词典**：记录某位用户通常如何使用自然语言词汇；
3. **任务上下文解释**：具体句子、参考脚本和当前任务可以覆盖个人默认。

个人词典建议结构：

```json
{
  "term_id": "term_...",
  "user_scope_id": "local_user_...",
  "term": "抖动",
  "preferred_meaning": "click_offset",
  "definition": "点击时增加小幅随机坐标偏移",
  "scope": "user",
  "confidence": 0.9,
  "source": "user_explicit_definition",
  "source_event_id": "evt_...",
  "exceptions": [
    "讨论滑动轨迹时可能指 gesture_path_jitter",
    "讨论等待间隔时可能指 retry_delay_jitter"
  ],
  "created_at": "...",
  "updated_at": "..."
}
```

不得把个人词典写入所有用户共用的 system prompt 或全局 API 定义。用户身份不可用时，将该偏好
限制在当前本地 profile；多用户环境必须按 `user_scope_id` 隔离。

### 23.3 Orient 的解释顺序

模型遇到模糊术语时按顺序处理：

1. 当前句子已明确含义则直接采用；
2. 用户明确要求“按某脚本做”，且脚本已定义该行为，则引用脚本证据；
3. 当前任务上下文只允许一种合理含义时采用，并写进逻辑草稿；
4. 使用当前用户个人词典作为默认；
5. 仍存在会显著改变结果的歧义时，`request_interaction`。

示例：

- “点击时加点抖动” → 当前用户默认解释为 `click_offset`；
- “滑动轨迹加点抖动” → 上下文明确为 `gesture_path_jitter`，覆盖个人默认；
- “没识别到就抖动一下” → 可能是鼠标移动、随机等待或点击偏移，查参考脚本后仍不明则询问；
- “不要点击，只抖动鼠标” → 明确为 `cursor_wiggle`。

首次在任务内采用个人默认时，应在初版逻辑草稿中使用无歧义描述，例如：“将抖动按你常用的
含义处理为点击坐标小幅随机偏移”。这不是要求每次重复确认，而是让用户有一次纠正机会。

### 23.4 术语的写入与修订

- 用户明确说明自己的词义时，可直接保存为 `user_terminology_preference`；
- LLM 从多次对话自行归纳时只能 `propose_claim`，不能直接成为高置信个人偏好；
- 用户后来明确改口时创建新版本并使旧版本失效，不覆盖审计历史；
- 任务级临时含义优先于用户级默认，但任务结束后不自动反向污染个人词典；
- 不从图片内容或外部现实的主张推导个人术语事实。

## 24. Runtime、LLM 与用户的写入权责

### 24.1 总原则

> Runtime 自动记录“实际发生了什么”；LLM 提出“这些证据可能意味着什么”；用户确认“我想要
> 什么以及我如何表达”。

LLM不能直接向权威事实表或领域事件表写任意内容。所有写入通过封闭命令进入 Runtime，由策略层
决定保存为事实、偏好、授权、主张还是假设。

### 24.2 Runtime 自动记录的客观事实

以下内容由本地系统从真实操作自动产生，不依赖模型总结：

- Task/MainTurn/Run/Subturn/Action/Attempt ID 与时间；
- 用户原始消息及补充消息；
- 脚本源码、最终产物 hash、提交与验证凭证；
- 素材 ID、版本、hash、尺寸、角色、父资源与裁剪坐标；
- query 名称、规范化参数、结果、空结果 reason 与缓存命中；
- capability 调用、参数、返回、异常和权限凭证；
- trial 开始、结束、脚本 hash、账号作用域与停止原因；
- match 阈值、分数、坐标、frame、耗时和结果；
- click 尝试、偏移、最终坐标、消息投递、确认策略和确认结果；
- 用户点击的确认选项；
- Runtime异常、调用栈、关键帧和伪录制引用。

事件 payload 必须来源于实际执行路径。模型说“点击成功”不能生成
`input.click_completed`；只有执行器观察到相应阶段后才可写入。

### 24.3 用户输入如何保存

Runtime始终原样保存用户消息，随后按类型建立结构化记录：

- 用户目标、偏好、术语定义、授权和明确选择：可直接确认为相应用户事实；
- 图片内容、数量、坐标或外部现实判断：保存为 `user_claim`，状态 `unverified`；
- 用户对运行现象的报告（如“点击没执行”）：保存为 `reported_symptom`，它是“用户确实如此
  报告”的事实，但现象根因仍需运行证据验证。

任何确认和授权继续遵守既定 scope 与失效规则。

### 24.4 LLM 只能提交候选解释

LLM通过 `propose_claim` 提交：

```json
{
  "claim_kind": "hypothesis",
  "statement": "模板可能因阈值偏高而未命中",
  "evidence_refs": ["evt_match_12", "evt_match_13"],
  "missing_evidence": ["降低阈值后的复测"],
  "verification_plan": "将阈值临时调整后重新试运行"
}
```

Runtime 校验所有 evidence ref 真实存在且处于允许作用域，然后保存为 hypothesis。禁止 LLM 直接
写入以下权威事实：

- 点击已执行或业务页面已变化；
- 模板一定存在/不存在；
- 试运行已经成功；
- 用户已经授权或确认；
- 某张图片一定包含某物；
- 某问题已经修复。

### 24.5 事实晋升

建议状态：

```text
raw_observation / user_claim
→ hypothesis
→ corroborated
→ task_fact
→ invalidated / superseded
```

晋升规则按类别不同：

- 用户目标/偏好/术语/选择：用户明确表达即可成为对应事实；
- API 能力：以当前 contract revision 为事实源；
- 静态代码结论：以 validation receipt 为证据；
- 运行结论：要求对应 trial、脚本 hash 和运行事件；
- 视觉/数量/坐标：要求工具/视觉证据及资源版本；
- “已修复”：要求新产物验证通过，若涉及运行行为还必须有新版本复测；
- 推测阈值问题：至少有 near-miss 遥测；只有调整后复测才能成为已验证结论。

同指标出现互斥事实时不能静默并存；按版本、时间、证据强度和作用域标记 superseded，并保留审计
关系。

### 24.6 第二主轮查询什么

新主轮不读取上一轮 LLM 的自由总结作为事实，而是查询 Runtime投影：

```text
task.get_current            用户目标、约束、确认和未决问题
resource projection         当前素材及准确版本
latest script projection    已提交/已验证脚本
latest trial projection     真实运行记录
hypothesis projection       尚未验证的模型判断（明确标记）
```

这样既保持主轮空白，又不会让上一主轮的模型猜测污染下一主轮。

### 24.7 命令与权限建议

Runtime 对外只暴露封闭命令：

- `propose_claim`：LLM 提交候选主张；
- `record_user_preference`：仅从本轮明确用户原话派生；
- `request_confirmation`：请求用户确认；
- `link_evidence`：为候选主张补充已存在证据；
- `invalidate_claim`：提出失效请求，由 Runtime校验依赖；
- 执行器内部事件写入接口：不暴露给 LLM。

所有命令写 `proposer`、来源 main/subturn/action、scope 和 evidence refs。前端只传递用户选择，不能
自行伪造 Runtime观察事件。

### 24.8 测试

1. 当前用户的“抖动”默认解析为 `click_offset`，其他用户不继承；
2. “滑动轨迹抖动”由上下文覆盖个人默认；
3. 实质歧义无法查询消除时必须询问；
4. 任务级临时定义不污染用户级词典；
5. LLM不能直接写 `input.click_completed` 或 `trial.succeeded`；
6. 不存在/越权的 evidence ref 使 `propose_claim` 被拒；
7. 用户明确偏好可直接形成用户事实，图片数量仍为未验证主张；
8. 静态校验不能把运行行为假设晋升为事实；
9. “已修复”在缺少新版本复测时被限制为“已修改并通过静态校验”；
10. 新主轮查询投影时能区分 fact、user_claim 和 hypothesis。

## 25. 已锁定决策：实施模型不得自行改题

以下决定已经由用户认可，实施时视为需求，不需要重新讨论：

1. 主轮对 LLM保持空白；跨主轮信息必须由 Runtime持久化并按需查询；
2. 每个副轮包含结构化 Orient，但不展示自由思维链，也不额外调用一次模型；
3. 用户意图存在重大且无法查询消除的歧义时，先讨论，不先生成；
4. API 文档、RAG查询与校验器必须共用一个结构化 registry；
5. 默认脚本 runtime profile 是实际试运行环境，desktop/browser 契约不可取并集；
6. 脚本只能使用批准的 Python 子集和当前 profile 契约 API；
7. 校验必须覆盖最终替换产物，并生成绑定 hash/revision 的验证凭证；
8. Task/MainTurn/Run/Subturn/Action/Attempt 使用后端签发的稳定层级 ID；
9. 本地事件系统负责客观记录和投影，不把模型总结当作事实；
10. LLM事实判断引用 evidence，推测必须显式标为 hypothesis；
11. 相同查询指纹在相关 epoch 未变化时回放缓存，包括空结果；
12. 试运行和伪录制自动开启基础遥测，脚本第一行异常也必须有 run 记录；
13. 当前用户的“抖动”是 user-scoped 的 `click_offset` 默认，不是全局唯一释义；
14. GUI只展示和传递用户操作，核心判断、事件、证据、校验尽量留在本地 Runtime；
15. 不用本任务顺便引入 LangGraph；先把边界和事实流做正确，后续再评估编排框架。

遇到现有代码无法满足时，实施模型应报告冲突并给兼容迁移方案，不得静默删减以上要求。

## 26. 推荐的最终本地架构

```text
GUI / 用户输入
       │
       ▼
V3 Runner ── Orient/Action 协议 ── LLM
   │                         ▲
   │                         │ 结构化查询结果/证据引用
   ▼                         │
Query Gateway ───────────────┘
   │
   ├─ Task/Resource/Script Projections
   ├─ API Contract Registry
   ├─ Trial Diagnosis Queries
   └─ Event Drill-down

本地执行器 UserWindow/UserBrowser
   │ 真实 match/click/frame/exception
   ▼
Local Event Store + Trial/Pseudo Recorder
   │
   ├─ Projectors + Epochs + Query Cache
   └─ Validation/Confirmation/Evidence Receipts

Script Candidate
   → Source Gate
   → Asset Resolution
   → Final Artifact Gate
   → Import Smoke
   → Fake Runtime Smoke
   → Validation Receipt
   → Finish/Commit
```

依赖方向必须保持单向：执行器发事实事件，但不依赖 LLM；LLM通过 Query Gateway 读，不直接读
SQLite/文件；GUI不承担领域判断；validator读取 contract registry，不反向依赖 prompt renderer。

## 27. 面向较弱实施模型的逐阶段任务单

每阶段单独完成、测试和报告。前一阶段未通过，不进入下一阶段；不要一次修改所有大文件。

### Phase 0：只调查，不改行为

必须输出并写入开发记录：

- 协作试运行真实使用 `UserWindow` 还是 `UserBrowser` 的调用链；
- 模型素材 token 到最终脚本的替换函数和时点；
- 脚本加载/import/执行入口；
- `domain_events`、workspace trace、trial registry、runtime backup、PseudoRecorder 的写入关系；
- 当前主轮恢复导致 subturn 序号重置的代码位置；
- 当前 query fingerprint、cache 与 mutation epoch 的实现位置；
- `finish` 如何确认 validation 与逻辑确认。

Definition of Done：每一项都有文件、符号、行附近说明；没有猜测句。

### Phase 1：API Contract Registry

改动重点：新增 `api_contracts/`，以真实签名为准录入 desktop profile；browser profile 可随后补齐，
但结构必须支持。将旧 `api_catalog.py` 变为 facade，先不删除旧调用接口。

Definition of Done：真实签名漂移测试通过；旧调用输出仍兼容；`wait_image` profile 差异有回归测试；
文档渲染与 validator 使用同一 registry。

### Phase 2：契约校验与最终产物闸门

实现 AST API/await/参数/import/Python policy、素材 token、最终产物、import smoke；再实现安全有界的
Fake Runtime smoke。校验结果生成 receipt 并绑定 hash/profile/contracts/assets/validator version。

Definition of Done：第 10 节用例全部通过；修改一个字符或素材换版后旧 receipt 失效；真实执行方法
不被调用。

### Phase 3：层级 ID、事件信封与投影骨架

先只接关键低频事件：turn、action、query、script、validation、confirmation、trial 生命周期。
保持现有 trace/log 继续工作，通过 adapter 双写或由统一入口派生，禁止散落双写。

Definition of Done：暂停恢复后 subturn 单调递增；事件可追到 validation receipt；projector 可幂等
重建；旧会话可读但不冒充新式可信凭证。

### Phase 4：查询缓存、主轮索引与事实权责

实现领域 epoch、规范化查询指纹、空结果 reason、task index、fact/claim/hypothesis 分层、用户词典。
LLM只能用封闭命令提议，Runtime写客观事件。

Definition of Done：新主轮不带旧上下文也能查询到当前准确资源/确认/脚本；相同空查询不执行第二
次；用户术语跨用户隔离。

### Phase 5：Trial/Pseudo Recorder 2.0

让 TaskController 自动 start/finish run；把 match/click/frame/exception 接入统一事件和 trial；新增
诊断摘要及分页下钻查询。保留 runtime backup 日志，禁止重复把大量尾日志复制到其他文件。

Definition of Done：import 第一行异常有 run 和 exception；阈值 near-miss 可由结构化查询看见；
点击能追到产生坐标的 match/frame；日志与图片通过引用关联。

### Phase 6：Orient、证据输出与最小修改策略

更新模型协议；Orient 与 action 同响应；事实结论有 evidence refs；实现修复/资源替换任务的基线
绑定和 AST/语义 diff。重大歧义才问用户。

Definition of Done：“只替换资源”不能无理由改写控制流；无 trial 时不宣布运行根因；当前用户的
“点击抖动”默认解析为 click_offset，明确其他上下文可覆盖。

### Phase 7：删除重复事实源

全仓限定目录搜索旧白名单、手写签名、旁路 trial/event 写入。只有全部迁移且测试通过后删除；
保留必要兼容 facade 并注明删除条件。

Definition of Done：不存在两份可独立修改且会影响行为的 API 白名单；新增一个 API只需改 registry
语义覆盖并由签名核验；主干协作测试全绿。

## 28. 数据迁移与兼容规则

- SQLite schema migration 必须可重复执行；升级失败时不破坏旧表；
- 新字段先允许 null，写入端上线后再逐步收紧；
- 老记录标 `legacy=true`，缺少 hash/evidence 的记录不能生成新式 receipt；
- 旧 `turn_id` 可继续展示，但新 subturn/action 必须使用新 ID；
- 旧文本 trial log 仍可读，诊断摘要注明 `telemetry_quality=legacy_text_only`；
- 旧素材没有 version/hash 时在首次同步时补齐并发出 migration event；
- contract revision 改变时不要让所有旧脚本无条件失效：签名/语义兼容变更可由明确兼容策略接受，
  破坏性变更才要求重验；
- 双写迁移期必须有一致性测试和计数，不能永久双写；
- 所有新查询都有 payload 大小、分页与脱敏限制。

## 29. 失败模式与禁止捷径

实施模型不得用以下方式“完成”任务：

- 只扩大 system prompt，不做本地校验；
- 把全部 API 文档、日志和图片每轮全塞给模型；
- 用方法名白名单代替参数、await、返回值和 profile 契约；
- 把 `UserWindow` 与 `UserBrowser` 方法取并集；
- 让 LLM自己生成 event/validation/confirmation ID 并当真；
- 只凭 subturn ID 放行，不绑定 artifact hash 和 revision；
- 把 hypothesis 写进 task facts；
- 把用户词典变成所有用户共享提示词；
- trial 为空时自动制造“最可能原因”；
- 通过降低阈值后不复测就宣称修复；
- 为了 Fake Runtime 测试真的点击、联网或写用户文件；
- 在 GUI里实现事件归并、证据判断或查询缓存；
- 删除原有日志轮转或把完整日志重复贴进 daily_stop；
- 为通过新测试放宽已有权限、素材或 ground-truth 安全闸；
- 一次性重写 `agent.py` / `llm_turn.py` / runner，而没有兼容阶段和小步测试。

## 30. 完成报告模板

实施模型最后必须按此格式报告，缺一项即视为未完成：

```text
已完成 Phase：
新增/修改文件：
唯一事实源位置：
真实 runtime profile 调用链：
旧接口兼容方式：
新增 SQLite migration：
新增事件/投影/查询：
验证凭证绑定项：
试运行遥测字段：
仍存在的重复事实源：
仍未解决的问题（不得隐瞒）：
测试命令与结果：
手工复现场景与结果：
回滚方式：
```

## 31. 总体验收场景

除单元测试外，至少完成以下本地场景：

1. 新建脚本：模型查询 API契约，生成、素材解析、最终产物校验、receipt、finish；
2. 资源替换：以现有脚本为 baseline，只改变允许字段，diff 闸门通过；
3. API 写错：`move_by`、错误 keyword、错误 await 都在一次校验中结构化返回；
4. 主轮恢复：副轮序号继续递增，原确认按 logic hash 有效；
5. 新主轮修复：没有旧聊天上下文，通过 task index 找到实际交付脚本和素材；
6. trial 为空：只报告缺少证据，相同查询回放缓存；
7. near-miss：记录 threshold/best score/frame，LLM提出 hypothesis，调整后复测再晋升；
8. 点击链：从用户反馈追到 trial → click → match → frame → template version；
9. 用户术语：当前用户“抖动”解释为 click_offset，另一用户不继承，滑动上下文覆盖；
10. 模型胡扯：无 evidence 的事实结论被协议/闸门拒绝，不能进入下一主轮 task facts；
11. 脚本首行异常：仍能查询 run、script hash、exception 和关键上下文；
12. 兼容旧会话：能展示和查询，但不会伪造新验证凭证。

## 32. 最终优先级与停止条件

若时间有限，优先顺序固定为：

1. API 单一事实源与最终产物闸门；
2. validation receipt 与稳定层级 ID；
3. trial 自动登记、异常、match/click 结构化遥测；
4. 事件投影、主轮索引、查询缓存；
5. Orient、证据协议、用户词典；
6. 最小修改 diff 与高级诊断摘要；
7. 清理兼容层。

任一阶段若必须破坏用户现有脚本、删除旧记录、放宽安全检查或无法保证回滚，停止实施并报告具体
冲突，不得自行扩大授权。
