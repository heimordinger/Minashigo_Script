import { ActionNode } from "../../core/action-node.js";
import { openNodePropertyEditor } from "../../core/input-dialog.js";
import { pickFromAssets } from "../../js/utils/filePicker.js";

class ClickImageNode extends ActionNode {
  static title = "点击图片";

  constructor() {
    super("点击图片");
    this.category = "Action";
    this.addInput("触发", LiteGraph.EVENT);
    this.addInput("图片路径", "string");
    this.addOutput("下一步", LiteGraph.EVENT);
    this.addOutput("成功", "boolean");
    this.addOutput("点击X", "number");
    this.addOutput("点击Y", "number");
    this.addOutput("匹配度", "number");

    this.properties = {
      image: "",
      threshold: 0.9,
      pianyi_x: 0,
      pianyi_y: 0,
      down_time: 0.12,
      max_delay: null,
      start_count: 0,
      use_color_check: false,
      match_select: "best",
      use_human_offset: true,
    };

    this._imgPathWidget = this.addWidget("text", "图片路径", this.properties.image, v => (this.properties.image = v));
    this.addWidget("button", "选择图片", null, () => this._selectImage());
    this.addWidget("button", "从库选择", null, () => this._selectFromAssets());
    this.addWidget("number", "阈值", this.properties.threshold, v => (this.properties.threshold = v));
    this.addWidget("number", "按下时长", this.properties.down_time, v => (this.properties.down_time = v));
    this.addWidget("preview_image", "", null, null);

    this.previewImage = null;
    this.size = [360, 300];

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

    // 添加属性配置（不包含image，使用文件选择器）
    this.properties_info = [
      {
        name: "threshold",
        type: "number",
        label: "阈值",
        min: 0,
        max: 1,
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
        name: "down_time",
        type: "number",
        label: "按下时长",
        min: 0,
        step: 0.01
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
        name: "use_color_check",
        type: "boolean",
        label: "颜色校验"
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
          { value: "bottom", label: "底部" },
          { value: "top_left", label: "左上" },
          { value: "top_right", label: "右上" },
          { value: "bottom_left", label: "左下" },
          { value: "bottom_right", label: "右下" }
        ]
      },
      {
        name: "use_human_offset",
        type: "boolean",
        label: "拟人偏移"
      }
    ];
  }

  onDblClick() {
    openNodePropertyEditor(this);
    return true;
  }

  onConfigure(info) {
    if (this.widgets) {
      this.widgets.forEach(widget => {
        if (widget.name === "图片路径") {
          if (this.properties.image && this.properties.image.startsWith("data:image")) {
            widget.value = "(内嵌图片)";
            this._loadPreviewFromBase64(this.properties.image);
          } else {
            widget.value = this.properties.image;
          }
        } else if (widget.name === "阈值") {
          widget.value = this.properties.threshold;
        } else if (widget.name === "按下时长") {
          widget.value = this.properties.down_time;
        }
      });
    }
  }

  _selectImage() {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "image/*";

    input.onchange = e => {
      const file = e.target.files[0];
      if (!file) return;

      const path = file.path || file.name;

      // 转换为base64存储
      const reader = new FileReader();
      reader.onload = e => {
        this.properties.image = e.target.result; // 存储base64
        this._imgPathWidget.value = path; // widget显示路径
        this._loadPreviewFromBase64(e.target.result);
        this.setDirtyCanvas(true, true);
      };
      reader.readAsDataURL(file);
    };

    input.click();
  }

  _selectFromAssets() {
    pickFromAssets((dataUrl, fileName) => {
      this.properties.image = dataUrl;
      this._imgPathWidget.value = fileName;
      this._loadPreviewFromBase64(dataUrl);
      this.setDirtyCanvas(true, true);
    });
  }

  _loadPreviewFromBase64(base64) {
    const img = new Image();
    img.onload = () => {
      this.previewImage = img;
      this.setDirtyCanvas(true, true);
    };
    img.src = base64;
  }

  onAction(action) {
    this.run(action);
  }

  async onRun() {
    const image = this.getInputData(1) ?? this.properties.image;
    if (!image) throw new Error("image 为空");

    const name = image.startsWith("data:") ? "(内嵌图片)" : image.split("/").pop();
    this.log(`点击图片: ${name}`);

    const response = await this.callBackend("click_image", { ...this.properties, image });
    const data = response?.data || {};
    this.setOutputData(1, !!response?.success);
    this.setOutputData(2, data.clicked_x ?? null);
    this.setOutputData(3, data.clicked_y ?? null);
    this.setOutputData(4, data.match_value ?? null);

    if (data.clicked_x != null) {
      this.log(`点击完成: (${data.clicked_x}, ${data.clicked_y})`);
    } else {
      this.log(`点击失败: 未找到图片`, "warn");
    }
  }
}

LiteGraph.registerNodeType("action/click_image", ClickImageNode);