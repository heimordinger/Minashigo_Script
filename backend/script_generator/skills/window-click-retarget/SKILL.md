---
name: window-click-retarget
description: >-
  Diagnose Win32/emulator click targets when match succeeds but UI does not move
  (shell vs render layer). Use for MuMu/LDPlayer/Nox/BlueStacks/MEmu and any
  similar "screenshot works, message click does not" case. Prefer tools
  inspect_click_target / message_click_probe / retarget_click_window; never invent hwnd.
---

# window-click-retarget

## When

User phenomena (not thresholds):

- 点了没反应 / 点不动 / 空点击
- 识图或匹配成功，但画面不变
- 怀疑绑错窗、外壳/渲染分离

## Do

1. `get_runtime_context` — 账号绑定 hwnd + 裁剪日志  
2. `inspect_click_target` — 角色（shell/render/mirror）+ 推荐 hwnd + 候选  
3. 不确定时 `message_click_probe` 对比像素差  
4. 用户确认或证据充分 → `retarget_click_window`  
5. 用人话说明：截图层 ≠ 操作层（不限 MuMu）

## Don't

- 不要先改阈值 / 重写找图算法  
- 不要编造 hwnd  
- 不要把「点偏了」和「点到外壳」混为一谈；用探针区分  

## Tools

Implemented in collaborator runtime（不依赖玩法 skill 注册表）:

- `get_runtime_context`
- `inspect_click_target`
- `message_click_probe`
- `retarget_click_window`

Library: `backend.script_generator.runtime_diag` + `emulator_target.resolve_automation_target`.
