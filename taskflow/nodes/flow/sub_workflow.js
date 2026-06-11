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

    onConfigure() {
        if (this._pathWidget) this._pathWidget.value = this.properties.path;
    }

    _selectFromScripts() {
        import("../../core/workflow-browser.js").then(({ showWorkflowBrowser }) => {
            showWorkflowBrowser({
                mode: "select",
                title: "选择脚本",
                onSelect: (fullPath) => {
                    this.properties.path = fullPath;
                    if (this._pathWidget) this._pathWidget.value = fullPath;
                    this.setDirtyCanvas(true, true);
                    window.showToast?.(`已选择: ${fullPath}`, "info");
                }
            });
        }).catch(e => {
            window.showToast?.("打开文件浏览器失败: " + e.message, "error");
        });
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
