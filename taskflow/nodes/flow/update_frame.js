import { ActionNode } from "../../core/action-node.js";
import { openNodePropertyEditor } from "../../core/input-dialog.js";

class UpdateFrameNode extends ActionNode {
  static title = "刷新帧";

  constructor() {
    super("刷新帧");
    this.category = "Flow";

    this.addInput("触发", LiteGraph.EVENT);
    this.addOutput("下一步", LiteGraph.EVENT);
    this.addOutput("成功", "boolean");

    this.properties = {
      save_screenshot: false
    };

    this.addWidget("toggle", "保存截图", this.properties.save_screenshot, v => (this.properties.save_screenshot = v));

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
        name: "save_screenshot",
        type: "boolean",
        label: "保存截图"
      }
    ];
  }

  getHelpText() {
        return "刷新当前页面帧";
    }

  onDblClick() {
    openNodePropertyEditor(this);
    return true;
  }

  onConfigure(info) {
    if (this.widgets) {
      this.widgets.forEach(widget => {
        if (widget.name === "保存截图") {
          widget.value = this.properties.save_screenshot;
        }
      });
    }
  }

  async onAction(action) {
    await this.run(action);
  }

  async onRun() {
    this.log("刷新浏览器帧...");
    try {
      const response = await this.callBackend("update_frame", {
        save_screenshot: this.properties.save_screenshot
      });
      const success = response?.success !== false;
      this.setOutputData(1, success);
      this.log(success ? "帧刷新完成" : "帧刷新失败", success ? "info" : "warn");
    } catch (e) {
      this.log(`帧刷新异常: ${e.message}`, "error");
      console.error("UpdateFrameNode failed:", e);
      throw e;
    }
  }
}

LiteGraph.registerNodeType("flow/update_frame", UpdateFrameNode);
