# 自动化协作者、能力目录与本地脚本写作系统补充方案

> 独立补充方案，不修改已经提交的 `design_executable_api_contract.md`。  
> 日期：2026-09-28  
> 起因：实况把“用模板分析一张素材图”误路由为脚本制作，并生成了把
> `MatchResult` 当列表使用的代码。
>
> **状态说明（2026-09-29）**：本文保留完整讨论和方向演变。当前可实施方案已经整理到
> `design_local_api_contract_ticket.md`；第8—17节的完整断言Writer已暂缓，不应据此直接开工。

## 1. 目标

解决四类问题：

1. LLM因语义搜索漏召回而看不到已经存在的工具；
2. 系统角色过度强调“写脚本”，把分析、澄清和诊断请求都变成脚本任务；
3. API只展示签名但没有充分展示返回对象的可用操作；
4. 在保留LLM算法设计能力的同时，让本地系统承担容易机械出错的代码落盘工作。

## 2. 每个主轮提供完整能力索引

废除“模型必须先猜关键词才能知道能力存在”的单一路径。每个主轮开始时提供一次紧凑目录：

```json
{
  "catalog_revision": "sha256:...",
  "categories": [
    {
      "id": "image_analysis",
      "items": [
        {
          "id": "image.template_match",
          "name": "本地模板匹配",
          "summary": "用模板图在另一张本地图片中查找一个或多个命中",
          "inputs": ["template_image", "target_image", "threshold"],
          "outputs": ["matches", "count", "overlay"],
          "available": true
        }
      ]
    }
  ]
}
```

目录至少分为：对话与需求、图片分析、素材管理、脚本设计、脚本生成/修改、试运行诊断、
标注与裁剪、本地探针。

目录只给名称、用途、输入输出概要和可用状态；模型选中后用 `capability.get(ids=[...])`
一次取得完整schema。能力数量较少时可直接提供全部schema，但同一主轮只发送一次，不在每个
副轮重复消耗token。

`capability.search`保留为目录过滤器，不能再是发现能力的唯一入口。空搜索结果只代表关键词
未命中，不代表系统没有该类能力。

## 3. 系统角色改为“本地自动化协作者”

建议核心提示：

> 你是本地自动化协作者。你负责理解和澄清目标、检查素材、直接调用本地分析工具、帮助用户
> 确定自动化所需能力、设计脚本逻辑、生成或修复脚本，并依据试运行证据迭代。不要把所有请求
> 都视为脚本生成。只有用户明确要求制作/修改自动化，或经过协作已经确认需要脚本时，才创建
> 脚本任务。

每个副轮的Orient必须先选任务类型：

```text
conversation
requirement_clarification
asset_analysis
tool_operation
script_design
script_generation
script_modification
trial_diagnosis
```

只有后三种脚本类型进入逻辑确认、代码校验和脚本完成闸门。`asset_analysis`直接使用分析能力并
返回结果；`trial_diagnosis`查询证据后直接回答，不得重新要求确认脚本逻辑。

分类必须展示给用户的简短“决策轨迹”：目标理解、任务类型、已知证据、未知项、下一步及原因。
不展示自由思维链。

## 4. 返回数据模型作为一等契约

`api.get`除函数签名外必须返回数据模型。对小型公共对象可附准确的公共类定义或等价接口：

```python
class MatchResult:
    x: int | None
    y: int | None
    max_val: float
    score: float
    match_success: bool

    def __bool__(self) -> bool:
        return self.match_success
```

同时机器可读地标注：

```json
{
  "type": "MatchResult",
  "iterable": false,
  "sized": false,
  "mapping": false,
  "fields": ["x", "y", "max_val", "score", "match_success"]
}
```

示例必须明确：`match_image`只返回一个结果；不得`len()`、遍历或下标访问；多结果使用相应多目标
能力。模型是否读过文档不能代替本地返回类型检查。

## 5. 本地契约感知写作系统

该设想可实现，推荐定位为“LLM负责算法与意图，本地Writer负责可靠语法和API接线”，而不是
用僵硬模板取代LLM。

### 5.1 分工

LLM输出结构化 `ScriptIntent/ScriptIR`：

- 状态、循环、分支和停止条件；
- 使用哪些素材和能力；
- 匹配结果如何选择；
- 参数调整策略；
- 需要的纯计算算法；
- 日志、进度和验收条件。

本地Writer负责：

- 按runtime profile选择准确API；
- import、入口签名、await和关键字参数；
- 素材token与路径材料化；
- 返回类型安全的取值、判断和遍历；
- 标准日志、状态、超时、异常和伪录制挂接；
- 生成源码映射和validation receipt。

### 5.2 IR示例

```json
{
  "entry": "do_work",
  "profile": "portable_common",
  "resources": {
    "badge": "asset_badge@1",
    "scene": "asset_scene@1"
  },
  "steps": [
    {
      "op": "offline_template_match",
      "template": "badge",
      "target": "scene",
      "threshold": 0.8,
      "result": "badge_hits"
    },
    {"op": "log", "value": {"count_of": "badge_hits"}},
    {"op": "return", "value": {"count_of": "badge_hits"}}
  ]
}
```

Writer从能力schema知道该操作返回列表，因此合法生成`len(badge_hits)`；如果操作是
`match_image`并返回`MatchResult`，Writer不会提供`count_of`操作。

### 5.3 保留LLM算法能力

不能把Writer做成固定状态机生成器。提供三层表达能力：

1. 标准IR节点：match/click/wait/loop/branch/log/state/return；
2. 纯计算表达式：筛选、排序、距离、聚类、计数、窗口统计；
3. 受限`pure_python`函数：LLM可以写复杂算法，但不得直接调用browser、文件、网络和进程API。

`pure_python`经过AST安全检查，可调用批准的数学/集合能力，并要求附最小测试样例。本地Writer把
它嵌入受控脚本。这样API接线由本地保证，算法创造力仍归LLM。

无法由IR表达的新算法不会被偷偷降级：Writer返回`IR_UNSUPPORTED_OPERATION`，模型可以提出
扩展IR或提供受限纯函数，而不是绕过契约直接拼危险源码。

### 5.4 Writer不是新的重复事实源

Writer必须读取同一API Contract Registry、Python Policy、资源registry和runtime profile；禁止
自己维护方法白名单。生成结果仍经过source gate、artifact gate、import smoke和Fake Runtime。

每段源码记录来源：`ir_node_id → generated line/span`。错误可反向定位到具体IR节点，由LLM修改
意图而不是反复猜Python语法。

### 5.5 渐进落地

1. 先覆盖高频可靠节点和本地图片分析；
2. 原始LLM写Python作为兼容路径，但走同一校验器；
3. 新脚本默认IR+Writer，复杂算法使用`pure_python`；
4. 统计Writer无法表达的真实请求，再扩展IR；
5. 不追求一次覆盖仓库全部旧脚本。

## 6. 严格契约闭环

1. Orient选择任务类型；
2. 主轮能力目录保证模型知道有哪些工具；
3. LLM声明计划使用的能力/API；
4. Runtime自动注入完整契约和返回数据模型；
5. Writer生成，或LLM兼容路径直接写源码；
6. AST提取实际调用，与声明和profile核对；
7. 基础返回类型流检查阻断`len(MatchResult)`、遍历非迭代对象等；
8. async检查阻断未await；
9. 最终材料化与冒烟验证；
10. receipt绑定契约revision、IR/source/artifact hash和素材版本。

## 7. 验收场景

1. “用A模板匹配B素材，告诉我数量”分类为`asset_analysis`，直接调用工具，不创建脚本任务；
2. 关键词搜索为空但能力目录存在时，模型仍能找到对应工具；
3. `api.get(match_image)`明确展示MatchResult不可计数/迭代；
4. `len(await match_image(...))`在本地闸门被拒；
5. 未await异步API被拒；
6. Writer能生成离线模板匹配计数脚本；
7. LLM提供纯排序/聚类算法并由Writer安全嵌入；
8. `trial_diagnosis`可以直接回答，不触发逻辑确认；
9. 决策轨迹展示任务类型和下一步，但不展示自由思维链；
10. Writer、直接Python兼容路径和API文档使用同一registry。

## 8. Writer的正式定位：断言驱动程序编辑器

Writer不是文本补全器，也不是旧式固定模板生成器。它维护一份有版本的“程序断言文档”：LLM
描述程序必须做什么、数据如何流动、算法如何选择；Writer把这些断言编译成确定性Python。

此处的assertion不是Python的`assert`语句，而是结构化程序事实、步骤和约束。修改原则固定为：

```text
插入新断言
删除旧断言
查询当前文档/生成物/诊断
```

不提供任意`update`、JSON Patch或字段路径修改。改变一个断言必须表现为“删除旧ID + 插入新
断言”，保留完整来源和修订历史。

## 9. Writer职责边界

### 9.1 LLM负责

- 理解和澄清用户目标；
- 决定算法、步骤顺序、循环、分支和停止条件；
- 选择素材、能力和参数策略；
- 描述如何筛选、排序、聚类、计数和选择结果；
- 插入/删除断言并检查Writer输出；
- 结合试运行证据调整算法和参数。

### 9.2 Writer负责

- 保存文档、断言ID、revision和墓碑历史；
- 校验断言schema、引用、类型和依赖；
- 从唯一API Contract Registry解析方法、参数、await和返回模型；
- 构建数据流和控制流；
- 生成import、入口、素材token、日志、状态、超时和异常保护；
- 输出源码、source map、诊断、满足度和凭证；
- 对不可能或互相冲突的断言明确拒绝。

Writer不得自行改变目标、发明能力、删除业务步骤、增加点击/网络/文件等副作用，也不得为了让代码
通过而猜测模型未说明的业务策略。

### 9.3 Runtime与Validator负责

Runtime提供资源/能力目录、权限、执行、遥测和事实记录；Validator独立检查Writer最终产物。
Writer的“构建成功”不是运行成功，业务结果必须由trial证据验证。

## 10. 基础编辑API（封闭集合）

### 10.1 `writer.catalog`

返回Writer版本、支持的断言kind、每种schema、操作符、输出类型、示例和限制。该API很小，每个
主轮可完整提供给LLM，不依赖语义搜索。

### 10.2 `writer.create`

```json
{
  "goal": "统计结界1中的勋章数量",
  "runtime_profile": "local_analysis"
}
```

返回`document_id/revision`。目标成为有来源的根断言，不能被普通子断言静默覆盖。

### 10.3 `writer.query`

只读查询，支持：文档快照、单条断言、依赖者、变量及类型、能力/资源清单、未满足断言、冲突、
生成代码、source map和revision历史。查询结果必须带当前revision。

### 10.4 `writer.insert`

```json
{
  "document_id": "doc_123",
  "expected_revision": 3,
  "after": "ast_resource_scene",
  "assertion": {
    "kind": "operation",
    "operation": "image.offline_template_match",
    "inputs": {
      "template": {"resource": "asset_badge@1"},
      "target": {"resource": "asset_scene@1"},
      "threshold": 0.8
    },
    "output": "badge_hits"
  }
}
```

只接受catalog中的kind/operation/schema。未知能力返回`UNKNOWN_OPERATION`，不得模糊匹配或自创。

### 10.5 `writer.delete`

按稳定`assertion_id + expected_revision`删除。存在依赖者时默认拒绝并返回依赖ID；LLM应在同一
事务中处理依赖关系。删除写墓碑，不物理抹除审计历史。

### 10.6 `writer.transact`

只允许组合`insert/delete`：

```json
{
  "document_id": "doc_123",
  "expected_revision": 12,
  "operations": [
    {"op": "delete", "assertion_id": "ast_threshold_old"},
    {"op": "insert", "after": "ast_match", "assertion": {}}
  ]
}
```

Writer先在临时revision完成schema、引用、类型、权限和依赖检查；全部成功后一次提交，任一失败则
零修改。它不是update，只是增删的原子组合。

### 10.7 `writer.build`与`writer.check`

`build`从指定revision确定性生成代码和映射，不执行；`check`运行断言一致性、契约、Python
Policy、AST、import smoke、Fake Runtime和断言满足度检查。这两者不改变文档。

## 11. 第一版断言类型

保持少而稳定，先覆盖：

- `goal`：根目标与验收结果；
- `resource`：素材ID/version、变量名、预期类型和角色；
- `operation`：调用登记能力并绑定输出；
- `compute`：纯数据变换；
- `if`：条件及then/else节点引用；
- `loop`：循环模式、body、超时和次数上限；
- `log/state`：用户可读日志和运行状态；
- `constraint`：禁止点击区域、只读、必须保留等；
- `output`：返回、文件、标注图或统计字段；
- `acceptance`：可机器验证的完成条件；
- `pure_function`：受限纯算法扩展。

断言通过稳定ID互相引用，不以内嵌任意Python源码表示控制流。插入位置用`before/after/parent`
引用稳定ID，不使用易漂移的数组下标。

## 12. 算法表达与自由度

### 12.1 标准节点

match、click、wait、loop、branch、log、state、return等高频节点由Writer稳定生成。

### 12.2 组合计算

提供`map/filter/sort/group/min/max/count`以及距离、IoU、聚类、窗口统计等受控表达式。类型由输入
契约推导，禁止对`MatchResult`使用`count_of`，只允许对list/set等Sized结果使用。

### 12.3 `pure_function`逃生舱

LLM可提供复杂纯Python算法，必须声明输入输出类型并附测试：

```json
{
  "kind": "pure_function",
  "name": "choose_best_opponent",
  "inputs": {"candidates": "list[Candidate]"},
  "returns": "Candidate | None",
  "python": "def choose_best_opponent(candidates): ...",
  "tests": [{"input": [], "expected": null}]
}
```

禁止访问browser、Runtime、文件、网络、进程、动态import及全局可变状态；只允许批准的数学、集合
和数据处理能力。AST安全检查与测试通过后由Writer嵌入。Writer无法表达时返回
`IR_UNSUPPORTED_OPERATION`，不得偷偷降级或让模型绕过契约调用底层对象。

## 13. Writer输出协议

每次build返回：

1. `document_id/revision/assertions`文档快照；
2. `source/source_sha256`生成源码；
3. `source_map`：assertion ID到代码行/AST节点；
4. `diagnostics`：code、severity、assertion ID、证据和修复约束；
5. `satisfied/unsatisfied/conflicts`断言满足度；
6. `variables`及推导类型；
7. 使用的`capabilities/resources/contracts`及版本；
8. materialize后的artifact和validation receipt（完成check之后）；
9. `determinism_key`：相同文档revision+registry revision必须生成相同源码。

典型诊断：

```json
{
  "code": "TYPE_NOT_SIZED",
  "assertion_id": "ast_count_1",
  "message": "MatchResult不支持count_of",
  "producer_assertion_id": "ast_match_1",
  "actual_type": "MatchResult",
  "allowed_operations": ["truth_test", "field_access"]
}
```

LLM据此修改相关断言，不全文重写Python。

## 14. 完整编辑循环

```text
LLM读取writer.catalog和主轮能力目录
→ 查询素材/能力完整schema
→ writer.create
→ insert/transact提交一组断言
→ writer.build
→ LLM检查源码摘要、source map、诊断和未满足断言
→ delete旧断言 + insert新断言（原子事务）
→ writer.check
→ materialize + validation receipt
→ 试运行
→ LLM读取结构化运行证据
→ 继续增删断言或完成
```

每次LLM动作只引用当前`document_id/revision`。revision冲突返回`REVISION_CONFLICT`并附最新摘要，
模型必须重新query，不能覆盖别的修改。

## 15. 示例：修复错误的MatchResult计数

若原断言使用单结果`runtime.match_image → MatchResult`，随后对其`count_of`，Writer不生成错误代码，
而是返回`TYPE_NOT_SIZED`。LLM通过事务：

1. 删除产生单个MatchResult的操作断言；
2. 删除依赖它的计数断言；
3. 插入返回`list[Match]`的`image.offline_template_match`；
4. 插入对新列表的count输出。

事务通过后才产生新revision；失败时旧的可查询文档保持完整。

## 16. Writer安全与一致性不变量

- Writer API、断言kind和operation是封闭集合；
- operation只能引用当前能力目录的精确ID/version；
- Writer、文档、Validator不得各维护一份API白名单；
- 文档修改只有insert/delete组成，且带expected_revision；
- 删除保留墓碑和来源；
- 生成确定且有source map；
- Writer不得执行真实外部副作用；
- pure_function无外部能力；
- Writer构建成功不等于试运行成功；
- 最终产物仍必须经过独立Validator；
- LLM不能直接修改生成源码后复用旧receipt；
- 用户补充导致目标变化时终止旧revision驱动的未来计划，再进行兼容性评估。

## 17. Writer专项验收

1. 不存在`writer.update`，未知动作被拒；
2. 原子delete+insert任一失败时文档revision不变；
3. 删除有依赖者的断言给出稳定依赖ID；
4. 未知operation不被模糊映射；
5. 相同revision与registry revision生成字节一致源码；
6. `MatchResult`不能count/遍历，`list[Match]`可以；
7. Writer自动生成正确await与参数；
8. source map可从错误代码反查断言；
9. pure_function能执行纯排序/聚类且无法访问文件/browser；
10. revision冲突不会覆盖新修改；
11. Writer代码经过现有source/artifact/import/Fake Runtime闸门；
12. 试运行反馈可以定位到IR断言并通过增删完成修订。

## 18. 方向收敛：完整Writer暂缓，优先解决本地API幻觉

进一步讨论后确认：完整断言Writer并没有消除“LLM遵守契约”的问题，而是可能把原来的Python
API契约换成一套新的Writer IR契约。若让模型手写复杂断言树，会同时引入DSL学习成本、编译器
维护成本和新的表达偏差。

因此本方案调整优先级：

1. 第8—17节保留为远期设计储备，不作为近期必须实现范围；
2. 暂不实现完整断言语言、控制流编译器和Writer类型系统；
3. LLM继续使用普通Python表达算法和业务逻辑；
4. 近期首先实现“本地API契约凭据 + 源码调用对账”；
5. 只有真实数据证明大量故障无法通过契约凭据、AST和数据流检查解决时，才逐步引入少量IR节点。

这项收敛不否定Writer价值。Writer可先退化为薄的源码工作区和局部编辑器，但不能成为第二套API
事实源，也不能要求模型先掌握一门自创语言。

## 19. 本地API契约凭据

### 19.1 目标边界

契约凭据只解决一类问题：LLM对本地架构、对象、profile、方法、参数、异步属性和返回模型产生
幻觉，或者依据旧记忆使用已经失效的API。

凭据证明：

- API真实存在并已在唯一Contract Registry登记；
- 方法可以从当前Runtime接收者解析到；
- 当前profile批准该方法；
- LLM在本主轮取得了对应的完整契约；
- 源码调用形式与该契约一致；
- 契约版本、资源范围和权限作用域仍然有效。

凭据不证明：

- 算法符合用户需求；
- 素材或模板选择正确；
- 返回值被合理组合成业务逻辑；
- 点击位置和阈值适合真实界面；
- 真机运行一定成功；
- 最终业务结果正确。

因此凭据是“架构真实性闸”，不是“脚本合格证”。需求正确性继续由逻辑确认、验收条件和试运行
证据负责。

### 19.2 本地签发，不由LLM填写

LLM不能自己填写或修改凭据。标准流程是：

```text
主轮提供完整紧凑能力索引
→ LLM请求某项能力的完整契约
→ 本地按当前Runtime事实、profile和Registry解析
→ 本地签发contract_ticket
→ LLM依据契约编写Python
→ 交付时本地从源码AST提取实际调用
→ 实际调用与contract_ticket及Registry三方对账
→ 对账通过后进入类型、控制流和运行前检查
```

本地至少记录三类事件：

- `CONTRACT_VIEWED`：模型取得过完整契约；
- `CALL_AUTHORIZED`：本地批准在指定作用域使用该能力；
- `CALL_EXTRACTED`：交付源码中实际提取到该调用点。

“看过契约”和“获准产生副作用”必须分开；读取API说明不等于取得点击、写文件、删除等执行权限。

### 19.3 什么调用需要凭据

不是简单按“父类属于本项目”判断，而是按接收者来源和最终解析结果判断：

> 当调用接收者能够追溯到Runtime注入对象或登记的本地能力，且最终解析的方法存在于当前
> Contract Registry时，该调用必须匹配本轮有效凭据。

解析过程：

1. 追踪接收者来源，例如入口参数`UserWindow`、`UserBrowser`或登记Runtime对象；
2. 支持简单别名传播，例如`bb = browser`，重新赋值后旧来源失效；
3. 按真实MRO、Mixin和登记的`__getattr__`代理链解析最终方法；
4. 子类覆盖、实例属性遮蔽和profile同名异签名均按实际解析结果处理；
5. 只有Registry中`approved=true`的方法可以签发凭据；
6. 动态`getattr`、monkey patch或无法确定接收者的Runtime调用默认拒绝，不能猜测放行。

调用分三类：

- Runtime及本地登记能力：必须有契约凭据；
- 批准的Python内置函数和标准库：由Python Policy统一裁决，不逐调用签发；
- 本次交付源码内定义的纯函数：经过AST和安全检查，不需要本地API凭据。

### 19.4 凭据结构

```json
{
  "ticket_id": "ct_01JXYZ",
  "main_turn_id": "turn_...",
  "contract_revision": "sha256:...",
  "capability_id": "runtime.image.match_one",
  "receiver_type": "UserWindow",
  "requested_method": "match_image",
  "resolved_owner": "UserWindow",
  "resolution_chain": ["UserWindow.match_image"],
  "runtime_profile": "desktop_user_window",
  "call_kind": "async",
  "parameters": {
    "required": ["img_path"],
    "allowed": ["img_path", "threshold", "pianyi"]
  },
  "return_type": "MatchResult",
  "permission_scope": ["screen.read"],
  "resource_scope": ["asset_badge@3"],
  "environment_revision": "env_...",
  "signature": "local-mac-or-equivalent"
}
```

代理方法必须记录完整解析链，例如：

```text
UserWindow.__getattr__ → Win32Target.click
```

凭据由本地存储并签名或摘要保护，LLM只能引用`ticket_id`，不能提供凭据事实。

### 19.5 交付时的确定性对账

本地从最终交付源码AST自动提取调用点，而不是相信模型的调用清单。每个本地调用点必须映射到
当前主轮的一张有效凭据。

以下情况确定为交付错误并拒绝：

- `UNREGISTERED_API_CALL`：源码调用本地能力但没有凭据；
- `CALL_TICKET_CONTRACT_MISMATCH`：方法、接收者或解析链不符；
- `PROFILE_CHANGED`：当前执行profile与凭据不符；
- `CONTRACT_REVISION_EXPIRED`：Registry版本已变化；
- `ARGUMENT_CONTRACT_MISMATCH`：实际参数超出或不满足契约；
- `CALL_RECEIVER_UNRESOLVED`：无法证明接收者是哪个Runtime对象；
- `PERMISSION_SCOPE_MISMATCH`：调用所需副作用超出授权；
- `RESOURCE_SCOPE_MISMATCH`：实际素材或目标超出凭据范围。

有凭据但最终没有使用通常不是错误，只记录为`unused_ticket`。若该能力属于用户已确认的必要步骤，
再由需求验收报告为步骤缺失。

调用对账通过也不能放行返回值误用。例如`match_image`凭据合法，但对返回的`MatchResult`执行
`len()`仍应由返回类型和数据流检查报`TYPE_NOT_SIZED`。

### 19.6 有效期与失效

契约凭据默认绑定：

- `main_turn_id`；
- `contract_revision`；
- `runtime_profile`；
- `environment_revision`；
- 能力、权限和资源作用域。

环境、执行目标、profile、Registry、素材版本或授权发生变化时，尚未开始的调用必须重新核验。
用户在主轮中补充导致Runtime产生新快照时，旧快照驱动的未来计划停止；兼容的凭据可经本地重新
核验后续用，不兼容凭据失效。已经执行并留下事实证据的事件保留，但不能被当成未来调用授权。

源码发生变化后，旧交付对账和validation receipt失效，必须重新提取调用和校验。

### 19.7 调用点凭据与执行事件分离

循环中的同一调用执行一百次，不签发一百张静态凭据：

- 调用点凭据证明“这处源码允许调用什么”；
- 执行事件记录“实际调用了几次、输入输出、耗时和异常是什么”。

两者通过`ticket_id + source_span/source_node_id`关联。异常模式库和试运行诊断读取执行事件，而不是
把动态结果塞回契约凭据。

### 19.8 与最终产物绑定

Receipt必须绑定：

- 原始source hash；
- materialized artifact hash；
- 调用点到ticket的映射；
- Contract Registry revision；
- runtime profile和profile来源；
- 资源版本与权限快照；
- materializer version。

运行器只接受与receipt中artifact hash完全一致的文本，避免“校验了一份、实际运行另一份”。

## 20. 三层质量结论

界面和协议不得再用一个含糊的“脚本合格”覆盖所有结论，应分别显示：

### 20.1 架构真实性

由契约凭据对账负责：

```text
API调用对账：通过 / 拒绝
```

它回答“是否编造、混淆或使用过期的本地API”。

### 20.2 代码可运行性

由语法、AST、参数、await、返回类型、数据流、Python Policy、import smoke和Fake Runtime负责：

```text
运行前检查：通过 / 拒绝 / 有警告
```

它回答“是否存在能够在运行前证明的代码错误”。

### 20.3 需求符合性

由用户目标、确认后的逻辑、验收条件和真机试运行证据负责：

```text
需求验收：待验证 / 证据不足 / 已通过 / 未通过
```

它回答“脚本是不是用户真正需要的、运行后是否达成目标”。凭据系统不得越权把这一层判为通过。

## 21. 契约凭据专项验收

1. LLM无法自行构造或修改有效ticket；
2. 调用不存在的本地方法因无凭据而被拒；
3. `UserWindow`与`UserBrowser`同名异签名能按profile正确区分；
4. 登记的`__getattr__`代理链可以正确解析并写入ticket；
5. 子类覆盖同名方法时不能误用父类凭据；
6. `bb = browser`可以继承来源，`bb = other`后旧来源立即失效；
7. 动态`getattr(browser, name)`不能绕过凭据对账；
8. 内部未批准方法即使定义在本地类中也不能取得凭据；
9. 无凭据调用、profile漂移和参数漂移分别返回稳定诊断码；
10. 未使用ticket不会误报为非法调用；
11. 合法`match_image`凭据不能掩盖`len(MatchResult)`类型错误；
12. Registry、环境、素材或权限版本变化能使相关ticket失效；
13. 循环执行多次只复用一张调用点凭据，同时产生多条执行事件；
14. 源码或materialized artifact变化后旧receipt不能继续使用；
15. “API调用对账通过”不会被展示成“需求验收通过”。
