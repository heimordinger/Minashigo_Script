// taskflow/nodes/flow/end.js
import { reportNodeEvent } from "../../core/node-reporter.js";

class End extends LiteGraph.LGraphNode {
    constructor() {
        super();
        this.title = "结束";
        this.addInput("结束", LiteGraph.EVENT);
    }

    onAction() {
        window.taskflowLog?.("info", "[结束] 流程结束");
        reportNodeEvent(this, "finish");
        const controller = window.workflowController;
        if (controller) {
            controller.finish();
        }
    }
}

LiteGraph.registerNodeType("flow/end", End);