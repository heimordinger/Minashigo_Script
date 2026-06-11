// taskflow/nodes/flow/start.js
import { reportNodeEvent } from "../../core/node-reporter.js";
import { openNodePropertyEditor } from "../../core/input-dialog.js";

class Start extends LiteGraph.LGraphNode {
    constructor() {
        super();
        this.title = "起点";
        this.addOutput("next", LiteGraph.EVENT);
    }

    getHelpText() {
        return "流程起点，开始执行的地方";
    }

    onDblClick() {
        openNodePropertyEditor(this);
        return true;
    }

    async onAction() {
        await this.execOutput(0);
        window.taskflowLog?.("info", "[起点] 流程开始");
        reportNodeEvent(this, "start");
    }
}

LiteGraph.registerNodeType("flow/start", Start);
