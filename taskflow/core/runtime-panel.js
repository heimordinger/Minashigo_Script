// taskflow/core/runtime-panel.js
import { currentTab } from "../js/state.js";
import { STATE } from "./workflow-controller.js";

export function initRuntimePanel() {
  const panel = document.getElementById("runtime-panel");
  const header = document.getElementById("runtime-drag");
  let dragging = false, offsetX = 0, offsetY = 0;

  header.addEventListener("mousedown", e => {
    if (e.target.tagName === "BUTTON") return;
    dragging = true;
    const rect = panel.getBoundingClientRect();
    offsetX = e.clientX - rect.left;
    offsetY = e.clientY - rect.top;
    panel.style.transition = "none";
  });

  document.addEventListener("mousemove", e => {
    if (!dragging) return;

    const panelWidth = panel.offsetWidth;
    const panelHeight = panel.offsetHeight;
    const maxX = window.innerWidth - panelWidth;
    const maxY = window.innerHeight - panelHeight;

    let x = e.clientX - offsetX;
    let y = e.clientY - offsetY;

    x = Math.max(0, Math.min(x, maxX));
    y = Math.max(0, Math.min(y, maxY));

    panel.style.left = `${x}px`;
    panel.style.top = `${y}px`;
  });

  document.addEventListener("mouseup", () => {
    if (dragging) {
      dragging = false;
      panel.style.transition = "transform 0.2s ease, height 0.25s ease, box-shadow 0.2s ease";
    }
  });

  // ===== 控制按钮 =====
  const btnStart = document.getElementById("btn-start");
  const btnPause = document.getElementById("btn-pause");
  const btnResume = document.getElementById("btn-resume");
  const btnStop = document.getElementById("btn-stop");

  // ===== 新增运行状态管理 =====
  let runState = STATE.IDLE;

  function updateRunButtons() {
    // 清除所有 active 样式
    [btnStart, btnPause, btnResume, btnStop].forEach(btn => btn.classList.remove("active"));

    // 默认禁用所有按钮
    btnStart.disabled = true;
    btnPause.disabled = true;
    btnResume.disabled = true;
    btnStop.disabled = true;

    switch (runState) {
      case STATE.RUNNING:
        btnStart.classList.add("active");
        btnStart.disabled = true;
        btnPause.disabled = false;
        btnResume.disabled = true;
        btnStop.disabled = false;
        break;

      case STATE.PAUSED:
        btnPause.classList.add("active");
        btnStart.disabled = false;
        btnPause.disabled = true;
        btnResume.disabled = false;
        btnStop.disabled = false;
        break;

      case STATE.STOPPED:
      case STATE.FINISHED:
        btnStop.classList.add("active");
        btnStart.disabled = false;
        btnPause.disabled = true;
        btnResume.disabled = true;
        btnStop.disabled = true;
        break;

      case STATE.IDLE:
        btnStart.disabled = false;
        btnPause.disabled = true;
        btnResume.disabled = true;
        btnStop.disabled = true;
        break;
    }
  }

  function getController() {
    return window.workflowController;
  }

  btnStart.onclick = () => {
    getController()?.start();
    // 状态由workflow-controller管理，通过updateRuntimeState更新
  };
  btnPause.onclick = () => {
    getController()?.pause();
    // 状态由workflow-controller管理，通过updateRuntimeState更新
  };
  btnResume.onclick = () => {
    getController()?.resume();
    // 状态由workflow-controller管理，通过updateRuntimeState更新
  };
  btnStop.onclick = () => {
    getController()?.stop();
    // 状态由workflow-controller管理，通过updateRuntimeState更新
  };

  // ===== 状态显示 =====
  const runStateEl = document.getElementById("run-state");
  const currentNodeEl = document.getElementById("current-node");
  const stateText = {
    [STATE.IDLE]: "空闲",
    [STATE.RUNNING]: "运行中",
    [STATE.PAUSED]: "暂停中",
    [STATE.STOPPED]: "已停止",
    [STATE.FINISHED]: "已完成",
  };

  window.updateRuntimeState = state => {
    runStateEl.innerText = stateText[state] || state;
    // 同步按钮状态
    runState = state;
    updateRunButtons();
  };

  window.updateCurrentNode = node => currentNodeEl.innerText = node?.title || "-";

  // ===== FPS更新 =====
  const fpsEl = document.getElementById("run-fps");
  const delayEl = document.getElementById("run-delay");

  function updateFPS() {
    const canvas = currentTab?.graphCanvas;
    if (canvas && canvas.fps !== undefined) {
      fpsEl.innerText = canvas.fps.toFixed(1);
    } else {
      fpsEl.innerText = "0";
    }

    // 更新延迟显示（后端响应延迟）
    const controller = window.workflowController;
    if (controller && controller.lastBackendDelay !== undefined) {
      delayEl.innerText = `${controller.lastBackendDelay}ms`;
    } else {
      delayEl.innerText = "0ms";
    }
  }

  // 每2秒更新一次FPS
  setInterval(updateFPS, 2000);

  // 初始化按钮状态
  updateRunButtons();
}

// ===== 模块级事件委托：折叠按钮 =====
// 使用 document 委托确保即使 initRuntimePanel 未执行也能正常工作
document.addEventListener("click", (e) => {
  const btn = e.target.closest("#runtime-toggle");
  if (!btn) return;

  const p = document.getElementById("runtime-panel");
  if (!p) return;

  const isCollapsed = p.classList.contains("collapsed");
  const COLLAPSED_H = 36;

  if (isCollapsed) {
    p.classList.remove("collapsed");
    const fullH = p.scrollHeight;
    p.classList.add("collapsed");
    p.style.height = COLLAPSED_H + "px";
    p.style.overflow = "hidden";
    void p.offsetHeight;
    p.classList.remove("collapsed");
    p.style.height = fullH + "px";
  } else {
    const curH = p.offsetHeight;
    p.style.height = curH + "px";
    p.style.overflow = "hidden";
    void p.offsetHeight;
    p.classList.add("collapsed");
    p.style.height = COLLAPSED_H + "px";
  }

  const cleanup = () => {
    p.style.height = "";
    p.style.overflow = "";
  };
  p.addEventListener("transitionend", cleanup, { once: true });
  setTimeout(cleanup, 300);
});
