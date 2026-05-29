// core/account-manager.js

export class AccountManager {
  constructor() {
    this.currentAccount = null;
    this.accounts = new Map();
  }

  /**
   * 设置当前账号
   * @param {Object} account - 账号信息 {name: string, email: string}
   */
  setCurrentAccount(account) {
    this.currentAccount = account;
    if (account) {
      this.accounts.set(account.email, account);
    }
    
    // 通知其他组件账号已变更
    window.dispatchEvent(new CustomEvent('accountChanged', {
      detail: { account: this.currentAccount }
    }));
  }

  /**
   * 获取当前账号
   * @returns {Object|null} 当前账号信息
   */
  getCurrentAccount() {
    return this.currentAccount;
  }

  /**
   * 获取账号名称
   * @returns {string} 账号名称
   */
  getAccountName() {
    return this.currentAccount?.name || 'unknown';
  }

  /**
   * 获取账号邮箱
   * @returns {string} 账号邮箱
   */
  getAccountEmail() {
    return this.currentAccount?.email || '';
  }

  /**
   * 检查是否有当前账号
   * @returns {boolean} 是否有当前账号
   */
  hasAccount() {
    return this.currentAccount !== null;
  }

  /**
   * 添加账号到缓存
   * @param {Object} account - 账号信息
   */
  addAccount(account) {
    this.accounts.set(account.email, account);
  }

  /**
   * 根据邮箱获取账号
   * @param {string} email - 账号邮箱
   * @returns {Object|null} 账号信息
   */
  getAccount(email) {
    return this.accounts.get(email) || null;
  }

  /**
   * 获取所有账号
   * @returns {Array} 账号列表
   */
  getAllAccounts() {
    return Array.from(this.accounts.values());
  }

  /**
   * 清除当前账号
   */
  clearCurrentAccount() {
    this.currentAccount = null;
    window.dispatchEvent(new CustomEvent('accountChanged', {
      detail: { account: null }
    }));
  }

  /**
   * 将账号信息添加到请求参数
   * @param {Object} params - 请求参数
   * @returns {Object} 包含账号信息的参数
   */
  addAccountToParams(params = {}) {
    if (this.currentAccount) {
      return {
        ...params,
        account: this.currentAccount
      };
    }
    return params;
  }

  /**
   * 获取账号标识符（用于日志、调试等）
   * @returns {string} 账号标识符
   */
  getAccountIdentifier() {
    if (!this.currentAccount) return 'no-account';
    return `${this.currentAccount.name}(${this.currentAccount.email})`;
  }
}

// 全局账号管理器实例
export const accountManager = new AccountManager();

// 从URL参数初始化账号信息
function initAccountFromURL() {
  const urlParams = new URLSearchParams(window.location.search);
  const name = urlParams.get('name');
  const email = urlParams.get('email');
  
  if (name && email) {
    accountManager.setCurrentAccount({
      name: decodeURIComponent(name),
      email: decodeURIComponent(email)
    });
    console.log(`[AccountManager] 初始化账号: ${accountManager.getAccountIdentifier()}`);
  }
}

// 页面加载完成后初始化
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initAccountFromURL);
} else {
  initAccountFromURL();
}

// 导出到全局作用域供其他脚本使用
window.accountManager = accountManager;
