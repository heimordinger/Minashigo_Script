// taskflow/nodes/flow/variable_access.js
// 读写变量 —— 从变量池读取/写入变量值，不自动声明

import { openNodePropertyEditor } from "../../core/input-dialog.js";

class VariableAccessNode extends LiteGraph.LGraphNode {
    static title = "读写变量";

    constructor() {
        super();
        this.title = "读写变量";
        this.category = "Flow";

        this.addInput("触发", LiteGraph.EVENT);
        this.addInput("新值", "");          // 写入的值（可选，仅在 write 模式需要）

        this.addOutput("下一步", LiteGraph.EVENT);
        this.addOutput("当前值", "");        // 输出的值

        this.properties = {
            name: "",
            mode: "read"       // read | write | readwrite
        };

        this._nameW = this.addWidget("text", "变量名", this.properties.name, (v) => {
            this.properties.name = v;
        });

        this._modeW = this.addWidget("combo", "模式", this.properties.mode, (v) => {
            this.properties.mode = v;
        }, { values: ["read", "write", "readwrite"] });

        this.size = [200, 130];
        this.widgets_start_y = 60;

        // 属性编辑器配置
        this.properties_info = [
            { name: "name", type: "text", label: "变量名" },
            {
                name: "mode", type: "select", label: "模式",
                options: [
                    { value: "read", label: "只读" },
                    { value: "write", label: "只写" },
                    { value: "readwrite", label: "读写" }
                ]
            }
        ];
    }

    onConfigure() {
        if (this._nameW) this._nameW.value = this.properties.name;
        if (this._modeW) this._modeW.value = this.properties.mode;
    }

    onAction() {
        const name = this.properties.name?.trim();
        if (!name || name === "NaN") {
            this.setOutputData(1, null);
            this.execOutput(0);
            return;
        }

        const mode = this.properties.mode;

        // 写入
        if (mode === "write" || mode === "readwrite") {
            const newVal = this.getInputData(1);
            if (newVal !== undefined && newVal !== null) {
                LiteGraph.Globals[`__var_${name}`] = newVal;
            }
        }

        // 读取
        if (mode === "read" || mode === "readwrite") {
            const raw = LiteGraph.Globals[`__var_${name}`];
            this.setOutputData(1, raw !== undefined ? raw : null);
        }

        this.execOutput(0);
    }

    getHelpText() {
        return "从变量池读取/写入变量值\n" +
               "变量名需与「声明变量」节点中的名称一致\n" +
               "模式:\n" +
               "  read      — 读取变量值输出到「当前值」\n" +
               "  write     — 将「新值」输入的值写入变量池\n" +
               "  readwrite — 先写入新值，再读取并输出";
    }

    onDblClick() {
        openNodePropertyEditor(this);
        return true;
    }
}
LiteGraph.registerNodeType("flow/variable_access", VariableAccessNode);
