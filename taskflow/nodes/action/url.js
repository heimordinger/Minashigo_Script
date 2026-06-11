// taskflow/nodes/action/url.js
import { ActionNode } from "../../core/action-node.js";
import { openNodePropertyEditor } from "../../core/input-dialog.js";

class URLNode extends ActionNode {
    static title = "URL跳转";
    
    constructor() {
        super("URL跳转");
        this.category = "Action";
        
        this.addInput("触发", LiteGraph.EVENT);
        this.addInput("url", "string");
        this.addInput("wait_for_load", "boolean");
        this.addInput("timeout", "number");
        
        this.addOutput("下一步", LiteGraph.EVENT);
        this.addOutput("成功", "boolean");
        
        // 属性（默认值）
        this.properties = {
            url: "https://www.example.com",
            wait_for_load: true,
            timeout: 30
        };
        
        // 在节点上添加输入框
        this.addWidget("text", "URL", this.properties.url, (value) => {
            this.properties.url = value;
        });
        
        this.addWidget("toggle", "等待加载", this.properties.wait_for_load, (value) => {
            this.properties.wait_for_load = value;
        });
        
        this.addWidget("number", "超时(秒)", this.properties.timeout, (value) => {
            this.properties.timeout = value;
        }, { min: 1, max: 300 });
    }
    
    async onAction() {
        const url = this.getInputData(0) || this.properties.url;
        const waitForLoad = this.getInputData(1) ?? this.properties.wait_for_load;
        const timeout = this.getInputData(2) ?? this.properties.timeout;

        this.log(`跳转到: ${url}`);

        if (!url) {
            this.log(`URL为空`, "error");
            throw new Error("URL不能为空");
        }

        // 获取当前tab的账号信息
        const currentTabAccount = this.getCurrentTabAccount();
        const callParams = {
            url: url,
            wait_for_load: waitForLoad,
            timeout: timeout,
            account: currentTabAccount
        };

        try {
            const result = await this.callBackend("goto", callParams);

            if (result && result.success) {
                this.log(`跳转成功: ${url}`);
                this.setOutputData(1, true);
                this.setOutputData(2, url);
                await this.execOutput(0);
            } else {
                this.log(`跳转失败: ${result?.error || '未知错误'}`, "error");
                throw new Error(result?.error || "跳转失败");
            }
        } catch (error) {
            this.log(`执行异常: ${error.message}`, "error");
            throw error;
        }
    }
    
    onDblClick() {
        // 设置属性信息，供属性编辑器使用
        this.properties_info = [
            {
                name: "url",
                type: "string",
                label: "目标URL"
            },
            {
                name: "wait_for_load",
                type: "boolean",
                label: "等待加载完成"
            },
            {
                name: "timeout",
                type: "number",
                label: "超时时间(秒)",
                min: 1,
                max: 300
            }
        ];
        
        openNodePropertyEditor(this);
        return true;
    }
    
    onOpenPropertyEditor() {
        this.onDblClick();
    }
    
    getIcon() {
        return "🌐";
    }
    
    getCurrentTabAccount() {
        try {
            // 获取当前活跃的tab的账号信息
            const tabs = window.tabs || [];
            const currentTab = window.currentTab;
            
            console.log(`[URLNode] 获取当前tab账号信息:`);
            console.log(`[URLNode]   - 总tabs数: ${tabs.length}`);
            console.log(`[URLNode]   - 当前tab: ${currentTab?.id}`);
            
            if (currentTab && currentTab.account) {
                console.log(`[URLNode] ✅ 从当前tab获取账号: ${currentTab.account.email}`);
                return currentTab.account;
            }
            
            // 如果当前tab没有账号信息，尝试从其他tab获取
            for (const tab of tabs) {
                if (tab.account && tab.account.email) {
                    console.log(`[URLNode] ⚠️  从其他tab获取账号: ${tab.account.email}`);
                    return tab.account;
                }
            }
            
            // 最后回退到全局accountInfo
            if (window.accountInfo && window.accountInfo.email) {
                console.log(`[URLNode] ⚠️  从全局accountInfo获取账号: ${window.accountInfo.email}`);
                return window.accountInfo;
            }
            
            // 最后回退到默认账号
            console.log(`[URLNode] ❌ 使用默认账号`);
            return { name: 'default', email: 'default@example.com' };

        } catch (error) {
            console.error(`[URLNode] 获取当前tab账号失败:`, error);
            return { name: 'default', email: 'default@example.com' };
        }
    }

    getHelpText() {
        return `
URL跳转节点用于导航到指定网址。

参数说明：
- 目标URL: 要跳转的网址，支持http://和https://
- 等待加载完成: 是否等待页面完全加载
- 超时时间: 页面加载的最大等待时间

使用示例：
1. 设置目标URL为 https://www.google.com
2. 启用等待加载完成
3. 设置超时时间为30秒
4. 运行节点将跳转到Google首页
        `;
    }
}

// 注册节点类型
LiteGraph.registerNodeType("action/url", URLNode);
