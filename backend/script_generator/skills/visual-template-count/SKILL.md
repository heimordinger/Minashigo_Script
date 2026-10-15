---
name: visual-template-count
description: >-
  Count repeated UI elements (icons / buttons / cards / slots / list rows) in a
  screenshot with template matching, and pick the richest candidate when several
  images are compared. Use for any "这个界面里有几个 X" or multi-instance count
  flow. Never hardcode a specific screen's slot count or pitch.
---

# visual-template-count（通用视觉模板计数）

## When

- 用户问"这张图里有几个 X"（硬币、按钮、卡片、格子、列表项…）；
- 需要在多张候选图里挑"最多的那张"；
- 已有一张模板图和一张目标图（或共同视口底图），要给出**可复核**的数量结论。

## Do

1. 先用 `resource.search` / `resource.get` 找到场景与模板的**精确名称**（Runtime 会在调用前
   绑定并校验版本/状态/用途，工具拿到的是真实路径；不要自己写文件名或绝对路径）。
2. 调 `match_template_multi`：`asset_id` = 待寻找的模板，`target_asset_id` = 被搜索场景；
   需要时给 `scales` / `threshold` / `min_distance` / `search_region` / `max_count`。
3. 读**结构化结果**：`count` / `matches` / `ambiguous` / `saturated` / `warning`。
   - `ambiguous=true`：模板信息量不足或候选泛滥 → 换更清晰的模板/收紧搜索区域，**不要**拿这个数字下结论；
   - `saturated=true`：命中顶到 `max_count` → 提高 `max_count` 再跑一次。
4. 要给人复核就用 `mark_on_image`（或视口 overlay）把命中画出来；数量结论只引用**通过结果校验**的 count。
5. 多个候选之间比较时，逐张跑同一套参数，再比较 `count`；不要拿不同参数的结果互相比。
6. 单张图得到一次合格结果后直接回答；`count=0`也完成本次匹配步骤，说明零命中而非不断重跑。
   `cached=true`表示已有结果，复用其证据。复核需指出具体冲突并改变模板、区域或参数；
   多图任务只继续尚未处理的目标。标注是独立派生步骤，完成后交付图片与结论。

## Don't

- 不要把某个具体界面的固定数量/每行几个/槽位间距写死（换一个界面就失效）；
- 不要用 `crop_asset` 的产物直接当计数结论（裁剪只产生派生资源，不证明数量）；
- 不要"看起来排得很规则"就自己推数量而不做测量；
- 不要为了一次计数去写临时脚本或自造工具 —— 通用能力已经在工具表里。

## Related

- 工具：`match_template_multi`（计数/定位）、`match_template`（单目标，只证明存在/位置）、
  `fit_repeated_regions` + `commit_repeated_regions`（重复区域批量裁剪）、`mark_on_image`（复核标注）
- 设计：`backend/script_generator/design_main_turn_reliability_pipeline.md` §4.4–§4.8、§10 Phase B4 第 6 项
