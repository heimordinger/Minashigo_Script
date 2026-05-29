// core/account-api.js
import { accountManager } from './account-manager.js';

export class AccountAPI {
  constructor(backend) {
    this.backend = backend;
  }

  /**
   * 执行账号特定操作
   * @param {string} operation - 操作类型
   * @param {Object} parameters - 操作参数
   * @param {Object} account - 账号信息 (可选，默认使用当前账号)
   * @returns {Promise<Object>} 操作结果
   */
  async executeAccountOperation(operation, parameters = {}, account = null) {
    const targetAccount = account || accountManager.getCurrentAccount();
    
    if (!targetAccount) {
      throw new Error('没有指定账号或当前账号为空');
    }

    const props = {
      account: targetAccount,
      operation,
      parameters
    };

    console.log(`[AccountAPI] 执行账号操作: ${targetAccount.name} -> ${operation}`);
    
    return await this.backend.invoke('account_operation', props);
  }

  /**
   * 启动账号浏览器
   * @param {Object} account - 账号信息 (可选)
   * @returns {Promise<Object>} 操作结果
   */
  async startBrowser(account = null) {
    return await this.executeAccountOperation('start_browser', {}, account);
  }

  /**
   * 停止账号浏览器
   * @param {Object} account - 账号信息 (可选)
   * @returns {Promise<Object>} 操作结果
   */
  async stopBrowser(account = null) {
    return await this.executeAccountOperation('stop_browser', {}, account);
  }

  /**
   * 重启账号浏览器
   * @param {Object} account - 账号信息 (可选)
   * @returns {Promise<Object>} 操作结果
   */
  async restartBrowser(account = null) {
    return await this.executeAccountOperation('restart_browser', {}, account);
  }

  /**
   * 获取账号状态
   * @param {Object} account - 账号信息 (可选)
   * @returns {Promise<Object>} 账号状态
   */
  async getAccountStatus(account = null) {
    return await this.executeAccountOperation('get_status', {}, account);
  }

  /**
   * 为账号执行脚本
   * @param {string} script - 脚本内容或路径
   * @param {Object} account - 账号信息 (可选)
   * @returns {Promise<Object>} 执行结果
   */
  async executeScript(script, account = null) {
    return await this.executeAccountOperation('execute_script', { script }, account);
  }

  /**
   * 为账号截图
   * @param {Object} account - 账号信息 (可选)
   * @returns {Promise<Object>} 截图结果
   */
  async takeScreenshot(account = null) {
    return await this.executeAccountOperation('take_screenshot', {}, account);
  }

  /**
   * 批量操作多个账号
   * @param {Array} operations - 操作数组 [{account, operation, parameters}]
   * @returns {Promise<Array>} 批量操作结果
   */
  async executeBatchOperations(operations) {
    const promises = operations.map(({ account, operation, parameters }) => 
      this.executeAccountOperation(operation, parameters, account)
    );
    
    return await Promise.all(promises);
  }

  /**
   * 获取所有账号状态
   * @param {Array} accounts - 账号列表
   * @returns {Promise<Array>} 所有账号状态
   */
  async getAllAccountsStatus(accounts = null) {
    const targetAccounts = accounts || accountManager.getAllAccounts();
    
    const operations = targetAccounts.map(account => ({
      account,
      operation: 'get_status',
      parameters: {}
    }));
    
    return await this.executeBatchOperations(operations);
  }
}

// 创建全局实例
export const accountAPI = new AccountAPI(window.taskflow?.backend);

// 导出到全局作用域
window.accountAPI = accountAPI;
