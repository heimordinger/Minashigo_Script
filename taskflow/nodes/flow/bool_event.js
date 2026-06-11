// taskflow/nodes/flow/bool_event.js
// 布尔事件 —— 根据布尔值输出不同分支

import { openNodePropertyEditor } from "../../core/input-dialog.js";

class BoolEventNode extends LiteGraph.LGraphNode {
    static title = "布尔事件";

    constructor() {
        super();
        this.title = "布尔事件";
        this.category = "Flow";

        this.addInput("触发", LiteGraph.EVENT);
        this.addInput("条件", "boolean");

        this.addOutput("true", LiteGraph.EVENT);
        this.addOutput("false", LiteGraph.EVENT);

        this.size = [160, 70];
    }

    async onAction(action, param, options) {
        const cond = this.getInputData(1);
        if (cond) {
            await this.execOutput(0); // true
        } else {
            await this.execOutput(1); // false
        }
    }

    getHelpText() {
        return "根据布尔值走 true/false 分支";
    }

    onDblClick() {
        openNodePropertyEditor(this);
        return true;
    }
}
LiteGraph.registerNodeType("flow/bool_event", BoolEventNode);
