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

## 多开（同进程多个实例）

MuMu 12 多开常见于**同一进程**，pid 没有区分力（另一个实例的窗口也是同 pid）。
用实例分组，不要用 pid 判断「是不是同一开」：

- `group_emulator_instances()`（`backend.automation.emulator_target`）→ 每实例一组：
  `{index, name, pid, shell, render, mirrors, click_hwnd, layers, notes}`。
  分组键 = 顶层祖先 hwnd；独立顶层的镜像窗（ToolSaveBits）按 pid + 矩形重叠并回同组。
- 自检 CLI：`python tools/emulator_instances.py [--json] [--title MuMu] [--click-only]`
- `inspect_click_target` 结果带 `instance_index` / `instance_name` / `instance_layers`，
  候选带 `same_instance_as_bound`：**另一个实例的候选一律不建议改绑**。
- 窗口选择器已显示「角色 / 实例」两列，确认绑定时会自动解析到该实例的渲染层。

## Tools

Implemented in collaborator runtime（不依赖玩法 skill 注册表）:

- `get_runtime_context`
- `inspect_click_target`
- `message_click_probe`
- `retarget_click_window`

Library: `backend.script_generator.runtime_diag` +
`emulator_target.resolve_automation_target` / `group_emulator_instances`.
