import { tabs, tabBar, newTabBtn, MIN_TAB_WIDTH } from "./state.js";

export function updateTabWidths() {
  const tabBtns = tabs.map(t => t.btn);
  const count = tabBtns.length;
  const totalBarWidth = tabBar.clientWidth - 10;
  const newBtnWidth = newTabBtn.offsetWidth;
  const MAX_TAB_WIDTH = totalBarWidth * 0.25;

  let width = (totalBarWidth - newBtnWidth - (count-1)*2) / count;
  width = Math.max(MIN_TAB_WIDTH, Math.min(width, MAX_TAB_WIDTH));

  tabBtns.forEach(btn => btn.style.width = width + "px");
}