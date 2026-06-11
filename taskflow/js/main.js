// taskflow/js/main.js

import {
  tabs,
  currentTab,
  newTabBtn,
  draggedTab,
  setDraggedTab,
  setCurrentTab
} from "./state.js";

import { updateTabWidths } from "./layout.js";
import { createTab, showTabCreationDropdown } from "./tab.js";
import { initNodePanel } from "../core/node-panel.js";
import { loadAllNodes } from "../core/loader.js";
import { initControlPanel } from "../core/control-panel.js";
import { WSManager } from "../JS&PyMessage/wsManager.js";
import { WS_PORT } from "../ws_port.js";
import { WorkflowController } from "../core/workflow-controller.js";
import {initRuntimePanel} from "../core/runtime-panel.js";
import { TaskflowBackend } from "../core/taskflow-backend.js";
import { LogPanel } from "../core/log-panel.js";

// WSManager（任务执行）连旧端口 8011，那里有 TaskDispatcher 处理 task 消息
const TASK_WS_PORT = WS_PORT;  // 8011
const REALTIME_WS_PORT = WS_PORT + 100;  // 8111，实时事件

const taskWsUrl = `ws://127.0.0.1:${TASK_WS_PORT}`;
export const ws = new WSManager(taskWsUrl);
ws.connect(taskWsUrl);
console.log(`[Main] WSManager已连接任务WebSocket: ${taskWsUrl}`);

// connectWebSocket（接收create_tab等命令）连实时端口 8111
const eventWsUrl = `ws://127.0.0.1:${REALTIME_WS_PORT}`;
console.log(`[Main] 事件WebSocket端口: ${REALTIME_WS_PORT}`);

// 简单的WebSocket连接管理
let wsConnection = null;
let isConnecting = false;
let _wsReconnectAttempts = 0;
const _WS_MAX_RECONNECT = 5;

// 直接连接WebSocket
function connectWebSocket() {
    if (_wsReconnectAttempts >= _WS_MAX_RECONNECT) {
        console.log('[Main] WebSocket重连次数已达上限，停止重连');
        return;
    }
    if (wsConnection && wsConnection.readyState === WebSocket.OPEN) {
        console.log('[Main] WebSocket已连接');
        return;
    }

    if (isConnecting) {
        console.log('[Main] WebSocket正在连接中...');
        return;
    }

    isConnecting = true;
    console.log('[Main] 开始连接WebSocket...');

    try {
        wsConnection = new WebSocket(eventWsUrl);

        wsConnection.onopen = () => {
            console.log('[Main] WebSocket连接成功');
            isConnecting = false;
            _wsReconnectAttempts = 0;  // 连接成功重置计数
            setupMessageHandler();
        };

        wsConnection.onerror = (error) => {
            console.error('[Main] WebSocket连接错误:', error);
            isConnecting = false;
        };

        wsConnection.onclose = (event) => {
            console.log('[Main] WebSocket连接关闭，代码:', event.code);
            wsConnection = null;
            isConnecting = false;

            // 非正常关闭时重连（最多 _WS_MAX_RECONNECT 次）
            if (event.code !== 1000) {
                _wsReconnectAttempts++;
                console.log(`[Main] WebSocket重连 (${_wsReconnectAttempts}/${_WS_MAX_RECONNECT})`);
                setTimeout(connectWebSocket, 3000);
            }
        };

    } catch (error) {
        console.error('[Main] WebSocket连接失败:', error);
        isConnecting = false;
        _wsReconnectAttempts++;
        if (_wsReconnectAttempts < _WS_MAX_RECONNECT) {
            setTimeout(connectWebSocket, 5000);
        }
    }
}

// 全局任务队列实例（需在消息处理器之前初始化）
// ===== 任务队列系统 =====
class TaskQueue {
    constructor() {
        this.tasks = [];
        this.isProcessing = false;
        this.websocketReady = false;
    }
    
    addTask(task) {
        console.log(`[TaskQueue] 添加任务: ${task.type}`, task.data);
        this.tasks.push(task);
        this.processQueue();
    }
    
    setWebSocketReady(ready) {
        console.log(`[TaskQueue] WebSocket状态: ${ready ? '就绪' : '未就绪'}`);
        this.websocketReady = ready;
        if (ready) {
            this.processQueue();
        }
    }
    
    async processQueue() {
        if (this.isProcessing || this.tasks.length === 0 || !this.websocketReady) {
            return;
        }
        
        this.isProcessing = true;
        console.log(`[TaskQueue] 开始处理任务队列，待处理任务数: ${this.tasks.length}`);
        
        while (this.tasks.length > 0 && this.websocketReady) {
            const task = this.tasks.shift();
            console.log(`[TaskQueue] 执行任务: ${task.type}`);
            
            try {
                await this.executeTask(task);
                console.log(`[TaskQueue] 任务完成: ${task.type}`);
            } catch (error) {
                console.error(`[TaskQueue] 任务执行失败: ${task.type}`, error);
            }
        }
        
        this.isProcessing = false;
        console.log(`[TaskQueue] 任务队列处理完成`);
    }
    
    async executeTask(task) {
        switch (task.type) {
            case 'create_account_tab':
                await this.executeCreateAccountTab(task.data);
                break;
            case 'node_operation':
                await this.executeNodeOperation(task.data);
                break;
            default:
                console.warn(`[TaskQueue] 未知任务类型: ${task.type}`);
        }
    }
    
    async executeCreateAccountTab(accountInfo) {
        console.log(`[TaskQueue] 创建账号tab任务: ${accountInfo.name} (${accountInfo.email})`);
        createAccountTab(accountInfo);
    }
    
    async executeNodeOperation(operationData) {
        console.log(`[TaskQueue] 执行节点操作:`, operationData);
        // 实现节点操作逻辑
        if (operationData.action && window[operationData.action]) {
            await window[operationData.action](operationData.params);
        }
    }
}

// ===== 全局TaskFlow初始化 =====
function initializeGlobalTaskFlow() {
    console.log('[Main] 初始化全局TaskFlow，建立WebSocket连接');

    // 清除现有tabs，保留+号按钮
    tabs.length = 0;
    const tabBar = document.getElementById("tab-bar");
    const canvasContainer = document.getElementById("canvas-container");
    tabBar.innerHTML = "";
    canvasContainer.innerHTML = "";

    // 重建+号按钮
    const newBtn = document.createElement("button");
    newBtn.id = "new-tab-btn";
    newBtn.className = "new-tab-btn";
    newBtn.textContent = "+";
    newBtn.onclick = () => showTabCreationDropdown();
    tabBar.appendChild(newBtn);

    // 初始化日志面板
    const logPanel = new LogPanel();
    window._logPanel = logPanel;

    // 不再检查URL参数，完全依赖任务队列
    console.log('[Main] 全局TaskFlow初始化完成，等待WebSocket连接和任务');
}

// ===== 从Account-Panel创建绑定Tab =====
// 由account-panel调用，为指定账号创建tab
function createAccountTab(accountInfo) {
    console.log(`[Main] ========== 开始创建账号tab ==========`);
    console.log(`[Main] 从account-panel接收账号: ${accountInfo.name} (${accountInfo.email})`);

    // 计算同名账号的序号（支持同一账号开多个tab）
    const sameNameCount = tabs.filter(t =>
        t.account && t.account.email === accountInfo.email
    ).length;
    const suffix = sameNameCount > 0 ? ` #${sameNameCount + 1}` : '';
    const tabTitle = `${accountInfo.name} (${accountInfo.email})${suffix}`;
    createTab(tabTitle);

    // 覆盖账号信息为真实账号
    const tab = tabs[tabs.length - 1];
    if (tab) tab.account = accountInfo;

    // 同步到 accountManager，确保 callBackend/addAccountToParams 能获取到真实账号
    if (window.accountManager) {
        window.accountManager.setCurrentAccount(accountInfo);
        console.log(`[Main] accountManager 已更新为: ${accountInfo.name} (${accountInfo.email})`);
    }

    // 将账号添加到账号池（供+号下拉菜单使用）
    if (!window.accountPool) window.accountPool = [];
    if (!window.accountPool.find(a => a.email === accountInfo.email)) {
        window.accountPool.push(accountInfo);
        console.log(`[Main] 账号已添加到账号池: ${accountInfo.name}`);
    }

    console.log(`[Main] ========== 账号tab创建完成: ${tabTitle} ==========`);
    return tab;
}

// 全局函数，供account-panel调用
window.createAccountTab = createAccountTab;

// ===== WebSocket命令处理 =====
// 处理来自account-panel的创建tab命令
function handleWebSocketCommand(command) {
    console.log('[Main] 收到WebSocket命令:', command);
    if (command.type === 'create_tab') {
        console.log('[Main] 收到创建tab命令:', command.account_info);
        createAccountTab(command.account_info);
    }
}

// 监听WebSocket消息 - 多种方式确保接收
window.addEventListener('message', (event) => {
    try {
        const data = JSON.parse(event.data);
        console.log('[Main] 收到window消息:', data);
        if (data.type === 'command') {
            handleWebSocketCommand(data.payload);
        }
    } catch (e) {
        // 忽略非JSON消息
    }
});

// 设置任务队列监听
setTimeout(() => {
    console.log('[Main] 设置任务队列监听');
    taskQueue.setWebSocketReady(true);
}, 4000);

const taskQueue = new TaskQueue();

// 设置消息处理器
function setupMessageHandler() {
    if (!wsConnection) return;
    
    wsConnection.onmessage = (event) => {
        console.log('[Main] 收到WebSocket消息:', event.data);
        
        try {
            const data = JSON.parse(event.data);
            console.log('[Main] 解析后的WebSocket数据:', data);
            
            if (data.type === 'init_accounts') {
                // 后端下发的已注册账号列表（页面刷新后重建账号池）
                console.log('[Main] 收到账号列表:', data.accounts);
                if (!window.accountPool) window.accountPool = [];
                for (const acct of data.accounts) {
                    if (!window.accountPool.find(a => a.email === acct.email)) {
                        window.accountPool.push(acct);
                    }
                }
                console.log('[Main] 账号池已重建:', window.accountPool);

            } else if (data.type === 'log_output') {
                // 后端推来的日志
                window.taskflowLog?.(data.level || 'info', data.message);

            } else if (data.type === 'command') {
                console.log('[Main] 检测到command类型消息:', data.payload);

                if (data.payload.type === 'create_tab') {
                    // 创建新tab
                    taskQueue.addTask({
                        type: 'create_account_tab',
                        data: data.payload.account_info
                    });
                } else if (data.payload.type === 'focus_tab') {
                    // 聚焦已有tab
                    const existing = tabs.find(t =>
                        t.account && t.account.email === data.payload.account_info.email
                    );
                    if (existing) {
                        console.log('[Main] 聚焦到已有tab:', existing.id, existing.account?.name);
                        switchTab(existing.id);
                    } else {
                        console.log('[Main] 未找到对应tab，回退创建新tab:', data.payload.account_info);
                        taskQueue.addTask({
                            type: 'create_account_tab',
                            data: data.payload.account_info
                        });
                    }
                }
            }
        } catch (e) {
            console.warn('[Main] 解析WebSocket消息失败:', e);
        }
    };
    
    // 连接成功后等待WS命令创建账号tab
    setTimeout(() => {
        console.log('[Main] WebSocket就绪，等待后端创建账号tab...');
        taskQueue.setWebSocketReady(true);
    }, 2000);
}

window.connectWebSocket = connectWebSocket;

// 页面加载后连接WebSocket
window.addEventListener('load', () => {
    console.log('[Main] ========== 页面加载完成 ==========');
    console.log('[Main] TaskFlow页面已完全加载，准备连接WebSocket...');
    
    // 初始化账号池
    initializeAccountPool();
    
    // 延迟3秒后连接WebSocket
    setTimeout(() => {
        console.log('[Main] 开始连接WebSocket...');
        connectWebSocket();
    }, 3000);
});

// 初始化账号池
function initializeAccountPool() {
    console.log('[Main] ========== 初始化账号池 ==========');
    
    // 初始化账号池（可以从localStorage或其他地方加载）
    window.accountPool = [];
    
    console.log('[Main] 账号池已初始化为空:', window.accountPool);
}

const backend = new TaskflowBackend(ws);
// 账号由getCurrentTabAccount()在运行时从当前tab获取，不在加载时预设

// 同步tabs引用到window，供TaskflowBackend等通过window.tabs读取
window.tabs = tabs;

window.taskflow = { backend };

const controller = new WorkflowController(() => currentTab?.graph);
window.workflowController = controller;

// ===== 聚焦/置顶功能：点击悬浮窗时提高显示优先级 =====
let _focusZ = 10001;
document.addEventListener("mousedown", (e) => {
  const panel = e.target.closest("#node-panel, #control-panel, #runtime-panel, #log-panel");
  if (panel) {
    _focusZ++;
    panel.style.zIndex = _focusZ;
  }
}, true); // 使用 capture 确保不被子元素 stopPropagation 阻断

try {
        console.log("[Main] 开始加载节点库...");
        // 逐个加载以定位错误
        const _nodeFiles = [
            "action/click.js","action/dmm_login.js","action/click_text.js","action/click_until_gone.js",
            "action/match_image.js","action/url.js","action/url_goto.js","action/click_image.js",
            "flow/sub_workflow.js","flow/sleep.js","flow/cond_goto.js","flow/start.js","flow/delay.js",
            "flow/counter.js","flow/relay.js","flow/variable.js","flow/label.js","flow/update_frame.js",
            "flow/confirm.js","flow/bool_event.js","flow/compare.js","flow/goto.js","flow/wait_image.js",
            "flow/logic_gates.js","flow/math_op.js","flow/end.js",
            "mnsg/scene_detect.js","test/test_error.js",
        ];
        const _nodeBase = "../nodes/";
        for (const _f of _nodeFiles) {
            try {
                await import(_nodeBase + _f + "?t=" + Date.now());
            } catch (e) {
                console.error("[Main] 文件加载失败: " + _f, e);
                throw e;
            }
        }
        console.log("[Main] 节点库加载完成");
        
        // 等待一小段时间确保节点完全注册
        await new Promise(resolve => setTimeout(resolve, 100));
        
        console.log("[Main] 开始初始化面板...");
        initNodePanel();
        initControlPanel();
        initRuntimePanel();
        console.log("[Main] 面板初始化完成");
        
        // 初始化全局TaskFlow
        setTimeout(() => {
            console.log('[Main] 初始化全局TaskFlow');
            initializeGlobalTaskFlow();
        }, 100);

        // 验证节点注册状态
        setTimeout(() => {
            console.log("[Main] 验证节点注册状态...");
            const nodeTypes = LiteGraph.registered_node_types;
            console.log("[Main] 已注册的节点类型:", Object.keys(nodeTypes));
            console.log("[Main] 节点总数:", Object.keys(nodeTypes).length);
            
            // 重新初始化node-panel以确保显示
            if (Object.keys(nodeTypes).length > 0) {
                console.log("[Main] 重新初始化node-panel...");
                initNodePanel();
            }
        }, 500);
        
    } catch (error) {
        console.error("[Main] 节点加载失败:", error);
        console.error("[Main] 错误堆栈:", error.stack);
        // 显示错误信息给用户
        document.body.innerHTML += `
            <div style="position: fixed; top: 10px; right: 10px; background: #ff4444; color: white; padding: 10px; border-radius: 5px; z-index: 9999;">
                节点库加载失败: ${error.message}<br>
                <span style="font-size:12px">${error.stack?.split('\\n').slice(0,4).join('<br>') || ''}</span>
            </div>
        `;
    }

// 将 currentTab 挂载到 window 以便调试
// 注意: currentTab 和 setCurrentTab 已在文件顶部的第一个 import 中导入
window.getCurrentTab = () => currentTab;
window.setCurrentTab = setCurrentTab;

// 节点高亮/取消高亮方法
window.highlightNode = (node) => {
  if (!node) return;
  const canvas = window.getCurrentTab()?.graphCanvas;
  if (canvas) {
    canvas.highlight_node_id = node.id;
    canvas.setDirty(true, true);
    console.log(`[highlightNode] 高亮节点: ${node.title} (id: ${node.id})`);
  }
};

window.unhighlightNode = (node) => {
  if (!node) return;
  const canvas = window.getCurrentTab()?.graphCanvas;
  if (canvas) {
    canvas.highlight_node_id = null;
    canvas.setDirty(true, true);
    console.log(`[unhighlightNode] 取消高亮节点: ${node.title} (id: ${node.id})`);
  }
};

// 连接线高亮/取消高亮方法
window.highlightLink = (link) => {
  if (!link) return;
  const canvas = window.getCurrentTab()?.graphCanvas;
  if (canvas) {
    canvas.highlight_link_id = link.id;
    canvas.setDirty(true, true);
    console.log(`[highlightLink] 高亮连接线: ${link.id}`);
  }
};

window.highlightErrorNode = (node) => {
  if (!node) return;
  const canvas = window.getCurrentTab()?.graphCanvas;
  if (canvas) {
    canvas.highlight_error_node_id = node.id;
    canvas.setDirty(true, true);
    console.log(`[highlightErrorNode] 高亮错误节点: ${node.title} (id: ${node.id})`);
  }
};

window.clearAllErrorHighlights = () => {
  const tab = window.currentTab;
  if (tab?.graphCanvas) {
    tab.graphCanvas.highlight_error_node_id = null;
    tab.graphCanvas.setDirty(true, true);
  }
  if (window.tabs) {
    window.tabs.forEach(t => {
      if (t.graphCanvas) {
        t.graphCanvas.highlight_error_node_id = null;
        t.graphCanvas.setDirty(true, true);
      }
    });
  }
};

// ===== 切换 Tab =====
export function switchTab(tabId) {

  if (draggedTab) {
    draggedTab.btn.classList.remove("dragging");
    setDraggedTab(null);
  }

  tabs.forEach(tab => {
    const active = tab.id === tabId;

    tab.canvas.classList.toggle("active", active);
    tab.btn.classList.toggle("active", active);

    if (active) {
      setCurrentTab(tab);
    }
  });

  // 同步到window，供TaskflowBackend等外部模块读取当前账号
  window.tabs = tabs;
  window.currentTab = currentTab;
}

// ===== 新建 Tab =====
newTabBtn.onclick = createTab;

// ===== 窗口变化 =====
window.addEventListener("resize", updateTabWidths);

// ===== UI 记忆：保存/恢复所有面板状态 =====
setTimeout(() => {
  const PREFIX = "wf_";
  const controls = {
    "toggle-sort-mode":    { key: "sortMode",    type: "checkbox" },
    "toggle-category-mode":{ key: "categoryMode",type: "checkbox" },
    "toggle-node-order":   { key: "nodeOrder",   type: "checkbox" },
    "select-link-style":   { key: "linkStyle",   type: "value" },
    "node-delay-input":    { key: "nodeDelay",   type: "value" },
  };

  // 恢复
  for (const [id, cfg] of Object.entries(controls)) {
    const el = document.getElementById(id);
    if (!el) continue;
    const saved = localStorage.getItem(PREFIX + cfg.key);
    if (saved === null) continue;
    if (cfg.type === "checkbox") el.checked = saved === "true";
    else el.value = saved;
  }

  // 恢复面板折叠状态
  for (const panelId of ["node-panel", "control-panel", "runtime-panel"]) {
    const saved = localStorage.getItem(PREFIX + panelId + "_collapsed");
    if (saved === "true") {
      const el = document.getElementById(panelId);
      if (el) {
        el.classList.add("collapsed");
        // 折叠按钮文字同步
        const toggle = el.querySelector("#" + panelId.replace("-panel", "-toggle"));
        if (toggle) toggle.textContent = "+";
      }
    }
  }

  // 保存控件变化
  function saveHandler(e) {
    const el = e.target;
    const cfg = controls[el.id];
    if (cfg) {
      const val = cfg.type === "checkbox" ? String(el.checked) : el.value;
      localStorage.setItem(PREFIX + cfg.key, val);
    }
  }
  document.addEventListener("change", saveHandler);

  // 面板折叠状态保存（委托 click）
  document.addEventListener("click", (e) => {
    const toggle = e.target.closest("#panel-toggle, #control-toggle, #runtime-toggle");
    if (!toggle) return;
    const panel = toggle.closest("[id$='-panel']");
    if (!panel) return;
    // 点击后等一帧让 class 变化完成再保存
    requestAnimationFrame(() => {
      localStorage.setItem(PREFIX + panel.id + "_collapsed", String(panel.classList.contains("collapsed")));
    });
  });
}, 0);