export let tabCounter = 0;
export const tabs = [];
export let currentTab = null;
// state.js
export let draggedTab = null;

export function setCurrentTab(tab) {
  currentTab = tab;
  window.currentTab = tab;
}
export function setDraggedTab(tab) {
  draggedTab = tab;
}

export function getDraggedTab() {
  return draggedTab;
}

export const tabBar = document.getElementById("tab-bar");
export const newTabBtn = document.getElementById("new-tab-btn");
export const canvasContainer = document.getElementById("canvas-container");

export const MIN_TAB_WIDTH = 20;

export function initGraph(canvas) {
    const graph = new LGraph();
    const graphCanvas = new LGraphCanvas(canvas, graph);
    graph.start();
    return { graph, graphCanvas };
}

export let currentMode = "select";

export function setMode(mode) {
  currentMode = mode;
}