# backend/quick_script/recorder.py
"""快速脚本录制器 —— 监听鼠标点击，自动截取按钮模板，生成步骤序列。"""

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from pynput import mouse

from backend.matcher.matcher import Matcher


@dataclass
class QuickScriptStep:
    """录制的一个步骤"""
    action: str  # click / wait / detect_appear / detect_disappear
    template_name: str = ""   # 模板文件名（相对于模板目录）
    template_img: Optional[np.ndarray] = None  # 模板图像
    threshold: float = 0.9
    x: int = 0    # 录制时的原始点击坐标（仅参考）
    y: int = 0
    wait_seconds: float = 1.0  # wait 动作的时长
    timeout: float = 10.0       # detect 动作的超时


class QuickScriptRecorder:
    """快速脚本录制器
    启动后监听全局鼠标点击，每次点击自动截取按钮元素。
    """

    def __init__(self, output_dir: str | Path):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.steps: list[QuickScriptStep] = []
        self._listener: mouse.Listener | None = None
        self._running = False
        self._last_click_time = 0.0
        self._min_click_interval = 0.3  # 最小点击间隔，防连点
        self._step_counter = 0

        # 元素检测参数
        self._matcher = Matcher()

    # ── 控制 ──

    def start(self):
        """启动监听。"""
        if self._running:
            return
        self._running = True
        self.steps.clear()
        self._step_counter = 0
        self._listener = mouse.Listener(on_click=self._on_click)
        self._listener.start()
        print(f"[QuickScript] 录制已开始，输出目录: {self.output_dir}")

    def stop(self):
        """停止监听。"""
        self._running = False
        if self._listener:
            self._listener.stop()
            self._listener = None
        print(f"[QuickScript] 录制已停止，共录制 {len(self.steps)} 步")

    @property
    def is_running(self) -> bool:
        return self._running

    # ── 鼠标回调 ──

    def _on_click(self, x, y, button, pressed):
        if not self._running:
            return
        if not pressed:  # 只响应按下，不响应抬起
            return
        if button != mouse.Button.left:  # 只响应左键
            return

        now = time.time()
        if now - self._last_click_time < self._min_click_interval:
            return
        self._last_click_time = now

        self._capture_step(x, y)

    def _capture_step(self, x: int, y: int):
        """在点击坐标处截取按钮并记录步骤。"""
        self._step_counter += 1
        name = f"step_{self._step_counter:03d}"

        # 截取整个屏幕（后续可改为只截目标窗口）
        screen = self._screenshot_screen()

        # 从点击位置检测元素边界
        elem = self._detect_element(screen, x, y)
        if elem:
            ex, ey, ew, eh = elem["x"], elem["y"], elem["w"], elem["h"]
            template = screen[ey:ey + eh, ex:ex + ew]
        else:
            # 检测失败 —— 用固定区域
            size = 60
            ex = max(0, x - size // 2)
            ey = max(0, y - size // 2)
            template = screen[ey:ey + size, ex:ex + size]
            ew, eh = template.shape[1], template.shape[0]

        # 保存模板
        template_path = self.output_dir / f"{name}.png"
        cv2.imwrite(str(template_path), template)
        print(f"[QuickScript] 步骤{self._step_counter}: 点击({x},{y}) → 模板 {name}.png ({ew}×{eh})")

        # 记录步骤
        step = QuickScriptStep(
            action="click",
            template_name=f"{name}.png",
            template_img=template.copy(),
            threshold=0.85,
            x=x, y=y,
        )
        self.steps.append(step)

    # ── 截图 ──

    def _screenshot_screen(self) -> np.ndarray:
        """截取全屏（用 mss / PIL / win32 均可，这里用 pyautogui 兜底）。"""
        try:
            import pyautogui
            img = pyautogui.screenshot()
            return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
        except Exception:
            pass
        # 兜底：用 win32 API
        try:
            from backend.automation.win32_target import Win32Target
            hwnd = None  # None = 截取全屏
            return Win32Target._screenshot_screen()
        except Exception as e:
            raise RuntimeError(f"截图失败: {e}")

    # ── 元素检测 ──

    @staticmethod
    def _detect_element(frame: np.ndarray, cx: int, cy: int) -> dict | None:
        """同 ElementInspector 的边缘检测算法。"""
        h, w = frame.shape[:2]
        if cx < 0 or cy < 0 or cx >= w or cy >= h:
            return None
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        def _find_edge_x(start, step):
            x = start
            while 0 <= x < w - 1:
                nx = x + step
                if abs(int(gray[cy, x]) - int(gray[cy, nx])) > 25:
                    return x
                x = nx
            return start

        def _find_edge_y(start, step):
            y = start
            while 0 <= y < h - 1:
                ny = y + step
                if abs(int(gray[y, cx]) - int(gray[ny, cx])) > 25:
                    return y
                y = ny
            return start

        left = _find_edge_x(cx, -1)
        right = _find_edge_x(cx, 1)
        top = _find_edge_y(cy, -1)
        bottom = _find_edge_y(cy, 1)
        ew, eh = right - left + 1, bottom - top + 1
        if ew < 8 or eh < 8:
            return None
        return {"x": left, "y": top, "w": ew, "h": eh}

    # ── 导出 ──

    def export_to_taskflow(self, file_path: str | Path) -> bool:
        """将步骤导出为 TaskFlow JSON 工作流。"""
        # TODO: 完善导出
        import json
        nodes = []
        links = []
        node_id = 1

        # 起点
        nodes.append({
            "id": 1, "type": "flow/start", "title": "起点",
            "pos": [100, 100], "size": [140, 26],
            "outputs": [{"name": "next", "type": -1, "links": [1]}],
            "properties": {}
        })

        prev_id = 1
        link_id = 1
        for step in self.steps:
            node_id += 1
            nid = node_id
            if step.action == "click":
                template_abs = str((self.output_dir / step.template_name).resolve())
                nodes.append({
                    "id": nid,
                    "type": "action/click_image",
                    "title": f"点击 {step.template_name}",
                    "pos": [100 + (nid - 1) * 50, 200 + (nid - 1) * 60],
                    "size": [360, 300],
                    "inputs": [{"name": "触发", "type": -1, "link": link_id, "slot_index": 0}],
                    "outputs": [
                        {"name": "下一步", "type": -1, "links": None},
                        {"name": "成功", "type": -1, "links": [link_id + 1]}
                    ],
                    "properties": {
                        "image": template_abs,
                        "threshold": step.threshold,
                        "pianyi_x": 0, "pianyi_y": 0,
                        "down_time": 0.12,
                    }
                })
                link_id += 2
            elif step.action == "wait":
                nodes.append({
                    "id": nid,
                    "type": "flow/sleep",
                    "title": f"等待 {step.wait_seconds}s",
                    "pos": [100 + (nid - 1) * 50, 200 + (nid - 1) * 60],
                    "inputs": [{"name": "输入", "type": -1, "link": link_id}],
                    "outputs": [{"name": "输出", "type": -1, "links": [link_id + 1]}],
                    "properties": {"seconds": step.wait_seconds}
                })
                link_id += 1
            prev_id = nid

        workflow = {
            "last_node_id": node_id,
            "last_link_id": link_id,
            "nodes": nodes,
            "links": []
        }
        Path(file_path).write_text(json.dumps(workflow, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[QuickScript] 已导出工作流: {file_path}")
        return True
