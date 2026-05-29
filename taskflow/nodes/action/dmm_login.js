// taskflow/nodes/action/dmm_login.js
import { ActionNode } from "../../core/action-node.js";
import { openNodePropertyEditor } from "../../core/input-dialog.js";

class DMMLoginNode extends ActionNode {
  static title= "DMM 登录";
  constructor() {
    super("DMM 登录");
    this.category = "Action";

    this.addInput("触发", LiteGraph.EVENT);
    this.addOutput("下一步", LiteGraph.EVENT);
    this.addOutput("成功", "boolean");

    this.properties = {
      game_name: "",
      email: "",
      password: "",
      timeout: 30000
    };

    this.addWidget("number", "超时(ms)", this.properties.timeout, v => (
      this.properties.timeout = v),
      {step: 100 }
    );

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
        name: "game_name",
        type: "text",
        label: "游戏名称"
      },
      {
        name: "email",
        type: "text",
        label: "邮箱"
      },
      {
        name: "password",
        type: "text",
        label: "密码"
      },
      {
        name: "timeout",
        type: "number",
        label: "超时(ms)",
        min: 0,
        step: 100
      }
    ];
  }

  onDblClick() {
    openNodePropertyEditor(this);
    return true;
  }

  onAction(action) {
    this.run(action);
  }

  async onRun() {
    if (!this.properties.game_name) throw new Error("game_name 为空");
    this.log(`DMM登录: ${this.properties.game_name}`);
    const response = await this.callBackend("dmm_login", { ...this.properties }, this.properties.timeout);
    this.setOutputData(1, !!response?.success);
    this.log(response?.success ? "DMM登录成功" : "DMM登录失败", response?.success ? "info" : "warn");
  }
}

LiteGraph.registerNodeType("action/dmm_login", DMMLoginNode);