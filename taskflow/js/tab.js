/**
 * Fix tab.js: use dynamic getTabBar() instead of cached import reference.
 * The cached tabBar from state.js can become stale if the DOM element is replaced.
 */
import {tabs, canvasContainer, setCurrentTab, currentTab} from "./state.js";
import { setupDrag } from "./drag.js";
import { updateTabWidths } from "./layout.js";
import { switchTab } from "./main.js";

function getTabBar() {
  return document.getElementById("tab-bar");
}

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
  insertTabButton(btn, tabId);

  // ===== canvas =====
  const canvas = document.createElement("canvas");
  canvas.id = tabId;

  canvasContainer.appendChild(canvas);
  resizeCanvas(canvas);
  setTimeout(() => resizeCanvas(canvas), 0);

  const graph = new LGraph();
  const graphCanvas = new LGraphCanvas(canvas, graph);
  graphCanvas.render_connection_arrows = false; // 关闭连线上的箭头
  graphCanvas.render_link_tooltip = false;      // 关闭连线中点的悬停圆点（点击连接线菜单入口）
  graphCanvas.multi_select = false;              // 默认关闭多选，按住Ctrl进入多选模式
  graphCanvas.drag_mode = true;                 // 允许多选后整体拖拽

  // 全局关闭 widget 弹出式菜单/编辑器，所有修改走属性编辑器（双击节点）
  LiteGraph.disable_combo_menu = true;          // combo 下拉菜单
  LiteGraph.disable_inline_editor = true;       // string/text 内联编辑

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

  // 双击节点标题 → 属性编辑器（内附标题编辑）
  graphCanvas.onNodeDblClicked = null;

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

/**
 * Insert a tab button into the tab bar before the "+" button.
 * Uses live DOM lookup to avoid stale reference issues.
 */
function insertTabButton(btn, tabId) {
  const tabBar = getTabBar();
  if (!tabBar) {
    console.warn('[Tab] tabBar未找到，延迟插入');
    setTimeout(() => {
      const retryTabBar = document.getElementById("tab-bar");
      if (retryTabBar) {
        insertIntoBar(retryTabBar, btn);
      }
    }, 100);
    return;
  }

  const newTabBtn = document.getElementById("new-tab-btn");

  if (newTabBtn && tabBar.contains(newTabBtn)) {
    // +号按钮存在且在tabBar中，直接插入前面
    tabBar.insertBefore(btn, newTabBtn);
    console.log('[Tab] 插入到+号按钮前');
  } else {
    // +号按钮不存在或不在当前tabBar中，重建
    if (!newTabBtn) {
      const newBtn = document.createElement("button");
      newBtn.id = "new-tab-btn";
      newBtn.textContent = "+";
      newBtn.className = "new-tab-btn";
      newBtn.onclick = () => { showTabCreationDropdown(); };
      tabBar.appendChild(newBtn);
      console.log('[Tab] 重新创建+号按钮');
    }

    // 检查tabBar是否在DOM中
    if (!document.body.contains(tabBar)) {
      console.warn('[Tab] tabBar已脱离DOM，使用实时查找');
      const liveBar = document.getElementById("tab-bar");
      if (liveBar) {
        ensureNewBtnExists(liveBar);
        liveBar.insertBefore(btn, document.getElementById("new-tab-btn"));
        return;
      }
    }

    const retryNewTabBtn = document.getElementById("new-tab-btn");
    if (retryNewTabBtn && tabBar.contains(retryNewTabBtn)) {
      tabBar.insertBefore(btn, retryNewTabBtn);
      console.log('[Tab] 插入到重新创建的+号按钮前');
    } else if (retryNewTabBtn) {
      // retryNewTabBtn存在但不在tabBar中 → tabBar引用可能已失效
      console.warn('[Tab] +号按钮不在tabBar中，尝试实时查找tabBar');
      const liveBar = document.getElementById("tab-bar");
      if (liveBar && liveBar !== tabBar) {
        liveBar.insertBefore(btn, retryNewTabBtn);
        console.log('[Tab] 使用实时tabBar插入成功');
      } else {
        tabBar.appendChild(btn);
        console.log('[Tab] 添加到末尾');
      }
    } else {
      tabBar.appendChild(btn);
      console.log('[Tab] 添加到末尾');
    }
  }
}

function ensureNewBtnExists(bar) {
  if (!document.getElementById("new-tab-btn")) {
    const newBtn = document.createElement("button");
    newBtn.id = "new-tab-btn";
    newBtn.textContent = "+";
    newBtn.className = "new-tab-btn";
    newBtn.onclick = () => showTabCreationDropdown();
    bar.appendChild(newBtn);
  }
}

/** Insert into bar before + button (helper for delayed insertion). */
function insertIntoBar(bar, btn) {
  ensureNewBtnExists(bar);
  const newBtn = document.getElementById("new-tab-btn");
  if (newBtn && bar.contains(newBtn)) {
    bar.insertBefore(btn, newBtn);
  } else {
    bar.appendChild(btn);
  }
  console.log('[Tab] 延迟插入成功');
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

  // 同步 LiteGraph 的 bgcanvas
  const tab = tabs.find(t => t.canvas === canvas);
  if (tab?.graphCanvas) {
    const gc = tab.graphCanvas;
    if (gc.bgcanvas && gc.bgcanvas !== canvas) {
      gc.bgcanvas.width = canvas.width;
      gc.bgcanvas.height = canvas.height;
      gc.bgcanvas.style.width = canvas.style.width;
      gc.bgcanvas.style.height = canvas.style.height;
    }
    gc.setDirty(true, true);
  }
}

// 窗口缩放时自适应所有 canvas
window.addEventListener("resize", () => {
  tabs.forEach(tab => {
    if (tab.canvas) resizeCanvas(tab.canvas);
  });
});

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

        // 点击其他地方关闭下拉菜单（once 确保一次后自动移除）
        setTimeout(() => {
            document.addEventListener('click', hideTabCreationDropdown, { once: true });
        }, 50);
    }
}

// 隐藏tab创建下拉菜单
function hideTabCreationDropdown() {
    const dropdown = document.getElementById("tab-creation-dropdown");
    if (dropdown) {
        dropdown.remove();
    }
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
