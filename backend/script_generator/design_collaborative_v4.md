# Script Generator 协作 Agent V4 架构方案书

> 文档状态：原型实施中；本文同时保存设计决定、实际落地状态和后续缺口  
> 版本：0.2  
> 日期：2026-10-06  
> 适用范围：协作式需求澄清、素材分析、工具执行、脚本设计、生成、校验与试运行修订  
> 核心定位：由本地 Runtime 完整掌握会话、主轮、副轮、工具发现、工具执行、事实、权限、缓存与提交；LLM 只负责在当前主轮内提出结构化的下一步决策  
> 与 V2/V3 的关系：V4 建立独立协议和运行内核；初期可复用底层确定性能力，但不继承 V2/V3 的编排语义

V4 独立系统提示词草案：`collaborator_v4/prompts/system_prompt.md`。

---

## 1. 背景与本次决定

V4 从以下讨论结论出发：

1. 一个产品会话包含多个主轮。
2. 用户发送一次需求，到系统给出一次最终可见回复，是一个主轮。
3. 一个主轮内部，Runtime 与 LLM 的一次请求和响应，是一个副轮。
4. 每个主轮拥有一个独立的 LLM 逻辑上下文；主轮内的副轮采用追加式上下文。
5. 新主轮不自动继承上一主轮的 LLM 自由文本、推理草稿和原始工具流水。
6. 跨主轮需要保留的任务、资源、事实、证据和权限由 Runtime 持久化，并由新主轮按需查询。
7. 业务工具不在主轮开始时完整暴露。LLM 先查询精简目录，选定后再取得完整、带版本的调用契约。
8. LLM 只能提出动作；Runtime 负责校验、授权、执行、记账、缓存、完成判定和提交。
9. V4 不依赖模型厂商的原生 function calling。模型传输层只要求能够收发文本；所有动作统一使用 V4 JSON 协议。

上述第 4、7、8、9 项是 V4 相对当前实现最重要的边界。

---

## 2. 建设目标

### 2.1 目标

- 完整掌握模型上下文，不把 Agent 行为绑定到任一厂商的工具调用格式。
- 每个主轮具有清晰、可审计、可暂停和可恢复的事务边界。
- 主轮内通过追加式副轮获得稳定语义和较好的 Prompt/KV Cache 命中。
- 工具按需发现，避免把全部工具 schema 塞入每次模型请求。
- 工具调用严格绑定能力版本、参数 schema、权限和资源版本。
- Runtime 对所有事实、资源、证据和完成状态拥有最终解释权。
- 模型、工具或协议出错时，失败停留在当前主轮，不污染长期状态。
- 可逐步复用现有视觉、素材、脚本校验和试运行能力，又不受其旧编排逻辑约束。

### 2.2 非目标

- 首版不实现多 Agent 自由协商。
- 不保存或展示模型隐藏推理。
- 不把聊天历史当作事实数据库。
- 不让 LLM 直接访问文件、窗口、数据库或 Python 函数。
- 不要求所有工具在 V4 首版一次迁移完成。
- 不在 V2/V3 主循环上继续追加兼容分支来模拟 V4。

---

## 3. 运行单位

### 3.1 会话 Session

用户看到的长期协作空间。会话包含多个主轮、一个当前任务引用和可展示的聊天记录。

会话不是 LLM 的连续上下文。关闭、重启或更换模型后，只要本地存储仍在，会话事实仍然存在。

### 3.2 主轮 Main Turn

一次用户输入到一次最终用户可见结果之间的完整事务。

每个主轮创建：

- 唯一 `main_turn_id`；
- 独立的 LLM 上下文；
- 固定的模型、系统协议和工具内核版本；
- 主轮工作区；
- 副轮序列；
- 预算、快照和提交候选。

同一个主轮暂停后恢复，`main_turn_id` 不变；新的执行段使用新的 `run_id`，副轮序号继续递增。

### 3.3 副轮 Subturn

Runtime 向 LLM 发出一次请求并收到一个动作响应，记为一个副轮。

主轮内上下文采用追加式记录：

```text
固定 system protocol
主轮初始输入
LLM 动作 1
Runtime 结果 1
LLM 动作 2
Runtime 结果 2
...
```

Runtime结果使用结构化、裁剪后的事实，不回灌大段日志或内部对象。

### 3.4 动作 Action

LLM 在一个副轮中只能提出一个顶层动作。首版动作集合：

```text
respond               直接回答并结束主轮
request_user          请求用户补充、选择或授权
query_runtime         查询本地事实、资源或能力目录
select_capability     选择一个候选能力并申请完整契约
invoke_capability     按已取得的契约提交工具参数
update_plan           更新本主轮内部计划
propose_change        提议需要提交的任务、资源或脚本变化
validate_artifact     请求本地校验确定产物
finish                提交最终答复与完成声明
```

一个响应不能同时调用工具、请求用户和宣称完成。

### 3.5 主轮工作区 Turn Workspace

只属于当前主轮的临时状态：

- 原始用户请求及补充 revision；
- 当前目标和成功条件；
- 查询结果；
- 当前可用的资源句柄；
- 已取得的能力摘要和完整契约票据；
- 工具执行结果与证据引用；
- 临时计划；
- 待提交变更；
- 预算、失败计数和熔断状态。

工作区不是长期事实源。主轮结束时只提交通过验证的部分。

---

## 4. 总体架构

```text
GUI / 用户
    │
    ▼
Conversation Service
    │ 创建主轮、展示最终回复
    ▼
V4 Main Turn Runtime
    ├─ Context Manager        主轮内追加式模型上下文
    ├─ Protocol Validator     校验一个副轮只能有一个动作
    ├─ Query Gateway          查询本地任务、资源、事实和能力
    ├─ Capability Registry    工具目录、版本和完整契约
    ├─ Permission Engine      授权与作用域
    ├─ Capability Executor    真正执行本地工具
    ├─ Evidence Store         保存可验证证据
    ├─ Cache Manager          Prompt布局与Runtime结果缓存
    ├─ Completion Gate        完成声明与产物验证
    ├─ Committer              原子提交长期变化
    └─ Trace Store            副轮和动作审计
            │
            ▼
       LLM Provider
       只收文本消息，只回V4 JSON动作
```

LLM Provider 是可替换适配器，不拥有工具执行权，也不是状态存储。

---

## 5. 一个主轮的完整流程

### 5.1 启动

用户发送需求后，Runtime：

1. 创建 `main_turn_id` 和工作区；
2. 固定本轮模型与协议版本；
3. 记录用户原话，不由模型改写覆盖；
4. 读取最小任务索引，但不自动展开历史；
5. 创建主轮内的追加式消息上下文；
6. 发起第一个副轮。

V4 中的 LLM 是本主轮的调度员兼需求分析者。Runtime 不预先替模型决定必须查什么或必须调用哪个工具；Runtime 负责给出当前合法菜单、执行所选动作并反馈确定结果。

主轮采用两阶段循环：

```text
阶段 A：需求分析
  Runtime 发需求分析表
  → LLM 填目标、交付物、未知项和成功条件
  → Runtime 校验并建立本轮目标契约

阶段 B：行动循环
  Runtime 发下一步菜单
  → LLM 每次选择一个动作
  → Runtime 校验、执行或记录
  → Runtime 把结果写入下一张菜单
  → 直到等待用户，或最终回复候选通过提交闸门
```

### 5.2 第一个副轮输入

```json
{
  "protocol_version": 1,
  "main_turn_id": "mt_...",
  "user_request": "看看素材区里有几张包含勋章",
  "task_index": {
    "task_id": "task_...",
    "revision": 4,
    "available_projections": [
      "current_resources",
      "latest_script",
      "latest_trial"
    ]
  },
  "runtime_limits": {
    "remaining_subturns": 15,
    "remaining_tool_calls": 10
  }
}
```

动态 ID 和预算位于固定系统协议之后，以保护公共 Prompt 前缀缓存。

### 5.3 需求澄清或直接回答

如果缺少会改变结果的关键信息：

```json
{
  "type": "request_user",
  "reason": "material_ambiguity",
  "question": "你要统计任意勋章，还是只统计绿色勋章？",
  "choices": ["任意勋章", "绿色勋章"]
}
```

如果不需要本地事实即可可靠回答：

```json
{
  "type": "respond",
  "text": "……"
}
```

需求分析完成后，Runtime 不立即把全部能力 schema 发送给模型，而是生成当前状态下的行动菜单。基础菜单至少可以包含：

```text
query_local          查询本地任务、资源、工具、脚本或运行记录
use_local_capability 调用已经取得完整契约的本地能力
script_workflow      进入脚本设计、生成、校验或试运行处理
self_continue        本地不做事，由模型使用现有信息或自身能力继续处理
ask_user             继续问清会影响结果的关键需求
submit_response      提交最终回复候选
```

菜单由 Runtime 根据状态裁剪。例如尚未取得任何 L2 能力契约时，不提供可直接执行的 `use_local_capability`；已经存在足够事实时可以提供 `submit_response`。

菜单项不能只有动作 id 或短标签。Runtime 必须给每个选项发送完整语义边界：

```json
{
  "id": "query_local",
  "label": "查询本地信息",
  "meaning": "只读取 Runtime 管理的任务、资源、能力、脚本或运行记录，不执行业务副作用",
  "use_when": [
    "完成当前目标需要本地事实",
    "需要寻找尚未取得契约的本地能力"
  ],
  "do_not_use_when": [
    "已有有效查询结果",
    "仅需整理当前上下文即可回答",
    "准备执行已经取得契约的能力"
  ],
  "required_inputs": ["query_name", "arguments"],
  "side_effect": "none",
  "expected_result": "Runtime 返回结构化查询结果并刷新行动菜单"
}
```

所有菜单项至少包含：

- 稳定且不可本地化的机器 `id`；
- 面向模型阅读的语义说明；
- `use_when` 与 `do_not_use_when`；
- 必填输入；
- 副作用和权限；
- 成功后 Runtime 会返回什么；
- 与相邻选项最容易混淆的边界。

LLM 必须按语义边界选择，不能从英文 id 猜功能。若 Runtime 无法为两个选项给出互斥或可判别的边界，应先修菜单设计，不把歧义交给提示词兜底。

`self_continue` 的含义是“本地本副轮不执行查询或业务工具”。它允许模型整理已有信息、进行推导，或使用供应商在当前模型调用中已经允许的模型侧能力。任何由此产生的外部事实仍必须声明来源，并经过 Runtime 的证据和完成规则；不能因为是模型自身能力就自动成为本地事实。

### 5.4 查询精简能力目录

需要工具时，LLM先描述能力需求：

```json
{
  "type": "query_runtime",
  "query": "capability.search",
  "arguments": {
    "kind": "tool",
    "purpose": "检查多张素材中是否包含勋章"
  }
}
```

Runtime返回 L1 精简目录：

```json
{
  "query": "capability.search",
  "ok": true,
  "candidates": [
    {
      "capability_id": "cap_compare_assets",
      "version": "sha256:...",
      "name": "compare_assets",
      "purpose": "联合查看并比较多张素材",
      "input_summary": ["asset_names", "question", "target"],
      "output_summary": ["per_asset_observations", "conclusion", "evidence_refs"],
      "required": ["asset_names", "question"],
      "side_effect": "none",
      "permission_scope": "read_assets"
    }
  ]
}
```

L1 必须提前披露副作用和权限范围，不能只给名称。

### 5.5 选择能力并取得完整契约

LLM选择候选：

```json
{
  "type": "select_capability",
  "capability_id": "cap_compare_assets",
  "version": "sha256:..."
}
```

Runtime校验能力仍有效，然后返回 L2 完整契约和短期票据：

```json
{
  "capability_id": "cap_compare_assets",
  "version": "sha256:...",
  "ticket_id": "ticket_...",
  "input_schema": {
    "type": "object",
    "properties": {
      "asset_names": {
        "type": "array",
        "items": {"type": "string"}
      },
      "question": {"type": "string"},
      "target": {"type": "string"}
    },
    "required": ["asset_names", "question"]
  },
  "output_schema": {
    "type": "object"
  },
  "permission_scope": "read_assets",
  "side_effect": "none",
  "expires_at_subturn": 8
}
```

### 5.6 提交工具调用

```json
{
  "type": "invoke_capability",
  "ticket_id": "ticket_...",
  "arguments": {
    "asset_names": ["结界1", "结界2"],
    "question": "逐张判断是否包含勋章并统计图片数量",
    "target": "勋章"
  }
}
```

Runtime执行前必须校验：

- ticket 属于当前主轮且未过期；
- capability id、版本和 schema 未变化；
- 参数通过 JSON Schema；
- 资源名称能在当前作用域唯一解析；
- 所需权限有效；
- 调用未触发重复失败、无进展或预算闸门；
- 副作用与当前用户授权一致。

通过后才调用本地执行器。

### 5.7 返回工具结果

工具结果作为 Runtime 消息追加到同一个主轮上下文：

```json
{
  "type": "capability_result",
  "call_id": "call_...",
  "ticket_id": "ticket_...",
  "ok": true,
  "result": {
    "matched_assets": ["结界1"],
    "count": 1
  },
  "evidence_refs": ["ev_..."],
  "workspace_revision": 6
}
```

LLM下一副轮可以继续查询、选择能力、调用工具或申请完成。

### 5.8 完成与提交

LLM返回：

```json
{
  "type": "finish",
  "final_response": {
    "text": "检查完成：2 张素材中有 1 张包含勋章。"
  },
  "claims": [
    {
      "text": "2 张素材中有 1 张包含勋章",
      "evidence_refs": ["ev_..."]
    }
  ],
  "proposed_commit_refs": []
}
```

Runtime经 Completion Gate 核对证据、产物和成功条件。通过后原子提交并向用户显示回复；不通过则返回结构化缺口，主轮继续，不要求用户发送“继续”。

---

## 6. 工具暴露分层

### 6.1 L0：能力类别

长期稳定、可放入 system protocol：

```text
resources、vision、annotation、runtime、script、validation、trial
```

只用于帮助 LLM形成查询，不包含业务工具事实。

### 6.2 L1：精简目录

由 `capability.search` 按需返回：

- id 和版本；
- 名称与用途；
- 输入、输出摘要；
- 必填参数名；
- 副作用；
- 权限范围；
- 是否可批量。

L1 不能被用来执行工具。

### 6.3 L2：完整调用契约

由 `select_capability` 返回：

- 完整 input/output schema；
- 精确版本；
- 资源参数语义；
- 权限和副作用；
- 超时、幂等和重试策略；
- ticket 与有效期。

只有持有有效 L2 ticket 才能 `invoke_capability`。

### 6.4 同主轮复用

同一主轮内，同版本 L2 契约可复用，不必反复搜索和选择。以下情况立即失效：

- 能力版本改变；
- 权限被撤销；
- 执行目标改变；
- 资源作用域改变且会影响参数解析；
- 主轮结束。

---

## 7. 模型上下文与缓存

### 7.1 主轮内 Prompt/KV Cache

主轮内副轮必须追加消息，禁止每轮重新生成一份内容次序不同的大摘要来替换历史。

稳定前缀顺序：

```text
固定 system protocol
固定核心动作 schema
固定 Runtime 查询内核说明
主轮初始输入
后续追加的动作与结果
```

这样副轮 N+1 的输入以前一副轮为前缀，有利于服务商 Prompt/KV Cache 命中。

### 7.2 跨主轮 Prompt Cache

不同主轮不共享动态模型上下文，但可以共享完全一致的固定前缀：

- system protocol；
- 核心动作 schema；
- Runtime查询内核说明。

时间戳、随机 ID、预算和动态状态必须放在固定前缀之后。

### 7.3 Runtime 查询缓存

查询缓存与模型缓存分离。建议键：

```text
query_name
+ canonical_arguments
+ task_revision
+ resource_revision_or_hash
+ display_context_revision（仅 Runtime 内部，用于“这张图”指代失效；不暴露给模型）
+ capability_registry_revision
+ relevant_mutation_epoch
```

相同键直接回放，包括空结果。相关 revision 或 epoch 变化后失效。

### 7.4 工具结果缓存

仅对明确声明为纯查询、幂等且结果可指纹化的能力启用。带副作用的能力不能因参数相同而自动跳过执行。

视觉结果至少绑定：

```text
content_hash + task_kind + normalized_question + backend_version
```

跨主轮可复用的是经过验证的 Runtime 证据，不是上一主轮 LLM 的自由文本结论。

### 7.5 副轮预算（首版暂定）

V4 首版每个主轮设置 **50 个 LLM 副轮的绝对上限**，不提供无限模式。凡实际向 LLM 发起一次请求，无论其结果是查询、缓存回放、协议修复、动作拒绝还是最终提交，都消耗一个副轮。

查询可以不消耗“业务执行预算”，但绝不能反向增加 LLM 副轮绝对上限。总预算与分类预算必须分开：

```text
llm_subturn_hard_limit = 50
query_attempt_budget   = 独立计数
tool_execution_budget  = 独立计数
protocol_repair_budget = 独立计数
finish_repair_budget   = 独立计数
```

50 只是灾难保护，不是允许模型空转到第 50 次。Runtime 必须更早执行重复查询与无进展熔断，并至少为最终提交/错误收口预留副轮。

建议首版预留：当剩余副轮少于 5 时，只允许 `ask_user`、`submit_response`、明确的缺口修复或保存 checkpoint；不得开启新的开放式能力搜索。

---

## 8. 状态所有权

| 信息 | 权威所有者 | 是否跨主轮 |
| --- | --- | --- |
| 用户原始消息 | Conversation Store | 是 |
| 主轮内消息上下文 | Context Manager | 否；审计副本可保留 |
| LLM计划 | Turn Workspace | 默认否 |
| 任务目标与约束 | Task Store，经提交后 | 是 |
| 素材与版本 | Resource Store | 是 |
| 工具目录与 schema | Capability Registry | 是，按版本 |
| 工具选择和参数 | Trace Store | 审计保留，不作为事实 |
| 工具执行结果 | Workspace / Evidence Store | 验证后可跨主轮 |
| 权限 | Permission Engine | 按作用域和有效期 |
| 完成状态 | Completion Gate + Committer | 是 |
| 用户可见回复 | Conversation Store | 是 |

LLM输出永远是提案，不直接成为长期事实。

---

## 9. Runtime 状态机

```text
CREATED
  → ANALYZING
  → QUERYING / SELECTING_CAPABILITY / EXECUTING
  → VALIDATING
  → COMMITTING
  → COMPLETED

可中断出口：
  → WAITING_USER
  → PAUSED_BUDGET
  → FAILED
  → CANCELLED
```

主轮只有以下合法用户出口：

1. 已完成并通过验证；
2. 确实需要用户信息、选择或授权；
3. 达到预算，保存可恢复 checkpoint；
4. 不可恢复错误，给出具体诊断。

中间步骤完成、一次工具失败、模型说“稍后继续”都不是结束主轮的理由。

---

## 10. 失败、重试与无进展

每次动作生成规范化签名：

```text
action_type + target_id + version + canonical_arguments + relevant_revision
```

Runtime记录：

- 连续相同动作次数；
- 相同输入下的失败码；
- 工作区 revision 是否变化；
- 是否新增资源、证据或已验证事实。

同签名重复且无进展时：

1. 第一次返回精确修复建议；
2. 第二次要求改参数、换能力或请求用户；
3. 达到阈值后熔断该调用，在本主轮内禁止原样重试；
4. 不把“继续”按钮当作清空失败计数的手段。

### 10.1 重复查询的三层判定

只比较原始 JSON 不够。V4 同时维护三种重复键：

1. **精确查询键**：`query_name + canonical_arguments + relevant_revision`。
2. **语义目的键**：当前步骤、要填补的 unknown、资源作用域和期望结果；防止模型只换关键词重复搜索同一件事。
3. **进展键**：任务、资源、能力目录、证据和工作区 revision 是否发生变化。

处理规则暂定：

- 相同精确查询键且 revision 未变：直接回放一次缓存，并从下一张菜单移除该查询；
- 同一语义目的连续 2 次没有新增候选、事实或证据：不再允许继续改写关键词搜索，菜单只提供换路线、询问用户、如实收口；
- 同一状态 revision 下连续 3 个 LLM 副轮没有任何进展：进入无进展修复页；
- 修复后仍连续 2 次无进展：保存 checkpoint 并确定性收口，不继续消耗到硬上限。

空结果是有效结果，必须进入工作区。只有相关 registry/resource/task revision 改变后，才允许相同语义目的重新查询。

### 10.2 V3 实况空转案例（供 V4 防回归）

主轮 `mt_20261002T101400Z_E1C293` 的用户目标是“结界1里有多少勋章”。该主轮共 30 个副轮，其中 28 个是 query，最后因 budget 暂停。

实况链条：

1. 首次上下文查询成功，找到“结界1”和“勋章”两个素材。
2. 模型直接调用不存在的 `cap_v2_count_objects`，Runtime 返回 `CAPABILITY_NOT_FOUND`。
3. `capability.search("匹配 计数 模板 图像")` 实际返回 `capabilities=[] / returned=0`。
4. 图状态已经进入 `acquire_evidence`，该节点只允许证据查询；模型仍不断提交 `capability.search`、`api.get_many`、`query.catalog` 等发现类查询。
5. Runtime 每次以 `GRAPH_EVIDENCE_QUERY_NOT_ALLOWED` 拒绝，并提示回到 `discover_context`；恢复边又允许模型再次搜索，因此在两个节点间摆动。
6. 间歇穿插的 `resource.get` 只是回放同两项缓存，重复次数达到 5，没有新增事实。
7. 查询副轮会扩充总副轮上限；被图提前拒绝的 query 又没有计入已执行查询预算，导致分类预算无法及时熔断，最终只能等待总预算耗尽。

V4 必须防止这四类结构性原因：空能力搜索没有形成“搜索已穷尽”事实；状态机返回恢复节点但下一张菜单仍暴露原错误选项；等价查询只按参数而未按语义目的去重；分类预算能够扩充绝对副轮上限。

---

## 11. 安全与权限

- 工具发现不授予执行权限。
- L2 契约票据不等于权限票据。
- Runtime执行前必须同时验证能力 ticket 和 permission grant。
- 写文件、控制窗口、发送外部消息等副作用必须具有精确作用域。
- 所有资源参数在 Runtime解析，LLM不能直接把任意本地路径当作可信资源。
- 工具结果先经过输出 schema、大小限制和异常哨兵，再进入模型上下文。
- 来自文件、日志、网页、图片 OCR 和工具结果的文本全部视为数据，不得覆盖 system protocol。

### 11.1 调用表不合法时的有限修复

LLM 返回的调用表必须先通过 JSON 解析、当前页面 schema、枚举/互斥条件、ticket、能力版本和资源绑定校验，未通过时绝不执行工具。

Runtime 返回同一张表及机器可读错误：

```json
{
  "screen": "invoke_capability",
  "repair": {
    "attempt": 1,
    "max_attempts": 3,
    "errors": [
      {
        "path": "arguments.asset_names",
        "code": "REQUIRED_FIELD_MISSING",
        "meaning": "asset_names 是必填数组，必须至少包含一个已解析素材名"
      }
    ]
  }
}
```

首版同一页面连续最多修复 3 次。第三次仍不合法时：

1. 不执行任何本地动作；
2. 暂停当前主轮，而不是继续自由重试；
3. 保存 checkpoint；
4. 保存原始模型输出、表单/schema版本、校验错误、provider/model、主轮/副轮 id、当前状态 revision 和上下文哈希；
5. 向用户显示简短的协议错误，不要求用户代替模型修参数；
6. 供开发者事后判断是表单歧义、模型不遵循、适配器解析还是状态机错误。

协议修复次数包含在 50 次 LLM 副轮绝对上限内。

### 11.2 能力查询与源码披露

业务能力只能通过 Capability Query Gateway 发现，不允许模型凭 system prompt、旧主轮记忆或猜测直接调用能力。

L1 工具箱条目必须包含：

- 精确 capability id、版本、名称；
- 输入摘要和输出摘要；
- 实际作用，而不只是营销式描述；
- 适用条件与禁用条件；
- 副作用、权限、成本等级；
- 成功结果、常见失败码和是否产生证据/资源；
- 是否支持批量、是否幂等、是否可缓存。

模型选定候选后，可继续申请：

```text
capability.get_contract   完整输入/输出 schema 与调用 ticket
capability.get_source     实现源码或相关代码片段
```

源码按需披露，不默认塞进工具箱。Runtime 应优先返回直接相关的入口函数、辅助函数和版本哈希；小文件可返回完整源码，大文件按符号/行区间分页。源码必须去除密钥、绝对私有路径和无关配置，并标记为不可信数据；调用时仍以 Registry 契约而非源码推断为准。

源码 revision 与 capability version 绑定。源码变化后旧理解和旧 ticket 同时失效。

#### 查询器的数据作用域

查询器读取用户数据时，作用域固定为**当前产品会话**：

- 当前会话中用户发送、导入、粘贴或截取的素材；
- 当前会话中本地工具、临时脚本或模型侧能力产生并已登记的资源；
- 当前会话的用户消息、已提交事实、证据、计划、脚本和运行记录；
- 用户明确从外部选择并挂入当前会话的资源。

默认禁止：

- 搜索其他会话的用户消息、素材、脚本和运行记录；
- 因名称相似自动把其他会话资源带入当前会话；
- 读取未被用户选择或Runtime登记的任意本地文件；
- 把全局历史用户数据作为“可复用知识库”。

系统级 Capability Registry、API契约和沙箱说明不包含用户内容，可以作为能力元数据在当前会话中查询。它们与“跨会话查询用户数据”是两个不同权限域。

查询结果必须带 `session_id`、作用域和来源；资源引用进入查询结果前，Runtime 再次确认资源确实属于当前会话。若用户主动选择其他会话或磁盘资源，应先创建显式导入事件和当前会话资源版本，之后才能被查询器看到。

### 11.3 临时脚本的权限模型

临时脚本不能因为由 LLM 编写就继承应用进程、当前用户或项目目录的全部权限。LLM 只能提出权限清单，Runtime 计算实际权限；模型不能自我授权。

权限至少按以下维度独立声明：

```json
{
  "language": "python",
  "resource_reads": ["asset_123@2"],
  "filesystem_read": [],
  "filesystem_write": ["temporary_output"],
  "network": false,
  "spawn_process": false,
  "shell": false,
  "environment_variables": false,
  "registry": false,
  "runtime_control": false,
  "gui_control": false,
  "timeout_seconds": 30,
  "max_memory_mb": 512,
  "max_output_bytes": 52428800
}
```

首版建议的默认临时 Python 档：

- 只读取Runtime挂载的当前会话资源；
- 只写独立临时输出目录；
- 允许经过白名单的纯计算、图片和结构化数据模块；
- 禁止网络、子进程、Shell、注册表、环境变量和GUI/运行时控制；
- 结果只能通过结构化 `result` 和输出清单交回；
- 新图片由Runtime验证后自动登记和显示。

#### PowerShell

技术上可以支持临时 PowerShell，但它必须是与 Python 分离的高风险执行档，不能仅凭 `language="powershell"` 获得运行权。

原因：PowerShell天然能访问文件系统、注册表、进程、服务、COM、网络和外部可执行文件；`-NoProfile`、ExecutionPolicy 或简单字符串/AST黑名单都不构成可靠安全边界。同一用户权限下启动一个带超时的 PowerShell 进程，也不能称为沙箱。

V4 首版建议：

1. 任意临时 PowerShell 默认关闭；
2. 常见Windows操作优先做成结构化、可审计的本地能力，而不是让模型写Shell；
3. 如果任务确实只能用PowerShell，模型先提交目的、完整源码、输入输出和逐项权限清单；
4. Runtime 静态检查后，涉及文件、注册表、进程、服务、网络、外部命令或GUI时必须取得对应用户授权；
5. 使用独立低权限执行进程、受控工作目录、资源/时间限制和输出回收；在真正隔离完成前，界面必须明确标记为“受信本地执行”，不得宣称安全沙箱；
6. 执行一次后默认不成为正式能力，源码、权限、输出和审计记录必须保存。

更稳妥的过渡方案是提供 `windows_command` 白名单能力：模型选择预登记命令和结构化参数，Runtime执行；只有白名单无法满足时才考虑完整PowerShell。

### 11.4 图片资源自动入区与展示

V4 不把“共同视口”作为需要告诉 LLM 的领域概念，也不向模型提供 `viewport.get_current` 之类的业务入口。共同视口只保留为 GUI 展示实现，不参与模型规划。

任何新图片资源一旦由以下来源成功产生：

- 用户导入、粘贴或截图；
- LLM 侧允许的图片生成/返回；
- 本地工具、探针或脚本生成；
- 裁剪、标注、对照板或可视化派生产物；

Runtime 必须统一执行：

```text
校验图片文件/数据
→ 计算内容哈希
→ 创建版本化 Resource
→ 自动加入当前会话素材区
→ 发出 ImageResourceCreated 事件
→ GUI 自动把最新图片显示在展示区域
```

这套动作不需要 LLM 再调用“加入素材区”或“显示图片”。LLM 后续只通过资源查询看到图片资源及其来源、角色、版本和证据状态。

用户说“这张图”“刚生成的图”时，由 Runtime 根据 GUI 当前显示资源和最近创建事件在输入表中直接解析成明确资源引用；模型不需要知道共同视口、焦点或控件状态。无法唯一解析时，Runtime 才把候选资源交给模型或用户消歧。

自动显示不等于视觉内容已被模型检查，也不自动成为事实证据。标注图、裁剪图、探针输出仍保留各自 role，不能冒充原始场景素材。

---

## 12. 与现有版本的差异

| 项目 | V2 | V3 当前实现 | V4 提案 |
| --- | --- | --- | --- |
| 模型工具协议 | 厂商原生 `tools=` | 自有 JSON Action | 自有 JSON Action |
| 业务工具暴露 | 每轮完整工具表 | search → get → act | L0 → L1 → L2 ticket → invoke |
| 主轮内上下文 | 短聊天历史 + 重建状态表 | 每副轮重建 user JSON + workspace | 一个主轮一个追加式上下文 |
| 跨主轮记忆 | 会话消息和 artifacts 混合 | 本地结构化存储 | 只读已提交投影和证据 |
| 工具实现 | V2 resolver | V3编排复用V2 resolver | V4原生 executor；迁移期适配旧工具 |
| 完成判定 | 表单字段 + 多处补丁 | Runtime Gate | 单一 Completion Gate |
| 厂商依赖 | 原生 tool calling | 仅文本 JSON | 仅文本 JSON |

V4 不直接复制 V3 的 `ProviderLogicModel.build_messages()` 行为。V3 当前每次只构造一条新的 user JSON；V4 要由 Context Manager 保存并追加本主轮的模型可见消息。

---

## 13. 建议代码边界

```text
backend/script_generator/collaborator_v4/
├─ domain/
│  ├─ ids.py
│  ├─ models.py
│  ├─ actions.py
│  └─ errors.py
├─ protocol/
│  ├─ system_prompt.py
│  ├─ parser.py
│  ├─ schemas.py
│  └─ validator.py
├─ runtime/
│  ├─ main_turn.py
│  ├─ context_manager.py
│  ├─ workspace.py
│  ├─ state_machine.py
│  ├─ completion_gate.py
│  └─ committer.py
├─ capabilities/
│  ├─ registry.py
│  ├─ discovery.py
│  ├─ tickets.py
│  ├─ permissions.py
│  └─ executor.py
├─ cache/
│  ├─ query_cache.py
│  └─ evidence_cache.py
├─ stores/
│  ├─ session_store.py
│  ├─ turn_store.py
│  ├─ evidence_store.py
│  └─ trace_store.py
├─ adapters/
│  ├─ provider.py
│  ├─ gui_bridge.py
│  └─ legacy_capability.py
└─ testsupport/
```

V4包不反向 import V2/V3 的编排器。迁移期只允许 `legacy_capability.py` 适配确定性的底层能力。

---

## 14. 分阶段实施

### Phase 0：锁定协议

- 定义所有 ID、动作 JSON Schema 和错误码。
- 实现协议解析、冲突字段拒绝和单动作约束。
- 用假模型和假工具跑通完整主轮。
- 确认追加式上下文的精确消息格式。

### Phase 1：最小 Runtime

- MainTurn、Subturn、Workspace、Trace Store。
- `respond`、`request_user`、`query_runtime`、`finish`。
- 预算、暂停恢复和无进展熔断。
- 不接真实业务工具。

### Phase 2：能力发现与执行

- L0/L1/L2 能力协议。
- capability ticket、schema验证和权限校验。
- 接入 2–3 个只读能力：素材列表、素材检查、运行状态查询。
- 确认同主轮 L2 复用和版本失效。

### Phase 3：证据与提交

- Evidence Store、事实/主张分层。
- Completion Gate 和原子 Committer。
- 查询缓存、视觉证据缓存和 mutation epoch。

### Phase 4：脚本工作流

- 计划、逻辑确认、脚本生成、API契约查询、静态校验和交付物哈希。
- 试运行诊断与修订闭环。
- 任何“已修复”声明必须绑定真实复测证据。

### Phase 5：GUI灰度

- 按会话选择 V4；V2/V3 保持不变。
- 独立数据库和日志命名空间。
- 收集副轮数、延迟、缓存命中、失败码、用户打断和恢复成功率。
- 达到验收条件后再讨论默认版本切换。

---

## 15. 最低测试矩阵

1. 一个会话连续两个主轮，第二主轮不含第一主轮副轮原文。
2. 同一主轮的消息严格追加，既有前缀不被改写。
3. 未取得 L2 ticket 时，`invoke_capability` 被拒绝。
4. ticket版本、主轮或执行目标不匹配时被拒绝。
5. L1 目录不包含完整 schema，L2 包含完整 schema。
6. 参数不符合 schema 时工具绝不执行。
7. 权限不足时工具绝不执行，并只请求必要授权。
8. 相同只读查询在 epoch 未变时命中缓存，包括空结果。
9. 带副作用工具不会因参数相同被错误回放。
10. 工具结果不能通过提示注入修改 system protocol。
11. 无证据的图片数量结论不能完成主轮。
12. 相同失败调用达到阈值后熔断。
13. 主轮暂停恢复时 `main_turn_id` 不变，`run_id` 改变，副轮序号不归零。
14. 模型协议错误只允许一次定向修复，不进入无限重试。
15. Commit失败时长期状态不出现半提交。

---

## 16. 尚待决定

以下问题在编码前需要单独形成决定记录：

1. 主轮内追加上下文由应用完整重放，还是使用供应商 response/conversation handle；默认倾向应用完整掌握并自行重放。
2. 主轮恢复时是否重放全部模型可见消息，还是从受信 checkpoint 重建等价前缀。
3. L2 ticket 的默认有效副轮数和是否允许一次取得多个契约。
4. `select_capability` 是否单独占一个副轮，或允许 `capability.get` 作为内核查询直接返回 ticket。
5. 工具输出进入上下文前的统一大小上限和分页协议。
6. 首批迁移的业务工具清单。
7. Prompt cache 命中率的遥测口径及不同供应商的统一字段。

---

## 17. 决策记录

### 2026-10-04 / ADR-V4-001：一个主轮一个独立 LLM 上下文

决定：产品会话包含多个主轮；每个主轮创建独立模型上下文，主轮内副轮追加，跨主轮只通过 Runtime 恢复已提交事实。

原因：控制上下文污染和无限增长，同时保留主轮内连贯性与缓存前缀。

### 2026-10-04 / ADR-V4-002：业务工具分阶段暴露

决定：业务工具按 L1 精简目录、L2 完整版本契约、调用 ticket 三步暴露；不在主轮开始时发送全部工具 schema。

原因：减少 token、降低误选和参数猜测，并让版本、权限、副作用可在执行前验证。

### 2026-10-04 / ADR-V4-003：不用厂商原生工具协议作为运行核心

决定：LLM只输出 V4 JSON动作；Runtime自己解析、校验和执行。供应商原生工具调用即使未来支持，也只能作为可替换传输优化，不能成为领域协议。

原因：保证多模型一致性，并让全部权限、审计、缓存和完成规则由本地掌握。

### 2026-10-04 / ADR-V4-004：LLM提议，Runtime裁决

决定：LLM没有直接写入长期状态、执行工具或判定完成的权力。

原因：把概率性决策与确定性执行、证据和提交边界分开。

### 2026-10-04 / ADR-V4-005：Runtime出表，LLM填表

决定：V4 每个副轮由 Runtime 根据当前状态生成结构化表，表内声明本轮允许的下一步类型、字段、类型、枚举和互斥条件；LLM 只能填写并返回同一张表，不自行定义动作结构。

System prompt 只保存稳定的身份、权责和填表原则。业务字段、工具候选、完整契约和动态状态都由 Runtime 随副轮表发送。开头草案见 `collaborator_v4/prompts/system_prompt_v1.md`。

原因：让本地系统掌握协议演进和动作边界，缩短稳定提示词，并避免模型根据旧记忆编造字段、工具或参数。

### 2026-10-05 / ADR-V4-006：LLM担任主轮调度员与需求分析者

决定：每个主轮先执行一次结构化需求分析，再进入由 Runtime 出表的行动循环。LLM 每个副轮从菜单中选择一个动作，可以查询本地信息、发现或调用本地能力、进入脚本流程、要求本地不执行并由模型自身继续处理、询问用户，或提交最终回复候选。

LLM 决定下一步工作方向以及何时申请提交；Runtime 决定动作是否合法、是否执行、结果是否可信、最终回复是否满足证据和完成条件。提交被拒后，Runtime 必须返回具体缺口并继续同一主轮，不能让用户用“继续”重新唤醒。

原因：LLM适合做语义理解、需求澄清和动态调度，本地系统适合做确定性执行、权限、事实、证据和提交控制。两者职责分开后，既保留模型自主性，又不放弃本地可控性。

### 2026-10-05 / ADR-V4-007：英文机器标识，中文语义契约

决定：V4 协议字段、动作 id、枚举值和错误码使用稳定英文；system prompt 与菜单语义说明首版使用中文。菜单项必须附带 meaning、use_when、do_not_use_when、required_inputs、side_effect 和 expected_result，不能只发送英文动作名。

是否将完整 system prompt 改为英文，必须通过目标模型集合的协议遵循率、字段错误率、歧义选择率、token 成本和修复副轮数对照测试决定，不凭经验直接切换。即使未来使用英文 system prompt，菜单的机器 id 和语义边界仍必须分离。

原因：语言本身不能消除协议歧义。英文适合作为稳定机器标识，中文更适合当前用户语境和需求原话；明确的 schema、正反适用条件和 Runtime 校验才是消歧的主要手段。

### 2026-10-05 / ADR-V4-008：50副轮绝对上限，重复查询提前熔断

决定：V4 首版不采用无限副轮。每个主轮最多发起 50 次 LLM 请求，查询、缓存回放、拒绝和修复全部计数；查询预算不得扩充该绝对上限。Runtime 同时按精确查询、语义目的和状态进展三层识别重复，并在远早于 50 次时切断空转。

原因：多级表单与能力发现确实会增加正常副轮数，因此上限需要高于 V3 的常规任务；但无限副轮会把协议错误、空搜索和状态机摆动变成无界费用与等待。50 应作为灾难保护，重复/无进展闸才是主要控制机制。

### 2026-10-05 / ADR-V4-009：共同视口退出模型协议，图片资源自动入区

决定：共同视口仅是 GUI 展示层，不向 LLM 宣告，也不作为模型可查询或可操作的领域能力。任何新产生的有效图片自动版本化、加入当前会话素材区并显示；指代解析由 Runtime 把 GUI 当前显示状态转换为明确资源引用后再交给模型。

原因：让模型围绕资源和任务工作，而不是学习 GUI 焦点、共同视口和显示控件，减少信息负载并避免自主性被界面状态绑架。

### 2026-10-05 / ADR-V4-010：非法调用表最多修复三次

决定：调用表不合法时返回同一表单、精确字段路径、稳定错误码和中文含义，最多修复 3 次。仍失败则暂停主轮并保存完整诊断，任何工具都不得执行。

原因：格式错误可以定向修复，但无限重填会掩盖表单歧义、模型适配错误和协议缺陷。暂停后的数据应首先服务于开发诊断，而不是把问题推给用户。

### 2026-10-05 / ADR-V4-011：能力只经查询器发现，可按需查看源码

决定：业务工具不写入 system prompt，也不能凭名称猜测。能力查询先返回带输入、输出、实际作用、边界、副作用和失败语义的 L1 工具箱；选定后再取完整契约。模型确有需要时，可以继续申请与能力版本绑定的清理后源码或相关代码片段。

原因：既控制默认信息负载，又允许模型在契约不足以解释行为时自主下钻实现；源码用于理解，Registry 契约仍是调用和校验的唯一权威。

### 2026-10-05 / ADR-V4-012：用户数据查询限定当前会话

决定：查询器默认只能读取当前产品会话中的用户素材、消息、事实、证据、脚本和运行记录。其他会话或任意磁盘资源必须由用户明确选择，并通过导入事件登记到当前会话后才能查询。系统工具目录、API契约和沙箱说明属于不含用户内容的能力元数据，不受跨会话用户数据禁令影响。

原因：会话是用户可理解的数据边界。禁止隐式跨会话检索可以避免旧素材、同名脚本和历史结论混入当前任务，也使资源来源和授权可审计。

### 2026-10-06 / ADR-V4-013：补充在当前副轮结束后抢占注入

决定：用户在 LLM 请求进行中发送的补充不取消当前网络请求，也不等到下一主轮。GUI 立即收下并显示为“等待注入”；当前副轮返回且通过表单校验后，Runtime 先保存返回值，但**暂缓执行其中尚未发生的动作**，优先把本批补充送入同一主轮的补充审查表。

补充审查表至少携带：原始请求、当前目标、刚返回但尚未执行的动作、按提交顺序排列的补充、补充提交时的资源引用上下文。LLM 只能从以下处理方式中选择：

```text
keep                补充不影响待执行动作，继续执行
modify              废弃待执行动作，回行动菜单重新选择
replace_requirement 补充改变目标/交付物/成功条件，重填需求分析表
ask_user            新旧要求实质冲突且无法可靠消歧
```

规则：

- 停止/取消 > 修改目标或交付物 > 增加约束 > 增加素材或事实 > 普通说明；
- 一个副轮期间收到的多条补充合并进一张审查表，保留顺序，避免一条补充消耗一个副轮；
- 只有补充已经进入模型可见表单后才标记 `consumed`；网络失败、暂停或进程退出不得静默丢失；
- 补充审查本身计算一个副轮；审查期间新到的补充在该副轮结束后处理；
- 完全相同的补充可去重，但原始事件仍保留在审计日志；
- 连续三批补充都推翻目标时暂停自动推进，请用户先明确最终要求；
- 补充不得覆盖原始用户请求，目标变化以 revision 形式追加。

原因：副轮中断会造成请求状态不确定；等到下一主轮又失去“补手”的意义。在副轮完成与动作执行之间设置确定检查点，可以同时保持请求完整性和用户控制权。

实现状态：已接入 V4。GUI 按补充提交时刻保存资源上下文；V4 在每张表单校验成功后、尚未执行动作前检查信箱，批量注入 `supplement_review` 表，并记录 `supplement_received` / `supplement_injected`。`modify` 会废弃冻结动作，`replace_requirement` 会重做需求分析，`ask_user` 会暂停等待确认；连续三次替换需求会停止自动推进。未赶上下一个检查点的补充仍按原规则在主轮收尾时转成新消息或退回输入框。

### 2026-10-06 / ADR-V4-014：跨主轮历史只给索引，内容按需查询

决定：新主轮不自动展开以前主轮的全文。启动表只给轻量历史可用性提示；当用户使用“继续、刚才、之前、还是那个、在结果上修改”等承接表达时，Runtime 标记 `history_reference_likely=true`，由 LLM 通过查询器按需读取。

新增目标查询：

```text
session.main_turns       返回当前会话的主轮摘要列表
session.main_turn_detail 按 main_turn_id 返回需求、补充、关键结果、产物和最终回复
```

摘要至少包含 `main_turn_id`、用户请求、目标、状态、最终回复摘要、产物引用和时间。详细查询仍限定当前产品会话，不返回隐藏推理，也不把旧主轮自由文本自动认作事实。

原因：既避免每个新主轮背负完整历史，又使“继续上次工作”能够由模型主动查明，而不是靠模糊聊天窗口猜测。

实现状态：已经存在 `session.messages(limit<=30)`，可以读取当前会话最近的用户/Agent 消息；但它没有按主轮分组。当前只保存 `artifacts.v4_last_turn`，会被下一主轮覆盖。因此真正的主轮索引和详情查询尚未落地。

### 2026-10-06 / ADR-V4-015：能力发现先选类别，再下钻工具

决定：寻找本地工具时先调用 `capability.list_categories`，再用 `capability.list_by_category` 查看该类能力；`capability.search` 只作为跨类别兜底，并要求模型先看过类别菜单。类别、工具摘要和完整契约分别控制信息量。

首版类别包括视觉查看、图像匹配、图片标注、会话资源、技能工作流、运行环境、脚本扩展、诊断权限。每个类别和能力都带用途、适用时机、输入摘要、输出摘要、实际作用及副作用。选定能力后仍须取得完整契约才能调用；源码只按需披露。

原因：一次展示全部脚本和工具会挤压上下文，也容易让关键工具（例如 `mark_on_image`）被截断或忽略。类别导航能让模型用较低负载逐层定位。

实现状态：类别查询、分类工具列表、兜底搜索前置闸和完整契约查询已经进入 V4 原型。

### 2026-10-06 / ADR-V4-016：标注形状由目标几何决定

决定：`mark_on_image` 继续作为纯绘制能力，同时支持矩形 `rect:[x0,y0,x1,y1]` 和圆形 `{x,y,radius}`。上游选择标注形状遵循：

- 已有 bbox 时优先传 `rect`，不得丢掉尺寸后退化成固定半径圆；
- 只有中心点而没有可靠尺寸时才使用默认圆；
- 用户明确要求“圈出”且目标近圆形时，由 bbox 计算圆心和半径，并增加少量边距；
- 用户只说“标出”时，优先选择能忠实表达定位证据的 bbox；
- 绘制函数不承担识别，圈偏时必须回查上游坐标、bbox 和尺度，不能把绘图产物当成新的识别证据。

原因：固定默认半径只能表达“这里有一个点”，不能表达目标真实范围。此次勋章标注偏差说明，几何信息应从定位结果完整传递到绘制层。

实现状态：`annotation.py` 已支持矩形和圆；默认圆半径为 18 原图像素。上述 bbox→贴合圆和形状选择规则尚未形成 Runtime 校验闸。

### 2026-10-06 / ADR-V4-017：共同视口双击窗口保持原图 1:1

决定：双击共同视口打开的图片窗口默认以原始像素显示，不再为了铺满窗口自动放大小素材或缩小大截图。小图按原尺寸居中，大图按原尺寸放入滚动容器；矩形和画笔覆盖层也在这套原图坐标上编辑。用户可用滚轮主动按固定倍率档缩放，工具栏实时显示百分比，并提供“原始尺寸”按钮精确回到 100%。

原因：当前游戏页面可能已经跳转，已保存素材是后续定位、裁剪和制作模板的唯一来源。若查看窗口擅自放大，用户容易把显示像素误认为原图像素，也不利于判断小模板的真实清晰度和尺寸。以 `结界1` 为例，用户应当能在保存的 1371×820 原图上滚动查看并定位空格子，而不是依赖已经变化的游戏窗口。

实现状态：`CollabViewportEditor` 已改为原图尺寸画布，并由 `QScrollArea` 提供居中和滚动；支持 25%–800% 固定档滚轮缩放、倍率显示和 100% 复位。缩放时尽量保持当前视野中心。预览区本身仍保持自适应缩略显示，只有双击窗口默认使用 1:1。

### 2026-10-06 / ADR-V4-018：选择卡片恢复同一主轮

决定：V4 的需求澄清、参数选择和冲突消歧卡片属于主轮内等待点，不是主轮完成后的快捷消息。`ask_user` 保存完整可恢复 checkpoint 后进入 `WAITING_USER`；用户点击卡片或手动输入答案时，本地追加一条带 `main_turn_id` / `question_id` 的 `user_response`，恢复同一个主轮并继续副轮序号，不再重新执行需求分析。

checkpoint 至少保存：模型可见消息、需求分析、Runtime结果、已取得契约、副轮/修复/无进展计数、素材候选、补充 revision、等待问题和选项。完成主轮后删除等待点；再次询问则用新问题覆盖旧等待点。导航、截图、试运行等本地操作卡片仍保持原有路由，不冒充主轮内回答。

原因：用户选择是模型提出问题后的直接答复。如果另开主轮，上一轮的目标、证据、工具契约和问题身份都会丢失，模型只能重新猜测上下文；“尺寸适配试试”被当作孤立需求就是这种断裂的实际表现。

实现状态：V4 已持久化 `artifacts.v4_waiting_turn`。GUI 识别等待点后只记录用户回答，不覆盖 `goal_text`；Runtime 恢复原 `turn_id`，注入 `user_response` 后直接进入行动菜单。日志分别记录 `main_turn_resumed` 和 `user_response_injected`。

### 2026-10-06 / ADR-V4-019：标图先产候选，模型不得裸写坐标

决定：用户要求圈出、框出或标出视觉目标时，V4 优先加载 `visual-locate-and-mark` skill。流程固定拆成素材绑定、目标契约、候选获取、Runtime 候选闸、绘制和结果复核。`mark_on_image` 仍是纯绘制工具；模型只能选择已验证的 `candidate_id`，默认不得直接提交未经登记的 `{x,y}` 或 bbox。

重复 UI 不再只依赖整图模板响应的最高峰。若目标位于卡片、行列或槽位网格中，应先拟合稳定父结构并生成全部候选，再用正例/负例参照逐项分类。已知数量仅用于核对，不能迫使算法凑数；标注产物不得回流为本轮识别证据。

原因：在“结界1”验收中，整图模板匹配只返回 1 个空格子，而按 4 行 × 2 列卡片、每卡 5 槽建立 40 个候选后，参考模板逐槽分类得到 24 个空槽，分数两类最大断层为 0.251619；由候选框计算的 24 个圆均落在空槽中心。问题不在绘图函数，而在绘图之前缺少完整候选和验证闸。

实现状态：说明书 skill 已加入 `backend/script_generator/skills/visual-locate-and-mark/SKILL.md`，会由能力目录自动同步并能通过中文查询发现；候选闸目前写入 skill 约束，尚未成为 V4 Runtime 的强制数据结构校验。

### 2026-10-07 / ADR-V4-020：三版本共用同一份 SKILL.md 目录

决定：`backend/script_generator/skills/*/SKILL.md` 是 V2/V3/V4 唯一的说明书技能来源，不为不同 Runtime 复制或改写 skill。统一能力目录必须区分 `kind=skill` 与 `kind=tool`：skill 可搜索、可按需读取完整正文，但不可取得调用契约或直接执行；tool 可取得输入契约并调用。

V2 通过 `list_skills` 发现、`get_skill_doc` 按需读取；V3 继续通过共享 Capability Store 的 `capability.search/get`；V4 的类别目录直接合并同一个 SKILL.md 扫描结果，并通过 `capability.get` 读取正文，`capability.get_contract` 对 skill 返回 `CAPABILITY_NOT_EXECUTABLE`。

原因：此前 V3 已同步说明书 skill，但 V4 的临时目录只读取 `all_collab_tools()`，导致 `visual-locate-and-mark` 虽已创建，标图主轮仍只能看到 `inspect_asset` 与 `mark_on_image` 等工具。skill 必须统一来源和身份，同时保持按需加载，不能靠把全文塞进 system prompt 解决。

实现状态：三版本读取入口已接到同一 `load_skill_docs()`；V4 类别/搜索结果会携带 skill 摘要，正文仍只在显式 `capability.get` 后进入上下文。

### 2026-10-07 / ADR-V4-021：截图预览采用可开关的连续匹配模式

决定：截图预览中的“开始匹配”不再是每点一次只执行一次的普通命令按钮，而是匹配模式开关：

1. 用户第一次点击“开始匹配”时，Runtime 先用当前参数执行一次匹配；本次调用成功完成（允许结果为 0 项）后进入匹配模式，按钮文字改为“停止匹配”。输入无效、模板不存在、区域缺失或执行异常时不进入模式。
2. 匹配模式开启期间，每次实际修改匹配参数都执行一次新的匹配。参数包括匹配类型、目标图片、最低匹配度、颜色一致、区域匹配和匹配区域；重新获取截图并更新画面后也对新画面重匹配。
3. 用户再次点击按钮时立即退出匹配模式，按钮恢复“开始匹配”。退出后修改参数不再触发匹配，现有标记不因退出而自动清除。
4. “最低匹配度”改为 0–100 的整数滑块并在旁边显示换算值。图片/像素匹配向执行器传 `slider / 100`，显示两位小数；文字匹配保留 0–100 语义。图片匹配默认 0.90，像素匹配默认 0.98。
5. 滑块关闭拖动跟踪。未进入匹配模式时，拖动或键盘修改只更新阈值及其数值显示，不执行匹配；仅在匹配模式开启时，鼠标松开、数值正式提交后才触发一次匹配，避免拖动经过每个刻度都运行昂贵的图像匹配。匹配模式下，键盘等离散修改仍按每次修改触发一次。

原因：阈值和颜色/区域条件需要反复试调。旧设计要求用户每改一个参数再手动点一次“开始匹配”，反馈链过长；若简单监听滑块的所有移动事件，又会在拖动时产生高频重复计算。显式的可开关模式让用户决定何时进入实时试调，并使“首次执行、参数重算、停止自动执行”三种动机互不混淆。

实现状态：`gui/panels/screenshot_viewer.py` 已以 `matching_mode` 保存状态，匹配执行从按钮槽拆到 `_perform_match()`；按钮、参数控件、区域绘制结束和截图更新均按上述规则接入。回归测试覆盖首次匹配、参数重匹配、停止后静默和初始失败不入模式。

### 2026-10-10 / ADR-V4-022：探针源码与试运行交付必须显式跨环境翻译

决定：自造工具源码属于 `probe_only` 执行环境，只用于说明已经验证过的业务逻辑，不再被当作试运行脚本模板。进入 `load_trial_script` 时，本地必须同时提供：固定 Runner 导入骨架、目标为 `UserBrowser | UserWindow` 的入口、当前需要的 `portable_common` API 契约，以及探针能力到 Runner 能力的转换表。

当前首批确定性转换为：

- `read_image` / `ARGS` / `resolve_image` 是探针专有全局量，交付脚本禁止使用；
- 当前绑定目标画面由 `await browser.update_frame()` 取得，返回值已是画面，不得再次 `cv2.imdecode`；
- 探针中的 `cv2.matchTemplate` / `numpy` 多目标筛选，在试运行环境改用 `await browser.match_image_multi(path, threshold=...)`；
- 需求要求行列数不固定时，卡片按 y 聚类后以每行相邻 x 间距断层切分，禁止沿用探针源码的 `mid_x` 左右二分；
- 类型导入固定为 `from backend.browser.user_browser import UserBrowser` 与 `from backend.automation.user_window import UserWindow`，不得猜测合并模块；
- 素材路径只能来自本轮 `asset_bindings/available_assets`，由交付桥打包替换真实 `asset_id` 占位，模型不得虚构 `[[IMG_DIR]]`。

原因：2026-10-10 的卡片识别主轮中，模型连续两次复制 `detect_opponent_cards` 的探针实现，生成含 `cv2/numpy`、错误 `core.user_browser` 导入和伪造 `IMG_DIR` 的脚本；同时文字声称“不固定行列”，实现仍按 `mid_x` 分左右两列。根因不是 Runner 缺少能力：`match_image_multi` 已同时登记在 `UserBrowser`、`UserWindow` 和 `portable_common`，而是 V4 交付表只突出旧源码，没有把新契约与跨环境翻译规则放在同一张表中。

实现状态：V4 交付表已注入 `update_frame`、`match_image_multi`、`script_log` 的 Registry 契约；源码描述带 `execution_scope=probe_only`、禁止复制策略和确定性转换表。表单校验提前拒绝 `cv2/numpy`，并直接给出 `browser.match_image_multi` 替代方法；固定类型导入现在既写入模板，也由 AST 校验。探针工具的可执行源码保持探针实现，避免为了迎合 Runner 而破坏它自身的执行环境。算法是否真正满足“不固定行列”仍属于需求语义验收，不能只凭模型文字声明通过。

---

## 18. V4 原型实施状态与迭代账（截至 2026-10-10）

本节只描述已经存在于代码中的行为；“已定案待实现”继续留在 ADR 的实现状态中。

### 18.1 已落地

| 能力 | 当前实现 |
| --- | --- |
| 表单主循环 | 独立 `collaborator_v4` 包；需求分析表 → 行动菜单 → 查询/调用/临时脚本/模型继续/询问用户/提交回复 |
| 协议修复 | 表单 JSON 解析和字段校验失败时，带字段路径返回同表重填；最多 3 次 |
| 副轮预算 | 主轮硬上限 50；接近上限时只保留询问用户和提交回复；另有无进展计数 |
| 主轮上下文 | 当前主轮内 `state.messages` 追加请求和模型表单回复；新主轮重新建立上下文 |
| 素材起始页 | 需求分析首屏携带轻量素材目录和语义候选；后续表单不重复携带完整目录 |
| 素材指代 | 用户文字与素材名称/id做轻量候选绑定；有唯一高置信候选时禁止反问用户重新提供同一素材 |
| 能力分类 | 类别 → 类内工具 → 完整契约；没有契约时禁止调用能力 |
| 工具与脚本产物 | 本地能力和临时脚本产生的图片统一登记进会话素材区，并刷新运行时素材状态 |
| JSON边界 | `VisualEvidence`、Path、Enum、集合和旧对象统一转成 JSON 安全值，避免对象不可序列化中断主轮 |
| 本地日志 | 每个 V4 主轮写独立 JSONL 轨迹，记录表单请求/返回、校验、查询、调用、异常和主轮结果；错误保存日志路径 |
| 副轮观察器 | GUI 可打开最新 V4 主轮日志，以双方消息形式查看各副轮表单和返回，原始大对象不再直接挤进普通聊天气泡 |
| 本地显示 | 聊天气泡禁用 Markdown 删除线解释，避免 `10~16`、`0.83~0.90` 一类普通范围被画成划线 |
| 用户补充 | 副轮结束、动作执行前批量注入补充审查表；可保持、废弃动作、重析需求或询问用户，且保留提交时资源上下文和审计事件 |
| 原图查看 | 双击共同视口默认以 1:1 原始像素打开；滚轮固定档缩放，显示倍率并可复位 100%；预览区仍自适应 |
| 主轮内选择 | `ask_user` 保存 checkpoint；点击动态卡片或手动回答后恢复同一主轮和副轮计数，不重复需求分析 |
| 标图工作流 | `visual-locate-and-mark` skill 已可经能力查询发现；要求目标契约、候选追溯、验证后绘制，并禁止裸坐标 |
| 统一 skill 目录 | V2 `get_skill_doc`、V3 `capability.get`、V4 `capability.get` 均读取同一 `skills/*/SKILL.md`；skill 与工具分离执行权限 |
| 截图连续匹配 | 阈值改为带数值显示的滑块；开始/停止按钮控制匹配模式，模式内参数或截图变化各触发一次重匹配 |
| 探针→试运行转换 | 自造工具按 `probe_only` 展示；交付表注入 portable Runner API 契约、固定双端导入和转换规则；`cv2/numpy` 在表单阶段即拒绝并提示合法替代 |

### 18.2 已知过渡性缺口

- V4 当前仍通过适配器调用部分 V2 确定性工具解析器，尚未形成原生 ticket、权限和证据执行器。
- 跨主轮只有最近消息查询和单个 `v4_last_turn` 摘要，尚无可分页主轮索引。
- 完成提交目前主要依赖模型表单字段，统一 Completion Gate 和 claim/evidence 校验仍未落地。
- 临时脚本仍使用现有 probe_script 权限模型；V4 独立沙箱和细粒度授权尚未完成。
- 标注工具已有圆/框绘制，但上游 bbox 保真和自动贴合圆规则尚未强制。
- `visual-locate-and-mark` 的候选闸目前是模型工作流约束，V4 尚无原生 `candidate_id` 注册表和提交时强校验。
- 试运行表单已经能约束执行环境和 API，但尚无通用的需求语义静态验证器；例如“行列数不固定”与代码里的 `mid_x` 两分法是否冲突，目前仍需专项规则或运行证据判断。

### 18.3 迭代记录维护方式

每次 V4 行为变化至少同步三处之一：

1. 改变长期语义或边界：新增 ADR；
2. 只是完成既定设计：更新“已落地/已知缺口”；
3. 线上故障修复：记录现象、根因、改法、回归测试和仍未解决的部分。

不得只写“已修复”。方案书需要能够回答当时为什么这样设计、代码实际做到哪一步，以及后来为何再次调整。

---

## 19. 文档维护规则

- 本文首先记录已经明确的 V4原则；尚未决定的内容留在“尚待决定”，不得伪装成已实施。
- 每次改变主轮、副轮、能力、权限、缓存或提交语义，都新增 ADR，不只修改正文。
- 实现状态必须单独标注，不以测试草案或接口骨架冒充生产接入。
- V4 实现开始后，本文中的代码位置和完成进度应在独立“实施状态”章节持续更新。
