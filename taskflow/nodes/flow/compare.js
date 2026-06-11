// taskflow/nodes/flow/compare.js
// 条件判断 —— 比较两个值的大小/相等关系，支持数字、字符串、布尔值、集合包含

import { openNodePropertyEditor } from "../../core/input-dialog.js";

class CompareNode extends LiteGraph.LGraphNode {
    static title = "条件判断";

    constructor() {
        super();
        this.title = "条件判断";
        this.category = "Flow";

        this.addInput("触发", LiteGraph.EVENT);
        this.addInput("A", "");
        this.addInput("B（集合）", "");

        this.addOutput("下一步", LiteGraph.EVENT);
        this.addOutput("成功", LiteGraph.EVENT);
        this.addOutput("失败", LiteGraph.EVENT);

        this.properties = { operator: "==" };
        this.addWidget("combo", "运算符", this.properties.operator, (v) => {
            this.properties.operator = v;
        }, { values: ["==", "===", "!=", "!==", ">", "<", ">=", "<=", "包含", "不包含"], property: "operator" });

        this.properties_info = [
            { name: "operator", type: "select", label: "运算符",
              options: [
                  { value: "==", label: "==" }, { value: "===", label: "===" },
                  { value: "!=", label: "!=" }, { value: "!==", label: "!==" },
                  { value: ">", label: ">" }, { value: "<", label: "<" },
                  { value: ">=", label: ">=" }, { value: "<=", label: "<=" },
                  { value: "包含", label: "包含" }, { value: "不包含", label: "不包含" }
              ] }
        ];
        this.size = [200, 120];
    }

    _parseCollection(v) {
        // 将 B 解析为数组（逗号分隔/已有数组）
        if (Array.isArray(v)) return v;
        if (typeof v === "string") {
            return v.split(",").map(s => s.trim()).filter(Boolean);
        }
        return [String(v ?? "")];
    }

    async onAction() {
        // force_update 反向拉取源节点数据
        const a = this.getInputData(1, true);
        const b = this.getInputData(2, true);

        // 检查 A/B 是否有效
        if (!this.isInputConnected(1) && a === undefined) {
            throw new Error("A 未接入数据线且无默认值");
        }
        if (!this.isInputConnected(2) && b === undefined) {
            throw new Error("B（集合）未接入数据线且无默认值");
        }
        // force_update 后仍为 undefined 说明源节点未输出有效值
        if (a === undefined) throw new Error("A 的源节点未输出有效值");
        if (b === undefined) throw new Error("B（集合）的源节点未输出有效值");

        let result = false;

        switch (this.properties.operator) {
            case "==":  result = a == b; break;
            case "===": result = a === b; break;
            case "!=":  result = a != b; break;
            case "!==": result = a !== b; break;
            case ">":   result = a > b; break;
            case "<":   result = a < b; break;
            case ">=":  result = a >= b; break;
            case "<=":  result = a <= b; break;
            case "包含":
                if (b != null) {
                    const col = this._parseCollection(b);
                    result = col.includes(a);
                }
                break;
            case "不包含":
                if (b != null) {
                    const col = this._parseCollection(b);
                    result = !col.includes(a);
                }
                break;
        }

        if (result) {
            await this.execOutput(1); // 成功
        } else {
            await this.execOutput(2); // 失败
        }
        await this.execOutput(0); // 下一步
    }

    getHelpText() {
        return "比较 A 和 B 的值，走成功/失败分支<br>" +
               "支持数字/字符串/布尔值/集合<br>" +
               "运算符: == === != !== > < >= &lt;= 包含 不包含<br>" +
               "包含/不包含: B 放集合（逗号分隔），判断 A 是否在集合中<br>" +
               "条件为真 → 成功，条件为假 → 失败，然后走下一步";
    }

    onDblClick() {
        openNodePropertyEditor(this);
        return true;
    }
}
LiteGraph.registerNodeType("flow/compare", CompareNode);
