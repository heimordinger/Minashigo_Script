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

        this.addOutput("下一步", LiteGraph.EVENT);
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
        this.size = [200, 110];
    }

    /** 弹出自定义确认对话框，返回用户选择 */
    _showConfirmDialog(title, message) {
        return new Promise(resolve => {
            // 移除已有
            const old = document.getElementById("tf-confirm-dialog");
            if (old) old.remove();

            const overlay = document.createElement("div");
            overlay.id = "tf-confirm-dialog";
            overlay.style.cssText = [
                "position:fixed", "inset:0", "display:flex",
                "align-items:center", "justify-content:center",
                "background:rgba(0,0,0,0.45)", "z-index:50001",
            ].join(";");

            const panel = document.createElement("div");
            panel.style.cssText = [
                "width:400px", "max-width:92vw",
                "background:#1f1f1f", "color:#eee",
                "border-radius:10px", "padding:16px",
                "box-shadow:0 10px 30px rgba(0,0,0,0.5)",
            ].join(";");

            // 标题
            const t = document.createElement("div");
            t.textContent = title || "确认";
            t.style.cssText = "font-size:16px;font-weight:bold;margin-bottom:12px;";
            panel.appendChild(t);

            // 分隔线
            const hr = document.createElement("div");
            hr.style.cssText = "height:1px;background:#444;margin-bottom:12px;";
            panel.appendChild(hr);

            // 消息
            const m = document.createElement("div");
            m.textContent = message;
            m.style.cssText = "font-size:14px;color:#ccc;margin-bottom:20px;line-height:1.6;";
            panel.appendChild(m);

            // 按钮区域（居中）
            const btns = document.createElement("div");
            btns.style.cssText = "display:flex;justify-content:center;gap:12px;margin-top:4px;";

            const btnCancel = document.createElement("button");
            btnCancel.textContent = "取消";
            btnCancel.style.cssText = "height:36px;padding:0 24px;border:0;border-radius:6px;background:#5a6b5a;color:#ddd;cursor:pointer;font-size:14px;";
            btns.appendChild(btnCancel);

            const btnOk = document.createElement("button");
            btnOk.textContent = "确认";
            btnOk.style.cssText = "height:36px;padding:0 24px;border:0;border-radius:6px;background:#6b8f6b;color:#fff;cursor:pointer;font-size:14px;font-weight:bold;";
            btns.appendChild(btnOk);

            panel.appendChild(btns);
            overlay.appendChild(panel);
            document.body.appendChild(overlay);

            const close = result => {
                overlay.remove();
                resolve(result);
            };

            btnOk.onclick = () => close(true);
            btnCancel.onclick = () => close(false);
            overlay.onclick = e => { if (e.target === overlay) close(false); };

            // ESC 取消，Enter 确认
            const keydown = e => {
                if (e.key === "Escape") { close(false); document.removeEventListener("keydown", keydown); }
                if (e.key === "Enter") { close(true); document.removeEventListener("keydown", keydown); }
            };
            document.addEventListener("keydown", keydown);
        });
    }

    onConfigure() {
        if (this.widgets) {
            this.widgets.forEach(w => {
                if (w.name === "标题") w.value = this.properties.title;
                else if (w.name === "内容") w.value = this.properties.message;
            });
        }
    }

    async onAction() {
        try {
            const title = window.resolveVars?.(this.properties.title) || this.properties.title;
            const message = window.resolveVars?.(this.properties.message) || this.properties.message;
            const confirmed = await this._showConfirmDialog(title, message);
            if (confirmed) {
                await this.execOutput(1); // 确认
            } else {
                await this.execOutput(2); // 取消
            }
            await this.execOutput(0); // 下一步
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
