import { reportNodeEvent } from "../../core/node-reporter.js";
import {openNodePropertyEditor} from "../../core/input-dialog.js";

class LabelNode extends LiteGraph.LGraphNode {
  static title = "标签";

  constructor() {
    super();
    this.title = "标签";
    this.category = "Flow";

    this.addOutput("标记", LiteGraph.EVENT);

    this.properties = {
      label: "label_1"
    };

    this._labelWidget = this.addWidget("text", "标签名", this.properties.label, (value) => {
      this.properties.label = value;
    });

    this.size = [200, 80];

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
        name: "label",
        type: "text",
        label: "标签名"
      }
    ];
  }

  onDblClick() {
    openNodePropertyEditor(this);
    return true;
  }

  onConfigure(info) {
    // 同步widget值到properties
    if (this._labelWidget) {
      this._labelWidget.value = this.properties.label;
    }
  }

  onAction(action, param, options) {
    window.taskflowLog?.("info", `[标签] ${this.properties.label}`);
    reportNodeEvent(this, "trigger", { label: this.properties.label });
  }
}

LiteGraph.registerNodeType("flow/label", LabelNode);