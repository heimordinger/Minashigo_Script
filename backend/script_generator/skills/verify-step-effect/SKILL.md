---
name: verify-step-effect
description: >-
  Verify an automation step by the action that actually ran and that step's own
  done condition. Use when a click, wait, match, write, or trial is said to have
  worked or failed, including dead clicks, loops, and match-success with no
  progress. Do not treat a weaker proxy as proof of the step.
---

# verify-step-effect

声称某一步做过、失败了、或已经修好时用。不限于外壳/渲染层，也不限于某张图。

## 顺序

按这个顺序问，第一问是「否」就停，不要跳去解释后面的层。

1. **跑起来的东西里有没有这一步的动作？**
   看实际执行的脚本或工具参数，不看逻辑步骤原文，也不看回复里的打算。
2. **动作打在这一步点名的目标上了吗？**
   目标是素材、坐标、文件或窗口里这一步写明的那个，不是「附近某个能产生反馈的点」。
3. **这一步自己的完成条件成立了吗？**
   完成条件来自下一步要出现的事实，或这一步写明的 `done_when`。不是「有变化」。

三问都是「是」，才能说这一步测过。

## 不能升级的证据

这些只证明自己那一列，不能写成上一列。

| 你实际看到的 | 只能说明 |
| --- | --- |
| 逻辑步骤 / 回复里写了要做 | 打算，不是已执行 |
| 匹配分数高、`wait_image` 为真 | 图在画面上 |
| 窗口角色是渲染层、hwnd 已改绑 | 点击发往哪一个窗 |
| 窗中心点击后像素有变化 | 这个窗吃消息点击 |
| 文件路径存在 | 还要看内容是不是这一步要写的 |

窗口分层只在第 2 问可能是「打错窗」时才用 `window-click-retarget`。第 1 问已经是否，改绑不能代替这一步。

## 怎么核对第 3 问

用脚本将会调用的同一条 API，对这一步点名的目标做，并带上完成条件。

- 点击：`click_image` 该素材。完成后看下一步的图出现，或当前图消失（`expect=appear` / `expect=gone`）。不要改点窗口中心。
- 等待：`wait_image` 该素材，超时算没出现。
- 写入：读回文件，内容含这一步要求的符号（例如该有的 `click_image`）。

没有控制窗口等权限时停下来问，不要在回复里写「已经跑过」。

## 不要

- 用更弱的检查解释更强的失败。
- 把「点」「戳」落成只等待；落盘后的脚本必须含对应动作。
- 第 1 问为否时讨论坐标、DPI、渲染层。
