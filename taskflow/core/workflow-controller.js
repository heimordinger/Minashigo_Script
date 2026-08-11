// core/workflow-controller.js

/**
 * 纯 execOutput 级联模式
 * 节点完成 → execOutput 驱动下游，分支只走选中，循环自然重复
 */

export class WorkflowController {
  constructor(getGraph) {
    this.getGraph = getGraph;
    this.state = STATE.IDLE;
    this.currentNode = null;
    this.currentLink = null;
    this.errorNode = null;
    this.nodeTimeout = 60000;
    this.nodeExecutionStartTime = null;
    this.timeoutCheckInterval = null;
    this._paused = false;      // 标记是否曾暂停（阻止提前 finish）
    this._startTimeoutChecker();
    this._registerExecOutput();
  }

  _registerExecOutput() {
    if (LGraphNode.prototype._execOutputRegistered) return;
    LGraphNode.prototype._execOutputRegistered = true;

    // 递归 force_update：通过深度计数器向上游传播
    if (!LGraphNode.prototype._forcePullRegistered) {
      LGraphNode.prototype._forcePullRegistered = true;
      let _forceDepth = 0;
      const origGet = LGraphNode.prototype.getInputData;
      LGraphNode.prototype.getInputData = function (slot, force) {
        if (force) _forceDepth++;
        try {
          return origGet.call(this, slot, force || _forceDepth > 0);
        } finally {
          if (force) _forceDepth--;
        }
      };
    }

    LGraphNode.prototype.execOutput = async function (slot) {
      const output = this.outputs?.[slot];
      if (!output?.links?.length) return;
      const graph = this.graph;
      if (!graph) return;
      const ctrl = window.workflowController;
      if (!ctrl || ctrl.state !== STATE.RUNNING) return;

      const targets = [];
      for (const linkId of output.links) {
        const link = graph.links[linkId];
        if (!link) continue;
        const t = graph._nodes.find(n => n.id === link.target_id);
        if (t && t.onAction) targets.push({ node: t, targetSlot: link.target_slot });
      }
      if (!targets.length) return;

      await new Promise(r => queueMicrotask(r));

      if (targets.length === 1) {
        await ctrl._runNode(targets[0].node, "flow", targets[0].targetSlot);
      } else {
        await Promise.all(targets.map(t => ctrl._runNode(t.node, "flow", t.targetSlot)));
      }
    };
  }

  setState(state) {
    this.state = state;
    window.updateControlUI?.(state);
    window.updateRuntimeState?.(state);
  }

  async start() {
    if (this.state === STATE.RUNNING) return false;
    // 清空变量池
    if (LiteGraph.Globals) {
      Object.keys(LiteGraph.Globals).forEach(k => { if (k.startsWith("__var_")) delete LiteGraph.Globals[k]; });
    }
    const startNode = this._findStartNode();
    if (!startNode) return false;
    this.setState(STATE.RUNNING);

    // 启动执行链（不阻塞监控循环）
    const chain = this._runNode(startNode, "flow").then(() => {
      if (this.state === STATE.RUNNING) this.finish();
    });

    // 监控循环：执行中或暂停时保持等待
    while (this.state === STATE.RUNNING || this.state === STATE.PAUSED) {
      await new Promise(r => setTimeout(r, 80));
    }

    await chain.catch(() => {});
    return true;
  }

  pause() {
    if (this.state !== STATE.RUNNING) return;
    this.setState(STATE.PAUSED);
    this._sendCtrlSignal("pause_task");
  }

  resume() {
    if (this.state !== STATE.PAUSED) return;
    this.setState(STATE.RUNNING);
    this._sendCtrlSignal("resume_task");
  }

  stop() {
    if (this.state === STATE.STOPPED || this.state === STATE.IDLE) return;
    this.setState(STATE.STOPPED);
    this.currentNode = null;
    this.currentLink = null;
    this.errorNode = null;
    window.updateCurrentNode?.(null);
    window.highlightLink?.(null);
    window.highlightErrorNode?.(null);
    // 清除所有节点高亮
    const tab = window.getCurrentTab?.();
    const gc = tab?.graphCanvas;
    if (gc) {
      gc.highlight_node_id = null;
      gc.setDirty(true, true);
    }
    // 通知后端停止正在执行的任务
    this._sendCtrlSignal("stop_task");
  }

  _sendCtrlSignal(taskName) {
    try {
      const ws = window.taskflow?.backend?.wsManager?.ws;
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      const tab = window.getCurrentTab?.();
      ws.send(JSON.stringify({
        task: {
          task_name: taskName,
          properties: { account: tab?.account || {} }
        },
        meta: { id: taskName + "_" + Date.now() }
      }));
    } catch(_) {}
  }

  finish() {
    if (this.state === STATE.FINISHED || this.state === STATE.STOPPED) return;
    this.setState(STATE.FINISHED);
    this.currentNode = null;
    this.currentLink = null;
    this.errorNode = null;
    window.updateCurrentNode?.(null);
    window.highlightLink?.(null);
    window.highlightErrorNode?.(null);
  }

  async _runNode(node, action, targetSlot) {
    if (!node) return;
    while (this.state === STATE.PAUSED) await new Promise(r => setTimeout(r, 80));
    if (this.state !== STATE.RUNNING) return;

    this.currentNode = node;
    this.nodeExecutionStartTime = Date.now();
    window.updateCurrentNode?.(node);
    window.highlightNode?.(node);

    const delay = window.getNodeDelay?.() || 0;
    if (delay > 0) await new Promise(r => setTimeout(r, delay));

    try {
      await node.onAction?.(action, null, null, targetSlot);
    } catch (err) {
      console.error(`[Workflow] 节点 ${node.title} 失败:`, err);
      window.unhighlightNode?.(node);
      // 先查是否有"错误"输出插槽且被连线
      const errorSlot = node.outputs?.findIndex(
        o => o.name === "错误" || o.name === "error"
      );
      if (errorSlot !== undefined && errorSlot >= 0 && node.outputs?.[errorSlot]?.links?.length) {
        window.showToast?.(`${node.title} 执行出错, 走错误分支: ${err.message}`, "warn");
        await node.execOutput(errorSlot);
        return;
      }
      // 没有错误分支 → 崩溃停图
      this.setErrorNode(node);
      this.stop();
      window.showToast?.(`节点 ${node.title} 执行失败: ${err.message}`, "error");
      return;
    }

    this.nodeExecutionStartTime = null;
    this.currentNode = null;
    window.unhighlightNode?.(node);
    window.highlightLink?.(null);
    this.currentLink = null;
    window.updateCurrentNode?.(null);
  }

  // ===== 工具 =====

  waitUntilRunnable() { return this.state === STATE.RUNNING; }
  startRequest() { this.nodeExecutionStartTime = Date.now(); }
  endRequest() { this.nodeExecutionStartTime = null; }
  setErrorNode(node) { this.errorNode = node; window.highlightErrorNode?.(node); }
  clearErrorNode() { this.errorNode = null; window.highlightErrorNode?.(null); }
  checkNodeTimeout() {
    if (this.nodeExecutionStartTime && this.state === STATE.RUNNING) {
      const elapsed = Date.now() - this.nodeExecutionStartTime;
      if (elapsed > this.nodeTimeout)
        throw new Error(`节点执行超时: ${elapsed}ms > ${this.nodeTimeout}ms`);
    }
  }
  _startTimeoutChecker() {
    this.timeoutCheckInterval = setInterval(() => {
      if (this.state === STATE.RUNNING) {
        try { this.checkNodeTimeout(); }
        catch (err) {
          console.error(err);
          if (this.currentNode) this.setErrorNode(this.currentNode);
          this.stop();
          window.showToast?.(`超时错误: ${err.message}`, "error");
        }
      }
    }, 1000);
  }
  _stopTimeoutChecker() {
    if (this.timeoutCheckInterval) { clearInterval(this.timeoutCheckInterval); this.timeoutCheckInterval = null; }
  }

  _findStartNode() {
    const graph = this.getGraph?.();
    if (!graph) return null;
    const node = graph._nodes.find(n => n.type === "flow/start");
    if (!node) {
      window.showToast?.("未找到 Start 节点", "error");
      this.setState(STATE.IDLE);
    }
    return node;
  }
}

export const STATE = {
  IDLE: "idle", RUNNING: "running", PAUSED: "paused",
  STOPPED: "stopped", FINISHED: "finished",
};

/**
 * 全局变量插值：将字符串中的 {varName} 替换为变量池中的值
 * @param {string} text
 * @returns {string}
 */
window.resolveVars = function(text) {
  if (!text || typeof text !== "string" || !text.includes("{")) return text;
  return text.replace(/\{(\w+)\}/g, (match, name) => {
    const poolKey = `__var_${name}`;
    const val = LiteGraph.Globals[poolKey];
    console.log(`[resolveVars] lookup ${poolKey} =`, val, `(type=${typeof val})`);
    return val !== undefined ? String(val) : match;
  });
};
