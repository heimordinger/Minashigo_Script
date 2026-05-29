// taskflow/core/node-panel.js
import { currentTab } from "../js/state.js";

const panel = document.getElementById("node-panel");
const toggleBtn = document.getElementById("panel-toggle");

toggleBtn.addEventListener("click", () => {
  panel.classList.toggle("collapsed");
});

/* ---------- 频率统计 ---------- */

const FREQ_KEY = "taskflow_node_frequency";
let nodeFrequency = {};

function loadFrequency() {
  try {
    const raw = localStorage.getItem(FREQ_KEY);
    nodeFrequency = raw ? JSON.parse(raw) : {};
  } catch {
    nodeFrequency = {};
  }
}

function saveFrequency() {
  try {
    localStorage.setItem(FREQ_KEY, JSON.stringify(nodeFrequency));
  } catch { /* ignore */ }
}

function recordNodeUse(nodeType) {
  nodeFrequency[nodeType] = (nodeFrequency[nodeType] || 0) + 1;
  saveFrequency();
}

function getFrequency(type) {
  return nodeFrequency[type] || 0;
}

/* ---------- 状态 ---------- */

function getSortMode() {
  const el = document.getElementById("toggle-sort-mode");
  return el?.checked ? "name" : "frequency";
}

function getCatMode() {
  const el = document.getElementById("toggle-category-mode");
  return el?.checked ? "categorized" : "flat";
}

/* ---------- 渲染 ---------- */

export function initNodePanel() {
  loadFrequency();

  const panelBody = document.getElementById("panel-body");
  if (!panelBody) {
    console.error("[NodePanel] 找不到panel-body元素");
    return;
  }

  // 绑定toggle事件
  document.getElementById("toggle-sort-mode")?.addEventListener("change", render);
  document.getElementById("toggle-category-mode")?.addEventListener("change", render);

  // 延迟一帧等待DOM稳定
  setTimeout(render, 100);
}

function render() {
  const panelBody = document.getElementById("panel-body");
  if (!panelBody) return;

  const sortMode = getSortMode();
  const catMode = getCatMode();

  const nodeTypes = LiteGraph.registered_node_types;
  const myNodes = getMyNodes(nodeTypes);
  const entries = buildEntries(myNodes);

  // 排序
  sortEntries(entries, sortMode);

  panelBody.innerHTML = "";

  if (catMode === "categorized") {
    renderCategorized(panelBody, entries, sortMode);
  } else {
    renderFlat(panelBody, entries, sortMode);
  }
}

/* ---------- 构建节点列表 ---------- */

function buildEntries(nodes) {
  const result = [];
  for (const type in nodes) {
    const cls = nodes[type];
    result.push({
      type,
      title: cls.title || type.split("/").slice(1).join("/"),
      category: type.split("/")[0],
      freq: getFrequency(type),
    });
  }
  return result;
}

/* ---------- 排序 ---------- */

function sortEntries(entries, sortMode) {
  if (sortMode === "frequency") {
    entries.sort((a, b) => {
      if (b.freq !== a.freq) return b.freq - a.freq;
      return a.title.localeCompare(b.title);
    });
  } else {
    entries.sort((a, b) => a.title.localeCompare(b.title));
  }
}

/* ---------- 分类渲染 ---------- */

function renderCategorized(container, entries, sortMode) {
  const categories = collectCategories(entries);

  categories.forEach(cat => {
    const catEntries = entries.filter(e => e.category === cat);
    if (!catEntries.length) return;

    const wrap = document.createElement("div");
    wrap.className = "category";

    const title = document.createElement("div");
    title.className = "category-title";
    const displayName = formatCategoryName(cat);
    title.innerHTML = `<span>${displayName}</span><span>▾</span>`;
    title.addEventListener("click", () => {
      wrap.classList.toggle("collapsed");
      const arrow = title.querySelector("span:last-child");
      arrow.textContent = wrap.classList.contains("collapsed") ? "▸" : "▾";
    });

    const content = document.createElement("div");
    content.className = "category-content";

    catEntries.forEach(entry => {
      content.appendChild(createNodeButton(entry, sortMode));
    });

    wrap.appendChild(title);
    wrap.appendChild(content);
    container.appendChild(wrap);
  });
}

/* ---------- 不分类渲染 ---------- */

function renderFlat(container, entries, sortMode) {
  entries.forEach(entry => {
    container.appendChild(createNodeButton(entry, sortMode));
  });
}

/* ---------- 创建节点按钮 ---------- */

function createNodeButton(entry, sortMode) {
  const btn = document.createElement("button");
  btn.className = "node-button";

  if (sortMode === "frequency") {
    // 显示频率标记
    const freq = entry.freq;
    const freqBadge = freq > 0 ? ` <span class="node-freq-badge">${freq}</span>` : "";
    btn.innerHTML = `${entry.title}${freqBadge}`;
  } else {
    btn.textContent = entry.title;
  }

  btn.addEventListener("mousedown", (e) => {
    e.preventDefault();
    const tab = currentTab;
    if (!tab) return;

    const graph = tab.graph;
    const canvas = tab.graphCanvas;
    if (!graph || !canvas) return;

    const node = LiteGraph.createNode(entry.type);
    if (!node) return;

    const viewport = canvas.visible_area;
    const cx = viewport[0] + viewport[2] / 2;
    const cy = viewport[1] + viewport[3] / 2;
    node.pos = [cx - 50, cy - 25];

    graph.add(node);
    canvas.centerOnNode(node);

    // 记录频率并重新渲染
    recordNodeUse(entry.type);
    // 直接更新频率数字，避免全量重排闪烁
    const freqEl = btn.querySelector(".node-freq-badge");
    if (freqEl) {
      freqEl.textContent = getFrequency(entry.type);
    } else if (sortMode === "frequency") {
      btn.innerHTML = `${entry.title} <span class="node-freq-badge">${getFrequency(entry.type)}</span>`;
    }
    // 全量重渲染（确保排序更新）
    // 改为节流，避免频繁重绘
    scheduleRender();
  });

  return btn;
}

let _renderTimer = null;

function scheduleRender() {
  if (_renderTimer) return;
  _renderTimer = setTimeout(() => {
    _renderTimer = null;
    render();
  }, 500);
}

/* ---------- 辅助函数 ---------- */

function getMyNodes(nodeTypes) {
  return Object.fromEntries(
    Object.entries(nodeTypes).filter(([type]) =>
      type.startsWith("flow/") ||
      type.startsWith("action/") ||
      type.startsWith("mnsg/") ||
      type.startsWith("test/")
    )
  );
}

function collectCategories(entries) {
  const set = new Set(entries.map(e => e.category));
  const CATEGORY_ORDER = ["flow", "action", "mnsg", "test"];
  const result = [];
  for (const cat of CATEGORY_ORDER) {
    if (set.has(cat)) result.push(cat);
  }
  for (const cat of set) {
    if (!CATEGORY_ORDER.includes(cat)) result.push(cat);
  }
  return result;
}

const CATEGORY_NAME_MAP = {
  flow: "流程",
  action: "操作",
  mnsg: "MNSG",
  test: "测试",
};

function formatCategoryName(category) {
  return CATEGORY_NAME_MAP[category] || category;
}

/* ========== 面板拖拽 ========== */

(function initPanelDrag() {
  const panel = document.getElementById("node-panel");
  const dragBar = document.getElementById("panel-drag");
  if (!panel || !dragBar) return;

  let isDragging = false;
  let offsetX = 0;
  let offsetY = 0;

  dragBar.addEventListener("mousedown", (e) => {
    isDragging = true;
    const rect = panel.getBoundingClientRect();
    offsetX = e.clientX - rect.left;
    offsetY = e.clientY - rect.top;
    document.body.style.userSelect = "none";
  });

  document.addEventListener("mousemove", (e) => {
    if (!isDragging) return;
    panel.style.left = e.clientX - offsetX + "px";
    panel.style.top = e.clientY - offsetY + "px";
  });

  document.addEventListener("mouseup", () => {
    isDragging = false;
    document.body.style.userSelect = "";
  });
})();
