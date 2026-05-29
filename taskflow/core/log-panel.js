// taskflow/core/log-panel.js

const LEVEL_ORDER = { debug: 0, info: 1, warn: 2, error: 3 };
const LEVEL_LABEL = { debug: "调试", info: "信息", warn: "警告", error: "错误" };

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
      this._logs = window._logPanel?._logs || [];
      this._filterLevel = "all";
      this._autoScroll = true;
      this._collapsed = false;
      return; // 已有面板，不重复绑定事件
    }
    this._logs = [];
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

    // 默认位置：右下
    this.el.style.right = "60px";
    this.el.style.bottom = "0";
    this.el.style.width = "480px";
    this.el.style.height = "200px";
  }

  _bindEvents() {
    // 拖拽标题栏移动
    this._header.addEventListener("mousedown", e => {
      if (e.target.tagName === "BUTTON") return;
      this._startDrag(e);
    });

    // 右下角缩放
    this._resizeHandle.addEventListener("mousedown", e => {
      this._startResize(e);
    });

    // 折叠
    this._collapseBtn.addEventListener("click", () => this.toggleCollapse());

    // 清空
    this._clearBtn.addEventListener("click", () => this.clear());

    // 过滤
    this._filterBtns.forEach(btn => {
      btn.addEventListener("click", () => {
        this._filterBtns.forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        this._filterLevel = btn.dataset.level;
        this._render();
      });
    });

    // 自动滚检测：如果用户手动往上滚了，暂停自动滚
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
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  }

  // ====== 折叠 ======

  toggleCollapse() {
    this._collapsed = !this._collapsed;
    this.el.classList.toggle("collapsed", this._collapsed);
    this._collapseBtn.textContent = this._collapsed ? "□" : "—";
  }

  // ====== 日志操作 ======

  addLog(level, message, timestamp) {
    const time = timestamp || new Date().toLocaleTimeString("zh-CN", { hour24: true });
    this._logs.push({ level: level || "info", message, time });
    if (this._filterLevel === "all" || this._filterLevel === level) {
      this._appendEntry({ level: level || "info", message, time });
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

  clear() {
    this._logs = [];
    this._body.innerHTML = "";
  }

  _render() {
    this._body.innerHTML = "";
    const filtered = this._filterLevel === "all"
      ? this._logs
      : this._logs.filter(l => l.level === this._filterLevel);
    filtered.forEach(e => this._appendEntry(e));
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
