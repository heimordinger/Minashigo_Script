// taskflow/nodes/action/click_text.js
import { ActionNode } from "../../core/action-node.js";
import { openNodePropertyEditor } from "../../core/input-dialog.js";

class ClickTextNode extends ActionNode {
  static title = "点击文本";
  constructor() {
    super("点击文本");
    this.category = "Action";

    this.addInput("触发", LiteGraph.EVENT);
    this.addInput("文本", "string");
    this.addInput("阈值", "number");
    this.addInput("偏移X", "number");
    this.addInput("偏移Y", "number");
    this.addInput("最大延迟", "number");
    this.addInput("开始计数", "number");

    this.addOutput("下一步", LiteGraph.EVENT);
    this.addOutput("成功", "boolean");
    this.addOutput("点击X", "number");
    this.addOutput("点击Y", "number");
    this.addOutput("错误", LiteGraph.EVENT);

    this.properties = {
      text: "",
      threshold: 60,
      pianyi_x: 0,
      pianyi_y: 0,
      max_delay: null,
      start_count: 0,
      match_select: "best",
      use_human_offset: true
    };

    this.addWidget("text", "文本", this.properties.text, (v) => {
      this.properties.text = v;
    });
    this.addWidget("number", "阈值", this.properties.threshold, (v) => {
      this.properties.threshold = v;
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
        name: "text",
        type: "text",
        label: "文本"
      },
      {
        name: "threshold",
        type: "number",
        label: "阈值",
        min: 0,
        max: 100,
        step: 1
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
      {
        name: "match_select",
        type: "select",
        label: "匹配策略",
        options: [
          { value: "best", label: "最佳" },
          { value: "left", label: "左侧" },
          { value: "right", label: "右侧" },
          { value: "top", label: "顶部" },
          { value: "bottom", label: "底部" }
        ]
      },
      {
        name: "use_human_offset",
        type: "boolean",
        label: "拟人偏移"
      }
    ];

    this._executed = false;
  }

  getHelpText() {
        return "点击屏幕上指定文本的位置";
    }

  onDblClick() {
    openNodePropertyEditor(this);
    return true;
  }

  async onAction(action) {
    await this.run(action);
  }

  async onRun() {
    const text = this.getInputData(1) ?? this.properties.text;
    if (!text) throw new Error("text 为空");
    this.log(`点击文本: "${text}"`);
    const response = await this.callBackend("click_text", { ...this.properties, text });
    const data = response?.data || {};
    this.setOutputData(1, !!response?.success);
    this.setOutputData(2, data.clicked_x ?? null);
    this.setOutputData(3, data.clicked_y ?? null);
    if (data.clicked_x != null) {
      this.log(`文本点击完成: (${data.clicked_x}, ${data.clicked_y})`);
    } else {
      this.log(`文本未找到: "${text}"`, "warn");
    }
  }
}

LiteGraph.registerNodeType("action/click_text", ClickTextNode);