// taskflow/nodes/flow/logic_gates.js
// 逻辑门 —— AND / OR / NOT 逻辑运算节点

import { openNodePropertyEditor } from "../../core/input-dialog.js";

class LogicGateNode extends LiteGraph.LGraphNode {
    static title = "逻辑门";

    constructor() {
        super();
        this.title = "逻辑门";
        this.category = "Flow";

        this.addInput("A", "boolean");
        this.addInput("B", "boolean");

        this.addOutput("结果", "boolean");

        this.properties = { gate: "AND" };
        this.addWidget("combo", "运算", this.properties.gate, (v) => {
            this.properties.gate = v;
        }, { values: ["AND", "OR", "NOT", "XOR", "NAND", "NOR"] });

        this.properties_info = [
            { name: "gate", type: "select", label: "运算",
              options: [{ value: "AND", label: "AND" }, { value: "OR", label: "OR" },
                        { value: "NOT", label: "NOT" }, { value: "XOR", label: "XOR" },
                        { value: "NAND", label: "NAND" }, { value: "NOR", label: "NOR" }] }
        ];
        this.size = [160, 90];
    }

    onExecute() {
        const a = Boolean(this.getInputData(0));
        const b = Boolean(this.getInputData(1));
        let result = false;

        switch (this.properties.gate) {
            case "AND":  result = a && b; break;
            case "OR":   result = a || b; break;
            case "NOT":  result = !a; break;
            case "XOR":  result = a !== b; break;
            case "NAND": result = !(a && b); break;
            case "NOR":  result = !(a || b); break;
        }

        this.setOutputData(0, result);
    }

    onConfigure(info) {
        if (this.widgets) {
            this.widgets.forEach(w => {
                if (w.name === "运算") w.value = this.properties.gate;
            });
        }
    }

    getHelpText() {
        return "逻辑运算 AND/OR/NOT/XOR/NAND/NOR";
    }

    onDblClick() {
        openNodePropertyEditor(this);
        return true;
    }
}
LiteGraph.registerNodeType("flow/logic_gates", LogicGateNode);
