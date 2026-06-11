// taskflow/nodes/flow/sub_workflow.js
// 子流程 —— 加载并执行 scripts/ 下的 .json 流程文件

import { openNodePropertyEditor } from "../../core/input-dialog.js";

class SubWorkflowNode extends LiteGraph.LGraphNode {
    static title = "子流程";

    constructor() {
        super();
        this.title = "子流程";
        this.category = "Flow";

        this.addInput("触发", LiteGraph.EVENT);
        this.addOutput("下一步", LiteGraph.EVENT);
        this.addOutput("成功", "boolean");

        this.properties = {
            path: ""
        };

        this._pathWidget = this.addWidget("text", "流程文件", this.properties.path, (v) => {
            this.properties.path = v;
        });

        this.addWidget("button", "从库选择", null, () => this._selectFromScripts());

        this.size = [240, 110];
    }

    _selectFromScripts() {
        fetch("/api/list_scripts")
            .then(r => r.json())
            .then(result => {
                if (!result.success || !result.files || !result.files.length) {
                    window.showToast?.("scripts/ 目录下没有 .json 脚本", "warn");
                    return;
                }
                this._showScriptPicker(result.files);
            })
            .catch(e => {
                window.showToast?.("获取脚本列表失败: " + e.message, "error");
            });
    }

    _showScriptPicker(files) {
        const old = document.getElementById("script-picker");
        if (old) old.remove();

        const overlay = document.createElement("div");
        overlay.id = "script-picker";
        overlay.style.cssText =
            "position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.5);" +
            "z-index:9999;display:flex;align-items:center;justify-content:center;";

        const panel = document.createElement("div");
        panel.style.cssText =
            "background:#1f1f1f;border-radius:10px;padding:16px;min-width:400px;max-width:600px;" +
            "max-height:80vh;overflow-y:auto;box-shadow:0 10px 30px rgba(0,0,0,0.5);";

        const title = document.createElement("div");
        title.textContent = "选择脚本";
        title.style.cssText = "font-size:16px;font-weight:bold;margin-bottom:12px;color:#fff;";
        panel.appendChild(title);

        files.forEach(file => {
            const item = document.createElement("div");
            item.style.cssText =
                "padding:10px 12px;margin:4px 0;border-radius:6px;cursor:pointer;" +
                "background:#2a2a2a;color:#ddd;display:flex;align-items:center;gap:8px;";
            item.onmouseover = () => { item.style.background = "#3a3a3a"; };
            item.onmouseout = () => { item.style.background = "#2a2a2a"; };

            const icon = document.createElement("span");
            icon.textContent = "📄";
            icon.style.fontSize = "16px";

            const name = document.createElement("span");
            name.textContent = file;

            item.appendChild(icon);
            item.appendChild(name);

            item.onclick = () => {
                this.properties.path = file;
                if (this._pathWidget) this._pathWidget.value = file;
                this.setDirtyCanvas(true, true);
                overlay.remove();
                window.showToast?.(`已选择: ${file}`, "info");
            };

            panel.appendChild(item);
        });

        const closeBtn = document.createElement("button");
        closeBtn.textContent = "关闭";
        closeBtn.style.cssText =
            "margin-top:12px;padding:8px 16px;border:0;border-radius:6px;background:#444;color:#ddd;cursor:pointer;";
        closeBtn.onclick = () => overlay.remove();
        panel.appendChild(closeBtn);

        overlay.appendChild(panel);
        document.body.appendChild(overlay);
    }

    async onAction() {
        const path = this.properties.path;
        if (!path) {
            window.taskflowLog?.("error", "[子流程] 未指定流程文件路径");
            throw new Error("请指定流程文件路径");
        }

        this.log(`加载子流程: ${path}`);
        try {
            const backend = window.taskflow?.backend;
            if (!backend) throw new Error("后端未就绪");
            const response = await backend.invoke("run_sub_workflow", { path: path });
            if (response && response.success) {
                this.log(`子流程执行成功: ${path}`);
                this.setOutputData(1, true);
                await this.execOutput(0);
            } else {
                this.log(`子流程执行失败: ${response?.error || "未知错误"}`, "error");
                this.setOutputData(1, false);
                throw new Error(response?.error || "子流程执行失败");
            }
        } catch (error) {
            this.log(`子流程异常: ${error.message}`, "error");
            this.setOutputData(1, false);
            throw error;
        }
    }

    getHelpText() {
        return "执行 scripts/ 下的另一个流程文件(.json)";
    }

    onDblClick() {
        openNodePropertyEditor(this);
        return true;
    }
}
LiteGraph.registerNodeType("flow/sub_workflow", SubWorkflowNode);
