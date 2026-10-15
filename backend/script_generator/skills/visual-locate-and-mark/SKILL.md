---
name: visual-locate-and-mark
description: >-
  Reliably locate a user-described visual target and mark every verified instance
  on the original image. Use when the target meaning, reference image, scale, or
  repeated layout must be resolved before drawing; build and validate candidates
  first, then call mark_on_image. 适合圈出所有空格子、勋章或重复 UI；禁止模型裸写未验证坐标。
---

# Visual Locate and Mark

把“用户想找什么”“目标在哪里”“怎样画出来”拆成三个阶段。`mark_on_image`
只负责最后绘制，不负责识别。模型不得仅凭整图观感编出一组坐标。

## 何时走这条

- 用户说“在图上圈出/框出/标出所有……”；
- 目标名称可能需要结合参考素材理解；
- 目标重复出现、排列成行列，或单次模板匹配结果明显不完整；
- 必须交付一张可复核的标注图。

若已经有经过验证的匹配框，只需把结果画出来，可直接走 `mark-match-result`。
若要交付裁剪素材而不是标注图，走 `repeated-ui-crop`。

## 状态流

`bind_assets → define_target → acquire_candidates → validate_candidates → render → verify_output`

任何阶段失败都回到产生错误信息的上游阶段，不能跳到 `render` 猜坐标。

## 1. 绑定素材

从当前会话资源中明确绑定：

- `source_asset_id`：要被标注的原图；
- `reference_asset_ids`：零到多个目标参照图；
- `excluded_asset_ids`：旧标注图、探针预览等不能作为识别底图的产物。

每个 id 都要解析到实际资源。禁止依赖“焦点素材”或名称相似就自行换图。
记录原图尺寸与内容哈希；后续候选必须指回同一 source。

## 2. 建立目标契约

先形成简短的 `target_contract`，再定位：

```json
{
  "target": "空格子",
  "positive_features": ["圆形空槽", "内部无勋章图案"],
  "negative_features": ["带五角星/勋章的已占用槽"],
  "reference_asset_ids": ["asset_..."],
  "expected_layout": "每张卡一行五槽；卡片呈规则网格",
  "expected_count": null,
  "ambiguity": ""
}
```

必须同时查看 source 与 reference，确认参考图是正例而非相似干扰项。没有参考图时，
使用视觉查看获得正特征、负特征和易混淆项。只有会改变结果的语义歧义才询问用户。

## 3. 获取候选

按目标结构选择策略：

1. **可靠模板且目标彼此分离**：用 `match_template_multi`，检查模板信息量、峰值间隔、
   `max_count` 截断和尺度范围。
2. **重复 UI 或规则网格**：先定位卡片/行/列等稳定父结构，再本地拟合全部槽位；
   对每个槽逐个与正例/负例分类。不要只在整图响应图上取少数最高峰。
3. **没有模板**：视觉模型只给语义粗定位或父区域；再用本地图像结构生成像素级候选。
4. **已有可信 bbox**：直接注册候选，不把 bbox 丢成一个中心点。

若现有工具不能完成“拟合候选/逐项分类”，先完成能力查询；仍缺能力时才使用临时脚本。
临时脚本输出必须是结构化候选与诊断，不得直接悄悄画最终图。

每个候选至少保存：

```json
{
  "candidate_id": "c1",
  "source_asset_id": "asset_...",
  "source_hash": "sha256:...",
  "bbox_original": [x0, y0, x1, y1],
  "label": "空格子",
  "score": 0.91,
  "obtained_by": "grid_fit_then_reference_classification",
  "target_contract_id": "tc_..."
}
```

## 4. Runtime 候选闸

进入绘制前必须同时通过：

- source id、路径/哈希与当前原图一致；
- bbox 在原图范围内，宽高为正，使用原图像素；
- 候选间没有不合理重叠或重复；
- 几何尺寸、行列间距或父区域关系与同组候选一致；
- 目标与负例可区分，阈值两侧有可解释的分离；
- 若已知预期数量，命中数一致；未知时不得为了凑数修改阈值；
- `max_count` 未截断，低信息模板未被当成可靠证据。

模型在下一步只能选择通过闸门的 `candidate_id`。默认禁止提交裸 `{x,y}` 或裸 bbox；
仅用户手工标记，或经过上述验证并登记的脚本输出可以例外。

## 5. 绘制

从已验证 bbox 生成 `mark_on_image` 参数：

- 用户说“框出”或目标为矩形：保留 `rect`；
- 用户说“圈出”且目标近圆：由 bbox 计算圆心与半径，加 2–4 px 视觉边距；
- 用户只说“标出”：优先用最忠实表达定位范围的 bbox；
- 多类目标用不同颜色，同类目标保持一致样式；
- 在原图副本上绘制，产物登记为 `role=annotation`，绝不覆盖 source。

## 6. 交付前复核

同时复核原图、候选清单、实际绘制参数和最终标注图：

- 每个标记完整覆盖一个目标，圆心/框中心没有系统性偏移；
- 没有标到负例、文字、头像或相邻槽；
- `drawn_count == verified_candidate_count`；
- 标注产物的 source lineage 指回原图；
- 最终图不得再作为定位证据反馈进本轮识别。

复核失败就回候选阶段，不通过移动圆圈“修饰”错误定位。

## 完成判据

- 有目标契约和明确的 source/reference 绑定；
- 所有绘制目标都有可追溯的已验证 candidate；
- 生成一张新的 annotation 产物并自动加入素材区；
- 最终回复说明标记数量、采用的定位方法和验证结论；
- 共同视口展示的是最终标注图。

## 验收案例：结界1空格子

原图为 4 行 × 2 列卡片，每卡 5 个槽，共 40 个候选。先由卡片网格生成全部槽位，
再用“空格子”参考逐槽分类；两类相关分数最大断层为 0.251619，得到 24 个空槽。
由各候选 bbox 计算圆心和 21 px 半径后绘制，24 个圆均落在空槽中心，且没有覆盖勋章槽。
这个案例证明：单次整图模板匹配只命中 1 个时，应改用“父结构拟合 + 逐项分类”，
而不是降低阈值后盲目收集峰值或让模型猜坐标。

## 不要做

- 不要让 LLM 直接列一批未经登记和验证的坐标。
- 不要把视觉模型 bbox 当像素级真值；按实际送入尺寸换算并本地拟合。
- 不要用旧标注图验证新标注图，避免自反馈闭环。
- 不要因为目标呈规则点阵就判为假阳性；规则 UI 本来就会形成点阵。
- 不要把 `expected_count` 当作强迫算法凑数的依据。
