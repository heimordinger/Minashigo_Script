import {tabs, tabBar, newTabBtn, canvasContainer, setCurrentTab, currentTab} from "./state.js";
import { setupDrag } from "./drag.js";
import { updateTabWidths } from "./layout.js";
import { switchTab } from "./main.js";

export function createTab(customTitle = null) {

  // 检查是否已存在相同标题的tab
  if (customTitle) {
    const existingTab = tabs.find(tab => 
      tab.btn.querySelector('span')?.textContent === customTitle
    );
    if (existingTab) {
      console.log(`[Tab] 已存在相同标题的tab: ${customTitle}，跳过创建`);
      switchTab(existingTab.id);
      return existingTab;
    }
  }

  const tabId = "tab" + (tabs.length + 1);

  const btn = document.createElement("button");
  btn.className = "tab-btn";
  btn.dataset.tabId = tabId;
  btn.onclick = () => switchTab(tabId);

  const span = document.createElement("span");
  
  console.log(`[Tab] createTab called with customTitle:`, customTitle);
  
  // 如果有自定义标题，使用自定义标题，否则使用默认标题
  if (customTitle) {
    span.textContent = customTitle;
    console.log(`[Tab] Using custom title: ${customTitle}`);
  } else {
    span.textContent = "Tab " + (tabs.length + 1);
    console.log(`[Tab] Using default title: Tab ${tabs.length + 1}`);
  }
  
  btn.appendChild(span);

  // ===== 重命名 =====
  span.addEventListener("dblclick", (e) => {
    e.stopPropagation();

    const oldText = span.textContent;
    const input = document.createElement("input");

    input.value = oldText;
    input.style.width = "80%";

    btn.replaceChild(input, span);
    input.focus();

    input.onblur = () => {
      span.textContent = input.value || oldText;
      btn.replaceChild(span, input);
    };
  });

  // ===== 关闭 =====
  const closeBtn = document.createElement("button");
  closeBtn.className = "tab-close";
  closeBtn.textContent = "×";
  closeBtn.onclick = e => {
    e.stopPropagation();
    closeTab(tabId);
  };
  btn.appendChild(closeBtn);

  // 确保tab插入到+号按钮前面
if (tabBar) {
    // 重新获取newTabBtn确保它在DOM中
    const currentNewTabBtn = document.getElementById("new-tab-btn");
    
    if (currentNewTabBtn && tabBar.contains(currentNewTabBtn)) {
        // 确保newTabBtn可见且在正确位置
        currentNewTabBtn.style.display = 'inline-block';
        currentNewTabBtn.style.visibility = 'visible';
        
        // 插入到+号按钮前面
        tabBar.insertBefore(btn, currentNewTabBtn);
        console.log('[Tab] 使用insertBefore插入成功，+号按钮保持可见');
    } else {
        // 如果找不到newTabBtn，创建它并添加到tabBar
        if (!currentNewTabBtn) {
            const newBtn = document.createElement("button");
            newBtn.id = "new-tab-btn";
            newBtn.textContent = "+";
            newBtn.className = "new-tab-btn";
            newBtn.onclick = () => {
                showTabCreationDropdown();
            };
            tabBar.appendChild(newBtn);
            console.log('[Tab] 重新创建+号按钮');
        }
        
        // 然后插入tab按钮
        const retryNewTabBtn = document.getElementById("new-tab-btn");
        if (retryNewTabBtn) {
            tabBar.insertBefore(btn, retryNewTabBtn);
            console.log('[Tab] 插入到重新创建的+号按钮前');
        } else {
            tabBar.appendChild(btn);
            console.log('[Tab] 添加到末尾');
        }
    }
} else {
    console.warn('[Tab] tabBar未找到，延迟插入');
    setTimeout(() => {
        const retryTabBar = document.getElementById("tab-bar");
        if (retryTabBar) {
            // 确保tab-bar中有+号按钮
            let retryNewTabBtn = document.getElementById("new-tab-btn");
            if (!retryNewTabBtn) {
                const newBtn = document.createElement("button");
                newBtn.id = "new-tab-btn";
                newBtn.textContent = "+";
                newBtn.className = "new-tab-btn";
                newBtn.onclick = () => {
                    if (typeof createTab === 'function') {
                        createTab();
                    }
                };
                retryTabBar.appendChild(newBtn);
                retryNewTabBtn = newBtn;
                console.log('[Tab] 延迟重新创建+号按钮');
            }
            
            // 插入tab按钮
            retryTabBar.insertBefore(btn, retryNewTabBtn);
            console.log('[Tab] 延迟插入成功，+号按钮保持可见');
        }
    }, 100);
}

  // ===== canvas =====
  const canvas = document.createElement("canvas");
  canvas.id = tabId;

  canvasContainer.appendChild(canvas);
  resizeCanvas(canvas);
  setTimeout(() => resizeCanvas(canvas), 0);

  const graph = new LGraph();
  const graphCanvas = new LGraphCanvas(canvas, graph);

  // 节点顺序显示
  // graphCanvas.render_execution_order = true;

  graph.checkWorkflowStop = function(node, canvas) {

      let hasNext = false;
      for (let output of node.outputs) {
          if (output.links && output.links.length > 0) {
              hasNext = true;
              break;
          }
      }

      if (!hasNext) {
          console.log(`节点 ${node.title || node.id} 没有下一节点，停止执行`);
          if (canvas) canvas.stop();
          return true;
      }

      return false;
  };

  graphCanvas.onShowNodePanel = function(node) {
      console.log("已拦截打开节点面板:", node.title);
  };

  // graphCanvas.onNodeDblClicked = function(node) {
  //     console.log("双击事件:", node.title);
  // };

  // ===== 模式监听（核心）=====
  window.addEventListener("modeChange", e => {
    const mode = e.detail;

    switch (mode) {
      case "pan":
        graphCanvas.allow_dragcanvas = true;
        graphCanvas.allow_interaction = false;
        break;

      case "select":
        graphCanvas.allow_dragcanvas = true;
        graphCanvas.allow_interaction = true;
        break;

      case "readonly":
        graphCanvas.allow_dragcanvas = false;
        graphCanvas.allow_interaction = false;
        break;
    }
  });

  const mode = window.getCurrentMode?.() || "select";
  window.dispatchEvent(new CustomEvent("modeChange", { detail: mode }));

  // 绑定账号信息到tab
  const accountInfo = window.accountInfo || { name: 'default', email: 'default@example.com' };
  console.log(`[Tab] 绑定账号信息到tab ${tabId}:`, accountInfo);
  
  const tabObj = { 
    id: tabId, 
    btn, 
    canvas, 
    graph, 
    graphCanvas,
    account: accountInfo  // 绑定账号信息到tab
  };
  tabs.push(tabObj);

  setupDrag(btn, tabObj);
  updateTabWidths();
  switchTab(tabId);
  setTimeout(() => {
    resizeCanvas(canvas);
  }, 0);
}

export function closeTab(tabId) {
  const index = tabs.findIndex(tab => tab.id === tabId);
  if (index < 0) return;

  const tab = tabs[index];
  tab.graph?.clear?.();
  tab.canvas?.remove();
  tab.btn?.remove();
  tabs.splice(index, 1);

  const next = tabs[Math.max(0, index - 1)];
  if (next) {
    switchTab(next.id);
  } else {
    setCurrentTab(null);
  }

  updateTabWidths();
}

export function resizeCanvas(canvas) {
  const parent = canvas.parentElement;
  const rect = parent.getBoundingClientRect();

  const dpr = window.devicePixelRatio || 1;

  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;

  canvas.style.width = rect.width + "px";
  canvas.style.height = rect.height + "px";

  const ctx = canvas.getContext("2d");
  if (ctx) {
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
}

// 显示tab创建下拉菜单
export function showTabCreationDropdown() {
    console.log('[Tab] 显示tab创建下拉菜单');
    
    // 检查是否已有下拉菜单
    let dropdown = document.getElementById("tab-creation-dropdown");
    if (dropdown) {
        dropdown.remove();
    }
    
    // 创建下拉菜单
    dropdown = document.createElement("div");
    dropdown.id = "tab-creation-dropdown";
    dropdown.className = "tab-creation-dropdown";
    
    // 添加默认选项：新tab（空account）
    const defaultOption = document.createElement("div");
    defaultOption.className = "dropdown-item";
    defaultOption.textContent = "添加tab";
    defaultOption.onclick = () => {
        createTab();
        hideTabCreationDropdown();
    };
    dropdown.appendChild(defaultOption);

    // 添加账号选项（如果账号池非空）
    if (window.accountPool && window.accountPool.length > 0) {
        console.log('[Tab] 账号池非空，添加账号选项:', window.accountPool);

        // 添加分隔线
        const separator = document.createElement("div");
        separator.className = "dropdown-separator";
        dropdown.appendChild(separator);

        // 添加账号选项
        window.accountPool.forEach(account => {
            const accountOption = document.createElement("div");
            accountOption.className = "dropdown-item";
            accountOption.textContent = `${account.name} (${account.email})`;
            accountOption.onclick = () => {
                if (typeof window.createAccountTab === 'function') {
                    window.createAccountTab(account);
                } else {
                    createTabWithAccount(account);
                }
                hideTabCreationDropdown();
            };
            dropdown.appendChild(accountOption);
        });
    }
    
    // 定位下拉菜单
    const newTabBtn = document.getElementById("new-tab-btn");
    if (newTabBtn) {
        const rect = newTabBtn.getBoundingClientRect();
        dropdown.style.position = "absolute";
        dropdown.style.top = (rect.bottom + 2) + "px";
        dropdown.style.left = rect.left + "px";

        // 添加到页面
        document.body.appendChild(dropdown);
        
        // 点击其他地方关闭下拉菜单
        setTimeout(() => {
            document.addEventListener('click', hideTabCreationDropdown);
        }, 100);
    }
}

// 隐藏tab创建下拉菜单
function hideTabCreationDropdown() {
    const dropdown = document.getElementById("tab-creation-dropdown");
    if (dropdown) {
        dropdown.remove();
    }
    document.removeEventListener('click', hideTabCreationDropdown);
}

// 使用账号信息创建tab（从下拉菜单调用）
function createTabWithAccount(accountInfo) {
    console.log('[Tab] 使用账号信息创建tab:', accountInfo);
    
    // 设置当前账号信息
    window.currentAccount = accountInfo;
    window.accountInfo = accountInfo;
    
    // 使用原有的createTab函数创建tab
    if (typeof createTab === 'function') {
        // 使用账号名称作为tab标题
        createTab(accountInfo.name);
        console.log('[Tab] 已调用createTab函数，标题:', accountInfo.name);
    } else {
        console.warn('[Tab] createTab函数未找到');
    }
}

// 根据账号信息创建tab的独立函数（便于后续整合）
export function createTabFromAccount(accountInfo) {
    console.log('[Tab] 根据账号信息创建tab:', accountInfo);
    
    // 验证账号信息
    if (!accountInfo || !accountInfo.name) {
        console.warn('[Tab] 账号信息无效，使用默认值');
        accountInfo = {
            name: 'default',
            email: 'default@example.com'
        };
    }
    
    // 设置当前账号信息
    window.currentAccount = accountInfo;
    window.accountInfo = accountInfo;
    
    // 创建tab
    if (typeof createTab === 'function') {
        createTab(accountInfo.name);
        console.log('[Tab] 已创建账号tab，标题:', accountInfo.name);
        return true;
    } else {
        console.warn('[Tab] createTab函数未找到');
        return false;
    }
}
