# 本地 API 契约凭据与交付对账实施方案

> 状态：待实现，可直接交给实现代理。  
> 日期：2026-09-29。  
> 讨论原稿：`design_collaborator_capability_writer_addendum.md`。  
> 本文是当前有效方案；原稿第 8—17 节的完整断言 Writer 仅作远期参考，不属于本期范围。
>
> **收敛说明**：跨任务路由、资源、契约、类型和证据的最终实施顺序已统一整理到
> `design_main_turn_reliability_pipeline.md`。本文继续作为API ticket专项设计与真实反例记录；实施代理应先读统一方案。

## 1. 最终决策

本期不建设新的 Writer DSL，也不要求 LLM 学习第二套编程契约。LLM 继续使用普通 Python 编写
算法和业务逻辑。本地系统新增“API 契约凭据与源码调用对账”，专门解决 LLM 对本地架构产生的
幻觉：编造 API、混淆所属类或 profile、使用旧签名、未读取契约便凭记忆调用。

本期结论分成三层，不得混为“脚本合格”：

1. **架构真实性**：本地 API 调用是否均有有效契约凭据；
2. **代码可运行性**：语法、参数、await、类型、数据流和运行前检查是否通过；
3. **需求符合性**：脚本逻辑及真机结果是否满足用户目标。

契约凭据只负责第一层，并为第二层提供返回类型等事实。第三层继续由需求确认、验收条件和试运行
证据负责。

## 2. 本期目标与非目标

### 2.1 必须解决

- 本地 Runtime API 必须先取得本地主导的准确契约，不能由 LLM 自报；
- 交付源码中的每个本地调用必须能与本轮有效凭据对上；
- 正确区分 `UserWindow`、`UserBrowser`、Mixin、继承方法和登记的代理方法；
- Registry、profile、资源、环境或权限变化后，相关凭据可以失效；
- 对账绑定最终运行 artifact，杜绝“校验一份、运行另一份”；
- 给出稳定、可修复、能定位源码调用点的诊断；
- 与现有 API Contract Registry、Validator、materializer 和 receipt 共用事实源。

### 2.2 本期明确不做

- 不实现完整 Script IR、断言语言或控制流编译器；
- 不以凭据判断算法是否合理或是否满足需求；
- 不自动修改 LLM 的业务代码；
- 不把每次循环执行都签成一张新凭据；
- 不为 `len`、`sorted`、`math` 等批准的 Python 能力逐次签发；
- 不把一次运行异常直接自动晋升为全局硬规则；
- 不允许模型自己填写、签名或修改凭据事实。

## 3. 核心不变量

1. Contract Registry 是 API 身份、签名、返回模型和 profile 的唯一事实源；
2. 凭据由本地系统生成，LLM 只能取得并引用 `ticket_id`；
3. 交付调用表由源码 AST 自动提取，不能相信模型声明；
4. 每个已提取的本地调用点必须映射到当前有效凭据；
5. 本地类中的内部方法只有 `approved=true` 才能取得凭据；
6. 无法确定接收者或动态解析的 Runtime 调用默认拒绝，不能猜测放行；
7. 凭据通过不代表代码可运行，更不代表需求满足；
8. source 或 materialized artifact 变化后，旧对账和 receipt 立即失效；
9. 不新增第二份方法白名单，不在提示词、GUI或校验器复制 Registry 数据；
10. 所有拒绝必须带稳定诊断码、源码位置、证据和允许的修复方向。

## 4. 总体流程

```text
主轮开始
→ 本地提供一次紧凑能力索引
→ LLM按需请求一个或多个能力的完整契约
→ 本地解析当前Runtime事实/profile/Registry
→ 记录CONTRACT_VIEWED并签发contract_ticket
→ LLM编写普通Python
→ 交付时从最终source提取调用点
→ 解析接收者、MRO/Mixin/代理链和真实方法
→ 调用点与ticket及Registry三方对账
→ AST参数/await/返回类型/数据流检查
→ Python Policy、import smoke、Fake Runtime
→ materialize最终artifact并再次核验调用映射
→ receipt绑定全部hash、版本和ticket
→ 试运行
→ 需求与业务结果另行验收
```

模型不必手写 API 使用表。若需要向模型展示，可展示本地提取的只读结果。

## 5. 三类本地记录

### 5.1 `CONTRACT_VIEWED`

表示模型在当前主轮取得过完整契约。至少记录：

- `main_turn_id/subturn_id`；
- `capability_id/contract_revision`；
- 请求时的 `runtime_profile/profile_source`；
- 提供给模型的 schema hash；
- 时间和对应工具调用事件 ID。

### 5.2 `CALL_AUTHORIZED`

表示本地批准在指定作用域使用能力。它不能与用户副作用授权混淆；对于点击、写入、删除等操作，
还必须关联独立的 permission grant。

### 5.3 `CALL_EXTRACTED`

表示交付源码 AST 中实际发现的调用点。至少记录：

- source hash、AST node ID、行列范围；
- 接收者表达式及其推导来源；
- 方法名、位置参数和关键字参数；
- 是否 `await`；
- 解析出的 owner、profile 和代理链；
- 关联的 `ticket_id` 或拒绝原因。

## 6. 契约凭据模型

建议第一版结构：

```json
{
  "ticket_id": "ct_01JXYZ",
  "schema_version": 1,
  "main_turn_id": "turn_...",
  "view_event_id": "evt_...",
  "contract_revision": "sha256:...",
  "capability_id": "runtime.image.match_one",
  "receiver_type": "UserWindow",
  "requested_method": "match_image",
  "resolved_owner": "UserWindow",
  "resolution_chain": ["UserWindow.match_image"],
  "runtime_profile": "desktop_user_window",
  "profile_source": "runtime_instance",
  "call_kind": "async",
  "parameters": {
    "required": ["img_path"],
    "allowed": ["img_path", "threshold", "pianyi"]
  },
  "return_model": {
    "type": "MatchResult | falsy",
    "iterable": true,
    "sized": false,
    "indexable": true,
    "subscript_keys": ["x", "y", "score", "max_val"],
    "collection": false,
    "nullable_fields": ["x", "y", "match_success", "width", "height", "scale"]
  },
  "permission_scope": ["screen.read"],
  "resource_scope": ["asset_badge@3"],
  "environment_revision": "env_...",
  "issued_at": "...",
  "integrity": "local-mac-or-content-hash"
}
```

数据库或事件存储保存完整凭据；进入 LLM 上下文时只需返回 `ticket_id`、准确契约及必要解释。

## 7. 哪些调用必须有凭据

### 7.1 Runtime及本地登记能力

以下调用必须有凭据：

- 入口参数为 `UserWindow`、`UserBrowser` 或其他登记 Runtime 类型；
- 变量由上述对象确定性赋值得到，如 `bb = browser`；
- 通过登记的 Mixin 或 `__getattr__` 代理链到达的方法；
- Registry 中登记的本地自由函数或服务能力。

### 7.2 Python能力

批准的内置函数和标准库由 Python Policy 统一裁决，不逐调用签发，但仍参与参数和类型检查。

### 7.3 脚本内部函数

本次交付源码内定义的函数不需要契约凭据；其源码必须可见并经过 AST、安全和数据流检查。

### 7.4 默认拒绝

- 动态 `getattr(runtime, name)`；
- monkey patch、实例方法替换；
- 无法确定接收者来源的调用；
- 本地类中未被 Registry 批准的内部方法；
- 依赖隐式静默 profile 回退的调用。

## 8. 接收者与方法解析

不能用“父类属于本项目”作为唯一判据。解析器至少要支持：

1. 入口参数类型和 Runtime 注入来源；
2. 简单别名传播，重新赋值后旧来源失效；
3. Python真实MRO和Mixin；
4. Registry明确登记的代理链；
5. 子类覆盖和实例属性遮蔽；
6. 不同profile下的同名异签名；
7. 变量合流无法证明单一类型时给出不确定诊断，不静默选一个。

例：`UserWindow.__getattr__ → Win32Target.click`必须作为完整解析链写入凭据和提取结果，不能把
`Win32Target.click`误判成任意可用方法。

## 9. 交付对账与诊断

源码中的每个本地调用点执行以下对账：

1. 是否能解析接收者；
2. Registry是否登记并批准最终方法；
3. 是否存在当前主轮有效ticket；
4. receiver、owner、代理链和profile是否一致；
5. 参数、await和返回模型是否一致；
6. 资源和权限作用域是否覆盖实际使用；
7. ticket引用的契约、环境和资源版本是否仍有效。

第一版稳定诊断码：

| 诊断码 | 含义 | 处理 |
| --- | --- | --- |
| `UNREGISTERED_API_CALL` | 本地调用没有凭据 | 拒绝，要求取得当前契约 |
| `CALL_RECEIVER_UNRESOLVED` | 接收者来源无法证明 | 拒绝，改为可静态解析写法 |
| `CALL_TICKET_CONTRACT_MISMATCH` | 方法、owner或代理链不符 | 拒绝并展示双方证据 |
| `PROFILE_CHANGED` | 执行profile与凭据不符 | 凭据失效，重新查询 |
| `CONTRACT_REVISION_EXPIRED` | Registry版本变化 | 重新取得契约并校验 |
| `ARGUMENT_CONTRACT_MISMATCH` | 参数不符合签名 | 拒绝并展示准确签名 |
| `PERMISSION_SCOPE_MISMATCH` | 副作用超出授权 | 停止并走权限流程 |
| `RESOURCE_SCOPE_MISMATCH` | 使用了凭据范围外资源 | 重新绑定或取得凭据 |
| `RETURN_USAGE_MISMATCH` | 返回对象用法与模型不符 | 进入类型/数据流修复 |

已签发但未被源码使用的ticket只记 `unused_ticket`，默认不拒绝。若它对应确认后的必要业务步骤，
由需求验收报告步骤缺失，而不是由API对账器越权判断。

## 10. 返回类型与数据流仍是独立闸门

凭据一致只能证明调用真实，不能证明结果使用正确。例如：

```python
result = await browser.match_image(path)
count = len(result)
```

即使 `match_image` 的ticket合法，仍必须根据凭据中的 `MatchResult` 数据模型报告
`TYPE_NOT_SIZED`。至少覆盖：

- 不可迭代对象被循环；
- 不可计数对象传给 `len`；
- 非mapping对象被下标访问；
- nullable字段未保护就使用；
- 空列表直接取首项；
- API返回类型经简单赋值、分支和重新赋值后的传播。

该层使用同一Registry中的返回模型，不建立新的类型事实源。

## 11. 生命周期与失效

凭据默认绑定主轮，但失效由版本和作用域决定，不只看时间。以下变化触发重新核验或失效：

- Runtime profile或执行实例变化；
- Registry revision变化；
- 环境revision变化；
- 素材被替换、删除或版本变化；
- 权限撤销或作用域变化；
- 用户补充导致主轮快照更新；
- source或artifact发生变化。

用户在主轮中补充时，停止旧快照驱动的未来计划并进行兼容性评估。兼容ticket可由本地重新核验，
不兼容ticket失效。已经执行的事件作为历史事实保留，但不能充当未来授权。

## 12. 凭据、权限与执行事件分离

- `contract_ticket`：API真实存在且模型取得了当前契约；
- `permission_grant`：允许在指定范围产生副作用；
- `execution_event`：运行时实际输入、输出、耗时和异常。

三者通过稳定ID关联，但不得合并成一个字段。循环里同一调用点只需要一张ticket，每次真实执行分别
产生事件。

## 13. Receipt与最终artifact

最终 receipt 至少绑定：

- source hash；
- bundled hash；
- artifact hash；
- source调用点到ticket的映射；
- Registry revision；
- runtime profile及其事实来源；
- 环境、资源和权限版本；
- materializer version；
- Validator版本和检查结果。

运行器只接受 artifact hash 完全一致的产物。若materialize注入本地调用，应明确区分系统注入调用
与LLM源码调用，并由可信materializer身份担保，不能要求LLM为系统注入代码取得ticket。

## 14. 对外展示

界面只展示简短、可验证的状态，不展示自由思维链：

```text
架构真实性：API调用对账通过
代码可运行性：运行前检查通过
需求符合性：等待试运行验证
```

发生拒绝时展示源码行、实际调用、期望契约和修复方向。不得把“API对账通过”描述为“脚本已经
正确”或“需求已经满足”。

## 15. 实施阶段

### Phase 0：只读审计

- 盘点现有Registry、profile解析、Validator、materializer、receipt和Runtime事件链；
- 列出所有本地调用入口、代理路径和重复事实源；
- 用真实会话确认能力契约目前怎样进入LLM上下文；
- 不改实现，先输出带文件、符号和测试入口的审计报告。

### Phase 1：数据模型与本地签发

- 定义ticket schema、版本、存储和完整性字段；
- 在能力详情查询成功后记录 `CONTRACT_VIEWED`；
- 从唯一Registry生成ticket，不复制方法表；
- 添加失效和查询接口；
- 先不接交付硬闸。

### Phase 2：源码调用提取与解析

- AST提取调用点；
- 接收者来源、简单别名、重新赋值、MRO/Mixin和登记代理解析；
- 区分Runtime、Python Policy和脚本内部函数；
- 输出只读对账报告，暂不拒绝生产交付，以实况评估误报。

### Phase 3：硬闸与类型检查接线

- 对确定性错误启用拒绝；
- 对无法解析和不确定分支先建立专项测试，再决定是否硬闸；
- 接入参数、await、返回类型和基础数据流检查；
- 确保协作入口与其他脚本入口使用同一校验实现。

### Phase 4：Receipt与Runtime闭环

- 调用点映射和ticket写入receipt；
- 最终artifact hash复核；
- 试运行事件关联ticket和source span；
- GUI展示三层质量状态；
- 用一次真实GUI试运行验证全链路。

每个Phase单独报告改动、红灯、回归和未完成项，不自动扩建完整Writer或事件系统。

## 16. 必须先写的失败测试

1. 不存在的本地方法无法取得ticket；
2. LLM伪造ticket ID或字段不能通过完整性检查；
3. `UserWindow`和`UserBrowser`同名异签名按profile区分；
4. 登记代理链可以解析，未登记代理不能；
5. 子类覆盖不能误用父类ticket；
6. `bb = browser`继承来源，重新赋值后来源失效；
7. 动态`getattr`不能绕过；
8. 本地类内部未批准方法不能取得ticket；
9. 无ticket调用被报 `UNREGISTERED_API_CALL`；
10. Registry/profile/素材/权限变化使相关ticket失效；
11. ticket合法仍不能掩盖 `len(MatchResult)`；
12. 循环多次执行复用调用点ticket但生成多条执行事件；
13. source或artifact变化后旧receipt失效；
14. 系统materializer注入调用不被误判为LLM幻觉；
15. API对账通过不会自动令需求验收通过；
16. 同一脚本在协作入口与最终交付入口得到相同结论。

## 17. 完成定义

本方案只有同时满足以下条件才算完成：

- 所有本地调用均可从最终artifact追溯到Registry契约或可信系统注入来源；
- LLM不能伪造、修改或复用失效ticket；
- 已知代理、继承和profile差异有测试；
- 不存在新的独立API白名单；
- 凭据对账、类型检查和需求验收在协议及GUI中明确分层；
- source、materialized artifact和实际运行文本hash一致；
- 专项测试与协作测试通过；
- 至少完成一次真实GUI的“查询契约→生成→交付→试运行→诊断”全链路验证；
- 完成报告列出未覆盖动态语法、误报情况和后续工作，不把测试模拟当成真机结论。

## 18. 给实现代理的约束

1. 先审计再改代码，不根据本文猜测现有文件结构；
2. 不改已提交的 `design_executable_api_contract.md`；
3. 不继续实现原补充方案中的完整Writer DSL；
4. 不以“父类属于本项目”替代真实接收者和方法解析；
5. 不让LLM生成或填写ticket事实；
6. 不新增任何手写API白名单；
7. 不把凭据和用户权限合并；
8. 不在未完成最终artifact核验前宣称交付通过；
9. 不因某个测试锁定旧错误行为而保留错误默认；修改时必须说明依据并同步强化测试；
10. 遇到无法静态证明的调用，明确报告限制，不静默放行，也不擅自扩建第二套语言。

## 19. 真实主轮反例：勋章数量分析被错误路由并错误交付

> 记录日期：2026-09-29。  
> 会话工作区：`user_data/collab_workspaces/20260929_081703_chat_c083/trace/session.json`。  
> 性质：影子闸上线后的真实用户主轮；先记录事实，修法待讨论，不据此自动开启硬闸。

### 19.1 用户目标与实际正确结果

用户请求：“请你用模板匹配，回答我结界1里有多少勋章”。这是一次针对已有素材的
`asset_analysis`，预期行为是调用本地图片分析能力并直接返回有证据的数量，不是创建自动化脚本。

对原图逐卡核对：

```text
第一行：3 + 3
第二行：3 + 1
第三行：1 + 0
第四行：0 + 0
总数：11
```

### 19.2 实际主轮行为

该会话没有回答“11”，而是：

1. 首次模型输出触发 `MODEL_PROTOCOL_ERROR`（非JSON）；
2. 要求用户“继续未完成的计划”；
3. 把图片分析请求路由成脚本设计；
4. 两次要求用户确认脚本逻辑；
5. 生成脚本后要求用户自行前往试运行；
6. 最终仍未给出用户所问的勋章数量。

因此本轮在“需求符合性”层失败，且再次出现本可由Agent完成却要求用户反复唤醒/确认的问题。

### 19.3 本地分析工具失败

会话曾调用 `count_medals`，但工具把素材token：

```text
[[asset_7c110985]]
```

直接当成文件路径交给 `cv2.imdecode/np.fromfile`，没有先经过 `resolve_image`，最终产生
`FileNotFoundError`。工具失败后，模型没有修复参数解析或换用其他分析能力，而是退回脚本生成。

这一事实说明：任务路由错误不只是提示词问题，也与“分析工具失败后的恢复策略”有关。

### 19.4 生成脚本的返回语义错误

脚本使用：

```python
result = await browser.match_image(
    TEMPLATE_IMG,
    threshold=0.9,
    match_select="all",
)
```

随后把 `result`当作多目标集合遍历和计数。真实契约是：

- `match_image`只返回一个 `MatchResult`；
- `match_select="all"`不会把返回值变成多目标列表；
- `MatchResult.__iter__`依次产生 `x/y/max_val`三个标量；
- `MatchResult`可迭代但不是 `collection`，也不是多目标结果；
- 多目标数量必须来自登记的多目标能力及其Sized返回值。

该脚本遍历 `MatchResult`时会把三个标量分别交给 `_to_box`，均无法形成匹配框，最终候选列表为空，
很可能输出0，而真实数量是11。现有静态校验错误地允许其以“校验通过”交付。

### 19.5 凭据影子闸捕获到的事实

该真实主轮的影子报告显示：

```text
local_api_sites = 9
ticket_provided = 0
ticket_missing = 9
enforced = false
```

缺票调用包括 `match_image`、`update_frame`、`enable_pseudo_record`、`script_log`、`note_state`、
`note_progress`和`finish_pseudo_record`等。说明模型未通过 `api.get/api.get_many`取得当前契约，而是
继续凭已有知识生成代码。

这证明凭据机制能够观察到目标问题；由于当前是影子模式，它没有改变交付结果，这与阶段设计一致。

### 19.6 影子分类器的新误报

同一报告把以下普通Python对象调用标成 `not_approved`拒绝候选：

- `raw.append`；
- `raw.sort`；
- `kept.append`；
- 字符串 `.format`。

这些调用不属于本地Runtime API，应由Python语义/Policy处理，不应要求契约ticket。报告却同时记录
`false_positive_candidates = 0`，说明当前误报统计没有识别这些明显误报。

在修复并用真实主轮验证前，不得开启生产硬闸。

### 19.7 本反例暴露的独立问题

1. **任务分类错误**：明确的素材分析请求被路由为脚本生成；
2. **工具恢复错误**：素材token未解析，工具失败后没有在分析路径内恢复；
3. **契约查询未执行**：九个本地调用全部缺ticket；
4. **返回语义校验不足**：可迭代单结果被误当作多目标集合；
5. **影子分类误报**：普通对象方法被当成本地未批准API；
6. **误报指标失真**：明显误报未计入 `false_positive_candidates`；
7. **交互自治不足**：协议错误和工具错误被转嫁给用户继续/确认；
8. **结论层级混淆**：“静态校验通过”被用户界面表达得过于接近“脚本可用”。

### 19.8 修复前的硬约束

- 不得用增加确认步骤来掩盖任务分类或工具恢复问题；
- 不得把 `match_select="all"`解释成多目标返回；
- 不得仅按方法名判断本地Runtime调用；
- 不得把 `append/sort/format`等普通方法计为 `not_approved`；
- 不得依据当前 `false_positive_candidates`指标决定开启硬闸；
- 不得把“静态校验通过”表述为已得到正确图片结论；
- 不得用测试或重放替代后续真实用户主轮验证。

### 19.9 待讨论的解决面

后续应分别讨论并独立验收以下四条链，而不是用一个大修复混在一起：

1. `asset_analysis`任务识别与完成协议；
2. 本地分析工具的资源解析、失败恢复与直接答复；
3. Runtime调用识别、ticket补查和普通Python方法排除；
4. 返回对象的语义角色与跨行数据流检查，尤其是“iterable但不是collection”的类型。

每条链必须先有本反例的失败测试，再改实现；修复后用新的真实主轮复测，不能只证明旧记录重放变绿。

## 20. 补全副轮定向思考：从说明字段升级为任务路由协议

### 20.1 现状缺口

现有 `orientation` 已包含目标理解、已知事实、未知项、重大歧义和下一步原因，但存在四个结构性缺口：

1. 没有 `task_type`，不能区分素材分析和脚本生成；
2. orientation为了兼容旧响应仍是可选字段；
3. Runtime只用它阻止“重大歧义下直接执行”，不检查目标与动作是否一致；
4. 没有要求脚本意图引用用户原话，模型可以自行把分析任务解释成脚本任务。

因此现有实现只能称为“结构化摘要与歧义闸”，尚不是任务路由闸。

### 20.2 新的结构化字段

不额外增加一次模型调用；仍在每个副轮的同一次响应中先给orientation、再给一个action。增加：

```json
{
  "task_type": "asset_analysis",
  "task_type_evidence": {
    "source": "user_message",
    "message_id": "msg_...",
    "quote": "请你用模板匹配，回答我结界1里有多少勋章"
  },
  "interpreted_goal": "分析已有素材并直接回答勋章数量",
  "requested_changes": [],
  "must_preserve": [],
  "known_facts": ["素材区有底图和勋章模板"],
  "unknowns": ["实际命中数量"],
  "material_ambiguities": [],
  "can_act_now": true,
  "next_action_reason": "调用本地图片分析能力"
}
```

第一版任务类型使用封闭集合：

- `conversation`；
- `requirement_clarification`；
- `asset_analysis`；
- `tool_operation`；
- `script_design`；
- `script_generation`；
- `script_modification`；
- `trial_diagnosis`。

未知值返回稳定协议错误，不允许自由创造近义分类。

### 20.3 orientation何时必须存在

- 纯寒暄或不产生状态变化的普通回答可使用 `conversation`；
- `act`、`update_plan`、`request_interaction`、`validate_script`和任务型`finish`必须带完整orientation；
- 任务类型发生变化时必须重新提供orientation和证据；
- 查询也必须继承当前任务类型，不能通过若干query后静默变成另一任务；
- 旧客户端若不支持新字段，只能走明确的兼容路径，不能进入脚本创建和交付动作。

删除“orientation整体可选且所有动作照常放行”的宽松语义。

### 20.4 脚本意图必须由用户明确表达

进入 `script_design/script_generation/script_modification` 时，本地必须验证：

- `task_type_evidence.source == user_message`；
- message ID属于当前目标链中真实存在的用户消息；
- quote能在该消息中定位；
- 原话明确表达创建、生成、编写、修改或修复脚本/代码/自动化的意图。

模型自己的总结、工具失败、系统建议、历史计划或“为了完成分析最好写脚本”不能作为脚本意图证据。
证据不足时返回：

```text
SCRIPT_INTENT_NOT_EXPLICIT
```

该拒绝不是让模型立即追问“要不要写脚本”，而是要求它留在原任务类型内继续完成；只有现实上确实
必须改变交付物且会显著改变结果/成本时，才可按需求歧义策略询问用户。

### 20.5 task type与动作白名单

Runtime根据任务类型限制可执行动作：

| task type | 允许的主要动作 | 明确禁止 |
| --- | --- | --- |
| `conversation` | query、非任务finish | 创建脚本任务、validate_script |
| `requirement_clarification` | query、request_interaction、update_plan | 直接生成/交付脚本 |
| `asset_analysis` | query、act、propose_claim、直接回答finish | 创建脚本任务、逻辑草稿确认、validate_script、去试运行 |
| `tool_operation` | query、act、结果finish | 未获明确意图时创建脚本 |
| `script_design` | query、update_plan、逻辑确认 | 未确认即validate/finish脚本 |
| `script_generation` | query、validate_script、脚本finish | 绕过契约查询和逻辑确认 |
| `script_modification` | query、局部修改、validate_script、脚本finish | 无理由全文重写、改变原目标 |
| `trial_diagnosis` | trial查询、局部修订、证据回复 | 重问完整需求、重新确认整套逻辑 |

白名单应由Runtime状态机实现，不只写在提示词中。

### 20.6 续跑、重来和错误恢复

“继续”“接着做”“重来”“再试一次”是控制话语，不是新业务目标：

- 默认继承当前任务类型和目标；
- “继续”只能恢复被暂停的同类未来计划；
- “重来”清除旧计划但不把 `asset_analysis`改成 `script_generation`；
- 用户补充新目标时才重新分类，并执行兼容性评估；
- `MODEL_PROTOCOL_ERROR`修复响应必须重新提交完整orientation；
- 工具异常、资源解析失败和能力未找到均不得自行改变任务类型。

旧快照驱动的计划若已经与新orientation冲突，应停止而不是继续执行。

### 20.7 用户可见的决策轨迹

不展示自由思维链，只展示简短结构化结果：

```text
当前任务：分析素材并回答数量
已有：结界1底图、勋章模板
还缺：可靠匹配结果
下一步：调用本地多目标图片分析
```

任务类型发生变化时必须说明变化来自哪条用户请求；不能只显示“整理回复”或“继续计划”。

### 20.8 本地一致性校验

除了字段schema，还需要检查：

- `interpreted_goal`不得与用户证据明显冲突；
- action必须属于task type白名单；
- `can_act_now=false`时不能提交执行/校验/完成动作；
- `material_ambiguities`非空时继续沿用现有歧义闸；
- `asset_analysis`的finish若陈述图片数量，必须引用真实分析证据；
- `script_generation`若没有有效 `SCRIPT_INTENT`事实，禁止建立Task、提交logic draft或校验脚本。

### 20.9 专项失败测试

1. “回答图片里有多少勋章”必须分类为 `asset_analysis`；
2. 同一请求即使工具失败也不能建立script task；
3. 用户随后说“继续”仍保持 `asset_analysis`；
4. 用户说“重来”只清计划，不改变任务类型；
5. 模型为分析请求提交logic draft时返回 `SCRIPT_INTENT_NOT_EXPLICIT`；
6. 模型自己的解释不能充当用户脚本意图证据；
7. “帮我写一个以后自动统计的脚本”允许进入 `script_design`；
8. orientation缺失时不能执行任务型act/plan/validate/finish；
9. `MODEL_PROTOCOL_ERROR`修复响应必须重新定向，不能恢复旧的错误脚本计划；
10. `asset_analysis`有可靠证据后可以直接回答，不出现逻辑确认和“去试运行”；
11. task type变化必须引用新的用户消息并通过兼容性评估；
12. 用户未明确改变目标时，工具错误不能改变task type。

### 20.10 与其他修复的边界

补全orientation只能解决“为什么走到脚本流程”的问题，不能单独解决：

- 素材token没有转成真实文件路径；
- 本地工具本身算法或参数错误；
- 模型没有取得API contract ticket；
- 普通Python方法被影子闸误判为本地API；
- `MatchResult`被错误当成多目标集合；
- 静态校验通过却没有真实业务证据。

这些必须继续由资源解析、工具执行、ticket对账、返回数据流和试运行证据各自负责，不能把全部责任
重新压回“小思考提示词”。
