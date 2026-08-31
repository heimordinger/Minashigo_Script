# 最小多任务结构范式

与具体游戏、图片名、任务文案无关。生成端**保留骨架与接线**，业务细节由介绍填充。

**代码样本**：`corpus/few_shot/12_minimal_multitask_paradigm.py`  
**注入**：正常模式在匹配多任务/场景标识时置顶；自由模式仅注入此范式。

---

## 1. 三层分工

| 层 | 函数 | 做什么 | 禁止 |
|----|------|--------|------|
| **场景** | `unknown_state` | 并发 `match_image` 场景标识 → `return 场景名` | click；`return '未知'` |
| **行为** | `TASK*_STATES` 各步 handler | click / wait / 判断 → `return 下一步` 或 `__exit__` | 巨型 handler 包办整任务 |
| **编排** | `run_task` + `do_work` | 单任务 FSM；`do_work` 顺序多任务 | 硬编码初始 `state_name` |

## 2. 必备组件

1. `IMG_DIR`、`_img`、`CFG`（`threshold` / `nav_threshold`）
2. `GUARDS` + `check_guards`
3. `unknown_state` — 介绍「场景标识」每张 id 图一项
4. `helper_*` — 介绍「辅助步骤」；成功 return 业务态名，禁止 `__exit__`
5. `TASK*_SCENE_TO_STEP` — 场景名 → 步骤名
6. `_resolve_state` — 统一路由 handler / unknown 返回值
7. `_task_entry_state(states, task_name)` — 按任务选起跑导航态
8. `_bootstrap_state` — 启动先识场景再 resolve
9. `run_task(..., scene_map)` — 超时先识场景；`handler is None` 时 resolve
10. `TASK*_STATES` + `TASK*_TIMEOUT` — 含「未知」+ 辅助 + 场景桩 + 业务步
11. `do_work` — `for` 循环 `run_task`

单任务可省略 `run_task`，仍须场景层 + `SCENE_TO_STEP` + resolve。

## 3. 接线要点

- **场景名 ≠ 步骤名** → `SCENE_TO_STEP` / 场景桩 / `_resolve_state` 三选一或组合
- **`_resolve_state` 顺序** → 先 `SCENE_TO_STEP`，再 `states` 键；禁止场景名键绑定仅 `__exit__` 的 handler
- **枢纽态** → click 本任务入口，禁止 `return` 自身场景名空转
- **本步骤结束** → return 业务态名；**本任务完成** → `__exit__`
- **多任务入口** → `_task_entry_state` 按 `task_name` 分支，禁止一律同一导航态
- **禁止空壳** → 业务 handler 须 click/wait/match 或 `__exit__`

## 4. `run_task` 循环顺序

`bootstrap/entry` → `update_frame` → `check_guards` → 超时则 `unknown_state` + resolve → handler → resolve(`nxt`) → `__exit__` / 保持态

**过场规则**：`unknown_state` 全部标识未命中（`return None`）**且**通用导航按钮（如 `home`）也不可见 → 视为过场/loading，保持 `state_name`、重置 `se_time`。若仅有标识未命中但 **home 等导航 chrome 可见** → **非过场**，同样保持态 + 重置步超时，禁止 `_task_entry_state` 逃逸（除非当前已在「未知」态）。

## 5. 介绍 → 代码（抽象）

| 介绍 | 代码 |
|------|------|
| 场景标识 | `unknown_state` dict |
| 辅助步骤 | `helper_*` + STATES 键 |
| 任务流程 (1)(2)… | `TASK*_STATES` + `do_work` |
| 点击进场景 | 独立 handler + click + wait |
| 没有则任务完成 | `return '__exit__'` |
| 回到第 N 步 | `return` 对应 STATES 键名 |
| 点击后进过场/loading | 独立「等待*」步 + 长超时循环至下一阶段标识 |

## 6. 成品自检（对齐 validate）

- [ ] 多任务 → `run_task` + ≥2 `TASK*_STATES`
- [ ] 有场景标识 → `unknown_state` 有 dict
- [ ] 场景名在表内或 `SCENE_TO_STEP` 可解析
- [ ] 辅助步骤在 STATES 有键
- [ ] 业务 handler 非空壳
- [ ] 无「识 scene 后硬编码 state_name」
- [ ] 写入试运行文件后自动 **脚本检查**（结构 + 素材 `_img` 文件存在）
