# LiteGraph v1 使用文档

> 版本: 0.4 | 文件: `taskflow/js/litegraph_v1.js`

---

## 目录

1. [全局设置 (LiteGraph.\*)](#1-全局设置-litegraph)
2. [常量与枚举](#2-常量与枚举)
3. [LGraph — 图管理](#3-lgraph--图管理)
4. [LGraphCanvas — 画布渲染与交互](#4-lgraphcanvas--画布渲染与交互)
5. [LGraphNode — 节点基类](#5-lgraphnode--节点基类)
6. [Widget 控件类型](#6-widget-控件类型)
7. [上下文菜单 ContextMenu](#7-上下文菜单-contextmenu)
8. [节点回调方法](#8-节点回调方法)
9. [实用函数](#9-实用函数)

---

## 1. 全局设置 (LiteGraph.\*)

### 1.1 尺寸与布局

| 属性 | 默认值 | 说明 |
|---|---|---|
| `CANVAS_GRID_SIZE` | `10` | 网格吸附间距 |
| `NODE_TITLE_HEIGHT` | `30` | 节点标题栏高度 |
| `NODE_TITLE_TEXT_Y` | `20` | 标题文字 Y 偏移 |
| `NODE_SLOT_HEIGHT` | `20` | 插槽垂直间距 |
| `NODE_WIDGET_HEIGHT` | `20` | 控件行高度 |
| `NODE_WIDTH` | `140` | 节点默认宽度 |
| `NODE_MIN_WIDTH` | `50` | 节点最小宽度 |
| `NODE_COLLAPSED_WIDTH` | `80` | 折叠后宽度 |
| `NODE_COLLAPSED_RADIUS` | `10` | 折叠后圆角 |

### 1.2 颜色

| 属性 | 默认值 | 说明 |
|---|---|---|
| `NODE_TITLE_COLOR` | `"#999"` | 标题文字颜色 |
| `NODE_SELECTED_TITLE_COLOR` | `"#FFF"` | 选中时标题颜色 |
| `NODE_TEXT_SIZE` | `14` | 节点文字大小 |
| `NODE_TEXT_COLOR` | `"#AAA"` | 节点文字颜色 |
| `NODE_DEFAULT_COLOR` | `"#333"` | 节点前景色（标题栏） |
| `NODE_DEFAULT_BGCOLOR` | `"#353535"` | 节点背景色 |
| `NODE_DEFAULT_BOXCOLOR` | `"#666"` | 状态指示灯颜色 |
| `NODE_DEFAULT_SHAPE` | `"box"` | 默认节点形状 |
| `NODE_BOX_OUTLINE_COLOR` | `"#FFF"` | 选中轮廓颜色 |

**Widget 颜色：**

| 属性 | 默认值 |
|---|---|
| `WIDGET_BGCOLOR` | `"#222"` |
| `WIDGET_OUTLINE_COLOR` | `"#666"` |
| `WIDGET_TEXT_COLOR` | `"#DDD"` |
| `WIDGET_SECONDARY_TEXT_COLOR` | `"#999"` |

**连线颜色：**

| 属性 | 默认值 | 说明 |
|---|---|---|
| `LINK_COLOR` | `"#9A9"` | 数据连线 |
| `EVENT_LINK_COLOR` | `"#A86"` | 事件连线 |
| `CONNECTING_LINK_COLOR` | `"#AFA"` | 拖拽中的临时连线 |

### 1.3 行为开关

这些是 **全局设置**，在代码中直接设置 `LiteGraph.xxx = true/false`：

```javascript
// 在 main.js 或加载后的任意位置设置即可
LiteGraph.node_box_coloured_when_on = true;   // 节点执行时左上角灯亮
LiteGraph.node_box_coloured_by_mode = true;    // 灯按模式着色
LiteGraph.alt_drag_do_clone_nodes = true;      // Alt+拖拽复制节点
LiteGraph.auto_sort_node_types = true;         // 节点菜单自动排序
LiteGraph.search_filter_enabled = true;        // 搜索框按类型过滤
LiteGraph.middle_click_slot_add_default_node = true; // 中键点插槽自动创建节点
LiteGraph.release_link_on_empty_shows_menu = true;   // 连线丢空白处弹出菜单
LiteGraph.shift_click_do_break_link_from = true;     // Shift+点击断开输出
LiteGraph.click_do_break_link_to = true;             // 点击断开输入
LiteGraph.allow_multi_output_for_events = false;     // 事件输出只允许单连
LiteGraph.do_add_triggers_slots = false;             // 自动创建触发插槽
```

完整的开关列表：

| 属性 | 默认值 | 说明 |
|---|---|---|
| `debug` | `false` | 调试日志 |
| `catch_exceptions` | `true` | 节点操作是否 try-catch |
| `throw_errors` | `true` | catch 后是否继续抛 |
| `allow_scripts` | `false` | 允许执行不安全代码（Formula 节点） |
| `use_deferred_actions` | `true` | 延迟执行 action 队列 |
| `auto_sort_node_types` | `false` | 节点类型菜单自动排序 |
| `node_box_coloured_when_on` | `false` | 节点执行时指示灯变色 |
| `node_box_coloured_by_mode` | `false` | 指示灯按模式着色 |
| `shift_click_do_break_link_from` | `false` | Shift+点击输出断开连线 |
| `click_do_break_link_to` | `false` | 点击输入断开连线 |
| `search_hide_on_mouse_leave` | `true` | 鼠标离开时关闭搜索框 |
| `search_filter_enabled` | `false` | 搜索框按插槽类型过滤 |
| `search_show_all_on_open` | `true` | 打开搜索框时显示全部结果 |
| `auto_load_slot_types` | `false` | 自动计算插槽类型注册 |
| `alt_drag_do_clone_nodes` | `false` | Alt+拖拽复制节点 |
| `do_add_triggers_slots` | `false` | 自动创建触发插槽 |
| `allow_multi_output_for_events` | `true` | 事件输出允许多连 |
| `middle_click_slot_add_default_node` | `false` | 中键点插槽创建节点 |
| `release_link_on_empty_shows_menu` | `false` | 连线丢空白弹出菜单 |
| `ctrl_shift_v_paste_connect_unselected` | `false` | Ctrl+Shift+V 粘贴并连线 |
| `use_uuids` | `false` | 使用 UUID 而非整数 ID |

---

## 2. 常量与枚举

### 2.1 节点形状

| 常量 | 值 | 说明 |
|---|---|---|
| `LiteGraph.BOX_SHAPE` | `1` | 方角 |
| `LiteGraph.ROUND_SHAPE` | `2` | 圆角 |
| `LiteGraph.CIRCLE_SHAPE` | `3` | 圆形（未完全支持） |
| `LiteGraph.CARD_SHAPE` | `4` | 卡片（仅顶部圆角） |
| `LiteGraph.ARROW_SHAPE` | `5` | 箭头形插槽 |
| `LiteGraph.GRID_SHAPE` | `6` | 网格插槽（数组用） |

### 2.2 插槽方向

| 常量 | 值 |
|---|---|
| `LiteGraph.INPUT` | `1` |
| `LiteGraph.OUTPUT` | `2` |
| `LiteGraph.UP` | `1` |
| `LiteGraph.DOWN` | `2` |
| `LiteGraph.LEFT` | `3` |
| `LiteGraph.RIGHT` | `4` |
| `LiteGraph.CENTER` | `5` |

### 2.3 插槽类型

| 常量 | 值 | 说明 |
|---|---|---|
| `LiteGraph.EVENT` | `-1` | 事件输出（触发型） |
| `LiteGraph.ACTION` | `-1` | 事件输入（触发型） |

> EVENT 和 ACTION 的值都是 -1，但语义不同：EVENT 用于输出端，ACTION 用于输入端。

### 2.4 节点模式

| 常量 | 值 | 说明 |
|---|---|---|
| `LiteGraph.ALWAYS` | `0` | 每帧执行 `onExecute` |
| `LiteGraph.ON_EVENT` | `1` | 仅事件触发时执行 |
| `LiteGraph.NEVER` | `2` | 永不执行（仅显示） |
| `LiteGraph.ON_TRIGGER` | `3` | 触发时执行一次 |

模式颜色：`["#666","#422","#333","#224"]`（配合 `node_box_coloured_by_mode`）

### 2.5 连线渲染模式

| 常量 | 值 | 说明 |
|---|---|---|
| `LiteGraph.STRAIGHT_LINK` | `0` | 直线 |
| `LiteGraph.LINEAR_LINK` | `1` | 直角线 |
| `LiteGraph.SPLINE_LINK` | `2` | 贝塞尔曲线（默认） |

### 2.6 标题模式

| 常量 | 值 | 说明 |
|---|---|---|
| `NORMAL_TITLE` | `0` | 正常显示标题 |
| `NO_TITLE` | `1` | 不显示标题 |
| `TRANSPARENT_TITLE` | `2` | 透明标题栏 |
| `AUTOHIDE_TITLE` | `3` | 悬停时显示标题 |

---

## 3. LGraph — 图管理

图是节点和连线的容器。在 Minashigo 中每个标签页有一个 LGraph。

### 3.1 创建与基础操作

```javascript
const graph = new LGraph();
// 或从 JSON 恢复：
const graph = new LGraph(serializedData);
const graph = new LGraph(); // 然后 graph.configure(data);

graph.clear();              // 清空所有节点和连线
graph.start();              // 启动执行循环
graph.stop();               // 停止执行循环
```

### 3.2 节点管理

```javascript
graph.add(node);                    // 添加节点
graph.remove(node);                 // 移除节点（自动断开连线）
graph.getNodeById(id);              // 按 ID 查找
graph.findNodesByType("flow/start");// 按类型字符串查找
graph.findNodeByTitle("起点");       // 按标题查找
```

### 3.3 连线管理

```javascript
// 连线存储在 graph.links 对象中
// 每条连线：
{
    id: number,
    type: string | -1,        // 数据类型或 LiteGraph.EVENT
    origin_id: number,        // 源节点 ID
    origin_slot: number,      // 源输出插槽索引
    target_id: number,        // 目标节点 ID
    target_slot: number,      // 目标输入插槽索引
    _data: any,               // 连线上传输的数据
    color: string | null      // 自定义颜色
}
```

### 3.4 序列化

```javascript
const data = graph.serialize();     // 导出为 JSON 对象
graph.configure(data);              // 从 JSON 恢复
```

### 3.5 全局输入输出（子图用）

```javascript
graph.addInput("name", "number", 0);  // 添加全局输入
graph.setInputData("name", 42);       // 设置全局输入值
graph.getInputData("name");           // 读取全局输入

graph.addOutput("result", "string");  // 添加全局输出
graph.setOutputData("result", "ok");
graph.getOutputData("result");
```

---

## 4. LGraphCanvas — 画布渲染与交互

### 4.1 创建与生命周期

```javascript
const canvas = document.getElementById("my-canvas");
const graphCanvas = new LGraphCanvas(canvas, graph);
// 常用设置：
graphCanvas.render_connection_arrows = false;  // 关闭连线箭头
graphCanvas.render_shadows = false;             // 关闭阴影
graphCanvas.links_render_mode = LiteGraph.STRAIGHT_LINK;  // 直线连线
```

### 4.2 完整渲染设置

在 `graphCanvas` 实例上设置：

| 属性 | 默认值 | 说明 |
|---|---|---|
| `render_shadows` | `true` | 节点阴影 |
| `render_canvas_border` | `true` | 画布边框 |
| `render_connections_shadows` | `false` | 连线阴影（耗性能） |
| `render_connections_border` | `true` | 连线边框 |
| `render_curved_connections` | `false` | 弯曲连线 |
| `render_connection_arrows` | `true` | 连线中点箭头 |
| `render_collapsed_slots` | `true` | 折叠节点显示插槽 |
| `render_execution_order` | `false` | 显示执行序号 |
| `render_title_colored` | `true` | 标题栏着色 |
| `render_link_tooltip` | `true` | 悬停连线显示提示 |
| `links_render_mode` | `2 (SPLINE)` | 连线样式：`0 直线 / 1 直角 / 2 贝塞尔` |
| `connections_width` | `3` | 连线粗细 |
| `round_radius` | `8` | 节点圆角 |
| `background_image` | `base64` | 背景图片 |
| `highquality_render` | `true` | 高质量渲染 |
| `use_gradients` | `false` | 标题栏渐变 |
| `clear_background` | `true` | 清除背景 |
| `clear_background_color` | `"#222"` | 背景色 |
| `read_only` | `false` | 只读模式 |
| `live_mode` | `false` | 演示模式（隐藏编辑器 UI） |
| `show_info` | `true` | 显示 FPS/节点数 |
| `allow_dragcanvas` | `true` | 允许拖拽画布 |
| `allow_dragnodes` | `true` | 允许拖拽节点 |
| `allow_interaction` | `true` | 允许交互 |
| `allow_searchbox` | `true` | 允许搜索框 |
| `allow_reconnect_links` | `true` | 允许重新连接 |
| `multi_select` | `false` | 无需 Ctrl 即可多选 |
| `align_to_grid` | `false` | 拖放时吸附网格 |
| `editor_alpha` | `1` | 编辑器透明度 |
| `pause_rendering` | `false` | 暂停渲染 |

### 4.3 事件回调

在 `graphCanvas` 实例上设置：

```javascript
graphCanvas.onDrawBackground = (ctx, visible_area) => {
    // 在节点后面绘制自定义内容（受缩放位移影响）
};

graphCanvas.onDrawForeground = (ctx, visible_rect) => {
    // 在节点前面绘制自定义内容（受缩放位移影响）
};

graphCanvas.onDrawOverlay = (ctx) => {
    // 在最上层绘制（屏幕坐标，不受缩放影响）
};

graphCanvas.onNodeMoved = (node) => { /* 节点移动后 */ };
graphCanvas.onSelectionChange = (selected_nodes) => { /* 选择变化 */ };
graphCanvas.onBeforeChange = (graph) => { /* 图修改前 */ };
graphCanvas.onAfterChange = (graph) => { /* 图修改后 */ };
graphCanvas.onMouse = (e) => { /* 全局鼠标事件 */ };
graphCanvas.onClear = () => { /* 画布清空 */ };
graphCanvas.onRender = (canvas, ctx) => { /* 渲染时 */ };
```

### 4.4 方法

```javascript
graphCanvas.setGraph(graph);                    // 切换显示的图
graphCanvas.openSubgraph(subgraphGraph);         // 打开子图
graphCanvas.closeSubgraph();                     // 关闭子图
graphCanvas.centerOnNode(node);                  // 居中到某节点
graphCanvas.setZoom(scale, centerPoint);         // 设置缩放
graphCanvas.selectNode(node, addToSelection);    // 选中节点
graphCanvas.deselectAllNodes();                  // 取消全选
graphCanvas.deleteSelectedNodes();               // 删除选中节点
graphCanvas.copyToClipboard();                   // 复制选中到剪贴板
graphCanvas.pasteFromClipboard(isConnectUnsel);  // 粘贴
graphCanvas.bringToFront(node);                  // 置前
graphCanvas.sendToBack(node);                    // 置后
graphCanvas.convertOffsetToCanvas(pos);          // 图坐标→屏幕坐标
graphCanvas.convertCanvasToOffset(pos);          // 屏幕坐标→图坐标
graphCanvas.setDirty(foreground, background);    // 标记重绘
graphCanvas.startRendering();                    // 开始渲染循环
graphCanvas.stopRendering();                     // 停止渲染循环
```

### 4.5 节点预设配色

```javascript
LGraphCanvas.node_colors = {
    red:     { color: "#F00", bgcolor: "#500", groupcolor: "#F44" },
    brown:   { color: "#930", bgcolor: "#420", groupcolor: "#C74" },
    green:   { color: "#0F0", bgcolor: "#050", groupcolor: "#4F4" },
    blue:    { color: "#03F", bgcolor: "#005", groupcolor: "#44F" },
    pale_blue: { color: "#9CF", bgcolor: "#135", groupcolor: "#8CF" },
    cyan:    { color: "#0FF", bgcolor: "#055", groupcolor: "#4FF" },
    purple:  { color: "#F0F", bgcolor: "#505", groupcolor: "#F4F" },
    yellow:  { color: "#FF0", bgcolor: "#550", groupcolor: "#FF4" },
    black:   { color: "#CCC", bgcolor: "#222", groupcolor: "#888" },
};

// 用法：
node.color = LGraphCanvas.node_colors.green.color;
node.bgcolor = LGraphCanvas.node_colors.green.bgcolor;
node.boxcolor = LGraphCanvas.node_colors.green.groupcolor;
```

---

## 5. LGraphNode — 节点基类

所有节点继承自 `LGraphNode`。在 Minashigo 中，功能节点继承 `ActionNode`（内部又继承 `LGraphNode`），纯流程节点直接继承 `LGraphNode`。

### 5.1 生命周期

```javascript
class MyNode extends LiteGraph.LGraphNode {
    constructor() {
        super();
        this.title = "我的节点";
        this.size = [200, 80];
        this.properties = { threshold: 0.5 };

        // 添加插槽
        this.addInput("触发", LiteGraph.EVENT);
        this.addOutput("下一步", LiteGraph.EVENT);
        this.addOutput("结果", "number");

        // 添加控件
        this.addWidget("number", "阈值", this.properties.threshold,
            (v) => (this.properties.threshold = v));
    }

    // 事件触发时调用
    onAction(action, param, options, action_slot) {
        // param 是来自连线的数据
    }

    // 每帧执行（仅 mode === ALWAYS 时）
    onExecute(param, options) {}

    // 在节点内部绘制额外内容
    onDrawForeground(ctx, graphcanvas) {}

    // 双击
    onDblClick(e, pos, graphcanvas) { return true; }

    // 鼠标事件
    onMouseDown(e, local_pos, graphcanvas) { return false; }
    onMouseMove(e, local_pos, graphcanvas) {}
    onMouseUp(e, local_pos, graphcanvas) {}

    // 序列化后恢复
    onConfigure(info) {}

    // 序列化时附加数据
    onSerialize(o) {}

    // 属性变更（返回 false 可撤销）
    onPropertyChanged(name, value, prev_value) {}

    // 连线验证（返回 false 拒绝连接）
    onConnectInput(slot, type, output, origin_node, output_slot) { return true; }
    onConnectOutput(slot, type, input, target_node, target_slot) { return true; }

    // 连线变更
    onConnectionsChange(type, slot, connected, link_info, slot_info) {}

    // 右键菜单扩展
    getExtraMenuOptions(graphcanvas, options) {
        options.push({ content: "自定义选项", callback: () => { /* ... */ } });
    }
}
```

### 5.2 插槽管理

```javascript
// 添加
this.addInput(name, type, extra_info);       // 返回插槽对象
this.addOutput(name, type, extra_info);      // 返回插槽对象
this.addInputs([["A", "number"], ["B", "string"]]);
this.addOutputs([["out", "number"]]);

// 删除
this.removeInput(slotIndex);
this.removeOutput(slotIndex);

// 查找
this.findInputSlot("name");                  // 返回索引或 -1
this.findOutputSlot("name");
this.findInputSlotFree({ /*opts*/ });         // 找空闲输入
this.findOutputSlotFree({ /*opts*/ });        // 找空闲输出

// 连接
this.connect(output_slot, target_node, target_input_slot);

// 断开
this.disconnectOutput(slot);
this.disconnectInput(slot);

// 查询
this.isInputConnected(slot);                 // true/false
this.isOutputConnected(slot);
this.getInputNode(slot);                     // 获取上游节点
this.getOutputNodes(slot);                   // 获取下游节点数组
```

### 5.3 数据传输

```javascript
// 从输入获取数据
const val = this.getInputData(slotIndex);
const val = this.getInputDataByName("slot_name");
const val = this.getInputOrProperty("name");  // 有连线取连线值，否则取 properties

// 向输出设置数据
this.setOutputData(slotIndex, data);
this.setOutputDataType(slotIndex, typeString);
```

### 5.4 事件触发

```javascript
// 触发指定输出插槽（触发下游节点的 onAction）
this.triggerSlot(slot, param, link_id, options);

// 触发所有匹配 action 名称的输出
this.trigger(action, param, options);

// 添加标准触发插槽
this.addOnTriggerInput();        // 添加 "onTrigger" 输入
this.addOnExecutedOutput();      // 添加 "onExecuted" 输出
```

### 5.5 属性管理

```javascript
this.addProperty(name, defaultValue, type, extra_info);
this.setProperty(name, value);

// properties_info — 属性编辑器的元数据
this.properties_info = [
    { name: "threshold", type: "number", label: "阈值", min: 0, max: 1, step: 0.01 },
    { name: "enabled", type: "boolean", label: "启用" },
    { name: "mode", type: "select", label: "模式",
        options: [{ value: "a", label: "A" }, { value: "b", label: "B" }] },
    { name: "text", type: "text", label: "标签名" },
];
```

### 5.6 节点状态

```javascript
this.flags.collapsed = true;     // 折叠
this.flags.pinned = true;        // 锁定位置
this.mode = LiteGraph.ALWAYS;    // 模式
node.pin(true);                  // 锁定/解锁
node.collapse(true);             // 折叠/展开
node.alignToGrid();              // 吸附网格

// 尺寸
this.size = [200, 100];         // 直接设置
this.computeSize();             // 根据插槽/控件自动计算最小尺寸
this.setSize([200, 100]);

// 请求重绘
this.setDirtyCanvas(true, true);
```

### 5.7 序列化

```javascript
const data = node.serialize();   // 返回 JSON 对象
node.configure(data);            // 从 JSON 恢复（会自动调用 onConfigure）
```

---

## 6. Widget 控件类型

通过 `this.addWidget(type, name, value, callback, options)` 添加。

### 6.1 number — 数字

```javascript
this.addWidget("number", "阈值", 0.9, (v) => (this.properties.threshold = v), {
    min: 0,
    max: 1,
    step: 1,           // 注意：内部有 0.1 系数，实际步进 = step × 0.1
    precision: 3,       // 显示小数位数，默认 3
    property: "threshold"  // 可选：自动绑定到 properties
});
```

### 6.2 slider — 滑块

```javascript
this.addWidget("slider", "音量", 0.5, (v) => (this.volume = v), {
    min: 0,
    max: 1,
    precision: 2,
    slider_color: "#89A"
});
```

### 6.3 combo — 下拉

```javascript
this.addWidget("combo", "策略", "best", (v) => (this.mode = v), {
    values: ["best", "left", "right", "top", "bottom"]
});
// values 也可以是对象或返回数组的函数
```

### 6.4 toggle — 开关

```javascript
this.addWidget("toggle", "启用", true, (v) => (this.enabled = v), {
    on: "true",    // 打开时文字
    off: "false"   // 关闭时文字
});
```

### 6.5 button — 按钮

```javascript
this.addWidget("button", "重置", null, () => {
    this.properties.threshold = 0.9;
    this.setDirtyCanvas(true, true);
});
```

### 6.6 string / text — 文本

```javascript
this.addWidget("string", "标签", "label_1", (v) => (this.properties.label = v), {
    property: "label"   // 自动绑定到 properties
});
```

### 6.7 自定义渲染 — preview_image

```javascript
// 这是一个特殊类型，需在 onDrawForeground 中自行绘制
// 不会在 drawNodeWidgets 中做通用渲染
this.addWidget("preview_image", "", null, null);
```

### Widget 高级属性

```javascript
// 每个 widget 对象上都可以设置：
widget.disabled = true;       // 禁用
widget.label = "显示名称";    // 覆盖 name 的显示
widget.y = 100;              // 精确 Y 坐标
widget.options.height = 30;  // 高度覆盖
widget.options.width = 200;  // 宽度覆盖
```

---

## 7. 上下文菜单 ContextMenu

```javascript
const menu = new LiteGraph.ContextMenu(
    values,     // 数组：字符串、对象或 null（分隔线）
    options     // 选项
);
```

**values 项：**
```javascript
[
    "简单项",               // 直接文字，callback = options.callback
    { content: "自定义项",
      callback: (val, opt, e, menu, extra) => { /* ... */ },
      disabled: false,
      has_submenu: true,
      submenu: { title: "子菜单", callback: ..., options: ... } },
    null                    // 分隔线
]
```

**options：**
```javascript
{
    title: "菜单标题",
    callback: (value, options, e, menu, extra) => { /* 全局回调 */ },
    event: mouseEvent,       // 用于定位
    parentMenu: parentMenu,
    ignore_item_callbacks: false,
    autoopen: false,         // 悬停展开子菜单
    scale: 1,
    className: "",
    scroll_speed: 0.1
}
```

---

## 8. 节点回调方法

完整可用回调列表，按调用时机分组：

### 生命周期
| 回调 | 触发时机 |
|---|---|
| `onAdded` | 添加到图（`graph.add()`） |
| `onRemoved` | 从图移除 |
| `onStart` | 图开始执行 |
| `onStop` | 图停止执行 |
| `onNodeCreated` | 节点实例创建后（`createNode` 内） |

### 执行
| 回调 | 触发时机 |
|---|---|
| `onExecute(param, options)` | 每帧执行（仅 `ALWAYS` 模式） |
| `onAction(action, param, options, action_slot)` | 事件触发时 |
| `onAfterExecuteNode(param, options)` | 执行后，触发下游前 |

### 绘制
| 回调 | 触发时机 |
|---|---|
| `onDrawForeground(ctx, graphcanvas)` | 在节点内部前景层绘制 |
| `onDrawBackground(ctx, graphcanvas, canvas)` | 在节点内部背景层绘制 |
| `onResize(size)` | 节点尺寸变化 |

### 鼠标
| 回调 | 触发时机 |
|---|---|
| `onMouseDown(e, pos, graphcanvas)` | 在节点上按下（return true 阻止拖拽） |
| `onMouseMove(e, pos, graphcanvas)` | 鼠标在节点上移动 |
| `onMouseUp(e, pos, graphcanvas)` | 鼠标在节点上释放 |
| `onMouseEnter(e)` | 鼠标进入节点区域 |
| `onMouseLeave(e)` | 鼠标离开节点区域 |
| `onDblClick(e, pos, graphcanvas)` | 双击 |
| `onInputDblClick(slot, e)` | 双击输入插槽 |
| `onOutputDblClick(slot, e)` | 双击输出插槽 |
| `onInputClick(slot, e)` | 单击输入插槽 |
| `onOutputClick(slot, e)` | 单击输出插槽 |
| `onKeyDown(e)` | 节点选中时按键 |
| `onKeyUp(e)` | 节点选中时松键 |

### 序列化
| 回调 | 触发时机 |
|---|---|
| `onConfigure(info)` | 从 JSON 恢复后 |
| `onSerialize(o)` | 序列化时（修改 o 添加额外数据） |
| `onPropertyChanged(name, value, prev)` | 属性变更（return false 可撤销） |

### 连线
| 回调 | 触发时机 |
|---|---|
| `onConnectInput(slot, type, output, origin_node, output_slot)` | 输入端即将连接（return false 拒绝） |
| `onConnectOutput(slot, type, input, target_node, target_slot)` | 输出端即将连接（return false 拒绝） |
| `onConnectionsChange(type, slot, connected, link_info, slot_info)` | 连线变更 |

### 拖放
| 回调 | 触发时机 |
|---|---|
| `onDropItem(event)` | DOM 元素拖到节点上 |
| `onDropFile(file)` | 文件拖到节点上 |
| `onDropData(data, filename, file)` | 原始数据拖到节点上 |

### 菜单
| 回调 | 触发时机 |
|---|---|
| `getExtraMenuOptions(graphcanvas, options)` | 右键菜单时（向 options 数组 push 自定义项） |

---

## 9. 实用函数

### 9.1 画布工具

```javascript
// 在 graphCanvas 上设置即可使用
graphCanvas.onDrawBackground = (ctx, visible_area) => {
    // visible_area = [x, y, w, h] 当前可见区域（图坐标）
};

graphCanvas.onDrawOverlay = (ctx) => {
    // 屏幕坐标，适合绘制 HUD
    ctx.fillStyle = "#FFF";
    ctx.fillText("自定义覆盖", 10, 20);
};
```

### 9.2 坐标转换

```javascript
const graphPos = graphCanvas.convertCanvasToOffset([screenX, screenY]);
const screenPos = graphCanvas.convertOffsetToCanvas([graphX, graphY]);
```

### 9.3 几何工具

```javascript
LiteGraph.distance(a, b);                     // 欧几里得距离
LiteGraph.isInsideRectangle(x, y, left, top, w, h);
LiteGraph.overlapBounding(a, b);              // 矩形重叠检测（[x,y,w,h]）
LiteGraph.hex2num("#FF8800");                 // → [255, 136, 0]
LiteGraph.num2hex([255, 136, 0]);             // → "#FF8800"
```

---

## 快速参考：常用设置组合

```javascript
// 在 main.js 或 tab.js 等初始化位置设置

// 全局行为
LiteGraph.node_box_coloured_when_on = true;   // 执行高亮
LiteGraph.alt_drag_do_clone_nodes = true;      // Alt+拖拽克隆
LiteGraph.shift_click_do_break_link_from = true;
LiteGraph.auto_sort_node_types = true;

// 画布渲染
const canvas = new LGraphCanvas(canvasEl, graph);
canvas.render_connection_arrows = false;       // 关掉连线箭头
canvas.render_shadows = false;                 // 关阴影（更清爽）
canvas.render_connections_border = false;      // 关连线边框
canvas.connections_width = 2;                  // 细一点
canvas.round_radius = 4;                       // 小圆角
canvas.links_render_mode = LiteGraph.STRAIGHT_LINK;  // 直线
canvas.render_execution_order = true;          // 显示执行序号
canvas.align_to_grid = true;                   // 吸附网格
```
