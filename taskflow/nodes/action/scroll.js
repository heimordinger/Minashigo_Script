// taskflow/nodes/action/scroll.js
// 滚动操作节点 —— 支持增量滚动、滚到底部、滚到顶部
import { ActionNode } from "../../core/action-node.js";
import { openNodePropertyEditor } from "../../core/input-dialog.js";

class ScrollNode extends ActionNode {
  static title = "滚动";

  constructor() {
    super("滚动");
    this.category = "Action";

    this.addInput("触发", LiteGraph.EVENT);
    this.addOutput("下一步", LiteGraph.EVENT);
    this.addOutput("错误", LiteGraph.EVENT);

    // ── 模式选择 ──
    this.properties = {
      mode: "scroll",            // "scroll" | "scroll_to_bottom" | "scroll_to_top"
      delta_x: 0,
      delta_y: -300,             // 默认向下 300px（屏幕坐标负=向下）
      x: null,
      y: null,
      steps: 10,
      scroll_time: 0.3,
      smooth: true,
      step_size: 300,
      interval: 0.1,
    };

    this.addWidget("combo", "模式", this.properties.mode, (v) => {
      this.properties.mode = v;
      this._updateWidgets();
    }, { values: ["scroll", "scroll_to_bottom", "scroll_to_top"] });

    this._deltaXWidget = this.addWidget("number", "水平偏移", this.properties.delta_x, (v) => (this.properties.delta_x = v));
    this._deltaYWidget = this.addWidget("number", "垂直偏移", this.properties.delta_y, (v) => (this.properties.delta_y = v));
    this._stepsWidget = this.addWidget("number", "步数", this.properties.steps, (v) => (this.properties.steps = v));
    this._timeWidget = this.addWidget("number", "时长(秒)", this.properties.scroll_time, (v) => (this.properties.scroll_time = v));
    this._smoothWidget = this.addWidget("checkbox", "平滑滚动", this.properties.smooth, (v) => (this.properties.smooth = v));
    this._stepSizeWidget = this.addWidget("number", "每步像素", this.properties.step_size, (v) => (this.properties.step_size = v));
    this._intervalWidget = this.addWidget("number", "间隔(秒)", this.properties.interval, (v) => (this.properties.interval = v));

    this.size = [320, 200];

    // 禁用widget的键盘输入，强制使用属性编辑器
    if (this.widgets) {
      this.widgets.forEach(widget => {
        widget.onKeyDown = (e) => {
          e.stopPropagation();
          e.preventDefault();
          return false;
        };
      });
    }

    this.properties_info = [
      {
        name: "mode",
        type: "select",
        label: "模式",
        options: [
          { value: "scroll", label: "增量滚动" },
          { value: "scroll_to_bottom", label: "滚动到底部" },
          { value: "scroll_to_top", label: "滚动到顶部" },
        ],
      },
      { name: "delta_x", type: "number", label: "水平偏移", step: 1 },
      { name: "delta_y", type: "number", label: "垂直偏移", step: 1 },
      { name: "x", type: "number", label: "起始X（可选）", step: 1 },
      { name: "y", type: "number", label: "起始Y（可选）", step: 1 },
      { name: "steps", type: "number", label: "分步步数", min: 1, step: 1 },
      { name: "scroll_time", type: "number", label: "滚动时长(秒)", min: 0, step: 0.1 },
      { name: "smooth", type: "boolean", label: "平滑滚动" },
      { name: "step_size", type: "number", label: "每步像素", step: 1 },
      { name: "interval", type: "number", label: "间隔(秒)", min: 0, step: 0.1 },
    ];

    this._updateWidgets();
  }

  _updateWidgets() {
    const mode = this.properties.mode;
    // 增量滚动：显示偏移、步数、时长
    this._deltaXWidget?.setVisibility?.(mode === "scroll");
    this._deltaYWidget?.setVisibility?.(mode === "scroll");
    this._stepsWidget?.setVisibility?.(mode === "scroll");
    this._timeWidget?.setVisibility?.(mode === "scroll");
    // 滚到底/顶：显示平滑、步长、间隔
    this._smoothWidget?.setVisibility?.(mode !== "scroll");
    this._stepSizeWidget?.setVisibility?.(mode === "scroll_to_bottom");
    this._intervalWidget?.setVisibility?.(mode === "scroll_to_bottom");
    this.size = [320, mode === "scroll" ? 200 : mode === "scroll_to_bottom" ? 200 : 180];
  }

  getHelpText() {
    return (
      "滚动操作，支持三种模式：<br>" +
      "<b>增量滚动</b> — 按指定像素滚动<br>" +
      "<b>滚动到底部</b> — 平滑滚动至页面底部<br>" +
      "<b>滚动到顶部</b> — 滚动回到页面顶部<br><br>" +
      "提示: <b>垂直偏移</b>负值=向下，正值=向上"
    );
  }

  onDblClick() {
    openNodePropertyEditor(this);
    return true;
  }

  async onAction(action) {
    await this.run(action);
  }

  async onRun() {
    const mode = this.properties.mode;
    const taskName = mode;
    this.log(`滚动模式: ${mode}`);

    const props = { ...this.properties };
    // 只传模式对应的参数
    if (mode === "scroll") {
      props.x = this.properties.x;
      props.y = this.properties.y;
    }

    const response = await this.callBackend(taskName, props);
    this.log(response?.success ? "滚动完成" : "滚动失败", response?.success ? "info" : "warn");
  }
}

LiteGraph.registerNodeType("action/scroll", ScrollNode);
