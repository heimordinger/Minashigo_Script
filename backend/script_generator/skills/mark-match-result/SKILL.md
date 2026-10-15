---
name: mark-match-result
description: >-
  Mark template-matching results on an image and hand the annotated picture back
  to the user. Use when templates are already identified and the user asks to
  "mark the matches on the picture", "circle every medal", or "show me where
  they are". Covers multi-target matching, template trust self-check, and
  producing one annotated output image.
---

# Mark match results on an image

用户已经有模板、要一张"标好了"的图。目标不是出脚本，是**把匹配结果画给人看**。

## 何时走这条

- 「把你匹配的结果标在图上」「圈出所有勋章」「找出来并在图上标出来」
- 已经有明确的模板素材（素材区里能点名的图），界面图也确定
- 只要一张**标注图**当交付物

不适用：要的是**裁剪**出重复区域 → 走 `repeated-ui-crop`；要的是**用模板计数** → 走 `visual-template-count`。

## 流程

1. **点名素材**。用 `list_session_assets` / `resource.search` 拿到 id 或 name，模板和底图都要显式指定。
   不要靠"焦点素材"默认值，也不要凭文件名猜用途。
2. **多目标匹配用 `match_template_multi`**。它做跨尺度候选去重，返回每个命中的中心、框、分数。
   禁止手写 `cv2.matchTemplate` + NMS 长段。
   - 目标多且互相靠近时调 `min_distance`（默认是模板短边一半）
   - 只在局部找时给 `search_region`
   - 预期数量已知时给 `expected_count`（只用于核对，不会篡改命中）
3. **先自检模板可信度，再相信命中数**。这是最容易翻车的一步：

   | 现象 | 含义 | 怎么办 |
   |---|---|---|
   | 模板灰度标准差 < 6 | 近乎纯色，平坦背景上处处高分 | 换一张带边框/纹理的模板，或明确告知用户"这个模板分不出来" |
   | 峰值只比 P90 高 < 0.05 | 区分不出目标与背景 | 同上 |
   | 命中数顶到 `max_count` | 可能被截断 | 提高 `max_count` 或缩小 `search_region` 重跑 |

   **注意：排列规则不是可疑信号。** 游戏界面本来就是规则网格——规则排布的命中
   往往是**真实结果**（模板信息量够时 `gray_std` 高、`peak_margin` 大），不要因为
   "太整齐"就否掉正确结果。判断只看信息量指标（第 3 步那几条）。

4. **一张底图、一种颜色一种目标**。同一张底图上标多类目标时用不同颜色区分，
   不要叠两张图再让用户自己对照。
5. **用 `mark_on_image` 画**。坐标按**原图像素**（和 `describe_current_frame` 返回的 bbox 同一套）。
   中文标签走 `annotate_cn`（自动加载中文字体，避免问号乱码）。
6. **产出后切到共同视口**给用户看，并在回复里同时给**命中数**和**可信度结论**。

## 坐标不变量

- 视觉模型返回的坐标先按**实际送入尺寸**换算回原图，**不要相信模型自报的 `image_size`**。
  VL 网关常把 1920×1019 四舍五入成 1280×720，照自报值换算会把框纵向推偏约 20px。
- 面积小于最小有效边长的框标为无效，不进标注。

## 不要做

- 不要手写匹配 / NMS 长段代码（有 `match_template_multi`）。
- **不要把标注图当场景素材再去匹配**。标注图是 `role=annotation`，只给人看；
  拿它当识别对象会形成"拿自己画的图再去识图"的自反馈闭环。
- 不要把"命中数"当成"目标真实数量"直接下结论——先说可信度。
- 命中数异常时不要硬报数字，先按第 3 步自检。

## 完成判据

- 素材区里有本次的标注产物（`role=annotation`）
- 回复里给了命中数 + 模板可信度结论
- 标注图已切到共同视口

## 相关

- 要**数量**而不是**图**时用 `match_template_multi`（见技能 `visual-template-count`）
- `repeated-ui-crop`：要**裁剪**而不是标注时
- 设计依据：`design_collaborative_v2.md` §8「在图上标出来」、§11.2 坐标不变量
