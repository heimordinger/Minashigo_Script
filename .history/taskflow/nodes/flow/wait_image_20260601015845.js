import { ActionNode } from "../../core/action-node.js";
import { openNodePropertyEditor } from "../../core/input-dialog.js";
import { pickFromAssets } from "../../js/utils/filePicker.js";

class WaitImage extends ActionNode {
    static title = "等待图片";

    constructor() {
        super("等待图片");

        this.title = "等待图片";
        this.category = "Action";

        this.addInput("触发", LiteGraph.EVENT);
        this.addInput("图片路径", "string");
        this.addOutput("下一步", LiteGraph.EVENT);
        this.addOutput("是否超时", "boolean");

        this.properties = {
            image: "",
            threshold: 0.9,
            match_select: "best",
            use_color_check: false,
            timeout: 60000,
        };

        this.addWidget("number", "阈值", this.properties.threshold,
            v => this.properties.threshold = v,
            { min: 0, max: 1, step: 0.01 }
        );

        this.addWidget("toggle", "颜色校验",
            this.properties.use_color_check,
            v => this.properties.use_color_check = v
        );

        this._imgPathWidget = this.addWidget(
            "text", "图片", this.properties.image,
            v => this.properties.image = v
        );
        this.addWidget("button", "选择图片", null, () => this._selectImage());
        this.addWidget("button", "从库选择", null, () => this._selectFromAssets());
        this.addWidget("number", "超时(ms)", this.properties.timeout, v => (this.properties.timeout = v));
        this.addWidget("preview_image", "", null, null);

        this.previewImage = null;
        this.size = [160, 300];

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

    getHelpText() {
        return "等待屏幕上出现指定图片<br>超时: 最大等待时间(秒)";
    }

    onDblClick() {
        openNodePropertyEditor(this);
        return true;
    }

    onConfigure(info) {
        // 同步widget值到properties
        if (this.widgets) {
            this.widgets.forEach(widget => {
                if (widget.name === "阈值") {
                    widget.value = this.properties.threshold;
                } else if (widget.name === "颜色校验") {
                    widget.value = this.properties.use_color_check;
                } else if (widget.name === "图片") {
                    if (this.properties.image && this.properties.image.startsWith("data:image")) {
                        widget.value = "(内嵌图片)";
                        this._loadPreviewFromBase64(this.properties.image);
                    } else {
                        widget.value = this.properties.image;
                    }
                } else if (widget.name === "超时(ms)") {
                    widget.value = this.properties.timeout;
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

    async onAction(action) {
        await this.run(action);
    }

    _loadPreviewFromBase64(base64) {
        const img = new Image();
        img.onload = () => {
            this.previewImage = img;
            this.setDirtyCanvas(true, true);
        };
        img.src = base64;
    }

    async onRun() {
        const image = this.getInputData(1) ?? this.properties.image;
        if (!image) throw new Error("image 为空");
        const name = image.startsWith("data:") ? "(内嵌图片)" : image.split("/").pop();
        this.log(`等待图片: ${name} (超时=${this.properties.timeout}ms)`);
        const response = await this.callBackend("wait_image", {
            image,
            threshold: this.properties.threshold,
            match_select: this.properties.match_select,
            use_color_check: this.properties.use_color_check,
            timeout: this.properties.timeout,
        }, this.properties.timeout);
        const timedOut = !response?.success;
        this.setOutputData(1, timedOut);
        this.log(timedOut ? "等待超时，图片未出现" : "图片已出现", timedOut ? "warn" : "info");
    }
}

WaitImage.title = "等待图片"
LiteGraph.registerNodeType("action/wait_image", WaitImage);