// taskflow/nodes/flow/delay.js
import { reportNodeEvent } from "../../core/node-reporter.js";
import { openNodePropertyEditor } from "../../core/input-dialog.js";

class DelayNode extends LiteGraph.LGraphNode {
  static title = "延迟";

  constructor() {
    super();
    this.category = "Flow";
    this.addInput("触发", LiteGraph.EVENT);
    this.addOutput("下一步", LiteGraph.EVENT);
    this.properties = { ms: 1000 };
    this.addWidget("number", "延迟(ms)", this.properties.ms, v => (this.properties.ms = v));

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

    // 添加属性配置
    this.properties_info = [
      {
        name: "ms",
        type: "number",
        label: "延迟(ms)",
        min: 0,
        step: 1
      }
    ];
  }

  onDblClick() {
    openNodePropertyEditor(this);
    return true;
  }

  onConfigure(info) {
    // 同步widget值到properties
    if (this.widgets) {
      this.widgets.forEach(widget => {
        if (widget.name === "延迟(ms)") {
          widget.value = this.properties.ms;
        }
      });
    }
  }

  async onAction() {
    window.taskflowLog?.("info", `[延迟] ${this.properties.ms}ms`);
    reportNodeEvent(this, "trigger", { ms: this.properties.ms });
    const ms = Math.max(0, Number(this.properties.ms) || 0);
    await new Promise(resolve => setTimeout(resolve, ms));
    this.triggerSlot(0);
  }
}

LiteGraph.registerNodeType("flow/delay", DelayNode);