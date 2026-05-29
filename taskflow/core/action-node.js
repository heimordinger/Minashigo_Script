import { reportNodeEvent } from "./node-reporter.js";

export class ActionNode extends LiteGraph.LGraphNode {
  constructor(title) {
    super();
    this.title = title;
    this._running = false;
  }

  _controller() {
    return window.workflowController;
  }

  _backend() {
    return window.taskflow?.backend;
  }

  async _waitControllerReady() {
    const controller = this._controller();
    if (!controller) return false;
    return controller.waitUntilRunnable();
  }

  async callBackend(taskName, properties, timeout = 30000) {
    const backend = this._backend();
    if (!backend) {
      throw new Error("Taskflow backend is not ready");
    }
    
    // 添加账号信息到请求参数
    const propertiesWithAccount = window.accountManager?.addAccountToParams(properties) || properties;
    
    const controller = this._controller();
    controller?.startRequest?.();
    const response = await backend.invoke(taskName, propertiesWithAccount, timeout);
    return response;
  }

  async run(action) {
    if (this._running) return;

    if (action && action !== "trigger" && action !== "flow") return;

    const canRun = await this._waitControllerReady();
    if (!canRun) return;

    this._running = true;
    this._controller()?.onNodeStart?.(this);
    await reportNodeEvent(this, "start", { action });

    try {
      await this.onRun();
      await reportNodeEvent(this, "success");
      const stillRunnable = await this._waitControllerReady();
      if (stillRunnable) {
        this.triggerSlot(0);
      }
    } catch (error) {
      await reportNodeEvent(this, "error", { message: error.message || String(error) });
      console.error(`[${this.title}]`, error);
      window.showToast?.(`${this.title} 执行失败: ${error.message}`, "error");
      this._controller()?.stop();
    } finally {
      this._running = false;
      this._controller()?.onNodeFinish?.(this);
    }
  }

  async onRun() {}

  /**
   * 记录日志到悬浮日志面板
   * @param {string} message 日志内容
   * @param {'info'|'warn'|'error'} level 级别
   */
  log(message, level = "info") {
    const title = this.title || this.constructor.title || "节点";
    window.taskflowLog?.(level, `[${title}] ${message}`);
  }
}
