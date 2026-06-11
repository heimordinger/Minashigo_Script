import { ActionNode } from "../../core/action-node.js";
import { openNodePropertyEditor } from "../../core/input-dialog.js";

class urlGoto extends ActionNode {
  static title = "跳转网址";
  constructor() {
      super("跳转网址");
      this.category = "Action";

      this.addInput("触发", LiteGraph.EVENT);
      this.addInput("网址", "string");

      this.addOutput("下一步", LiteGraph.EVENT);
      this.addOutput("成功", "boolean");

      this.properties = { url: "" };

      this.addWidget("text", "网址", "", (v) => {
      this.properties.url = v;
    });

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
        name: "url",
        type: "text",
        label: "网址"
      }
    ];
  }

  getHelpText() {
        return "导航到指定 URL<br>等待加载: 是否等待页面完全加载";
    }

  onDblClick() {
    openNodePropertyEditor(this);
    return true;
  }

  async onAction(action) {
    await this.run(action);
  }

  async onRun() {
    const url = this.getInputData(1) ?? this.properties.url;
    if (!url) throw new Error("url 为空");
    this.log(`跳转到: ${url}`);
    const response = await this.callBackend("goto", { url });
    this.setOutputData(1, !!response?.success);
    this.log(response?.success ? "跳转成功" : "跳转失败", response?.success ? "info" : "warn");
  }
}

LiteGraph.registerNodeType("action/url_goto", urlGoto);