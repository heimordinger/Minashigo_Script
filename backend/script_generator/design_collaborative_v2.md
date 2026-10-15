# Script Generator 协作版方案（v2）

> 状态：正式版 P0 骨架落地中 · 2026-09-14  
> 定位：把「填表 → 单步成码」升级为「外行可参与的用户 ↔ Agent 共创」  
> 兼容：保留现有 generate / trial / revise / optimize 管线，渐进接入  
> 代码：`logic_graph.py` · `collaborator/`（Engine + Skill 注册表）· GUI 只渲染 ViewUpdate

---

## 1. 背景与目标

### 1.1 现状（v1）

```
介绍 + 素材 → plan → select_images → generate → validate/fix
                 ↓
              试运行 → 反馈修订 →（可选）pseudo 优化
```

强项：清单闸、架构区分、权威优先级、修订 tools、性能优化 skill。  
缺口：Agent **不能**像开发对话那样「看图 → 试算法 → 落 helper → 再接线」；外行必须写介绍、懂反馈话术；**逻辑对不对**主要靠读代码或试跑撞墙。

### 1.2 目标（v2）

| 维度 | 目标 |
|------|------|
| 协作方式 | 多轮对话共创，而不是一次生成碰运气 |
| 用户画像 | 外行默认可用；专家可展开高级面板 |
| 用户负担 | 只需：说目标、给画面/窗口、点「对不对」 |
| 可理解性 | 人话逻辑图 + 画面叠加，先于源码确认 |
| 能力边界 | 视觉 probe / 领域 helper / 库函数优先，少让 LLM 现场发明 CV |

**一句话**：Script Generator 从代码生成器，变成**低负担的脚本共创 Agent**；复杂度进 tool / skill / 库，不进用户表单。

---

## 2. 设计原则

1. **默认代劳，失败再问** — 先按合理默认跑 probe；歧义或失败才问，一次一题、给选项。
2. **现象级交互** — 问「个人还是寮」「点歪了/没反应」；不问阈值、FSM、NMS。
3. **先看见逻辑，再看见代码** — 逻辑图与画面叠加是主确认面；代码默认折叠。
4. **库函数优于生成** — 「数章选最大」等进稳定 helper；生成器只调用并校验禁止重造。
5. **权威不变** — 用户确认/试跑反馈 ＞ 自动介绍 ＞ 诊断（沿用 `authority.py`）。
6. **v1 可回退** — 旧「介绍生成」入口保留；v2 为默认新轨或并行 Tab。

---

## 3. 用户体验（外行路径）

### 3.1 四段式（多数步骤可自动跳过）

```text
① 说目标（必做）
   「寮突破打勋章最多的，没章就刷新」

② 对齐画面（可一键）
   绑窗口 / 丢截图 /「用当前游戏画面」

③ Agent 自测（用户旁观）
   状态条：认界面 → 数章 → 选中左上 4 枚
   失败才弹一个选择题

④ 确认逻辑 → 试跑点头
   看逻辑图 + 叠加预览 →「看起来对」
   试跑：「能用 / 不行」+ 可选一句话
```

介绍文、checklist、架构标签由 Agent **写给系统**；用户看到的是人话摘要与图。

### 3.2 界面信息架构（摘要）

| 区域 | 内容 | 默认 |
|------|------|------|
| 对话轨 | 白话多轮；附件=截图 | 主操作面 |
| 逻辑画布 | 步骤卡片 + 试跑游标 | 主确认面 |
| 画面预览 | 当前帧 + 命中框/计数/拟点击点 | 有帧时显示 |
| 素材/代码 | 识图目录、脚本源码 | 折叠「高级」 |
| 试跑条 | 复用 TrialHud | 试跑时顶栏 |

细则见下一节 **§3.3 GUI 设计**。

### 3.3 GUI 设计（协作主界面）

相对 v1「1～5 编号 Tab 向导」，v2 **默认一张工作台**；经典向导降为「经典模式」入口，避免外行被五步流程吓住。

#### 3.3.1 总体布局（桌面主路径）

```text
┌─ ScriptGen 协作 ──────────────────────────────────────────┐
│ 状态条：就绪 | 正在数勋章… | 等你确认逻辑 | 试跑中     [经典▼] │
├─────────────────────┬────────────────────────────────────┤
│                     │  ┌ 画面预览 ─────────────────────┐  │
│  对话轨（约 38%）    │  │  游戏帧 + 叠加（框/数字/叉丝） │  │
│                     │  │  [用当前窗口] [选截图] [清空]  │  │
│  ┌─消息──────────┐  │  └──────────────────────────────┘  │
│  │ Agent / 用户  │  │  ┌ 逻辑步骤 ─────────────────────┐  │
│  │ 气泡 + 选项   │  │  │ ① 进结界          ○ 待确认    │  │
│  │ 芯片按钮      │  │  │ ② 数章并选最多    ● 当前/预览 │  │
│  └───────────────┘  │  │ ③ 挑战            ○           │  │
│  [附件] [发送]      │  │ ④ 结算后继续      ○           │  │
│                     │  │ [看起来对，生成脚本] [有问题]  │  │
│                     │  └──────────────────────────────┘  │
├─────────────────────┴────────────────────────────────────┤
│ ▸ 高级：素材 · 介绍草稿 · 代码 · API · 优化（默认收起）   │
└──────────────────────────────────────────────────────────┘
│ 试跑时展开底栏：账号 | 试运行/停止 | TrialHud | 能用/不行 │
```

- **左对话 / 右确认**：外行眼睛主要在右侧「图+步骤」；左手只负责说话。  
- **底栏按需**：未成码前不强调试跑；成码或用户点「试跑」后展开（复用现有账号选择与 TrialHud）。  
- **顶栏状态**：一行人话，对应 Collaborator 状态机（`understand` / `explore` / `propose_logic` / …）。  
- **经典▼**：切回现有 5 Tab（API / 描述素材 / 生成 / 试运行 / 优化），专家或不适对话者用。

窄窗（&lt;1100px）：改为 **上预览+步骤 / 下对话** 纵向堆叠，避免三栏挤压。

#### 3.3.2 与 v1 控件映射（少造轮子）

| v2 区域 | 复用 / 改造 |
|---------|-------------|
| 对话轨 | 新：`QListWidget`/`QScrollArea` 气泡 + 底栏输入；会话可接 `chat_session.py` |
| 画面预览 | 扩现有试运行帧预览 + `draw_debug` 叠加；截图用 `ImagePreviewDialog` 同源 |
| 逻辑步骤 | 新：`LogicStepList`（卡片列表，P0 不做节点编辑器） |
| TrialHud | **原样**挂试跑底栏 |
| 生成轨迹 / 流式代码 | 塞进「高级 → 代码」；GenTrajectory 保留 |
| 介绍 txt / 素材夹 | 「高级 → 素材与介绍」；Agent 自动草稿可只读展示 |
| API 配置 | 「高级 → API」或首次空配置弹窗；不挡在第一步 |
| 优化页 | 「高级 → 优化」或试跑后的次级按钮 |
| 反馈大框 | 弱化；改为对话里「不行」+ 点红步骤；高级里仍可打开原文反馈 |

#### 3.3.3 关键组件规格

**A. 对话气泡**

- Agent：左对齐；可带「正在…」跳动点（复用 `_BounceDots`）  
- 用户：右对齐  
- **选项芯片**（一次最多 3～4 个）：如 `[个人突破]` `[阴阳寮]` `[我再想想]` — 点击即发送，降低打字  
- 附件条：缩略图；支持拖入截图  
- 禁止默认展示 tool JSON；可折叠「详情」给专家  

**B. 画面预览**

- 比例：保留帧宽高比，`QLabel`+pixmap 居中；无帧时占位文案：「绑窗口或拖一张游戏截图到这里」  
- 叠加层：命中圆/框、格子数字、best 叉丝（与 vision tool 输出同一张 debug 图即可，P0 不单独做矢量层）  
- 操作：`用当前窗口截一帧` / `选图` / `清除叠加`  

**C. 逻辑步骤列表（P0）**

每卡：

```text
┌────────────────────────────────────────┐
│ ② 统计勋章并选出最多的对手     [预览中] │
│ 若没有可打的 → 去刷新                  │
│ 绑定：结界勋章 · 需要画面               │
│              [这一步不对]              │
└────────────────────────────────────────┘
```

状态色：`草稿`灰 / `预览中`蓝 / `已确认`绿 / `试跑至此`亮 / `失败`红。  
主 CTA：

- 未成码：`看起来对，生成脚本`（主色） / `有问题`（打开对话并聚焦）  
- 已成码：`开始试跑` / `再改一改`  

**D. 试跑底栏**

```text
账号 [下拉]  [试运行] [停止]  │ TrialHud … │  [能用] [不行]  [保存脚本]
```

- `能用` → 引导确认保存（接现有落盘）  
- `不行` → 焦点回对话，占位：「哪里不对？或点左侧红步骤」；自动带最近日志摘要（用户不必粘贴）  

#### 3.3.4 关键界面状态

| 状态 | 左栏 | 右栏 | 底栏 |
|------|------|------|------|
| 空会话 | 欢迎语+示例目标芯片 | 空预览+空步骤 | 隐藏 |
| 澄清中 | 问题+选项芯片 | 可仍空 | 隐藏 |
| 自测中 | 「正在数勋章…」 | 帧刷新+叠加 | 隐藏 |
| 待确认逻辑 | 短摘要 | 步骤可点、主 CTA 亮 | 隐藏 |
| 生成中 | 进度；高级可看代码流 | 步骤锁定 | 隐藏 |
| 可试跑 | 简短「可以试了」 | 步骤保持 | **展开** |
| 试跑中 | 可只读 | 当前步骤高亮 | HUD 动 |
| 待反馈 | 引导一句 | 失败步红 | 能用/不行 |

顶栏文案与上表同步，避免用户不知道「轮到自己了」。

#### 3.3.5 交互原则（外行）

1. **主按钮永远只有一个**（确认逻辑 / 生成 / 试跑 / 保存）按阶段切换。  
2. **选项优先于输入框**；输入框 placeholder 给例句，不给术语。  
3. **出错不弹技术栈**；`QMessageBox` 用已有脚本生成友好文案风格（步骤化建议）。  
4. **高级默认折叠**；折叠条显示一行摘要：`素材 12 · 已生成 · API 已配置`。  
5. **不强迫走完 API→介绍→生成**；缺 API 时在点「生成」才引导配置。

#### 3.3.6 信息密度：显示 / 隐藏

| 默认给外行 | 仅高级 / 详情 |
|------------|----------------|
| 人话步骤、预览图叠加 | 匹配分数、scale、阈值 |
| 「正在认界面」 | tool 名与原始 JSON |
| 能用 / 不行 | 完整试跑日志（可展开） |
| 自动介绍摘要 3 行 | 完整介绍 txt 编辑器 |
| — | 自由模式、识图强制刷新等 |

#### 3.3.7 实现切分（GUI 工程）

在现有 `gui/widgets/script_gen_panel/` 上增，而不是重写整站：

| 模块 | 职责 |
|------|------|
| `panel.py` | 增加「协作 / 经典」模式切换；协作页为默认 |
| `mixin_collab.py`（新） | 对话发送、选项芯片、状态机驱动 UI |
| `mixin_logic_canvas.py`（新） | LogicGraph → 步骤列表、游标、CTA |
| `mixin_frame_overlay.py`（新） | 帧/截图/叠加图显示 |
| `mixin_runtime.py` | 试跑底栏挂接；`note_state` → 画布高亮 |
| `mixin_generate.py` | 仍负责调 Worker；入口改为「确认后生成」 |
| `mixin_settings.py` / `mixin_assets.py` | 挪进高级折叠区 |

样式：沿用 `script_gen.qss` 的 objectName 约定，为协作页增加 `CollabChat` / `LogicStepCard` / `FrameOverlay` 一组，dark/light 各补一套。

#### 3.3.8 P0 GUI 范围（刻意缩小）

- 一张工作台 + 左对话右预览/步骤  
- 步骤=纵向卡片（不做拖拽连线）  
- 叠加=直接显示 tool 输出的 debug PNG  
- 经典 5 Tab 可切换保留  
- 不做：节点编辑器、分屏代码 diff、移动端布局打磨  

#### 3.3.9 线框验收（结界 demo）

1. 打开即见对话欢迎 + 芯片「结界打最多勋章」。  
2. 点芯片 → 右侧出现步骤草稿；要图时预览区闪占位。  
3. 自测后预览出现计数与选中叉丝；步骤②标「预览中」。  
4. 点「看起来对，生成脚本」→ 顶栏生成中；完成后底栏出现试跑。  
5. 试跑时步骤游标移动；点「不行」回到对话且不丢图。  

---

## 4. 逻辑可视化（确认「对不对」）

### 4.1 三层视图

| 层 | 时机 | 用户读到什么 |
|----|------|----------------|
| A. 逻辑预览 | 成码前/后 | 「进结界 → 数章 → 点最多 → 战斗 → 结算 → 循环」；分支白话化 |
| B. 画面叠加 | 有帧时 | 框选模板、格子计数、将要点的叉丝 |
| C. 试跑跟读 | 试跑中/后 | 当前步骤高亮；走过路径；失败步骤+停帧 |

三层共享同一份 **LogicGraph**（见 §6），避免「图是一套、代码是另一套」。

### 4.2 交互

- 点步骤 → 高亮对应代码区（高级）或 helper 名  
- 点红步骤 /「这一步不对」→ 带入修订上下文（无需用户写技术反馈）  
- 允许改人话步骤顺序/删支（轻量）；落码由 Agent 执行，不开放外行拖拽改 FSM

### 4.3 会话历史

协作对话**独立于**顶栏「打开归档」（那是生成/试跑材料目录）。

| 规则 | 说明 |
|------|------|
| 自动保存 | 每次视图更新后短防抖写入 `corpus/collab_sessions/<id>.json` |
| 内容 | messages + LogicGraph + 探针摘要 + 阶段/芯片/CTA；代码过大截断 |
| 入口 | 对话栏「历史」菜单恢复；「新会话」先存再重置 |
| 保留 | 最近约 40 条；不进 Git |
| 与 LLM | 将来 tool 循环续写同一 `session.messages`，恢复后可接着聊 |

### 4.4 逻辑步骤装不下

步骤默认**纵向滚动列表**，CTA 钉在列表外（确认永远可见）。

| 密度 | 规则 |
|------|------|
| ≤5 步 | 卡片展开（说明 +「这一步不对」） |
| >5 步 | 默认收成一行标题+状态；仅「预览中 / 失败 / 试跑至此」展开并滚到该步 |
| 手动 | 「展开 / 收起」；单击一行切换 |
| 试跑 | `note_state(step_id)` 时展开当前步、收起其余（P1 接游标） |

禁止改成节点画布来塞步骤；长脚本用列表+折叠，不在首屏铺满。

### 4.5 不做

- 完整 IDE 级调试器  
- 默认热力图/逐像素分数  
- 强制用户维护流程图节点属性

---

## 5. 系统架构

### 5.1 总览

```text
┌─────────────────────────────────────────────────────────┐
│ GUI：对话轨 + 逻辑画布 + 画面预览 +（折叠）代码/素材      │
└───────────────────────────┬─────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────┐
│ Collaborator Agent（多轮）                               │
│  intents → clarify? → explore(tools) → logic_graph      │
│         → codegen(v1 graph) → trial → revise/optimize   │
└───────┬─────────────────────┬───────────────────────────┘
        │                     │
   ┌────▼────┐          ┌─────▼─────┐
   │ Skills  │          │ Tools     │
   │ 范式说明 │          │ 可执行探针 │
   └────┬────┘          └─────┬─────┘
        │                     │
   ┌────▼─────────────────────▼─────┐
   │ 领域 Helper / API catalog / 素材 │
   │ 例：scripts/yys/结界勋章.py      │
   └────────────────────────────────┘
```

### 5.2 与 v1 Graph 的关系

| 模块 | v1 | v2 |
|------|----|----|
| `plan → generate → validate` | 主路径 | **Code 子图**（输入变为：意图 + LogicGraph + tool 结论 + 自动介绍） |
| `chat_session` | 生成↔修订续写 | 扩展为协作会话（含 tool 轨迹摘要） |
| `execution_checklist` | 介绍抽取 | 与 LogicGraph 步骤对齐；图为源，清单为闸 |
| `revise_tools` | 修订期文本/原生 tool | 扩展 vision/runtime tools；生成 Explore 期也可用 |
| `optimize-script-perf` | 试跑后 | 保留；协作对话可触发 |
| `authority` | 修订 | 对话确认 ≡ 高优先级反馈 |

### 5.3 Collaborator 状态机（逻辑）

```text
idle → understand → [clarify] → explore → propose_logic
     → [user_ack] → codegen → ready_trial
     → trial → [ok|revise] → done
```

- `clarify`：最多连续 N 问（建议 3），选项按钮为主  
- `explore`：调用 tools，结果写入 artifact + 画布  
- `propose_logic`：展示 LogicGraph，等「看起来对」  
- `codegen`：调用现有 LangGraph；禁止在无 ack 时静默覆盖用户已否步骤  

---

## 6. 核心数据：LogicGraph

逻辑画布与试跑游标的契约（示意）：

```json
{
  "version": 1,
  "title": "结界突破·打最多勋章",
  "architecture": "scene_driven",
  "steps": [
    {
      "id": "enter_jjtp",
      "label": "进入结界突破界面",
      "when": "未在结界页",
      "do": ["match/click 入口素材"],
      "bind": { "images": ["结界入口.png"], "helpers": [] }
    },
    {
      "id": "count_medals",
      "label": "统计各对手勋章并选出最多",
      "do": ["调用 count_opponent_medals", "高亮 best"],
      "bind": { "helpers": ["scripts.yys.结界勋章.count_opponent_medals"] }
    },
    {
      "id": "attack_best",
      "label": "挑战勋章最多的对手",
      "when": "best.count > 0",
      "else": "refresh_or_wait",
      "do": ["click best.click_xy"]
    }
  ],
  "edges": [
    ["enter_jjtp", "count_medals"],
    ["count_medals", "attack_best"],
    ["attack_best", "battle_settle"],
    ["battle_settle", "count_medals"]
  ]
}
```

规则：

- `label` 给人看；`bind` 给生成/校验  
- 试跑 `note_state(step_id)` 与 `id` 对齐（生成时注入约定）  
- checklist 覆盖率按 `bind.images` / 步骤是否落码检查  

---

## 7. Tools（Agent 的手）

优先可执行、少幻觉。在 `revise_tools` 同构扩展，Explore / Revise 共用。

| Tool | 输入 | 输出 | 用途 |
|------|------|------|------|
| `capture_frame` | 窗口 | 帧路径/尺寸 | 对齐画面 |
| `match_probe` | 帧+模板(+thr) | 命中、分数、叠加图 | 认按钮/界面 |
| `multi_count` | 帧+模板 / helper | 计数矩阵、best、叠加图 | 结界数章等 |
| `scale_probe` | 帧+模板 | 推荐 scale | 定标 |
| `list_assets` | 素材目录 | 清单 | 缺图时引导 |
| `mark_on_image` | marks(rect / x,y)+图 | 标注图（role=annotation） | 在图上圈出来给人看 |
| `define_tool` | name/desc/params/code | 登记自造工具 | 手不够用时自己造 |
| `list_custom_tools` / `delete_custom_tool` | — / name | 自造工具清单 / 删除 | 管理自造工具 |
| `read_unit` / `patch_unit` | 已有修订 tools | 同现网 | 外科修订 |
| `pseudo_summary` | record 目录 | 有效耗时摘要 | 优化入口 |

约束：

- Tool 结果进会话 artifact；模型不得伪造 `<<<TOOL_RESULT>>>`  
- 空板/弱峰值策略与 helper 内策略一致（避免寮突破误检）  
- 用户可见：工具名译成人话（「正在数勋章」），原始 JSON 进高级  

---

## 8. Skills（Agent 的说明书）

放在 `backend/script_generator/skills/`，Cursor 侧薄包装（同 optimize）。

| Skill | 何时用 | 产出 |
|-------|--------|------|
| `collab-script-coach` | 整段共创 | 问法、默认、何时澄清、何时进 explore |
| `vision-count-select` | 多实例计数选最优 | 调用 multi_count / 结界 helper，禁止手写 NMS 长段 |
| `scene-fsm` | 场景驱动 | 与 architecture scene_driven 一致 |
| `calibrate-then-bind` | 点不准/阈值 | probe → 写 CFG/注释，不空改 |
| `optimize-script-perf` | 已有 | 不变 |

Skill 给模型与内部 Agent 读；**不**当作用户教程塞进主 UI。

---

## 9. 领域 Helper 策略

```text
用户意图含「最多勋章/结界」 
  → Skill 绑定 scripts/yys/结界勋章.py
  → api_catalog 增加 helper 卡片
  → validate：若逻辑步骤含 count_medals 却无 import/调用 → 报错
```

新玩法优先：**先工具验证 → 再沉淀 helper → 最后允许生成器引用**。  
避免每个用户会话让 LLM 重写一套 CV。

---

## 10. 分阶段落地

### P0 — 骨架可演示（约 1～2 周量级）

- [x] LogicGraph schema + 逻辑画布（纵向步骤卡片即可）  
- [x] 对话轨 MVP + CollaboratorEngine（白话目标 → Skill → LogicGraph）  
- [x] Tools：`match_probe`、`multi_count`（包装现有结界算法；结界仅为 skill）  
- [x] 画面叠加预览接画布  
- [x] Skill 插件：`vision-count-select`（不再作为 GUI 硬编码主路径）  
- [ ] Collaborator LLM 多轮 tool-calling（下一批）  

### P1 — 闭环

- [x] 「看起来对」→ 调 v1 codegen（模型写「生成用介绍」→ 经典 generate 管线出码）  
- [x] 试跑 `note_state` ↔ 画布游标（2026-09-21，见 §10.2）  
- [x] 现象级修订（点步骤「不对」→ 自动带上下文进对话，见 §10.2）  
- [ ] 空板/寮 2×N 与个人 3×3 回归 —— **未完成**：合成帧复现不稳；真图已拿到并定出根因（hint 0.6 vs 实测 0.682，见 §10.3.1），但阈值未改
- [x] Agent 自查三件套：标准答案 / 异常哨兵 / 尺度对照（2026-09-21，见 §10.7）  

#### 10.1 出码桥：协作确认 → 经典 generate（2026-09-19）

「看起来对」之后不再让模型自由写整份脚本，改成两步：

| 步骤 | 谁做 | 产物 |
|------|------|------|
| 写「生成用介绍」 | 协作 LLM（form 新字段 `intro`：目的 / 场景标识 / 图片说明 / 任务流程 / 特殊规则） | `session.intro_text` |
| 出码 | 经典 `generate_script`（`codegen_bridge.generate_from_logic`） | 脚本 + plan/checklist/架构闸 |

桥接细节：

- 素材物化：会话素材（`role != frame`）拷进 `assets/images/_collab/<session_id>/`，用素材别名做可读文件名，
  作为经典 `source_dir`；整帧截图不当模板。
- 主模型看不了图时自动不传图，并改用 API 配置里的辅助识图生成文字目录（复用经典两条路径）。
- 模型没给 `intro`（或只想聊天）时，用 `build_fallback_intro` 把 LogicGraph 摊成经典介绍格式兜底
  （已验证能被 `execution_checklist` 抽出步骤 + 素材）。
- 出码结果仍过协作交付闸（机械归一 + 试运行同闸）；失败则退回 `PROPOSE_LOGIC`，
  并自动兜底为「模型直接写 script」（chips 也可手动选「重新生成 / 让模型直接写代码」）。
- 待补：出码期可中止（现在只丢弃结果）；outro 归档（`corpus/sessions`）尚未接协作来源。

#### 10.2 试跑游标与现象级修订（2026-09-21）

**A. `note_state` ↔ 画布游标**

契约（§4.1 C / §6）落地为一条日志协议 + 一个纯逻辑模块：

| 环节 | 落点 | 行为 |
|------|------|------|
| 运行层 | `user_window.note_state` / `user_browser.note_state` | 状态名**真变化**时打一行 `[MINASHIGO_STEP] <name>`（FSM 每轮都调，不能每轮刷屏） |
| 解析/匹配 | `collaborator/trial_cursor.py`（新） | `parse_step_mark` / `match_step_id`（精确 > 前缀 > 词元包含），候选只来自 LogicGraph 自己的 step id —— **认不出就不动卡片**，游标宁可不走也不乱跳 |
| 引擎 | `engine.begin_trial_cursor` / `note_trial_state` / `end_trial_cursor_run` | 到过的步骤：末位=`running`，其余=`acked`；人工标过的 `failed` 不被覆盖 |
| GUI | `mixin_runtime._note_collab_trial_log` → `mixin_collab.note_trial_state` | 试跑日志走 `singleShot(0)` 干净栈重画步骤卡；开跑清游标、收尾落成轨迹 |
| 恢复 | `session.last_trial_trace` | 重开会话重放轨迹，步骤卡接着显示「试跑至此」 |

只对**协作来源**的脚本生效（`_trial_script_from_collab`），经典脚本不受影响。
样式：dark/light 的 `script_gen.qss` 补 `stepStatus="running"` 一组。
测试：`tests/test_collab_trial_cursor.py`（18 例，含「运行层→解析→引擎」整链一条）。

**B. 现象级修订（点步骤「不对」）**

- `collab_form.build_step_revision_request(step, session, logs=...)` 替用户写成一条修订请求：
  「不对：第 N 步「标签」」+ 原条件/说明 + **最近试跑日志节选**（只挑 错误/Traceback/警告/`[状态]`/`[MINASHIGO_STEP]`，≤18 行，≤4000 字符）。
- 没有证据（没脚本也没日志）时**不自动发**，只回一句人话引导补现象 —— 避免无凭据乱改。
- `mixin_collab._on_step_wrong`：有证据就 `_on_user_send` 进正常对话回合（走同一套预算/契约/续轮）；
  上一轮还在跑则排队提示；没配 API 则把写好的请求填进输入框并提示去配。
- 测试：`tests/test_collab_step_revision.py`（7 例：证据筛选、噪声不算证据、日志切片有界、无证据只引导）。

#### 10.3 结界数章回归：未完成 + 观察到的问题（2026-09-21）

目标是补「空板不误检 / 寮 2×N / 个人 3×3」回归（§10 P1 最后一项）。**结论：没交付**——
合成帧上聚类行为对「卡内勋章间距 / 卡距 / 行距」的比例极敏感，我画的合成板与真实截图
（真实模板 39×35、`medal_scale_hint=0.6`）对不上，测试反复在「测试自己的版式误差」上失败。
`scripts/yys/结界勋章.py` 已**还原原样**，未跑通的测试文件已删除。

过程中在 `scripts/yys/结界勋章.py` 读到三处**可疑行为**（有合成帧证据、未用真图定案）：

1. `_filter_sidebar_hits` 左护栏用「卡内间距 × 3.5」；个人 3×3 只有 3 列、卡内间距又小时，
   这个值会超过「第一列 → 整块中位」的距离 → **第一列可能被整列丢掉**。
2. `group_hits_to_grid` 里「单枚 + 本行最左 → 当噪声丢弃」会把**只拿 1 枚勋章的第一张对手卡**
   丢掉 → 网格少一列、列数据左移一格。
3. 切行阈值用 `median(全体 y 差值) × 0.5`：三行、每行三张卡（卡内只 1 枚）时差值里没有重复项，
   中位数被行间距本身带跑 → 三行并成一行。

要定案需要**真实游戏截图**（放到 `screenshots/` 或 `assets/images/yys/结界/` 下即可）。
拿到图之前不要凭合成帧改这三个阈值 —— 现有识别链在真图上是能用的。

#### 10.3.1 真图证据 + 「Agent 为什么发现不了」（2026-09-21，同批）

后来拿到了真实结界截图（1156×649，3×3 九张对手卡，用户确认**只有第 1 行右卡有 3 枚勋章**，
即 `003/000/000`）。结论：

| 项 | 实测 |
|---|---|
| `layout.json` 的 `medal_scale_hint` | **0.6** |
| hint=0.6 处的匹配峰值 | **0.414**（远低于 0.75 阈值）→ 数章 **0 命中**、被当成空板 |
| 模板最佳尺度 | **0.682**，峰值 **0.844** |
| 仓库自带的 `calibrate_medal_scale()` | 其实**算对了**（0.682 / 0.844） |
| 但 `resolve_medal_scale()` 没用它 | 被 `_RECALIBRATE_MIN_PEAK = 0.88` 挡住（0.844 < 0.88）→ 回退 hint 0.6 |

也就是说：**不是算法不会算，是一个过严的门槛把正确结果挡掉了。**
两个 ss 独立检测在两个尺度下都在同一位置（第三列卡名字行）找到那 3 枚，与用户答案一致。

同一批还暴露出协作 Agent 自己的三个能力缺口（这才是本节的重点）：

1. **没有 ground truth 入口** —— 用户说「实际是 003/000/000」这句话在填表协议里没有落点
   （`EMPTY_FORM` 只有 reply/plan/done_when/...），Agent 即使量错也无从知道，更存不下正确答案。
2. **没有异常哨兵** —— `rows=1, cols=1, hits=3`（明明有命中却只认出一格）不报错、不抛异常，
   Agent 看不出「这一栏本该是 9」。空板是合法状态，所以它也没动机去怀疑。
3. **没有尺度对照** —— `layout.json` 的 hint 过时这件事，代码里已经算出 0.682/0.844，
   但没有任何地方把「hint 峰值 0.414 vs 最佳 0.844」摆到模型眼前。

#### 10.7 Agent 自查三件套：标准答案 / 异常哨兵 / 尺度对照（2026-09-21）

对应上面三个缺口，新增 `collaborator/tool_sanity.py` 与 `collaborator/scale_ops.py`：

| # | 能力 | 落点 | 行为 |
|---|------|------|------|
| ① | **标准答案** | 表单新字段 `answer` + `session.artifacts["ground_truth"]` | 表单新增 `answer`（**只有用户明确给出事实时才填，否则 null**）；`remember_answer` 去重记账（≤8 条）；每轮状态摘要回灌`【用户给的标准答案】（权威，高于工具实测）`；`ground_truth_conflict` 发现"标准答案里的数字在最近实测里一个都找不到"时，`check_done_when` **直接拒绝判完成**并把实测结论退回重查 |
| ② | **异常哨兵** | `sanitize_tool_result`（`make_tool_resolver` 里统一 `_pack()`） | 网格类：`cols<=1 且有命中` → 明确写出「对手板通常是 3×3 / 2×N，只有一处命中时聚不出网格，**不代表画面里只有一张卡**」；全 0 → 提示"空板 vs 尺度/阈值"两种可能；尺度类：hint 与实测尺度差 >15% 且绝对值差 ≥0.03 → 报失配；匹配类：未命中但分数贴着阈值 → 建议先 `scale_sweep`；探针类：`result` 为空 → 提示"这一轮等于没测"。**只加 `_sanity` 字段，不改任何原值** |
| ③ | **尺度对照** | `scale_ops.scale_probe/scale_sweep` + 探针命名空间 + `match_template` 返回 | 探针预置 `layout_hint()` / `scale_probe()` / `scale_sweep()` / `calibrate_medal_scale` / `resolve_medal_scale`；`match_template_on_frame` 每次返回都附 `scale_hint` / `best_scale` / `peak_at_hint` / `peak_at_scale`（命中时也带，便于回答"这次用的什么尺度"），未命中且判定 hint 过时才附 `scale_advice` |

另外：`remember_measurement` 把每次实测的关键数字存进 `artifacts["measurements"]`（≤12 条），
状态摘要里以`【系统实测记录】`回灌，让"我上一轮量到了什么"不再只靠对话历史。

测试：`tests/test_collab_ground_truth.py`（23 例：接得住纠正 / 冲突拦截 done_when / 哨兵五种情形 /
哨兵不该吵的情形 / 尺度探针能定位最佳尺度 / 探针命名空间与匹配返回带对照）。
设计原则与既有 `authority.py` 一致：**用户确认 > 自动实测 > 诊断**，这次是把"用户确认"从"口头"变成"可落盘"。

#### 10.8 自造工具 / 图上标注 / 焦点退场（2026-09-22）

一次性收掉四个老毛病，新增 `collaborator/reusable_tools.py` 与 `collaborator/annotation.py`：

| # | 问题 | 改法 |
|---|------|------|
| ① | **「手上没这个工具」→ 反问用户** | 新增 `define_tool` / `list_custom_tools` / `delete_custom_tool`。模型先用 `run_probe_script` 把事做出来，再用 `define_tool` 登记（name/description/parameters/code，脚本从 `ARGS` 取参、结论赋 `result`）；`all_collab_tools()` = 内置 + 自造，每轮工具表都带上。持久化 `user_data/collab_tools.json`（≤24 个，20KB/个），执行仍受 `probe_script` 权限约束 |
| ② | **「在图上标出来」没有正经工具**（只能手写 cv2 或干脆答"做不到"） | 新增 `mark_on_image` + `annotation.py`（rect/circle、颜色名或 #hex、序号、标题横幅），坐标按**原图像素**。探针命名空间同时暴露 `mark_image` / `list_assets` / `save_image` |
| ③ | **标注产物混进场景素材**，被拿去当识别/匹配对象 | 标注图以 `group=标注` / `role=annotation` 登记，`is_scene_asset()` 把它排除在模板匹配、自动匹配之外；素材摘要里明确标「（标注图，不是场景素材）」 |
| ④ | **焦点静默回退** —— 省掉 `asset_id` 就悄悄用「焦点素材」，事后归错模板 | `resolve_template_path()` 不再回退，直接报「请提供 asset_id 或 template_path」。`focus_asset` 工具、表单字段 `focus_assets`、状态摘要里的焦点字段全部删除；GUI 里的星星标记保留（纯用户侧观感） |

配套：

- **需求 2：焦点不再进提示**。模型的世界里没有「焦点」这回事；素材一律 id/name 点名。
  `_infer_need_permission_from_text` 改为**请求语气感知**（同句要有权限名词 + 明确请求口吻，
  且排除假设语气），修掉「解释性提一句授权就被弹卡」。
- **素材清单进状态摘要**（`assets_summary_for_prompt` 原本是死代码）：模型看不到界面，
  不给清单就只能反问「你说的是哪张」。
- **done_when 新增 `已有标注图`**（`has_mark`），产物校验走素材区里的 `role=annotation`；
  `ground_truth_block` 措辞从「全局权威」改为**按指标权威**。
- 能力契约的缺口提示改成「先用 run_probe_script 做出来，再 define_tool 登记」，
  不再一律劝「请用户操作或换工具」。

测试：`tests/test_collab_custom_tools.py`（24 例：登记/校验/持久化/schema、走真探针执行器的
执行管线与权限闸、坐标归一与绘制、标注素材与场景素材分离、焦点回退已移除、素材清单与
标注图进入状态摘要、`has_mark` 需真产物）。

#### 10.4 出码交付兜底（2026-09-19）

**10.5 Agent 能力契约与"手"（2026-09-19，同批）**

**10.6 续轮预算与"继续"语义（2026-09-20）**

起因：模型每轮写 `turn=act`（未做完），系统内部只自动续 3 轮，用完就把"未完成"渲染成一个空洞的
「继续」丢给用户；用户点一次 = 重跑一整轮 LLM，但若卡在坏点上（例如裁剪产物文件名坏掉）永远同一个结果。

| 项 | 现在 |
|---|---|
| 自动续轮 | 上限 8 轮（原 3），并受 token 20 万 / 240 秒预算约束，任一超限即停 |
| 停下时 | 必须说明原因 + 停在哪一步 + 建议，不再给空洞「继续」 |
| 无进展检测 | `_progress_fingerprint` 只统计系统侧真产出（素材/匹配 overlay/逻辑步骤/代码长度/裁剪路径/窗口检查/探针/识图）；连续 2 轮指纹不变 → 判定"嘴上推进"，立刻停并汇报 |
| 芯片语义 | 未完成且系统还能做 → 「继续这一步」（带当前步骤）；判定卡住 → 「换个做法 / 我来手动处理」两条明确出路 |

- **能力契约**：plan 每步可带 `needs`（`tool:xxx` / `permission:xxx` / `user:说明`）。引擎每轮核对：
  工具不存在 → 不许当成「我去做」，强制再走一轮改计划；权限没给 → 强制 `turn=ask` + 授权芯片。
  起因：Agent 承诺「裁剪图片4」，而工具箱里没有任何裁剪能力，plan 那一步永远挂着。
- **完成要证据**：`done_when` 新增 `已有裁剪素材` / `已有匹配结果`，由系统核对真实产物
  （`artifacts.last_crop`、素材区「裁剪/探针」组、match overlay），没有产物不算完成。
- **补手**：新增 `crop_asset`（素材 id + 归一化矩形 → `screenshots/collab_crops/<会话>/` →
  自动进素材区并聚焦）；探针 `save_image` 的产出在执行后自动登记进素材区，并用 `new_assets` 回报给模型。
- **权限**：`write_files` 默认开——探针脚本本身已能跑任意 Python，卡住 `save_image` 只挡住产出、挡不住风险；
  `runtime_control` 仍默认关。
- **上下文与修复**：续写历史加 60k 字符总预算（保首轮介绍 + 最近轮次）；
  `patch_do_work_dual_target` 改为按符号独立补 import，修掉「已有 UserWindow import 就跳过补 UserBrowser」
  导致的多轮重修；每次 LLM 调用写 `llm_call` 事件（tokens + 上下文体积）。
- **可观测**：协作活动条超过 45 秒没有新内容，会提示「可能卡住，可停止重试」。

模型交回的 `script` 先过一遍**机械归一**（`collab_form.normalize_collab_script`），只补不改逻辑：

| 兜底项 | 规则 |
|--------|------|
| 素材占位符 | `[[asset_id]]`（含被引号包住的形式）→ 会话素材的真实路径；解析不到则报「素材引用没有对应素材」退回重修 |
| do_work 标注 | 复用经典管线的 `patch_do_work_dual_target`；仍是 `Any` / 缺标注时改写为 `UserBrowser \| UserWindow` 并补 import |
| import | 用到 `UserBrowser` / `UserWindow` / `Path` 却没 import 时补上 |
| IMG_DIR | 缺失时按协作素材目录补 `IMG_DIR = Path(...)`（已 import 或已赋值则不动） |

交付闸 `collab_script_issues(code, session)` = 结构 / 窗口接口白名单 + **试运行同一道闸**
`validate_script_local(require_fsm=False, source_dir="", free_mode=True)`；
试跑页对协作来源脚本也按同一档校验（不继承经典页的图片目录 / 自由模式开关），
避免「协作里通过、点试跑说没过」。自动补了什么会显示在代码区标题
（`已自动…`），不静默改写。

成品脚本一律落到 `scripts/_collab/<脚本名>.py`：不按游戏分目录，文件名即标识；
同名同内容复用，同名不同内容追加 `_2`、`_3`，不覆盖。`_collab/` 与 `_trial/` 一样
不进正式任务列表（`scan_process_tasks` 跳过）并加入 `.gitignore`；用户确定要用某个脚本时，
自己复制到 `scripts/<游戏>/` 即可。

**转正入口**：试跑通过在试运行页点「确认完成并保存」时弹保存对话框，用户自己选 `scripts/`
下的目录与文件名（默认预填当前脚本名，可选子目录或新建文件夹）；落在 `scripts/` 之外会被拦下重选。
保存成功后记住该目录 / 文件名，并提示脚本列表刷新方式。草稿文件保留在 `_collab/`，不自动删除。
注意：协作脚本的 `IMG_DIR` 仍指向 `assets/images/_collab/<session_id>/`，转正后若清理该目录需同步搬素材。

#### 10.9 会话体检首批修复（2026-09-24）

起因：`tools\_scratch\diag_collab_session.py` 对着实况会话 `20260922_101614_chat.json`
逐条复现了 6 项已知缺陷，外加 5 项体检没覆盖到的。判据一律**回到根因**，不只压症状。

| # | 现象（实况数据） | 改法 | 测试 |
|---|---|---|---|
| ① | 表外英文 CoT 整段当 `reply` 上屏（第 193/199/201/203 条） | `collab_form.looks_like_reasoning` 丢弃；`llm_turn._repair_form_followup` 退回一次「只输出表」 | `test_collab_reply_leak.py` |
| ② | 探针产出图 `role="template"` → 被当界面内容反复识图（4 张 / 9 次） | `PROBE_ROLE` / `PROBE_GROUP`；`is_scene_asset` 排除；显式引用仍可用 | `test_collab_probe_role.py` |
| ③ | 「空格子」42 假阳性，旧闸 `极差<0.02` 实测 0.022466 漏过 | 换「灰度标准差 / 峰值比 P90 / 点阵规律（众数步距+覆盖率）」三条判据 | `test_collab_empty_template_gate.py` |
| ④ | `logic_draft` 悬挂引用 → 草稿没落地 → `task_done` 报错打偏 | `prune_dangling_draft_refs` + `task_done_evidence_gap` 分型 | `test_collab_draft_recovery.py` |
| ⑤ | 预算暂停后 `turn=stop`，用户没有续跑入口（敲了 8 次「继续」） | `form_ui_state` 按 `last_auto_pause` 补「接着做完」芯片 | 同上 |
| ⑥ | 旧全局 `ground_truth` 里两条互斥答案并存 | `session.migrate_legacy_artifacts` 读/存盘都归档成 `legacy_ground_truth` | `test_collab_ground_truth_mutex.py` |
| ⑦ | 退化视觉框（36×45 图 / 自报 808×1014）被 clamp 成零面积 | 主换算不可用时按自报尺寸回缩，仍不可用标 `valid=False`；`crop_asset` 拒绝 | `test_collab_vision_and_spin.py` |
| ⑧ | 覆盖计数掺幽灵（`23/8` 类） | 分子取 `inspected ∩ 现存`，幽灵另注 | 同上 |
| ⑨ | 同图同工具 4 秒内跑 4 次、探针 result 全空 | `repeat_without_result` 执行前硬闸 | 同上 |

**验收**：`pytest tests\test_collab_*.py` 260 passed；`tests\test_*.py` 全量 356 passed / 27 skipped；
体检脚本 ⑤ 从「5 条互斥答案」变成「当前图片的用户说法 0 条；已归档 5 条」。

**仍未修**：`done_when` 与计划互斥（体检 #4）在当天实况里又复现，见 `AGENTS.md` 已知缺陷"仍未修"。

### 10.10 素材区复制（2026-09-24）

起因：素材区只支持「复制进去」（聊天区粘贴 / 拖入 / 导入），**没有「复制出来」**——
缩略图右键只有 底图 / 重命名 / 移出 / 导入 / 清空，想把某张模板拿去别处用只能去翻
`screenshots\collab_*` 目录。

| 入口 | 行为 |
|---|---|
| 右键 → 复制 → 图片 | 位图进剪贴板，可直接粘到聊天框、文档、画图 |
| 右键 → 复制 → 文件路径 | 路径进剪贴板 |
| 右键 → 复制 → 素材 id | `[[asset_xxx]]` 进剪贴板（对话里点名素材的写法） |
| 右键 → 在文件夹中显示 | `explorer /select,` 定位文件 |
| `Ctrl+C` | 复制**选中**缩略图的图片；只选中一张时可不先点 |

- 「选中」是 GUI 侧概念（琥珀色边框，`_selected_aid`），与后端「焦点素材」（★，蓝框，
  影响匹配/识图）不是一回事，**不要**复用 `focused` 属性。
- `Ctrl+C` 用 `WidgetWithChildrenShortcut` 限定在素材区子树，避免抢走聊天输入框的
  Ctrl+C；为此缩略图设了 `StrongFocus`。
- 结果反馈走 `notice` 信号 → 聊天区 system 气泡。
- 测试：`tests\test_collab_asset_copy.py`。

### 10.11 可操作 API 错误弹窗（2026-10-07）

明确且用户能直接处理的 API 错误，不再把整段供应商异常作为主要界面反馈：

- 当前先识别 `402 / Insufficient Balance`（同时兼容中文「余额不足」）。
- GUI 主线程弹出「API 余额不足」，明确提示充值或更换「1. API 配置」中的账号 / Key。
- 聊天气泡同步使用简短说明，不显示 request id 和供应商错误对象。
- 原始异常仍完整写入 runtime 日志，未知异常继续走原有失败流程，便于溯源且不掩盖问题。
- 映射入口统一放在 `collab_api_error_notice()`，后续可按同一方式增加认证失败、限流等类别。
- 测试：`tests\test_collab_api_error_notice.py`。

### 10.12 V4 标图证据闸（2026-10-08）

起因：新会话虽读取了 `visual-locate-and-mark` skill，却没有查看“勋章”参考图、没有执行
`match_template_multi`，直接把视觉模型给出的 5 个粗框传给 `mark_on_image`；粗框落在头像上，
Runtime 仍因“成功画出 5 个框”放行完成。

现把关键流程从提示建议升级为 Runtime 强制约束：

| 闸门 | 拒绝码 | 规则 |
|---|---|---|
| 素材检查 | `REFERENCE_ASSET_NOT_INSPECTED` | 存在参考素材时，原图和每张参考图都必须先分别 `inspect_asset` |
| 候选来源 | `VERIFIED_CANDIDATES_REQUIRED` | 视觉粗框不能直接绘制；须有同一原图+参考图上的成功 `match_template_multi`，且结果未截断、非 ambiguous |
| 数量一致 | `MARK_CANDIDATE_COUNT_MISMATCH` | 标记数必须等于本地候选数，防止把整排当一个目标或漏画 |
| 坐标对应 | `MARK_NOT_GROUNDED` | 每个标记中心必须唯一对应一个本地匹配候选，禁止裸猜坐标 |
| 成图复核 | `ANNOTATION_OUTPUT_NOT_VERIFIED` | 最新标注产物必须再经 `inspect_asset(mode=verify)`，之后才允许 `completed=true` |

V4 的 `capability_result` 同时保留 `capability_name` 与实际 `arguments`，使证据能核对所用模板、
目标原图和复核模式，而不是仅凭工具名“调用过”。拒绝结果进入 `recent_runtime_results`，模型下一副轮
能看到精确缺口并补齐步骤。共享 `mark_on_image` 工具说明也同步强调“只绘制、不识别”。

测试：`tests\test_collab_v4_protocol.py`。

### P2 — 外行打磨- [ ] 澄清题库（选项化）+ 连续提问上限  
- [ ] Skill 包齐；缺素材引导「请截哪」  
- [ ] 会话归档 / 一键导出介绍+脚本  
- [ ] 旧 Tab 与新轨设置项  

### P3 — 增强（可选）

- [ ] 逻辑步骤轻量编辑（删/排序）→ Agent 重生成  
- [ ] 更多 helper（列表选段位、多属性扫描等）  
- [ ] 可行性/成本预估接对话  

---

## 11. Demo 剧本（验收用）

**结界突破 · 打勋章最多**

1. 用户只说：「帮我写个结界脚本，每次打勋章最多的。」  
2. Agent 默认个人突破；要帧或用已有截图。  
3. `multi_count` → 画布显示 3×3 与「选中左上 4」。  
4. 逻辑图展示 5～7 步白话；用户点「看起来对」。  
5. 生成脚本：调用 `结界勋章`，不内嵌 matchTemplate。  
6. 试跑游标走动；换寮全空图 → best 为空、不乱点。  

外行全程不写介绍、不调阈值。

---

## 12. 风险与对策

| 风险 | 对策 |
|------|------|
| 对话空转、迟迟不成码 | Explore 超时/步数上限；强制出 LogicGraph 草案 |
| 逻辑图与代码漂移 | 图为生成输入；校验 bind；试跑 step_id 契约 |
| Tool 误检导致错逻辑 | 叠加预览强制人工 ack；弱峰值不重定标 |
| 外行仍被高级面板吓到 | 默认三区：对话 / 图 / 预览；其余折叠 |
| 与 v1 双轨维护成本 | Code 子图复用 v1；只新增 Collaborator 壳 |

---

## 13. 成功标准（产品）

- 外行 **不写介绍** 能完成至少一个完整脚本（结界 demo）。  
- 确认主路径是 **看图点头**，不是读 Python。  
- 同类视觉需求 **80%+** 走 helper/tool，而非生成器内嵌 CV。  
- 旧「介绍生成」路径回归不坏。  

---

## 14. 文档与代码落点（建议）

| 项 | 路径 |
|----|------|
| 本方案 | `backend/script_generator/design_collaborative_v2.md` |
| LogicGraph | `backend/script_generator/logic_graph.py` |
| Collaborator | `backend/script_generator/collaborator/`（Engine / phases / skills） |
| Vision tools | `backend/script_generator/vision_tools.py` |
| Skills | `backend/script_generator/skills/<name>/` |
| GUI | `gui/widgets/script_gen_panel/mixin_collab.py`（渲染 ViewUpdate） |
| 结界 helper | `scripts/yys/结界勋章.py`（skill 插件内部使用） |

---

## 15. 待你拍板的点

1. **默认入口**：协作工作台默认打开 + 「经典▼」保留 5 Tab（方案推荐），还是两 Tab 并列？  
2. **无窗口时**：是否允许纯截图共创（无试跑，只到成码）？  
3. **P0 画布**：纵向步骤卡片（方案推荐）还是节点连线？  
4. **第一个 Skill 包**：是否就定「结界数章选最大」为唯一 P0 demo？  
5. **API 配置**：首次缺失时弹窗，还是必须先去高级页？  

确认后可按 P0 清单拆任务开工。
### 需求契约表（V3，2026-10-08）

- 新主轮第一副轮固定为需求分析轮：LLM 只能用 `update_plan.requirement_contract`
  填写目标、任务类型、素材角色、覆盖范围、交付物、质量强度、验收条件与歧义。
- 本地执行语义下限：`标出/圈出/框出` 至少交付 `annotated_image`，
  `所有/全部/每个` 强制 `coverage=all`，避免模型在表中静默降低标准。
- 补充不一律重填：普通继续、授权、确认、重试沿用现有契约；可能改变目标、范围、
  交付物、验收标准或素材绑定的自由文本补充，将契约标成 `revision_required`，
  下一副轮只接受同结构的修订表，原契约在修订成功前仍完整保留。

### V4 未完成需求跨主轮继承（2026-10-09）

- 主轮仍然隔离，不回放上一主轮完整消息；本地只维护结构化的
  `v4_unfinished_requirements` 任务卡（目标、交付物、验收标准、剩余缺口、有效素材绑定与证据）。
- 新主轮首张需求表固定判断新旧需求关系：用户明确指定的范围优先；未指定时，兼容或无关需求
  一并纳入，互斥需求由新需求覆盖旧需求；只有无法可靠判断时才暂停并给出 2~3 个确认选项。
- 被覆盖或明确放弃的旧需求写入 `superseded_requirement_ids`，后续不再复活；已删除素材的绑定
  在进入首轮表前剔除。完成合并后的活跃需求会清除对应任务卡；副轮/无进展上限导致的暂停则保留为
  `blocked`，供下一主轮继续。
- `submit_response.completed=false` 不再被记成 completed，而是落为 `incomplete`。同主轮的
  `v4_waiting_turn` 等待/恢复协议保持不变，不与跨主轮任务卡混用。
- 自动视觉新调用额度由每主轮 2 次调到 4 次，覆盖“底图 + 两个参考模板 + 最终产物复核”；
  缓存命中不计数，用户主动要求重新识图仍有 1 次额外额度。

### V4 脚本交付—试运行反馈闭环（2026-10-09）

业务主路径固定为：

`需求交流 → LLM 理解 → 编写/验证能力 → 交付到试运行 → 用户测试 → 满意则保存；不满意则反馈 → LLM 修订 → 再交付试运行`。

- “本主轮完成”和“整个项目完成”分离。脚本成功加载后，本主轮可结束，但项目状态必须是
  `waiting_trial_result`；只有用户满意并保存后才进入最终完成状态。
- V4 菜单新增 `load_trial_script`。用户说“交付过来”“加载到试运行”“我去测试”，或一次修订完成
  后需要复测时，模型应选此动作；禁止用纯文字调用说明替代交付。
- 该动作分两张表递进：先用 `trial_source` 选择真实自造工具和运行行为，再由 `trial_script`
  把探针代码转换成 Runner 脚本。Runner 脚本必须包含带执行目标类型标注的
  `async def do_work(browser: UserBrowser | UserWindow)` 联合类型入口，通过
  `await browser.update_frame()` 读取用户当前切换到的绑定画面；不得依赖 `ARGS`、`read_image`、
  `resolve_image` 等探针专有全局量。
- Runtime 在交付前执行语法、入口和现有 `collab_script_issues` 检查。通过后返回真实 `script`、
  `script_name`、`open_trial=true` 与 `trial_handoff`；GUI 复用 `_push_code_to_host`，写入协作草稿、
  `_generated_code` 和试运行临时文件，并自动打开试运行页。
- 交付后的需求任务卡保持 `active`，等待用户反馈。后续新主轮继承当前脚本、有效素材绑定和
  试运行反馈；修改后再次走 `load_trial_script`，直到用户满意并保存。
- 临时脚本必须有生命周期时间：首次生成写 `created_at`；修改同一 `script_id`（试运行脚本按
  `script_name`）时保留创建时间并刷新 `updated_at`。临时探针、自造工具与试运行交付脚本统一遵守；
  自造工具覆盖登记也不得重置创建时间和使用次数。执行层另记 `started_at`、`finished_at`、
  `duration_ms`，用于区分“脚本何时改过”和“这次运行用了多久”。
- 入口合法性只由 AST 判定，不再用只接受字面量 `do_work(browser)` 的正则；避免 V4 要求
  “无类型标注”而现有 Runner 交付闸又要求“必须有类型标注”的矛盾修复循环。
