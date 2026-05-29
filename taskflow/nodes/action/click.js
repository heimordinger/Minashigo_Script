// taskflow/nodes/action/click.js
import { ActionNode } from "../../core/action-node.js";
import { openNodePropertyEditor } from "../../core/input-dialog.js";

class ClickNode extends ActionNode {
  static title = "点击坐标";
  constructor() {
    super("点击坐标");
    this.category = "Action";

    this.addInput("触发", LiteGraph.EVENT);
    this.addInput("x","number");
    this.addInput("y","number");

    this.addOutput("下一步",LiteGraph.EVENT);
    this.addOutput("成功","boolean");

    // 属性（默认值）
    this.properties = {
      down_time: 0.12,
      pianyi_x: 0,
      pianyi_y: 0,
      max_delay: null,
      start_count: 0,
      use_human_offset: true
    };

    // 添加小部件（属性面板）
    this.addWidget("number", "按下时长(ms)", this.properties.down_time, (v) => {
      this.properties.down_time = v;
    });
    this.addWidget("number", "偏移X", this.properties.pianyi_x, (v) => {
      this.properties.pianyi_x = v;
    });
    this.addWidget("number", "偏移Y", this.properties.pianyi_y, (v) => {
      this.properties.pianyi_y = v;
    });
    this.addWidget("checkbox", "拟人偏移", this.properties.use_human_offset, (v) => {
      this.properties.use_human_offset = v;
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
        name: "down_time",
        type: "number",
        label: "按下时长(ms)",
        min: 0,
        step: 0.01
      },
      {
        name: "pianyi_x",
        type: "number",
        label: "偏移X",
        step: 1
      },
      {
        name: "pianyi_y",
        type: "number",
        label: "偏移Y",
        step: 1
      },
      {
        name: "max_delay",
        type: "number",
        label: "最大延迟",
        min: 0,
        step: 0.01
      },
      {
        name: "start_count",
        type: "number",
        label: "开始计数",
        min: 0,
        step: 1
      },
    ];

    this._executed = false;
  }

  onDblClick() {
    openNodePropertyEditor(this);
    return true;
  }

  onAction(action) {
    this.run(action);
  }

  async onRun() {
    const x = this.getInputData(1) ?? this.properties.x;
    const y = this.getInputData(2) ?? this.properties.y;

    if (x == null || y == null) {
      throw new Error("x 或 y 为空");
    }

    this.log(`点击坐标: (${x}, ${y})`);
    const response = await this.callBackend("click", {
      x,
      y,
      down_time: this.properties.down_time,
      pianyi_x: this.properties.pianyi_x,
      pianyi_y: this.properties.pianyi_y,
      max_delay: this.properties.max_delay,
      start_count: this.properties.start_count,
    });
    this.setOutputData(1, !!response?.success);
    this.log(response?.success ? "点击完成" : "点击失败", response?.success ? "info" : "warn");
  }
}

LiteGraph.registerNodeType("action/click", ClickNode);