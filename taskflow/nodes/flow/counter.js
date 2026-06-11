// taskflow/nodes/flow/counter.js
// 计数器 —— 增加/减少/重置，输出 ≥目标 / <目标

import { openNodePropertyEditor } from "../../core/input-dialog.js";

class CounterNode extends LiteGraph.LGraphNode {
    static title = "计数器";

    constructor() {
        super();
        this.title = "计数器";
        this.category = "Flow";

        this.addInput("增加", LiteGraph.EVENT);
        this.addInput("减少", LiteGraph.EVENT);
        this.addInput("重置", LiteGraph.EVENT);

        this.addOutput("≥目标", LiteGraph.EVENT);
        this.addOutput("<目标", LiteGraph.EVENT);

        this.properties = {
            target: 5,
            step: 1,
            count: 0
        };

        this.addWidget("number", "目标值", this.properties.target, (v) => {
            this.properties.target = Math.max(1, Math.floor(v));
            this.setDirtyCanvas(true, true);
        }, { step: 10, precision: 0, min: 1 });

        this.addWidget("number", "步长", this.properties.step, (v) => {
            this.properties.step = Math.max(1, Math.floor(v));
            this.setDirtyCanvas(true, true);
        }, { step: 10, precision: 0, min: 1 });

        this.size = [220, 250];
        this.widgets_start_y = 95;
    }

    async onAction(action, param, options, action_slot) {
        const inputName = action_slot != null
            ? (this.inputs?.[action_slot]?.name || `slot_${action_slot}`)
            : action;
        console.log(`[Counter] 输入: ${inputName}`, `count=${this.properties.count}`);

        if (action_slot === 2 || inputName === "重置" || action === "重置") {
            this.properties.count = 0;
            this.setDirtyCanvas(true, true);
            return;
        }

        if (action_slot === 1 || inputName === "减少" || action === "减少") {
            this.properties.count = Math.max(0, this.properties.count - this.properties.step);
            this.setDirtyCanvas(true, true);
            return;
        }

        // 默认：增加 — 执行一次循环体并 +1，检查是否达目标
        await this._afterChange();
    }

    async _afterChange() {
        this.setDirtyCanvas(true, true);

        // 先执行一次循环体（<目标），再自增
        await this.execOutput(1);  // <目标 — 用户放循环体
        this.properties.count += this.properties.step;
        this.setDirtyCanvas(true, true);

        // 检查是否达到目标数
        const reached = this.properties.count >= this.properties.target;
        console.log(`[Counter] count=${this.properties.count}/${this.properties.target} -> ${reached ? ">=目标 : 结束" : "<目标 : 继续"}`);

        if (reached) {
            await this.execOutput(0); // >=目标 — 结束循环
            this.properties.count = 0;
            this.setDirtyCanvas(true, true);
        }
        // 未达目标 — 等待下一次触发
    }

    onDrawForeground(ctx) {
        if (this.flags.collapsed) return;

        const W = this.size[0];
        if (W <= 40) return;
        const WIDGET_H = LiteGraph.NODE_WIDGET_HEIGHT || 26;
        const top = this.widgets_start_y + 2 * WIDGET_H + 12;
        const h = 42;

        const lx = 14;
        const rw = 32;
        const cw = W - lx - rw;

        // ===== 背景面板 =====
        ctx.fillStyle = "rgba(0,0,0,0.2)";
        ctx.fillRect(lx, top, cw, h);

        // ===== 进度条 =====
        const barY = top + 6;
        const barH = 14;
        const progress = this.properties.target > 0
            ? Math.min(this.properties.count / this.properties.target, 1)
            : 0;
        const reached = progress >= 1;

        const padX = 6;
        const barW = cw - padX * 2;
        ctx.fillStyle = "rgba(0,0,0,0.3)";
        ctx.fillRect(lx + padX, barY, barW, barH);

        const fillW = Math.max(4, barW * progress);
        ctx.fillStyle = reached ? "#4caf50" : "#2196f3";
        ctx.fillRect(lx + padX, barY, fillW, barH);

        // ===== 计数文字 =====
        const textY = top + h - 9;
        const cx = lx + cw / 2;
        ctx.fillStyle = reached ? "#4caf50" : "#fff";
        ctx.font = "bold 16px monospace";
        ctx.textAlign = "center";
        ctx.textBaseline = "bottom";

        const txt = `${this.properties.count} / ${this.properties.target}`;
        ctx.fillText(txt, cx, textY);

        if (reached) {
            ctx.fillStyle = "#4caf50";
            ctx.font = "bold 13px sans-serif";
            ctx.textAlign = "right";
            ctx.fillText("✓", W - 18, textY);
        }
    }

    onConfigure() {
        // 载入存档后同步控件显示
        if (this.widgets) {
            this.widgets.forEach(w => {
                if (w.name === "目标值") w.value = this.properties.target;
                else if (w.name === "步长") w.value = this.properties.step;
            });
        }
    }

    getHelpText() {
        return "增加/减少/重置计数。达到目标值时触发 >=目标 并自动归零。";
    }

    onDblClick() {
        openNodePropertyEditor(this);
        return true;
    }
}
LiteGraph.registerNodeType("flow/counter", CounterNode);
