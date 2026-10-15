# 模拟器多开：层识别与实例分组

记录日期：2026-09-19 · 状态：**已落地**（`emulator_target.py` + CLI + 选择器）

---

## 目标

MuMu 这类模拟器是「外壳 + 独立渲染层 + 图像/镜像层」分离的：截图点外壳能出图，
但消息点击没反应。多开时还要回答「谁和谁是一开」，避免把点击打到别的实例上。

## 角色词表

`classify_layer_role()` 统一给出四类（`runtime_diag.classify_window_role` 已改为委托它）：

| role | 含义 | 能不能点 |
|------|------|----------|
| `shell` | 外壳/主窗（Qt 之类） | 常无效 |
| `render` | 渲染/操作层（`nemuwin` / `MuMuNativeWindow` / `NxDevice` / `TheRender` / `subWin`） | **点击目标** |
| `mirror` | 图像/镜像层（`ToolSaveBits` 等） | 无效，可截图 |
| `unknown` | 未归类 | 用 `message_click_probe` 验证 |

## 分组规则

`group_emulator_instances()`（纯函数核心是 `group_layers`）：

1. 枚举可见顶层窗 + 其子孙窗（同 pid），过滤掉客户区 < 80 的辅助窗与非模拟器窗；
2. **分组键 = 顶层祖先 hwnd**（`Win32Target.root_hwnd`）；
3. **合并**：pid 相同且矩形重叠 ≥ 55% 的组并回同一实例——镜像层常是独立顶层窗，
   靠这条挂回它的实例；
4. 每组给出 `shell` / `render`（面积最大者）/ `mirrors`，`click_hwnd = render or shell`。

解析侧同步收紧：

- `_candidate_targets()`：非子窗候选必须与外壳矩形相关（`instance_related`），
  否则视为**另一个实例**直接排除（多开同进程时 pid 无区分力）；
- `_resolve_shell_for()`：同 pid 多个外壳时取与 base 重叠最多者，不再拿第一个；
- 最小化（矩形 -32000 之类）时几何不可用 → 不做武断排除，沿用旧行为。

## 已知限制

1. 镜像层是位图镜像，可能比渲染层**晚几帧**；不要用动画瞬间做跨层像素比对。
2. 两个实例的画面可能**完全一样**（同账号同界面），像素相似度区分不了实例，
   必须以结构/几何/实例表为准。
3. 若某版本把渲染层做成顶层且与外壳无重叠，会被几何规则误判为另一实例——
   这时需要 MuMuManager/adb 端口做权威锚定（**未做**）。
4. 跨层坐标换算（在镜像层截图定位、把点击打到渲染层）**未做**：
   当前 `UserWindow` 是单层统一策略（截图/匹配/点击都走解析出的渲染层）。

## 验证

- 单测：`tests/test_emulator_instances.py`（同 pid 两实例、相同标题不同位置、
  远距镜像不合并、矩形判定、选择器列）
- 实机自检：`python tools/emulator_instances.py [--json] [--title MuMu] [--click-only]`
- 诊断输出：`inspect_click_target` 现在带 `instance_index` / `instance_name` /
  `instance_layers`，候选带 `same_instance_as_bound`
