# 待落地设计（落地状态）

记录日期：2026-09-11（补充执行清单 / 架构 / 假点击 / 阈值标定挂优化：同日）  
状态：A–F **已落地接线**；G **入口占位**（默认不进生成主路径）。

---

## 方案一览

| ID | 名称 | 对症 | 优先级 | 落地 |
|----|------|------|--------|------|
| A | 权威优先级（反馈＞介绍＞诊断） | 修订改反流程、诊断乱指挥 | 高 | ✅ `authority.py` → diagnose / revise |
| B | 削弱文件名/画面语义干扰 | 按字面猜按钮、轻度串台 | 低（可选） | ✅ IMAGE PARTS 措辞 + caption 降级 |
| C | 执行清单（抽出 + 硬闸覆盖） | 漏步骤（规格未机器化） | 高 | ✅ `execution_checklist.py` → prompt / validate / artifact |
| D | LLM 显式对照任务完成 | 漏步骤（写码后勾选/补缺） | 高（挂在 C 上） | ✅ 轻量：缺项对照附进校验错误（禁自证全完成） |
| E | 架构显式区分（scene_driven vs multi_task） | 推本抄成日常顺序多任务 | 高 | ✅ `architecture.py` → plan / prompt / validate / few-shot |
| F | 点击确认（开关 + 仅转场 + 粗间隔） | 假点击 / 空点观感 | 中（运行时） | ✅ `click_confirm.py` + `click_image(expect=…)` |
| G | 阈值从 1 下降标定（钉死后复用） | 匹配阈值不适配 | 低 | ⏸ `threshold_calibrate.py`；修订/优化仅 hint |

---

## 方案 A：权威优先级（反馈 ＞ 介绍 ＞ 诊断）

### 规则
1. **优先级**：用户试运行反馈 ＞ 脚本介绍 ＞ LLM 诊断（含停帧 caption / must_fix）。
2. **诊断服从前两者**：诊断不得产出与反馈或介绍冲突的 `must_fix` / `do_not`；冲突则丢弃该项，或降级为「请人工确认」，不得写入修订硬约束。
3. **反馈是补丁不是整页替换**：反馈只增量补洞；介绍中**未被反馈点名否定**的步骤默认保留（尤其 `@helper` 步骤，如确定属性后的 `2_back`、出击界面助战分支）。短反馈不得被理解成删除介绍中间态。

### 落地
- `backend/script_generator/authority.py`
- `diagnose_trial_failure` 出口 `filter_diagnosis_for_authority`
- 修订 system 注入 `AUTHORITY_BANNER`；降级项不进编号硬约束

---

## 方案 B：削弱文件名/画面语义干扰（可选、次优先）

### 落地
- IMAGE PARTS：禁止按文件名汉字推断用途
- 诊断 caption 点名介绍外 `_img` → 降级「请人工确认」

---

## 方案 C：执行清单（生成前抽出 + 生成后覆盖校验）

### 落地
- `execution_checklist.py`：抽出 / prompt 表 / `_img` 覆盖硬闸（含 `2_back`）
- 生成 system 注入；`generate_node` 写 `checklist` artifact
- `validate_generated_code` 缺项报错；修订同样注入清单
- 配置：`defaults.execution_checklist`（默认 true）

---

## 方案 D：LLM 显式对照任务完成（挂在 C 上）

### 落地（轻量）
- 离线 `checklist_coverage_report`；缺项时附「清单对照缺失项…禁止仅宣称已完成」
- 配置：`defaults.checklist_llm_coverage`（默认 true）
- 未另开一轮纯 LLM 自证；靠校验→修码轮对照 missing

---

## 方案 E：架构显式区分（scene_driven vs multi_task）

### 落地
- 介绍标签 `架构：scene_driven|multi_task` + 启发式
- `apply_architecture_to_plan`：scene → `single_fsm` + 清空顺序 tasks
- prompt `architecture_prompt_block`；校验禁止场景名顺序 `run_task` 队列
- few-shot：`scene_driven` 不注入日常 multi_task 范式
- 配置：`defaults.architecture_enforce`（预留；校验始终跑架构闸）

---

## 方案 F：点击确认（开关 + 仅转场 + 粗间隔）

### 落地
- `Browser` / `UserBrowser.click_image(..., expect=, appear_path=, …)`
- `click_confirm.confirm_after_click`：先等帧刷新，再粗间隔轮询
- 配置：`defaults.click_confirm` = `off|critical|all`（默认 `off`；显式 `expect=` 仍确认）
- API catalog / 生成 prompt 提示转场写 `appear`/`gone`

---

## 方案 G：阈值从 1 下降标定（挂修订 / 脚本优化；出问题再用）

### 状态
**默认不进生成主路径。** 模块可调用；修订/优化在反馈含「阈值/点不到/误匹配」时写 hint / `calibration_suggested`。

---

## 仍待观察

1. 清单/标签抽取误判 → 需介绍约定（`架构：`）+ 人工确认。
2. Vision caption 绑错 `_img`：靠 A/B 降级；未单独立案。
3. 反馈语义覆盖：默认离线语法+素材审查（`revise_feedback_review_mode=offline`）。

### 已落地（审查收窄）
- **修订审查默认离线**：`offline_syntax_asset_review`；LLM 审查仅 `mode=llm`。
