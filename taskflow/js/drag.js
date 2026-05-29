import { tabBar, newTabBtn, draggedTab, tabs, setDraggedTab } from "./state.js";
import { updateTabWidths } from "./layout.js";

export function setupDrag(btn, tabObj) {

  btn.addEventListener("mousedown", e => {

    let offsetX = 0;
    let placeholder = null;

    let cachedRects = [];
    let siblings = [];

    let lastX = e.clientX;
    let startX = e.clientX;
    let startY = e.clientY;
    let isDragging = false;

    const rect = btn.getBoundingClientRect();
    offsetX = e.clientX - rect.left;

    function startDrag() {
      isDragging = true;
      setDraggedTab(tabObj);

      placeholder = document.createElement("div");
      placeholder.className = "placeholder";
      placeholder.style.width = rect.width + "px";
      placeholder.style.height = rect.height + "px";

      tabBar.insertBefore(placeholder, btn.nextSibling);

      btn.classList.add("dragging");
      btn.style.position = "absolute";
      btn.style.left = rect.left + "px";
      btn.style.top = rect.top + "px";
      btn.style.width = rect.width + "px";
      btn.style.pointerEvents = "none";
      btn.style.zIndex = 1000;

      updateCache();
    }

    function updateCache() {
      siblings = [...tabBar.children].filter(el =>
        el !== btn && el !== newTabBtn
      );
      cachedRects = siblings.map(el => el.getBoundingClientRect());
    }

    function animateSwap() {
      const elements = [...tabBar.children].filter(el =>
        el !== btn && el !== newTabBtn
      );

      const rects = elements.map(el => el.getBoundingClientRect());

      requestAnimationFrame(() => {
        elements.forEach((el, i) => {
          const newRect = el.getBoundingClientRect();
          const dx = rects[i].left - newRect.left;

          if (dx !== 0) {
            el.style.transition = "none";
            el.style.transform = `translateX(${dx}px)`;

            requestAnimationFrame(() => {
              el.style.transition = "transform 0.3s cubic-bezier(0.22,1,0.36,1)";
              el.style.transform = "";
            });
          }
        });
      });
    }

    function updatePosition() {
      const x = lastX - offsetX;
      btn.style.left = x + "px";

      const center = x + btn.offsetWidth / 2;
      const movingRight = lastX > prevX;

      const children = [...tabBar.children].filter(el =>
        el !== btn && el !== newTabBtn
      );

      const index = children.indexOf(placeholder);

      if (movingRight) {
        const next = children[index + 1];
        if (!next) return;

        const rect = next.getBoundingClientRect();
        if (center > rect.left + rect.width / 2) {
          animateSwap();
          tabBar.insertBefore(placeholder, next.nextSibling);
          updateCache();
        }

      } else {
        const prev = children[index - 1];
        if (!prev) return;

        const rect = prev.getBoundingClientRect();
        if (center < rect.left + rect.width / 2) {
          animateSwap();
          tabBar.insertBefore(placeholder, prev);
          updateCache();
        }
      }
    }

    let prevX = lastX;

    function onMove(e) {
      const dx = e.clientX - startX;
      const dy = e.clientY - startY;

      // ⚡ 拖拽触发条件：水平移动大于5px，且水平移动大于垂直移动
      if (!isDragging && Math.abs(dx) > 5 && Math.abs(dx) > Math.abs(dy)) {
        startDrag();
      }
      if (!isDragging) return;

      prevX = lastX;
      lastX = e.clientX;

      updatePosition();
    }

    function onUp() {
  if (isDragging) {
    // 计算 placeholder 的位置
    const rect = placeholder.getBoundingClientRect();

    btn.style.transition = "all 0.25s";
    btn.style.left = rect.left + "px";
    btn.style.top = rect.top + "px";

    setTimeout(() => {
      // 把按钮放回正确位置
      if (placeholder.parentNode === tabBar) {
        tabBar.insertBefore(btn, placeholder);
        placeholder.remove();
      }

      // 清理样式和类
      btn.style.transition = "";
      btn.style.position = "";
      btn.style.left = "";
      btn.style.top = "";
      btn.style.width = "";
      btn.style.pointerEvents = "";
      btn.style.zIndex = "";
      btn.classList.remove("dragging");

      const newOrder = [...tabBar.children]
        .filter(el => el.classList.contains("tab-btn"))
        .map(el => el.dataset.tabId);

      tabs.sort((a, b) =>
        newOrder.indexOf(a.id) - newOrder.indexOf(b.id)
      );

      updateTabWidths();
    }, 250);
  }

  isDragging = false;
  setDraggedTab(null);
  document.removeEventListener("mousemove", onMove);
  document.removeEventListener("mouseup", onUp);
}

    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);

  });
}