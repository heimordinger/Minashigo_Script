import { Message } from "../JS&PyMessage/message.js";

export class TaskflowBackend {
  constructor(wsManager) {
    this.wsManager = wsManager;
    this.defaultAccount = "default";
    this.httpPort = 8010; // HTTP端口
  }

  setAccount(account) {
    this.defaultAccount = account || "default";
  }

  async invoke(taskName, properties = {}, timeout = 30000) {
    // 直接使用WebSocket连接
    const tabAccount = this.getCurrentTabAccount();
    console.log(`[TaskflowBackend] 使用WebSocket调用任务: ${taskName}`);
    console.log(`[TaskflowBackend] 账号: ${tabAccount?.email || this.defaultAccount}`);

    return await this._invokeViaWebSocket(taskName, properties, timeout);
  }

  async _invokeViaWebSocket(taskName, properties = {}, timeout = 30000) {
    console.log(`[TaskflowBackend] 调用任务: ${taskName}`);

    // 从当前活跃的tab获取账号信息
    const currentTabAccount = this.getCurrentTabAccount();

    // 确保 properties 中包含 account 信息（Python 后端从 props 中读取 account）
    const propsWithAccount = { ...properties };
    if (!propsWithAccount.account && currentTabAccount.email !== 'default@example.com') {
      propsWithAccount.account = currentTabAccount;
    }

    const form = Message.createEmptyTask();
    form.meta.account = currentTabAccount.email;
    form.task.task_name = taskName;
    form.task.properties = propsWithAccount;
    form.task.timeout = timeout;

    Message.validate(form);
    
    const response = await this.wsManager.sendTask(form);
    console.log(`[TaskflowBackend] 任务完成: ${taskName}`);
    return response;
  }

  getCurrentTabAccount() {
    try {
      // 获取当前活跃的tab
      const currentTab = window.currentTab;
      
      if (currentTab && currentTab.account) {
        return currentTab.account;
      }
      
      // 如果当前tab没有账号信息，尝试从其他tab获取
      const tabs = window.tabs || [];
      for (const tab of tabs) {
        if (tab.account && tab.account.email) {
          return tab.account;
        }
      }
      
      // 最后回退到全局accountInfo
      if (window.accountInfo && window.accountInfo.email) {
        return window.accountInfo;
      }
      
      // 最后回退到默认账号
      return { name: 'default', email: 'default@example.com' };
      
    } catch (error) {
      console.error(`[TaskflowBackend] 获取账号失败:`, error);
      return { name: 'default', email: 'default@example.com' };
    }
  }
}
