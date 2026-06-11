import { reportNodeEvent } from "../../core/node-reporter.js";
import { openNodePropertyEditor } from "../../core/input-dialog.js";

class LabelNode extends LiteGraph.LGraphNode {
  static title = "标签";

  constructor() {
    super();
    this.title = "标签";
    this.category = "Flow";

    this.addOutput("标记", LiteGraph.EVENT);

    this.properties = { label: "label_1" };

    this._labelWidget = this.addWidget("text", "标签名", this.properties.label, v => {
      this.properties.label = v;
      this.setDirtyCanvas(true, true);
    });

    this._labelWidget.onKeyDown = e => { e.stopPropagation(); e.preventDefault(); return false; };

    this.size = [200, 80];

    this.properties_info = [
      { name: "label", type: "text", label: "标签名" }
    ];
  }

  getHelpText() {
        return "标记位置，供跳转节点使用<br>标签名: 跳转目标名称";
    }

  onDblClick() {
    openNodePropertyEditor(this);
    return true;
  }

  onConfigure(info) {
    if (this._labelWidget) this._labelWidget.value = this.properties.label;
  }

  async onAction(action, param, options) {
    window.taskflowLog?.("info", `[标签] ${this.properties.label}`);
    reportNodeEvent(this, "trigger", { label: this.properties.label });
  }
}

LiteGraph.registerNodeType("flow/label", LabelNode);