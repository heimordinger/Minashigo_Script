# Script Generator 生成质量分析报告（corpus/sessions）

- 会话数：55（目录：`backend\script_generator\corpus\sessions`）
- 覆盖：生成 34/55 · 试运行日志 33/55 · 修订 20/55 · 诊断 17/55
- 修订后仍残留校验失败：**8** 会话；单会话最大补修轮数：2
- 累计 token：in 2,271,591 / out 636,055

## 1. 试运行终态 / 模型

| 终态 | 会话数 |
|---|---|
| stopped | 21 |
| finished | 11 |
| error | 1 |

| 模型 | 会话数 |
|---|---|
| deepseek-v4-flash | 32 |
| gemini-3.5-flash | 2 |

## 2. 校验错误 Top（按命中会话数，每会话去重）

| # | 会话数 | 类别(status) | 归一化错误 | 样例原文 |
|---|---|---|---|---|
| 1 | 5 | covered/场景路由阈值 | 场景路由 match_image 应使用 threshold=CFG.nav_threshold | unknown_state@58: 场景路由 match_image 应使用 threshold=CFG.nav_threshold |
| 2 | 2 | covered/弹窗循环(room_ok) | 房间领取 handler 引用了收取奖励但未引用 room_ok；介绍要求点击收取后处理 room_ok 弹窗 | room_sortie_state: 房间领取 handler 引用了收取奖励但未引用 room_ok；介绍要求点击收取后处理 room_ok 弹窗 |
| 3 | 2 | gap/代码区中文标点 | 第N行代码区含中文标点（非字符串/注释 | 第 148 行代码区含中文标点（非字符串/注释） |
| 4 | 1 | covered/素材文件缺失 | 图片文件不存在于所选目录：jjc_奖励.png | 图片文件不存在于所选目录：jjc_奖励.png |
| 5 | 1 | covered/状态表缺键 | handler `unknown_state` return 'room_claim' 但状态表无此键 | TASK_jjc_STATES: handler `unknown_state` return 'room_claim' 但状态表无此键 |
| 6 | 1 | covered/素材文件缺失 | 图片文件不存在于所选目录：platform_popup_close.png | 图片文件不存在于所选目录：platform_popup_close.png |
| 7 | 1 | covered/素材文件缺失 | 图片文件不存在于所选目录：room_back.png, back.png | 图片文件不存在于所选目录：room_back.png, back.png |
| 8 | 1 | gap/跨任务图/枢纽混点 | 主界面 的 `go_sortie_state` 点击了其它任务的图 (room_logo)；jjc 任务应点 jjc* 系列按钮 | TASK_jjc_STATES: 主界面 的 `go_sortie_state` 点击了其它任务的图 (room_logo)；jjc 任务应点 jjc* 系列按钮 |

## 3. 错误类别 → 修复策略（covered/gap/llm 会话数）

| 类别 | 会话数 |
|---|---|
| 场景路由阈值 | 5 |
| 素材文件缺失 | 3 |
| 弹窗循环(room_ok) | 2 |
| 代码区中文标点 | 2 |
| 状态表缺键 | 1 |
| 跨任务图/枢纽混点 | 1 |

| 策略 | 会话数 | 其中修订后仍失败 |
|---|---|---|
| covered | 8 | 8 |
| gap | 3 | 3 |
| llm | 0 | 0 |
| unknown | 0 | 0 |

## 4. 试运行日志失败信号（每会话去重）

| 信号 | 会话数 |
|---|---|
| 超时/timeout | 11 |
| 异常/Traceback | 6 |
| 未覆盖/卡住 | 4 |
| 失败/退出 | 28 |

## 5. 结论（自动摘要）

- 修订后仍失败的 8 个会话：`20260820_112438_generate`, `20260820_214253_generate`, `20260820_220917_generate`, `20260820_225309_generate`, `20260820_233425_generate`, `20260821_002510_generate`, `20260821_010403_generate`, `20260821_135726_generate`。
- 有 3 个会话的错误落在 **gap** 类别（建议新增确定性 patch）；其中修订后仍失败 3 个 —— 优先实现这些 patch 可减少 LLM 轮次。
- 有 8 个『修订后仍失败』会话的错误本属 **covered**（patch 未生效，通常是 revise 路径未传 explanation/plan，或归档生成于旧版『自由模式完全跳过本地 patch』的代码）。已修复 revise 路径补传 explanation/free_mode，建议以修复后新会话复测本指标。

> 口径：validation_*.txt 与 revise_summary 中『校验仍失败』仅在错误残留时写入，
> 因此‘覆盖数’为 55 个会话中经历过失败的子集，不代表全部生成会话的质量。
