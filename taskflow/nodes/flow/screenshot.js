import { openNodePropertyEditor } from "../../core/input-dialog.js";
// taskflow/nodes/flow/screenshot.js
import { reportNodeEvent } from "../../core/node-reporter.js";

class Screenshot extends LiteGraph.LGraphNode{
  constructor() {
    super();
    this.title = "截图";
    this.category = "Flow";

    this.addInput("触发",LiteGraph.EVENT);
    this.addOutput("下一步",LiteGraph.EVENT)
  }

  getHelpText() {
        return "截取当前屏幕";
    }

  onDblClick() {
    openNodePropertyEditor(this);
    return true;
  }

  async onAction() {
    window.taskflowLog?.("info", "[截图] 执行截图");
    reportNodeEvent(this, "trigger");
    await this.execOutput(0);
  }
}

LiteGraph.registerNodeType("flow/screenshot", Screenshot);