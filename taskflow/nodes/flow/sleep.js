// taskflow/nodes/flow/sleep.js
import { reportNodeEvent } from "../../core/node-reporter.js";
import {openNodePropertyEditor} from "../../core/input-dialog.js";

class SleepNode extends LiteGraph.LGraphNode {
  static title = "等待";

  constructor() {
    super();
    this.title = "等待";
    this.category = "Flow";
    this.addInput("触发", LiteGraph.EVENT);
    this.addInput("秒数", "number");
    this.addInput("上限", "number");
    this.addOutput("下一步", LiteGraph.EVENT);

    this.properties = { seconds: 1, upper_limit: null, step: 0.05 };
    this.addWidget("number", "秒数", this.properties.seconds, v => (this.properties.seconds = v));
    this.addWidget("number", "上限", this.properties.upper_limit, v => (this.properties.upper_limit = v));

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
        name: "seconds",
        type: "number",
        label: "秒数",
        min: 0,
        step: 0.05
      },
      {
        name: "upper_limit",
        type: "number",
        label: "上限",
        min: 0,
        step: 0.05
      },
      {
        name: "step",
        type: "number",
        label: "步长",
        min: 0.01,
        max: 1,
        step: 0.01
      }
    ];

    // 延迟事件队列
    this._pending = [];

    // 设置为ALWAYS模式，确保onExecute被调用
    this.mode = LiteGraph.ALWAYS;
  }

  getHelpText() {
        return "等待指定时间(秒)<br>支持小数，如 0.5 表示 500ms";
    }

  onDblClick() {
    openNodePropertyEditor(this);
    return true;
  }

  onConfigure(info) {
    // 同步widget值到properties
    if (this.widgets) {
      this.widgets.forEach(widget => {
        if (widget.name === "秒数") {
          widget.value = this.properties.seconds;
        } else if (widget.name === "上限") {
          widget.value = this.properties.upper_limit;
        }
      });
    }
  }

  async onAction(action, param, options) {
    const seconds = this.getInputData(1) ?? this.properties.seconds;
    const upper_limit = this.getInputData(2) ?? this.properties.upper_limit;

    window.taskflowLog?.("info", `[等待] 等待 ${seconds} 秒${upper_limit != null ? ` (上限 ${upper_limit}s)` : ""}`);

    if (seconds <= 0) {
      window.taskflowLog?.("info", "[等待] 等待时间为0，跳过");
      return;
    }

    // 使用async/await实现阻塞等待
    const delayMs = seconds * 1000;
    console.log(`[SleepNode] 开始等待 ${seconds} 秒 (${delayMs}ms)`);

    const controller = window.workflowController;
    const startTime = Date.now();

    const interval = 50;
    let elapsed = 0;

    while (elapsed < delayMs) {
      // 检查控制器状态，如果停止则中断
      if (controller && (controller.state === "stopped" || controller.state === "idle")) {
        window.taskflowLog?.("warn", "[等待] 控制器已停止，中断等待");
        return;
      }

      // 等待一小段时间
      await new Promise(resolve => setTimeout(resolve, Math.min(interval, delayMs - elapsed)));
      elapsed = Date.now() - startTime;
    }

    window.taskflowLog?.("info", `[等待] 等待完成 (${(elapsed/1000).toFixed(2)}s)`);
    await this.execOutput(0);
  }

  onExecute(param, options) {
    // 不需要onExecute
  }
}
LiteGraph.registerNodeType("flow/sleep", SleepNode);