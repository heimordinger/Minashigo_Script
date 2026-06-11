// taskflow/core/control-panel.js
import { currentTab, tabs } from "../js/state.js";
import { showWorkflowBrowser } from "./workflow-browser.js";

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

let _initialized = false;
export function initControlPanel() {
  if (_initialized) { console.warn("[ControlPanel] 重复调用 initControlPanel，跳过"); return; }
  _initialized = true;
  const saveBtn = document.getElementById("btn-save");
  const loadBtn = document.getElementById("btn-load");
  const clearBtn = document.getElementById("btn-clear");
  const fileInput = document.getElementById("file-input");
  const orderToggle = document.getElementById("toggle-node-order");
  const linkStyleSelect = document.getElementById("select-link-style");
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

  function applyLinkStyleState(mode) {
    tabs.forEach(tab => {
      const canvas = tab.graphCanvas;
      if (!canvas) return;
      canvas.links_render_mode = mode;
      canvas.setDirty(true, true);
      canvas.draw(true, true);
    });
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

    showWorkflowBrowser({
      mode: "save",
      jsonContent: jsonStr,
      onSave: async (name) => {
        try {
          const resp = await fetch("/api/save_workflow", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name, content: jsonStr }),
          });
          const result = await resp.json();
          if (result.success) {
            showToast(`已保存: ${name}.json`, "success");
            return true;
          }
          showToast(`保存失败: ${result.error}`, "error");
          return false;
        } catch (e) {
          showToast(`保存失败: ${e.message}`, "error");
          return false;
        }
      }
    });
  };

  loadBtn.onclick = () => {
    showWorkflowBrowser({
      mode: "load",
      title: "选择工作流",
      onSelect: async (fullPath, mergeMode) => {
        try {
          const loadResp = await fetch(`/api/load_workflow?name=${encodeURIComponent(fullPath)}`);
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

          const graph = tab.graph;
          const graphData = JSON.parse(loadResult.content);

          if (mergeMode) {
            // 添加模式：保留现有节点，新节点追加到右侧
            const currentData = graph.serialize();
            if (currentData.nodes) {
              // ---- 1. 偏移新节点 ID ----
              let maxNodeId = Math.max(0, ...currentData.nodes.map(n => n.id)) + 1;
              const nodeIdMap = new Map();
              // 计算原任务流最下方位置（用实际节点高度，包括图片撑大的部分）
              const bottomY = Math.max(0, ...currentData.nodes.map(n => {
                const y = n.pos ? (typeof n.pos[1] === 'number' ? n.pos[1] : (n.pos['1'] || 0)) : 0;
                let h = n.size ? (typeof n.size[1] === 'number' ? n.size[1] : (n.size['1'] || 80)) : 80;
                // 有些节点的实际高度 > size[1]，多加 40px 容差
                if (n.type && (n.type.includes("match_image") || n.type.includes("click_image"))) {
                  h += 200;
                }
                return y + h;
              })) + 150;
              for (const n of graphData.nodes) {
                nodeIdMap.set(n.id, maxNodeId);
                n.id = maxNodeId++;
                if (n.pos) {
                  let x = typeof n.pos[0] === 'number' ? n.pos[0] : (n.pos['0'] || 0);
                  let y = typeof n.pos[1] === 'number' ? n.pos[1] : (n.pos['1'] || 0);
                  n.pos = [x, y + bottomY];
                }
              }
              // ---- 2. 偏移新 link ID + 记录映射 ----
              const linkIdMap = new Map();
              if (graphData.links) {
                let maxLinkId = Math.max(0, ...(currentData.links || []).map(l => Array.isArray(l) ? l[0] : l.id)) + 1;
                for (const link of graphData.links) {
                  const oldId = Array.isArray(link) ? link[0] : link.id;
                  linkIdMap.set(oldId, maxLinkId);
                  if (Array.isArray(link)) {
                    link[0] = maxLinkId;
                    // 更新 node 引用
                    link[1] = nodeIdMap.get(link[1]) ?? link[1];
                    link[3] = nodeIdMap.get(link[3]) ?? link[3];
                  } else {
                    link.id = maxLinkId;
                    link.origin_id = nodeIdMap.get(link.origin_id) ?? link.origin_id;
                    link.target_id = nodeIdMap.get(link.target_id) ?? link.target_id;
                  }
                  maxLinkId++;
                }
              }
              // ---- 3. 更新节点内 inputs/outputs 的 link 引用 ----
              for (const n of graphData.nodes) {
                if (n.inputs) {
                  for (const inp of n.inputs) {
                    if (inp.link != null) inp.link = linkIdMap.get(inp.link) ?? inp.link;
                  }
                }
                if (n.outputs) {
                  for (const out of n.outputs) {
                    if (out.links) {
                      out.links = out.links.map(l => linkIdMap.get(l) ?? l);
                    }
                  }
                }
              }
              // ---- 4. 更新 last_link_id 避免新连线冲突 ----
              const allLinks = [...(currentData.links || []), ...(graphData.links || [])];
              const maxLinkId = allLinks.reduce((m, l) => Math.max(m, Array.isArray(l) ? l[0] : l.id), 0);
              graphData.last_link_id = maxLinkId + 1;
              // ---- 5. 合并数据 ----
              graphData.nodes = [...currentData.nodes, ...graphData.nodes];
              graphData.links = allLinks;
            }
          } else {
            graph.clear();
          }
          graph.configure(graphData);

          console.log(`[Load] ${mergeMode ? "添加" : "替换"}: ${fullPath}, 节点数: ${graph._nodes.length}`);
          tab.graphCanvas.render_execution_order = showExecutionOrder;
          tab.graphCanvas.setDirty(true, true);
          showToast(`${mergeMode ? "添加" : "加载"}: ${fullPath}`, "success");
        } catch (e) {
          showToast(`加载失败: ${e.message}`, "error");
        }
      }
    });
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

  // ===== 组管理 =====
  function getGraph() { return currentTab?.graph; }
  function getCanvas() { return currentTab?.graphCanvas; }

  const groupBtn = document.getElementById("btn-add-group");
  if (groupBtn) {
    groupBtn.disabled = true;
    groupBtn.style.opacity = "0.4";
    groupBtn.title = "Ctrl+拖拽选中节点后启用";

    // 劫持 canvas 的 onSelectionChange 来同步按钮状态
    let _origOnSelChange = null;
    function syncGroupBtn() {
      const canvas = getCanvas();
      const disabled = !canvas || !canvas.selected_nodes || !Object.keys(canvas.selected_nodes).length;
      groupBtn.disabled = disabled;
      groupBtn.style.opacity = disabled ? "0.4" : "1";
    }
    // 每帧检查（因为 LiteGraph 的选中变化没有 DOM 事件）
    let _checking = false;
    function startCheck() {
      if (_checking) return;
      _checking = true;
      function check() {
        syncGroupBtn();
        if (_checking) requestAnimationFrame(check);
      }
      requestAnimationFrame(check);
    }
    startCheck();

    groupBtn.onclick = () => {
      const graph = getGraph();
      const canvas = getCanvas();
      if (!graph) return;

      let title = prompt("输入组名称:", "新组");
      if (!title) return;

      // 为组分配不同颜色
      function darken(hex, amt) {
        let c = parseInt(hex.slice(1), 16);
        let r = Math.max(0, (c >> 16) - amt);
        let g = Math.max(0, ((c >> 8) & 0xff) - amt);
        let b = Math.max(0, (c & 0xff) - amt);
        return `#${(r << 16 | g << 8 | b).toString(16).padStart(6, '0')}`;
      }
      const groupColors = [
        "#3f51b5", "#e53935", "#43a047", "#fb8c00", "#8e24aa",
        "#00acc1", "#c0ca33", "#f4511e", "#3949ab", "#6d4c41"
      ];
      const colorIdx = graph._groups.length % groupColors.length;
      const group = new LiteGraph.LGraphGroup(title);
      group.graph = graph;
      group.color = groupColors[colorIdx];
      group._darkColor = darken(group.color, 40);
      group.font_size = 20;

      // 如果有选中的节点，包围它们
      const selNodes = canvas?.selected_nodes;
      if (selNodes && Object.keys(selNodes).length > 0) {
        const nodes = Object.values(selNodes);
        let minX = Infinity, minY = Infinity, maxX = 0, maxY = 0;
        for (const n of nodes) {
          if (n.pos[0] < minX) minX = n.pos[0];
          if (n.pos[1] < minY) minY = n.pos[1];
          if (n.pos[0] + n.size[0] > maxX) maxX = n.pos[0] + n.size[0];
          if (n.pos[1] + n.size[1] > maxY) maxY = n.pos[1] + n.size[1];
        }
        group._pos[0] = minX - 30;
        group._pos[1] = minY - 40;
        group._size[0] = maxX - minX + 60;
        group._size[1] = maxY - minY + 60;
      } else {
        group._pos[0] = 100;
        group._pos[1] = 100;
        group._size[0] = 200;
        group._size[1] = 150;
      }
      group.recomputeInsideNodes();
      graph._groups.push(group);
      showToast(`已创建组: ${title}`, "success");
      canvas?.setDirty(true, true);
    };
  }

  // 节点之上绘制气泡（屏幕空间，缩放平移不影响大小和位置）
  (function patchFrontDraw() {
    const proto = LiteGraph.LGraphCanvas.prototype;
    if (proto._bubblePatched) return;
    proto._bubblePatched = true;
    const orig = proto.drawFrontCanvas || function(){};
    proto.drawFrontCanvas = function() {
      try { orig.call(this); } catch (e) { console.warn("[Bubble] drawFrontCanvas error:", e); }
      if (!this.graph?._groups) return;
      const hasBubble = this.graph._groups.some(g => g._bubble);
      if (!hasBubble) return;
      const ctx = this.canvas?.getContext("2d");
      if (!ctx) return;
      const ds = this.ds;
      ctx.save();
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.restore(); // 清理可能残留的状态
      ctx.save();
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      for (const group of this.graph._groups) {
        if (!group._bubble) continue;
        const sx = group._pos[0] * ds.scale + ds.offsetX;
        const sy = group._pos[1] * ds.scale + ds.offsetY;
        const sw = group._size[0] * ds.scale;
        const label = group.title || "组";
        const fontSize = 16;
        ctx.font = `bold ${fontSize}px sans-serif`;
        const textW = ctx.measureText(label).width;
        const pad = 8;
        const bgW = textW + pad * 2;
        const bgH = fontSize + pad;
        const bgX = sx + sw / 2 - bgW / 2;
        const bgY = sy - bgH / 2;
        ctx.globalAlpha = 0.92;
        ctx.fillStyle = group._darkColor || group.color || "#335";
        ctx.beginPath();
        (ctx.roundRect ? ctx.roundRect(bgX, bgY, bgW, bgH, 5) : ctx.rect(bgX, bgY, bgW, bgH));
        ctx.fill();
        ctx.globalAlpha = 1;
        ctx.fillStyle = "#fff";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(label, bgX + bgW / 2, bgY + bgH / 2);
      }
      ctx.restore();
    };
  })();

  // ===== 组气泡提醒 =====
  const GroupProto = LiteGraph.LGraphGroup.prototype;
  if (!GroupProto._bubbleRegistered) {
    GroupProto._bubbleRegistered = true;

    Object.defineProperty(GroupProto, "bubble", {
      get() { return this._bubble || false; },
      set(v) { this._bubble = !!v; }
    });

    const origSer = GroupProto.serialize;
    GroupProto.serialize = function() {
      const data = origSer.call(this);
      data.bubble = this._bubble || false;
      return data;
    };
    const origCfg = GroupProto.configure;
    GroupProto.configure = function(o) {
      origCfg.call(this, o);
      this._bubble = o.bubble || false;
    };
  }

  // 增强组绘制：右上角气泡标记
  const CanvasProto = LiteGraph.LGraphCanvas.prototype;
  if (!CanvasProto._drawGroupsEnhanced) {
    CanvasProto._drawGroupsEnhanced = true;
    CanvasProto.drawGroups = function(canvas, ctx) {
      if (!this.graph) return;
      const groups = this.graph._groups;
      if (!groups) return;
      ctx.save();
      ctx.globalAlpha = 0.5 * this.editor_alpha;

      for (const group of groups) {
        if (this.visible_area && group._bounding) {
          const va = this.visible_area, b = group._bounding;
          if (b[0] + b[2] < va[0] || b[0] > va[0] + va[2] || b[1] + b[3] < va[1] || b[1] > va[1] + va[3]) continue;
        }

        const gx = group._pos[0], gy = group._pos[1], gw = group._size[0], gh = group._size[1];

        ctx.fillStyle = group.color || "#335";
        ctx.strokeStyle = group.color || "#335";
        ctx.globalAlpha = 0.25 * this.editor_alpha;
        ctx.beginPath();
        ctx.rect(gx + 0.5, gy + 0.5, gw, gh);
        ctx.fill();
        ctx.globalAlpha = this.editor_alpha;
        ctx.stroke();

        ctx.beginPath();
        ctx.moveTo(gx + gw, gy + gh);
        ctx.lineTo(gx + gw - 10, gy + gh);
        ctx.lineTo(gx + gw, gy + gh - 10);
        ctx.fill();

        if (group._bubble) {
          // 气泡由 onDrawOverlay 绘制（节点之上）
        } else {
          const font_size = group.font_size || 24;
          ctx.font = font_size + "px Arial";
          ctx.textAlign = "left";
          ctx.fillText(group.title, gx + 4, gy + font_size);
        }
      }
      ctx.restore();
    };
  }

  // 气泡开关
  const bubbleBtn = document.getElementById("btn-group-bubble");
  if (bubbleBtn) {
    function updateBubbleBtn() {
      const graph = getGraph();
      if (!graph || !graph._groups.length) { bubbleBtn.style.display = "none"; return; }
      bubbleBtn.style.display = "inline";
      const anyOn = graph._groups.some(g => g._bubble);
      bubbleBtn.textContent = anyOn ? "🔕 关气泡" : "🔔 开气泡";
    }

    bubbleBtn.onclick = () => {
      const graph = getGraph();
      if (!graph) return;
      const anyOn = graph._groups.some(g => g._bubble);
      for (const g of graph._groups) g._bubble = !anyOn;
      updateBubbleBtn();
      getCanvas()?.setDirty(true, true);
      showToast(anyOn ? "已关闭所有组气泡" : "已开启所有组气泡", "success");
    };

    // 组变化时刷新按钮
    const origGroupBtn = groupBtn?.onclick;
    if (origGroupBtn) {
      groupBtn.onclick = function() {
        origGroupBtn.call(this);
        setTimeout(updateBubbleBtn, 0);
      };
    }
    setTimeout(updateBubbleBtn, 500);
  }

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

    const ok = await ctrl.start();
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

  // 节点顺序开关 — 拦截 label 默认行为防止浏览器双重触发
  const orderLabel = orderToggle?.closest("label");
  orderLabel?.addEventListener("click", e => {
    e.preventDefault();
    const newState = !orderToggle.checked;
    orderToggle.checked = newState;
    updateExecutionOrder(newState);
  });

  // 连线样式下拉
  // LiteGraph 命名与字面相反：STRAIGHT_LINK=直角折线，LINEAR_LINK=接近直线
  const LINK_STYLE_MAP = {
    spline: LiteGraph.SPLINE_LINK,
    straight: LiteGraph.LINEAR_LINK,
    linear: LiteGraph.STRAIGHT_LINK,
  };
  const LINK_STYLE_LABEL = {
    spline: "曲线",
    straight: "直线",
    linear: "直角线",
  };
  linkStyleSelect?.addEventListener("change", e => {
    const mode = LINK_STYLE_MAP[e.target.value];
    if (mode == null) return;
    applyLinkStyleState(mode);
    showToast(`连线样式: ${LINK_STYLE_LABEL[e.target.value]}`, "info");
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
