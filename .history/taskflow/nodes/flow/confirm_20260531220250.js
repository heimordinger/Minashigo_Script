// taskflow/nodes/flow/confirm.js
// 确认框 —— 弹出确认对话框，根据用户选择走不同分支

import { openNodePropertyEditor } from "../../core/input-dialog.js";

class ConfirmNode extends LiteGraph.LGraphNode {
    static title = "确认框";

    constructor() {
        super();
        this.title = "确认框";
        this.category = "Flow";

        this.addInput("触发", LiteGraph.EVENT);

        this.addOutput("确认", LiteGraph.EVENT);
        this.addOutput("取消", LiteGraph.EVENT);

        this.properties = {
            title: "确认",
            message: "确定执行此操作？"
        };

        this.addWidget("text", "标题", this.properties.title, (v) => { this.properties.title = v; });
        this.addWidget("text", "内容", this.properties.message, (v) => { this.properties.message = v; });

        if (this.widgets) this.widgets.forEach(w => { w.onKeyDown = e => { e.stopPropagation(); e.preventDefault(); return false; }; });

        this.properties_info = [
            { name: "title", type: "text", label: "标题" },
            { name: "message", type: "text", label: "内容" }
        ];
        this.size = [180, 110];
    }

    async onAction() {
        const backend = window.taskflow?.backend;
        if (!backend) {
            window.taskflowLog?.("error", "[确认框] 后端未就绪");
            throw new Error("Taskflow backend is not ready");
        }

        try {
            const response = await backend.invoke("confirm_dialog", {
                title: this.properties.title,
                message: this.properties.message
            });

            if (response && response.success) {
                await this.execOutput(0); // 确认
            } else {
                await this.execOutput(1); // 取消
            }
        } catch (error) {
            window.taskflowLog?.("error", `[确认框] 调用失败: ${error.message}`);
            throw error;
        }
    }

    getHelpText() {
        return "弹出确认框，根据用户选择走不同分支";
    }

    onDblClick() {
        openNodePropertyEditor(this);
        return true;
    }
}
LiteGraph.registerNodeType("flow/confirm", ConfirmNode);
