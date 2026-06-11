import { reportNodeEvent } from "./node-reporter.js";

export class ActionNode extends LiteGraph.LGraphNode {
  constructor(title) {
    super();
    this.title = title;
  }

  _controller() {
    return window.workflowController;
  }

  _backend() {
    return window.taskflow?.backend;
  }

  async callBackend(taskName, properties, timeout = 30000) {
    const backend = this._backend();
    if (!backend) throw new Error("Taskflow backend is not ready");
    const propertiesWithAccount = window.accountManager?.addAccountToParams(properties) || properties;
    const controller = this._controller();
    controller?.startRequest?.();
    const response = await backend.invoke(taskName, propertiesWithAccount, timeout);
    if (response?.data?.error) {
      const m = response.data.error;
      if (m.includes("连接已断开") || m.includes("Target closed") || m.includes("has been closed")) {
        throw new Error(m);
      }
    }
    return response;
  }

  async run(action) {
    if (action && action !== "trigger" && action !== "flow") return;

    const canRun = await this._waitControllerReady();
    if (!canRun) return;

    this._controller()?.onNodeStart?.(this);
    await reportNodeEvent(this, "start", { action });

    try {
      await this.onRun();
      await reportNodeEvent(this, "success");
      // 执行完成后驱动 slot 0（下一步）
      // 非 ActionNode 子类可覆盖此行为
      await this.execOutput(0);
    } catch (error) {
      await reportNodeEvent(this, "error", { message: error.message || String(error) });
      console.error(`[${this.title}]`, error);
      window.showToast?.(`${this.title} 执行失败: ${error.message}`, "error");
      this._controller()?.stop();
    } finally {
      this._controller()?.onNodeFinish?.(this);
    }
  }

  async onRun() {}

  async _waitControllerReady() {
    const ctrl = this._controller();
    if (!ctrl) return false;
    while (ctrl.state === "paused") {
      await new Promise(r => setTimeout(r, 80));
    }
    return ctrl.state === "running";
  }

  log(message, level = "info") {
    const title = this.title || this.constructor.title || "节点";
    window.taskflowLog?.(level, `[${title}] ${message}`);
  }
}
