// taskflow/nodes/flow/cond_goto.js
// 条件跳转 —— 条件为 true 时跳转到标签，否则 fall through

import { openNodePropertyEditor } from "../../core/input-dialog.js";

class CondGotoNode extends LiteGraph.LGraphNode {
    static title = "条件跳转";

    constructor() {
        super();
        this.title = "条件跳转";
        this.category = "Flow";

        this.addInput("触发", LiteGraph.EVENT);
        this.addInput("条件", "boolean");

        this.addOutput("下一步", LiteGraph.EVENT);

        this.properties = { target: "label_1" };

        this._targetWidget = this.addWidget("text", "目标标签", this.properties.target, v => (this.properties.target = v));
        if (this.widgets) this.widgets.forEach(w => { w.onKeyDown = e => { e.stopPropagation(); e.preventDefault(); return false; }; });

        this.properties_info = [{ name: "target", type: "text", label: "目标标签" }];
        this.size = [240, 80];
    }

    async onAction(action, param, options) {
        const cond = this.getInputData(1);
        // 无连线时默认 true（跳转）
        if (cond === undefined || cond === null || cond) {
            await this._doJump(param, options);
        } else {
            // 条件为 false → 走下一步
            await this.execOutput(0);
        }
    }

    async _doJump(param, options) {
        const target = String(this.properties.target || "").trim();
        if (!target) { window.taskflowLog?.("warn", "[条件跳转] 目标标签为空"); return; }

        const graph = this.graph;
        if (!graph) return;

        const nodes = graph._nodes || [];
        const normalize = v => String(v ?? "").trim();
        const knownLabelTypes = new Set(["flow/label", "label"]);

        for (const node of nodes) {
            const type = normalize(node.type).toLowerCase();
            const title = normalize(node.title);
            const isLabelLike = knownLabelTypes.has(type) || title === "Label" || title === "标签";
            if (!isLabelLike) continue;

            const candidates = [node.properties?.label, node.title];
            if (candidates.map(normalize).filter(Boolean).includes(target)) {
                window.taskflowLog?.("info", `[条件跳转] -> ${target}`);
                const ctrl = window.workflowController;
                if (ctrl && typeof ctrl._runNode === "function") {
                    await ctrl._runNode(node, "flow");
                }
                if (typeof node.execOutput === "function") {
                    await node.execOutput(0);
                }
                return;
            }
        }

        window.taskflowLog?.("error", `[条件跳转] 未找到目标标签: ${target}`);
        window.workflowController?.stop();
    }

    getHelpText() {
        return "条件为 true 时跳转到指定标签<br>条件: 布尔值输入";
    }

    onDblClick() {
        openNodePropertyEditor(this);
        return true;
    }
    onConfigure(info) { if (this._targetWidget) this._targetWidget.value = this.properties.target; }
}
LiteGraph.registerNodeType("flow/cond_goto", CondGotoNode);
