import { openNodePropertyEditor } from "../../core/input-dialog.js";
import { pickFromAssets } from "../../js/utils/filePicker.js";

class MatchImage extends LiteGraph.LGraphNode {
  static title = "匹配图片";
  constructor() {
    super();
    this.title = "匹配图片";
    this.category = "Action";
    this.addInput("触发", LiteGraph.EVENT);

    this.addInput("图片路径", "string")
    this.addInput("阈值", "number")


    this.addOutput("下一步", LiteGraph.EVENT);
    this.addOutput("x","number");
    this.addOutput("y","number");
    this.addOutput("max_val","number");
    this.addOutput("成功", LiteGraph.EVENT);
    this.addOutput("失败", LiteGraph.EVENT);


    /* ========= Properties ========= */
    this.properties = {
      image: "",
      threshold: 0.9,
      match_select: "best",
      use_color_check: false
    };

    this.previewImage = null;
    this._executed = false;

    /* ========= Widgets ========= */

    this.addWidget(
      "number",
      "阈值",
      this.properties.threshold,
      v => (this.properties.threshold = v),
      { min: 0, max: 1, step: 0.01 }
    );

    this.addWidget(
      "combo",
      "匹配策略",
      this.properties.match_select,
      v => (this.properties.match_select = v),
      {
        values: [
          "best",
          "left",
          "right",
          "top",
          "bottom",
          "top_left",
          "top_right",
          "bottom_left",
          "bottom_right"
        ]
      }
    );

    this.addWidget(
      "toggle",
      "颜色校验",
      this.properties.use_color_check,
      v => (this.properties.use_color_check = v)
    );

    this._imgPathWidget = this.addWidget(
      "text",
      "图片路径",
      this.properties.image,
      v => (this.properties.image = v)
    );

    this.addWidget("button", "选择图片", null, () => this._selectImage());
    this.addWidget("button", "从库选择", null, () => this._selectFromAssets());

    this.addWidget("preview_image", "", null, null);

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
        name: "use_color_check",
        type: "boolean",
        label: "颜色校验"
      }
    ];

    // this.size = [360, 380];
  }

  getHelpText() {
        return "在屏幕上匹配图片位置<br>阈值: 匹配灵敏度(0-1)";
    }

  onDblClick() {
    openNodePropertyEditor(this);
    return true;
  }

  onConfigure(info) {
    // 从properties同步widget值
    if (this.widgets) {
      this.widgets.forEach(widget => {
        if (widget.name === "阈值") {
          widget.value = this.properties.threshold;
        } else if (widget.name === "匹配策略") {
          widget.value = this.properties.match_select;
        } else if (widget.name === "颜色校验") {
          widget.value = this.properties.use_color_check;
        } else if (widget.name === "图片路径") {
          // base64数据不显示原始字符串，显示占位文本
          if (this.properties.image && this.properties.image.startsWith("data:image")) {
            widget.value = "(内嵌图片)";
            this._loadPreviewFromBase64(this.properties.image);
          } else {
            widget.value = this.properties.image;
          }
        }
      });
    }
  }

  /* ========= File Picker ========= */

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

  /* ========= Select from Assets ========= */

  _selectFromAssets() {
    pickFromAssets((dataUrl, fileName) => {
      this.properties.image = dataUrl;
      this._imgPathWidget.value = fileName;
      this._loadPreviewFromBase64(dataUrl);
      this.setDirtyCanvas(true, true);
    });
  }

  /* ========= Load Preview from Base64 ========= */

  _loadPreviewFromBase64(base64) {
    const img = new Image();
    img.onload = () => {
      this.previewImage = img;
      this.setDirtyCanvas(true, true);
    };
    img.src = base64;
  }

  /* ========= Execution ========= */

  /* ===== compare.js 风格：onAction 内自己控制所有输出 ===== */

  async onAction(action) {
    if (action && action !== "trigger" && action !== "flow") return;
    const ctrl = window.workflowController;
    if (!ctrl || ctrl.state !== "running") return;

    const { image, threshold, match_select, use_color_check } = this.properties;
    if (!image) { console.warn("MatchImage: image empty"); return; }

    const name = image.startsWith("data:") ? "(内嵌图片)" : image.split("/").pop();
    this.log(`匹配图片: ${name} (阈值=${threshold})`);

    try {
      const response = await this._callBackend("match_image", { image, threshold, match_select, use_color_check });
      const result = response?.data || {};
      this.setOutputData(1, result.x ?? null);
      this.setOutputData(2, result.y ?? null);
      this.setOutputData(3, result.max_val ?? 0);

      if (result.x != null) {
        this.log(`匹配成功: (${result.x}, ${result.y})`);
        await this.execOutput(4); // 成功
      } else {
        this.log(`匹配失败: ${result.max_val?.toFixed(3) ?? "0"}`, "warn");
        await this.execOutput(5); // 失败
      }
      await this.execOutput(0); // 下一步
    } catch (e) {
      this.log(`匹配异常: ${e.message}`, "error");
      console.error("MatchImage failed:", e);
      window.workflowController?.stop();
    }
  }

  async _callBackend(taskName, properties) {
    const backend = window.taskflow?.backend;
    if (!backend) throw new Error("Backend not ready");
    const acct = window.accountManager?.getCurrentAccount();
    if (acct) properties.account = acct;
    const r = await backend.invoke(taskName, properties);
    if (r?.data?.error) {
      if (r.data.error.includes("连接已断开") || r.data.error.includes("Target closed")) throw new Error(r.data.error);
    }
    return r;
  }

  log(message, level) {
    const title = this.title || "匹配图片";
    window.taskflowLog?.(level || "info", `[${title}] ${message}`);
  }
}

LiteGraph.registerNodeType("action/match_image", MatchImage);