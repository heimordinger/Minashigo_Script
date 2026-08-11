// taskflow/nodes/flow/state_dispatch.js
// FSM 状态派发 —— 根据当前状态名触发对应输出分支
// 动态输出插槽：states 属性修改时自动重建 outputs

import { openNodePropertyEditor } from "../../core/input-dialog.js";

class StateDispatchNode extends LiteGraph.LGraphNode {
    static title = "状态派发";

    constructor() {
        super();
        this.title = "状态派发";
        this.category = "Flow";

        this.addInput("触发", LiteGraph.EVENT);
        this.addInput("当前状态", "string");

        this.properties = {
            states: "未知,选关,备战,跳过剧情,等待战后结算",
        };

        this.addWidget("text", "状态列表", this.properties.states, (v) => {
            this.properties.states = v;
            this._rebuildOutputs();
        });

        this.properties_info = [
            { name: "states", type: "text", label: "状态列表（逗号分隔）" },
        ];

        this._rebuildOutputs();

        if (this.widgets) {
            this.widgets.forEach(w => {
                w.onKeyDown = e => { e.stopPropagation(); e.preventDefault(); return false; };
            });
        }
    }

    /** 根据 states 属性重建输出插槽。 */
    _rebuildOutputs() {
        const names = this._parseStates();
        // 清除旧 outputs（保留 inputs）
        while (this.outputs?.length) this.removeOutput(0);

        // 依次添加每个状态的输出插槽
        for (const name of names) {
            this.addOutput(name, LiteGraph.EVENT);
        }
        // 兜底分支：未匹配
        this.addOutput("未匹配", LiteGraph.EVENT);
        // IR 契约一：错误输出
        this.addOutput("错误", LiteGraph.EVENT);

        this.size = [Math.max(220, 160 + names.length * 8), Math.max(80, 50 + names.length * 24)];
        this.setDirtyCanvas(true, true);
    }

    _parseStates() {
        const raw = String(this.properties.states ?? "").trim();
        if (!raw) return [];
        return raw.split(",").map(s => s.trim()).filter(Boolean);
    }

    getHelpText() {
        return "根据「当前状态」输入的值，触发对应名称的输出分支。<br>" +
               "状态列表：逗号分隔，首尾空格自动去除。<br>" +
               "无匹配时走「未匹配」分支。";
    }

    onDblClick() {
        openNodePropertyEditor(this);
        return true;
    }

    onConfigure(info) {
        // 恢复时同步 outputs 到最新的 states 列表
        this._rebuildOutputs();
    }

    async onAction(action, param, options) {
        // 读取当前状态值（slot 1 = "当前状态" 输入）
        const current = this.getInputData(1);
        const stateStr = String(current ?? "").trim();
        const names = this._parseStates();

        // 找匹配名称的输出插槽
        let targetSlot = -1;
        for (let i = 0; i < names.length; i++) {
            if (names[i] === stateStr) {
                targetSlot = i;
                break;
            }
        }

        if (targetSlot >= 0) {
            window.taskflowLog?.("info", `[状态派发] -> ${stateStr}`);
            await this.execOutput(targetSlot);
        } else {
            // 未匹配 → 走兜底分支（索引 = names.length）
            window.taskflowLog?.("warn", `[状态派发] 未识别状态: ${stateStr}，走未匹配`);
            await this.execOutput(names.length);
        }

        // 走下一步
        await this.execOutput(names.length + 1); // 错误
    }
}

LiteGraph.registerNodeType("flow/state_dispatch", StateDispatchNode);
