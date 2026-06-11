// taskflow/nodes/flow/variable.js
// 声明变量 —— 全局命名变量，跨节点读写
// 默认变量名为 NaN 时不读写变量池
// 输入值含英文逗号时自动识别为数组

import { openNodePropertyEditor } from "../../core/input-dialog.js";

class VariableNode extends LiteGraph.LGraphNode {
    static title = "声明变量";

    constructor() {
        super();
        this.title = "声明变量";
        this.category = "Flow";

        this.addInput("触发", LiteGraph.EVENT);
        this.addInput("新值", "");          // 任意类型，用于赋值
        this.addOutput("下一步", LiteGraph.EVENT);
        this.addOutput("当前值", "");        // 输出当前存储的值
        this.properties = {
            name: "NaN",
            type: "number",
            default: 0,
        };
        this.size = [240, 140];

        // 不传回调，避免构造时触发副作用
        this._nameW = this.addWidget("text", "变量名", this.properties.name, v => (this.properties.name = v));
        this._typeW = this.addWidget("combo", "类型", this.properties.type, v => {
            this.properties.type = v;
            this._syncDefaultWidget();
        }, { values: ["number", "string", "boolean"] });
        this._defaultW = this.addWidget("text", "默认值", String(this.properties.default), v => {
            this.properties.default = v;
        });

        // 属性编辑器配置
        this.properties_info = [
            { name: "name", type: "text", label: "变量名" },
            { name: "type", type: "select", label: "类型",
              options: [{ value: "number", label: "number" }, { value: "string", label: "string" }, { value: "boolean", label: "boolean" }] },
            { name: "default", type: "text", label: "默认值" },
        ];

        if (this.widgets) this.widgets.forEach(w => { w.onKeyDown = e => { e.stopPropagation(); e.preventDefault(); return false; }; });
    }

    _syncDefaultWidget() {
        if (this._defaultW) {
            let display = String(this.properties.default ?? "");
            if (this.properties.type === "boolean") {
                display = this.properties.default ? "true" : "false";
            }
            this._defaultW.value = display;
            this.setDirtyCanvas(true, true);
        }
    }

    _readValue() {
        if (!this.properties.name || this.properties.name === "NaN") {
            return this._coerce(this.properties.default);
        }
        const raw = LiteGraph.Globals[`__var_${this.properties.name}`];
        return raw !== undefined ? raw : this._coerce(this.properties.default);
    }

    /** 供上游节点 force_update 拉取数据时调用 */
    updateOutputData(slot) {
        if (slot === 1) {
            this.setOutputData(1, this._readValue());
        }
    }

    _writeValue(val) {
        if (!this.properties.name || this.properties.name === "NaN") return;
        LiteGraph.Globals[`__var_${this.properties.name}`] = val;
    }

    _coerce(v) {
        // 含英文逗号的字符串自动识别为数组
        if (typeof v === "string" && v.includes(",")) {
            const parts = v.split(",").map(s => s.trim()).filter(Boolean);
            if (parts.length > 1) {
                switch (this.properties.type) {
                    case "number":
                        return parts.map(s => Number(s) || 0);
                    case "boolean":
                        return parts.map(s => s === "true" || s === "1");
                    default:
                        // string 类型：每个元素自动去除首尾引号（ASCII 或中文）
                        return parts.map(s => {
                            s = s.trim();
                            const q = s.charAt(0);
                            if (q === '"' || q === '“' || q === '‘') {
                                const last = s.slice(-1);
                                if (last === q || (q === '“' && last === '”') || (q === '‘' && last === '’'))
                                    return s.slice(1, -1);
                            }
                            return s;
                        });
                }
            }
        }
        // 已经是数组则按类型转换元素
        if (Array.isArray(v)) {
            return v;
        }
        // 普通标量
        if (this.properties.type === "number") return Number(v) || 0;
        if (this.properties.type === "boolean") return v === true || v === "true" || v === 1 || v === "1";
        return String(v ?? "");
    }

    _formatValue(val) {
        if (Array.isArray(val)) {
            if (this.properties.type === "string") {
                return "[" + val.map(s => `"${s}"`).join(", ") + "]";
            }
            return "[" + val.join(", ") + "]";
        }
        if (typeof val === "string") return `"${val}"`;
        if (typeof val === "boolean") return val ? "true" : "false";
        return String(val ?? "null");
    }

    async onAction() {
        const newVal = this.getInputData(1);
        if (newVal !== undefined && newVal !== null) {
            this._writeValue(newVal);
        }
        this.setOutputData(1, this._readValue());
        await this.execOutput(0);
    }

    onDrawForeground(ctx) {
        if (this.flags.collapsed) return;
        const X = this.size[0], h = this.size[1];

        ctx.save();
        ctx.fillStyle = "rgba(255,255,255,0.03)";
        this._roundRect(ctx, 8, h - 30, X - 16, 22, 4);
        ctx.fill();

        ctx.fillStyle = "#bbb";
        ctx.font = "12px monospace";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";

        const raw = this._readValue();
        ctx.fillText(`= ${this._formatValue(raw)}`, X / 2, h - 19);
        ctx.restore();
    }

    _roundRect(ctx, x, y, w, h, r) {
        ctx.beginPath();
        ctx.moveTo(x + r, y);
        ctx.lineTo(x + w - r, y);
        ctx.quadraticCurveTo(x + w, y, x + w, y + r);
        ctx.lineTo(x + w, y + h - r);
        ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
        ctx.lineTo(x + r, y + h);
        ctx.quadraticCurveTo(x, y + h, x, y + h - r);
        ctx.lineTo(x, y + r);
        ctx.quadraticCurveTo(x, y, x + r, y);
        ctx.closePath();
    }

    getHelpText() {
        return "同名变量节点共享同一值<br>" +
               "变量名设为 NaN 时不读写变量池（默认）<br>" +
               "触发时如有新值输入则更新，然后输出当前值<br>" +
               "默认值含英文逗号时自动识别为数组<br>" +
               "string 型数组元素要用英文双引号括起来，用英文逗号分词，如: \"a\",\"b\",\"c\"<br>" +;
    }

    onDblClick() {
        openNodePropertyEditor(this);
        return true;
    }
    onConfigure(info) {
        if (this._nameW) this._nameW.value = this.properties.name;
        if (this._typeW) this._typeW.value = this.properties.type;
        if (this._defaultW) {
            this._defaultW.value = this.properties.type === "boolean"
                ? (this.properties.default ? "true" : "false")
                : String(this.properties.default ?? "");
        }
    }
}
LiteGraph.registerNodeType("flow/variable", VariableNode);
