// taskflow/nodes/flow/relay.js
// 事件中继 —— 透传触发信号，用两个控件独立管理输入输出数量

import { openNodePropertyEditor } from "../../core/input-dialog.js";

class RelayNode extends LiteGraph.LGraphNode {
    static title = "事件中继";

    constructor() {
        super();
        this.title = "事件中继";
        this.category = "Flow";

        // 初始触点不编号，新增的才编号
        this.addInput("输入", LiteGraph.EVENT);
        this.addOutput("输出", LiteGraph.EVENT);

        this.properties = {
            inputCount: 1,
            outputCount: 1
        };

        this.addWidget("number", "输入数量", this.properties.inputCount, (v) => {
            const n = Math.max(1, Math.floor(v));
            this.properties.inputCount = n;
            this._syncInputs(n);
        }, { step: 10, precision: 0, min: 1 });

        this.addWidget("number", "输出数量", this.properties.outputCount, (v) => {
            const n = Math.max(1, Math.floor(v));
            this.properties.outputCount = n;
            this._syncOutputs(n);
        }, { step: 10, precision: 0, min: 1 });

        this.size = [220, 100];
    }

    _syncInputs(target) {
        const current = this.inputs ? this.inputs.length : 0;
        if (target > current) {
            // 新增的从 1 开始编号（第一个是"输入"不编号）
            for (let idx = current; idx < target; idx++) {
                this.addInput(idx === 0 ? "输入" : `输入 ${idx}`, LiteGraph.EVENT);
            }
        } else if (target < current) {
            for (let i = current; i > target; i--) {
                this.removeInput(this.inputs.length - 1);
            }
        }
        this._resize();
    }

    _syncOutputs(target) {
        const current = this.outputs ? this.outputs.length : 0;
        if (target > current) {
            for (let idx = current; idx < target; idx++) {
                this.addOutput(idx === 0 ? "输出" : `输出 ${idx}`, LiteGraph.EVENT);
            }
        } else if (target < current) {
            for (let i = current; i > target; i--) {
                this.removeOutput(this.outputs.length - 1);
            }
        }
        this._resize();
    }

    _resize() {
        const rows = Math.max(
            this.inputs ? this.inputs.length : 1,
            this.outputs ? this.outputs.length : 1
        );
        this.size[1] = Math.max(100, 40 + rows * 28);
        this.setDirtyCanvas(true, true);
    }

    async onAction(action, param, options) {
        const outCount = this.outputs ? this.outputs.length : 0;
        for (let i = 0; i < outCount; i++) {
            await this.execOutput(i);
        }
    }

    /**
     * 载入存档时统一编号并同步控件数字
     */
    onConfigure(info) {
        // 重新编号：索引 0 不编号，后续从 1 开始
        if (this.inputs) {
            this.inputs.forEach((inp, i) => {
                inp.name = i === 0 ? "输入" : `输入 ${i}`;
            });
        }
        if (this.outputs) {
            this.outputs.forEach((out, i) => {
                out.name = i === 0 ? "输出" : `输出 ${i}`;
            });
        }
        const inCount = this.inputs ? this.inputs.length : 1;
        const outCount = this.outputs ? this.outputs.length : 1;
        this.properties.inputCount = inCount;
        this.properties.outputCount = outCount;
        if (this.widgets) {
            this.widgets.forEach(w => {
                if (w.name === "输入数量") w.value = inCount;
                else if (w.name === "输出数量") w.value = outCount;
            });
        }
        this._resize();
    }

    getHelpText() {
        return "透传触发信号，输入输出独立管理<br>输入/输出数量: 触点数";
    }

    onDblClick() {
        openNodePropertyEditor(this);
        return true;
    }
}
LiteGraph.registerNodeType("flow/relay", RelayNode);
