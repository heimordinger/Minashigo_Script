// taskflow/nodes/flow/start.js
import { reportNodeEvent } from "../../core/node-reporter.js";

class Start extends LiteGraph.LGraphNode {
    constructor() {
        super();
        this.title = "起点";
        this.addOutput("next", LiteGraph.EVENT);
    }

    start() {
        window.taskflowLog?.("info", "[起点] 流程开始");
        reportNodeEvent(this, "start");
    }
}

LiteGraph.registerNodeType("flow/start", Start);