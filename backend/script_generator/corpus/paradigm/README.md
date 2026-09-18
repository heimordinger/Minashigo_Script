# 结构范式

与业务无关的**最小多任务 FSM 骨架**（审阅说明 + few-shot 代码）。

| 文档 | 说明 |
|------|------|
| [minimal_multitask_paradigm.md](minimal_multitask_paradigm.md) | 规则与自检清单 |
| [../few_shot/12_minimal_multitask_paradigm.py](../few_shot/12_minimal_multitask_paradigm.py) | few-shot 代码（抽象场景甲/乙） |

**注入策略（类 RAG，`defaults.few_shot_inject=rag`）**

- **首轮生成**：只进目录卡（id / title / when），**不塞全文**
- **修码 / 修订评价**：校验或反馈命中 `run_task` / 未知 / 过场 / `_task_entry_state` 等时，再检索打开全文
- **eager**：旧行为全文硬塞（`few_shot_inject=eager`）
- **登记**：`index.json` → `minimal_multitask_paradigm`
