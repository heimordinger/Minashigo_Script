// taskflow/core/control-panel.js
import { currentTab, tabs } from "../js/state.js";

const controlPanel = document.getElementById("control-panel");
const controlDrag = document.getElementById("control-drag");
const controlToggle = document.getElementById("control-toggle");

let offsetX = 0;
let offsetY = 0;
let dragging = false;

function ensureToastContainer() {
  let toastContainer = document.getElementById("toast-container");
  if (!toastContainer) {
    toastContainer = document.createElement("div");
    toastContainer.id = "toast-container";
    document.body.appendChild(toastContainer);
  }
  return toastContainer;
}

function applyExecutionOrderState(visible) {
  tabs.forEach(tab => {
    const canvas = tab.graphCanvas;
    if (!canvas) return;

    canvas.render_execution_order = visible;
    canvas.setDirty(true, true);
    canvas.draw(true, true);
  });
}

export function initControlPanel() {
  const saveBtn = document.getElementById("btn-save");
  const loadBtn = document.getElementById("btn-load");
  const clearBtn = document.getElementById("btn-clear");
  const fileInput = document.getElementById("file-input");
  const orderToggle = document.getElementById("toggle-node-order");
  const nodeDelayInput = document.getElementById("node-delay-input");

  const btnStart = document.getElementById("btn-start");
  const btnPause = document.getElementById("btn-pause");
  const btnResume = document.getElementById("btn-resume");
  const btnStop = document.getElementById("btn-stop");

  let showExecutionOrder = false;
  let currentMode = "select";
  let nodeDelay = 100; // 节点延迟（毫秒）

  const modeItems = document.querySelectorAll(".mode-item");

  window.showToast = showToast;
  window.getCurrentMode = () => currentMode;
  window.isExecutionOrderVisible = () => showExecutionOrder;
  window.getNodeDelay = () => nodeDelay;

  function showToast(text, type = "info") {
    const container = ensureToastContainer();
    const toast = document.createElement("div");

    toast.className = `toast ${type}`;
    toast.innerText = text;

    container.appendChild(toast);

    requestAnimationFrame(() => {
      toast.classList.add("show");
    });

    setTimeout(() => {
      toast.classList.remove("show");
      toast.classList.add("hide");

      setTimeout(() => {
        toast.remove();
      }, 300);
    }, 1500);
  }

  function setMode(mode) {
    currentMode = mode;

    modeItems.forEach(item => {
      item.classList.toggle("active", item.dataset.mode === mode);
    });

    const modeTextMap = {
      pan: "平移模式",
      select: "编辑模式",
      readonly: "只读模式"
    };

    showToast(`已切换：${modeTextMap[mode]}`, "info");

    window.dispatchEvent(new CustomEvent("modeChange", {
      detail: mode
    }));
  }

  function getController() {
    return currentTab?.controller || window.workflowController;
  }

  function updateOrderToggleUI() {
    if (!orderToggle) return;
    orderToggle.checked = showExecutionOrder;
  }

  function updateExecutionOrder(visible) {
    showExecutionOrder = visible;
    applyExecutionOrderState(showExecutionOrder);
    updateOrderToggleUI();

    showToast(
      showExecutionOrder ? "已开启节点顺序" : "已关闭节点顺序",
      "info"
    );
  }

  function updateControlUI(state) {
    btnStart.disabled = state === "running";
    btnPause.disabled = state !== "running";
    btnResume.disabled = state !== "paused";
    btnStop.disabled = state === "idle";
  }

  modeItems.forEach(item => {
    item.addEventListener("click", () => setMode(item.dataset.mode));
  });

  saveBtn.onclick = async () => {
    const tab = currentTab;
    if (!tab) return;

    const data = tab.graph.serialize();
    const jsonStr = JSON.stringify(data, null, 2);

    // 获取已有工作流列表用于展示
    let existingFiles = [];
    try {
      const resp = await fetch("/api/list_workflows");
      const result = await resp.json();
      if (result.success) existingFiles = result.files || [];
    } catch (_) {}

    // 创建保存对话框
    const picker = document.createElement("div");
    picker.id = "workflow-save-picker";
    picker.style.cssText =
      "position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);" +
      "background:#2a2a2a;border:1px solid #555;border-radius:8px;" +
      "padding:16px;z-index:10000;min-width:320px;max-height:460px;overflow-y:auto;" +
      "box-shadow:0 4px 20px rgba(0,0,0,0.5);";
    picker.innerHTML = `<h3 style="margin:0 0 12px;color:#eee;font-size:16px;">保存工作流</h3>`;

    const nameInput = document.createElement("input");
    nameInput.type = "text";
    nameInput.value = "workflow";
    nameInput.style.cssText =
      "width:calc(100% - 16px);padding:8px;margin-bottom:12px;border:1px solid #555;" +
      "border-radius:4px;background:#3a3a3a;color:#eee;font-size:14px;outline:none;";
    nameInput.placeholder = "输入文件名";
    picker.appendChild(nameInput);

    const hint = document.createElement("div");
    hint.style.cssText = "color:#888;font-size:12px;margin-bottom:8px;";
    hint.textContent = "点击下方已有文件可覆盖保存：";
    picker.appendChild(hint);

    const list = document.createElement("div");
    list.style.cssText = "max-height:200px;overflow-y:auto;margin-bottom:12px;";
    existingFiles.forEach(file => {
      const item = document.createElement("div");
      item.style.cssText =
        "padding:6px 10px;margin:3px 0;background:#3a3a3a;border-radius:4px;" +
        "cursor:pointer;color:#aaa;font-size:13px;transition:background .15s;";
      item.textContent = file;
      item.onmouseenter = () => { item.style.background = "#4a4a4a"; };
      item.onmouseleave = () => { item.style.background = "#3a3a3a"; };
      item.onclick = () => { nameInput.value = file.replace(/\.json$/, ""); };
      list.appendChild(item);
    });
    if (!existingFiles.length) {
      hint.textContent = "（暂无已保存的工作流）";
    }
    picker.appendChild(list);

    // 按钮行
    const btnRow = document.createElement("div");
    btnRow.style.cssText = "display:flex;gap:8px;justify-content:flex-end;";

    const cancelBtn = document.createElement("button");
    cancelBtn.textContent = "取消";
    cancelBtn.style.cssText =
      "padding:6px 16px;background:#555;color:#eee;border:none;border-radius:4px;cursor:pointer;";
    cancelBtn.onclick = () => picker.remove();
    btnRow.appendChild(cancelBtn);

    const saveBtn2 = document.createElement("button");
    saveBtn2.textContent = "保存";
    saveBtn2.style.cssText =
      "padding:6px 16px;background:#4a9eff;color:#fff;border:none;border-radius:4px;cursor:pointer;" +
      "font-weight:bold;";
    saveBtn2.onclick = async () => {
      const name = nameInput.value.trim();
      if (!name) { showToast("请输入文件名", "warn"); return; }
      try {
        const resp = await fetch("/api/save_workflow", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name, content: jsonStr }),
        });
        const result = await resp.json();
        if (result.success) {
          showToast(`已保存: ${name}.json`, "success");
        } else {
          showToast(`保存失败: ${result.error}`, "error");
        }
      } catch (e) {
        showToast(`保存失败: ${e.message}`, "error");
      }
      picker.remove();
    };
    btnRow.appendChild(saveBtn2);
    picker.appendChild(btnRow);

    document.body.appendChild(picker);
    nameInput.focus();
    nameInput.select();
  };

  loadBtn.onclick = async () => {
    // 获取可用工作流列表
    try {
      const resp = await fetch("/api/list_workflows");
      const result = await resp.json();
      const files = result.success ? (result.files || []) : [];

      // 动态创建加载选择器
      let picker = document.getElementById("workflow-picker");
      if (!picker) {
        picker = document.createElement("div");
        picker.id = "workflow-picker";
        picker.style.cssText =
          "position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);" +
          "background:#2a2a2a;border:1px solid #555;border-radius:8px;" +
          "padding:16px;z-index:10000;min-width:320px;max-height:460px;overflow-y:auto;" +
          "box-shadow:0 4px 20px rgba(0,0,0,0.5);";

        const headerRow = document.createElement("div");
        headerRow.style.cssText = "display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;";
        const title = document.createElement("h3");
        title.style.cssText = "margin:0;color:#eee;font-size:16px;";
        title.textContent = "选择工作流";
        headerRow.appendChild(title);

        const browseBtn = document.createElement("span");
        browseBtn.title = "从其他位置选择文件";
        browseBtn.textContent = "📂";
        browseBtn.style.cssText =
          "font-size:20px;cursor:pointer;padding:2px 6px;border-radius:4px;transition:background .15s;" +
          "line-height:1;";
        browseBtn.onmouseenter = () => { browseBtn.style.background = "#444"; };
        browseBtn.onmouseleave = () => { browseBtn.style.background = "transparent"; };
        browseBtn.onclick = (e) => {
          e.stopPropagation();
          fileInput.value = "";
          fileInput.click();
        };
        headerRow.appendChild(browseBtn);
        picker.appendChild(headerRow);

        const list = document.createElement("div");
        list.id = "wp-list";
        list.style.cssText = "max-height:300px;overflow-y:auto;margin-bottom:12px;";
        picker.appendChild(list);

        const closeBtn = document.createElement("button");
        closeBtn.textContent = "取消";
        closeBtn.style.cssText =
          "padding:6px 16px;background:#555;color:#eee;border:none;border-radius:4px;cursor:pointer;";
        closeBtn.onclick = () => picker.remove();
        picker.appendChild(closeBtn);
        document.body.appendChild(picker);
      } else {
        picker.style.display = "block";
      }

      const list = document.getElementById("wp-list");
      list.innerHTML = "";
      if (!files.length) {
        const empty = document.createElement("div");
        empty.style.cssText = "color:#888;font-size:13px;text-align:center;padding:20px 0;";
        empty.textContent = "script/ 目录为空，点击右上角 📂 选择其他文件";
        list.appendChild(empty);
      } else {
        files.forEach(file => {
          const item = document.createElement("div");
          item.style.cssText =
            "padding:8px 12px;margin:4px 0;background:#3a3a3a;border-radius:4px;" +
            "cursor:pointer;color:#ddd;transition:background .15s;";
          item.textContent = file;
          item.onmouseenter = () => { item.style.background = "#4a4a4a"; };
          item.onmouseleave = () => { item.style.background = "#3a3a3a"; };
          item.onclick = async () => {
            try {
              const loadResp = await fetch(`/api/load_workflow?name=${encodeURIComponent(file)}`);
              const loadResult = await loadResp.json();
              if (!loadResult.success) {
                showToast(`加载失败: ${loadResult.error}`, "error");
                return;
              }

              let tab = currentTab;
              if (!tab) {
                showToast("没有当前标签页，正在创建新标签页", "info");
                const { createTab } = await import("../js/tab.js");
                createTab();
                tab = currentTab;
                if (!tab) {
                  showToast("创建标签页失败", "error");
                  return;
                }
              }

              const graphData = JSON.parse(loadResult.content);
              const graph = tab.graph;
              graph.clear();
              graph.configure(graphData);

              console.log(`[Load] 加载完成: ${file}, 节点数: ${graph._nodes.length}`);
              tab.graphCanvas.render_execution_order = showExecutionOrder;
              tab.graphCanvas.setDirty(true, true);
              showToast(`已加载：${file}`, "success");
            } catch (e) {
              showToast(`加载失败: ${e.message}`, "error");
            }
            picker.remove();
          };
          list.appendChild(item);
        });
      }
    } catch (e) {
      showToast(`获取工作流列表失败: ${e.message}`, "error");
    }
  };

  // 文件选择器：加载任意位置的 .json 工作流
  fileInput.accept = ".json";
  fileInput.onchange = async e => {
    const file = e.target.files[0];
    if (!file) return;
    try {
      const text = await file.text();
      let tab = currentTab;
      if (!tab) {
        showToast("没有当前标签页，正在创建新标签页", "info");
        const { createTab } = await import("../js/tab.js");
        createTab();
        tab = currentTab;
        if (!tab) {
          showToast("创建标签页失败", "error");
          return;
        }
      }
      const graphData = JSON.parse(text);
      const graph = tab.graph;
      graph.clear();
      graph.configure(graphData);
      tab.graphCanvas.render_execution_order = showExecutionOrder;
      tab.graphCanvas.setDirty(true, true);
      showToast(`已加载：${file.name}`, "success");
    } catch (e) {
      showToast(`加载失败: ${e.message}`, "error");
    }
    // 关闭加载选择器（如果有）
    const picker = document.getElementById("workflow-picker");
    if (picker) picker.remove();
  };

  clearBtn.onclick = () => {
    const tab = currentTab;
    if (!tab) return;

    if (confirm("确定清空当前流程？")) {
      tab.graph.clear();
      showToast("已清空当前流程", "success");
    }
  };

  // 检查后端浏览器是否就绪
  async function checkBrowserReady() {
    const backend = window.taskflow?.backend;
    if (!backend) {
      return { ready: false, reason: "后端服务未连接" };
    }

    const ws = backend.wsManager?.ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      return { ready: false, reason: "与后端的WebSocket连接未建立" };
    }

    const account = backend.getCurrentTabAccount();
    if (!account || !account.email) {
      return { ready: false, reason: "当前标签页未绑定账号" };
    }

    // 快速检查浏览器状态（5秒超时）
    try {
      const response = await backend.invoke("account_operation", {
        operation: "get_status",
        parameters: {}
      }, 5000);

      const data = response?.data;
      if (data?.result?.has_browser) {
        return { ready: true, account };
      }
      return { ready: false, reason: `账号 "${account.name}" 的浏览器未启动，请先在主界面启动浏览器` };
    } catch {
      return { ready: false, reason: `账号 "${account.name}" 的浏览器未启动，请先在主界面启动浏览器` };
    }
  }

  // 显示模态警告弹窗
  function showWarningModal(message) {
    const existing = document.getElementById("warning-modal-overlay");
    if (existing) existing.remove();

    const overlay = document.createElement("div");
    overlay.id = "warning-modal-overlay";
    overlay.className = "warning-modal-overlay";

    overlay.innerHTML = `
      <div class="warning-modal">
        <div class="warning-modal-header">
          <span class="warning-icon">!</span>
          <span>警告</span>
        </div>
        <div class="warning-modal-body">${message}</div>
        <div class="warning-modal-footer">
          <button class="warning-modal-btn">确定</button>
        </div>
      </div>
    `;

    overlay.querySelector(".warning-modal-btn").onclick = () => overlay.remove();
    overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };
    document.body.appendChild(overlay);
  }

  btnStart.onclick = async () => {
    const check = await checkBrowserReady();
    if (!check.ready) {
      showWarningModal(check.reason);
      return;
    }

    const ctrl = getController();
    if (!ctrl) return;

    const ok = ctrl.start();
    if (!ok) return;

    showToast("开始运行", "success");
  };

  btnPause.onclick = () => {
    const ctrl = getController();
    if (!ctrl) return;

    ctrl.pause();
    showToast("已暂停", "info");
    updateControlUI(ctrl.state);
  };

  btnResume.onclick = () => {
    const ctrl = getController();
    if (!ctrl) return;

    ctrl.resume();
    showToast("继续运行", "info");
    updateControlUI(ctrl.state);
  };

  btnStop.onclick = () => {
    const ctrl = getController();
    if (!ctrl) return;

    ctrl.stop();
    showToast("已停止", "warning");
    updateControlUI(ctrl.state);
  };

  orderToggle?.addEventListener("change", e => {
    updateExecutionOrder(e.target.checked);
  });

  nodeDelayInput?.addEventListener("input", e => {
    nodeDelay = parseInt(e.target.value) || 0;
  });

  window.updateControlUI = updateControlUI;

  setMode(currentMode);
  updateControlUI("idle");
  updateOrderToggleUI();

  if (currentTab?.controller) {
    updateControlUI(currentTab.controller.state);
  }
}

controlDrag.addEventListener("mousedown", e => {
  dragging = true;
  const rect = controlPanel.getBoundingClientRect();
  offsetX = e.clientX - rect.left;
  offsetY = e.clientY - rect.top;
  controlPanel.style.transition = "none";
});

document.addEventListener("mousemove", e => {
  if (!dragging) return;

  controlPanel.style.left = `${e.clientX - offsetX}px`;
  controlPanel.style.top = `${e.clientY - offsetY}px`;
});

document.addEventListener("mouseup", () => {
  if (!dragging) return;

  dragging = false;
  controlPanel.style.transition =
    "transform 0.2s ease, height 0.25s ease, box-shadow 0.2s ease";
});

controlToggle.addEventListener("click", () => {
  const isCollapsed = controlPanel.classList.contains("collapsed");
  const COLLAPSED_H = 32;

  if (isCollapsed) {
    // === 展开动画 ===
    controlPanel.classList.remove("collapsed");
    const fullH = controlPanel.scrollHeight;
    controlPanel.classList.add("collapsed");
    controlPanel.style.height = COLLAPSED_H + "px";
    controlPanel.style.overflow = "hidden";
    void controlPanel.offsetHeight;
    controlPanel.classList.remove("collapsed");
    controlPanel.style.height = fullH + "px";
  } else {
    // === 折叠动画 ===
    const curH = controlPanel.offsetHeight;
    controlPanel.style.height = curH + "px";
    controlPanel.style.overflow = "hidden";
    void controlPanel.offsetHeight;
    controlPanel.classList.add("collapsed");
    controlPanel.style.height = COLLAPSED_H + "px";
  }

  const cleanup = () => {
    controlPanel.style.height = "";
    controlPanel.style.overflow = "";
  };
  controlPanel.addEventListener("transitionend", cleanup, { once: true });
  setTimeout(cleanup, 300);
});
