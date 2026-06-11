// taskflow/nodes/flow/math_op.js
// 数学运算 —— 加减乘除等算术运算

import { openNodePropertyEditor } from "../../core/input-dialog.js";

class MathOpNode extends LiteGraph.LGraphNode {
    static title = "数学运算";

    constructor() {
        super();
        this.title = "数学运算";
        this.category = "Flow";

        this.addInput("A", "number");
        this.addInput("B", "number");

        this.addOutput("结果", "number");

        this.properties = { operator: "+" };
        this.addWidget("combo", "运算符", this.properties.operator, (v) => {
            this.properties.operator = v;
        }, { values: ["+", "-", "*", "/", "%", "^"], property: "operator" });

        this.properties_info = [
            { name: "operator", type: "select", label: "运算符",
              options: [{ value: "+", label: "+" }, { value: "-", label: "-" },
                        { value: "*", label: "*" }, { value: "/", label: "/" },
                        { value: "%", label: "%" }, { value: "^", label: "^" }] }
        ];
        this.size = [160, 90];
    }

    onExecute() {
        const a = Number(this.getInputData(0)) || 0;
        const b = Number(this.getInputData(1)) || 0;
        let result = 0;

        switch (this.properties.operator) {
            case "+": result = a + b; break;
            case "-": result = a - b; break;
            case "*": result = a * b; break;
            case "/": result = b !== 0 ? a / b : NaN; break;
            case "%": result = b !== 0 ? a % b : NaN; break;
            case "^": result = Math.pow(a, b); break;
        }

        this.setOutputData(0, result);
    }

    onConfigure(info) {
        if (this.widgets) {
            this.widgets.forEach(w => {
                if (w.name === "运算符") w.value = this.properties.operator;
            });
        }
    }

    getHelpText() {
        return "数学运算 + - * / % ^";
    }

    onDblClick() {
        openNodePropertyEditor(this);
        return true;
    }
}
LiteGraph.registerNodeType("flow/math_op", MathOpNode);
