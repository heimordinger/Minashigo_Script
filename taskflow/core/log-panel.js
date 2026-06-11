// taskflow/core/log-panel.js

const LEVEL_ORDER = { debug: 0, info: 1, warn: 2, error: 3 };
const LEVEL_LABEL = { debug: "调试", info: "信息", warn: "警告", error: "错误" };
const MAX_PER_LEVEL = 100;

export class LogPanel {
  constructor() {
    // 防止重复创建
    if (document.getElementById("log-panel")) {
      const existing = document.getElementById("log-panel");
      this.el = existing;
      this._body = existing.querySelector(".log-body");
      this._header = existing.querySelector(".log-header");
      this._resizeHandle = existing.querySelector(".log-resize-handle");
      this._collapseBtn = existing.querySelector(".log-collapse-btn");
      this._clearBtn = existing.querySelector(".log-clear-btn");
      this._filterBtns = existing.querySelectorAll(".log-filter");
      this._logs = window._logPanel?._logs || { debug: [], info: [], warn: [], error: [] };
      this._filterLevel = "all";
      this._autoScroll = true;
      this._collapsed = false;
      return;
    }
    this._logs = { debug: [], info: [], warn: [], error: [] };
    this._filterLevel = "all";
    this._autoScroll = true;
    this._collapsed = false;
    this._createDOM();
    this._bindEvents();
  }

  _createDOM() {
    this.el = document.createElement("div");
    this.el.id = "log-panel";
    this.el.innerHTML = `
      <div class="log-header">
        <span class="log-title">☰ 日志</span>
        <div class="log-filters">
          <button class="log-filter active" data-level="all">全部</button>
          <button class="log-filter" data-level="info">信息</button>
          <button class="log-filter" data-level="warn">警告</button>
          <button class="log-filter" data-level="error">错误</button>
        </div>
        <div class="log-actions">
          <button class="log-clear-btn" title="清空">⌂</button>
          <button class="log-collapse-btn" title="折叠">—</button>
        </div>
      </div>
      <div class="log-body"></div>
      <div class="log-resize-handle"></div>
    `;
    document.body.appendChild(this.el);

    this._body = this.el.querySelector(".log-body");
    this._header = this.el.querySelector(".log-header");
    this._resizeHandle = this.el.querySelector(".log-resize-handle");
    this._collapseBtn = this.el.querySelector(".log-collapse-btn");
    this._clearBtn = this.el.querySelector(".log-clear-btn");
    this._filterBtns = this.el.querySelectorAll(".log-filter");

    this.el.style.right = "60px";
    this.el.style.bottom = "0";
    this.el.style.width = "480px";
    this.el.style.height = "200px";
  }

  _bindEvents() {
    this._header.addEventListener("mousedown", e => {
      if (e.target.tagName === "BUTTON") return;
      this._startDrag(e);
    });

    this._resizeHandle.addEventListener("mousedown", e => {
      this._startResize(e);
    });

    this._collapseBtn.addEventListener("click", () => this.toggleCollapse());

    // 清空 — 只清当前筛选级别的日志
    this._clearBtn.addEventListener("click", () => this.clearCurrent());

    // 过滤
    this._filterBtns.forEach(btn => {
      btn.addEventListener("click", () => {
        this._filterBtns.forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        this._filterLevel = btn.dataset.level;
        this._render();
      });
    });

    this._body.addEventListener("scroll", () => {
      const { scrollTop, scrollHeight, clientHeight } = this._body;
      this._autoScroll = scrollHeight - scrollTop - clientHeight < 30;
    });
  }

  // ====== 拖拽 ======

  _startDrag(e) {
    e.preventDefault();
    const rect = this.el.getBoundingClientRect();
    const dx = e.clientX - rect.left;
    const dy = e.clientY - rect.top;
    this.el.style.transition = "none";

    const onMove = ev => {
      this.el.style.left = `${ev.clientX - dx}px`;
      this.el.style.top = `${ev.clientY - dy}px`;
      this.el.style.right = "auto";
      this.el.style.bottom = "auto";
    };
    const onUp = () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  }

  // ====== 缩放 ======

  _startResize(e) {
    e.preventDefault();
    e.stopPropagation();
    const { right, bottom } = this.el.getBoundingClientRect();
    const startW = this.el.offsetWidth;
    const startH = this.el.offsetHeight;

    const onMove = ev => {
      const w = Math.max(200, startW + (ev.clientX - right));
      const h = Math.max(80, startH + (ev.clientY - bottom));
      this.el.style.width = `${w}px`;
      this.el.style.height = `${h}px`;
    };
    const onUp = () => {
      document.removeEventListener("pointermove", onMove);
      document.removeEventListener("pointerup", onUp);
      document.removeEventListener("mouseleave", onUp);
    };
    // 用 pointer 事件替代 mouse，避免松开鼠标后粘滞
    document.addEventListener("pointermove", onMove);
    document.addEventListener("pointerup", onUp);
    document.addEventListener("mouseleave", onUp);
  }

  // ====== 折叠 ======

  toggleCollapse() {
    this._collapsed = !this._collapsed;
    this.el.classList.toggle("collapsed", this._collapsed);
    this._collapseBtn.textContent = this._collapsed ? "□" : "—";
  }

  // ====== 日志操作 ======

  // 从 DOM 读取当前激活的筛选级别，避免 JS 实例状态不同步
  _getActiveFilter() {
    const active = this.el?.querySelector(".log-filter.active");
    return active ? active.dataset.level : "all";
  }

  addLog(level, message, timestamp) {
    level = level || "info";
    const time = timestamp || new Date().toLocaleTimeString("zh-CN", { hour24: true });
    const entry = { level, message, time };

    // 按级别存入独立队列，超出上限则丢弃最旧的
    const queue = this._logs[level];
    if (queue) {
      queue.push(entry);
      if (queue.length > MAX_PER_LEVEL) queue.shift();
    }

    // 仅当匹配当前筛选时才追加到 DOM
    const currentFilter = this._getActiveFilter();
    if (currentFilter === "all" || currentFilter === level) {
      this._appendEntry(entry);
    }
  }

  _appendEntry(entry) {
    const div = document.createElement("div");
    div.className = `log-entry log-${entry.level}`;
    div.innerHTML = `<span class="log-time">${entry.time}</span><span class="log-msg">${this._escape(entry.message)}</span>`;
    this._body.appendChild(div);

    if (this._autoScroll) {
      this._body.scrollTop = this._body.scrollHeight;
    }
  }

  // 清空当前筛选级别的日志
  clearCurrent() {
    const currentFilter = this._getActiveFilter();
    if (currentFilter === "all") {
      // 全部 → 清空所有级别
      for (const k of Object.keys(this._logs)) {
        this._logs[k] = [];
      }
    } else if (this._logs[currentFilter]) {
      // 仅清空当前级别
      this._logs[currentFilter] = [];
    }
    this._body.innerHTML = "";
  }

  _render() {
    this._body.innerHTML = "";

    const currentFilter = this._getActiveFilter();
    let entries;
    if (currentFilter === "all") {
      // 按级别顺序合并：debug → info → warn → error
      const order = ["debug", "info", "warn", "error"];
      entries = [];
      for (const lv of order) {
        const arr = this._logs[lv];
        if (arr) entries.push(...arr);
      }
    } else {
      entries = this._logs[currentFilter] || [];
    }

    for (const e of entries) {
      this._appendEntry(e);
    }
    if (this._autoScroll) {
      this._body.scrollTop = this._body.scrollHeight;
    }
  }

  _escape(s) {
    const d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  }
}

// 全局日志函数，供节点调用
window.taskflowLog = (level, message) => {
  const panel = window._logPanel;
  if (panel) panel.addLog(level, message);
};
