# 结构范式

与业务无关的**最小多任务 FSM 骨架**，已纳入生成 prompt。

| 文档 | 说明 |
|------|------|
| [minimal_multitask_paradigm.md](minimal_multitask_paradigm.md) | 规则与自检清单 |
| [../few_shot/12_minimal_multitask_paradigm.py](../few_shot/12_minimal_multitask_paradigm.py) | few-shot 代码（抽象场景甲/乙） |

**注入策略**

- **正常模式**：介绍含多任务 / 场景标识 / 日常等标签时，范式置顶于 few-shot 列表
- **自由模式**：仅注入此范式（无其它 few-shot / Rules）
- **登记**：`index.json` → `minimal_multitask_paradigm`
