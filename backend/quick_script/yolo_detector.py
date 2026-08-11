# backend/quick_script/yolo_detector.py
"""YOLO UI 元素检测器 —— 用 OpenCV DNN 推理，无需 PyTorch。"""

from pathlib import Path

import cv2
import numpy as np
import requests




class YoloDetector:
    """基于 OpenCV DNN 的 YOLO 检测器（无需 PyTorch）。"""

    MODEL_DIR = Path(__file__).resolve().parent / "models"
    MODEL_URL = ("https://github.com/ultralytics/yolov5/releases/download/v7.0/"
                 "yolov5s.onnx")
    MODEL_FILE = "yolov5s.onnx"
    CONF_THRESH = 0.4
    NMS_THRESH = 0.45

    def __init__(self):
        self._net = None
        self._input_size = (640, 640)

    @property
    def available(self) -> bool:
        return self._load()

    def _model_path(self) -> Path:
        self.MODEL_DIR.mkdir(parents=True, exist_ok=True)
        return self.MODEL_DIR / self.MODEL_FILE

    def _download_model(self) -> bool:
        """首次运行下载模型。"""
        import requests
        from tqdm import tqdm

        dest = self._model_path()
        if dest.exists():
            return True

        print(f"[YoloDetector] 下载模型: {self.MODEL_FILE}")
        try:
            r = requests.get(self.MODEL_URL, stream=True, timeout=30)
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            with open(dest, "wb") as f:
                for chunk in tqdm(r.iter_content(8192), total=total // 8192,
                                  unit="KB", desc=self.MODEL_FILE):
                    if chunk:
                        f.write(chunk)
            print(f"[YoloDetector] 模型已保存: {dest}")
            return True
        except Exception as e:
            print(f"[YoloDetector] 下载失败: {e}")
            return False

    def _load(self) -> bool:
        if self._net is not None:
            return True
        if not self._download_model():
            return False
        try:
            self._net = cv2.dnn.readNetFromONNX(str(self._model_path()))
            self._net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
            self._net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
            return True
        except Exception as e:
            print(f"[YoloDetector] 加载模型失败: {e}")
            return False

    def detect(self, frame: np.ndarray, confidence: float = 0.3
               ) -> list[dict]:
        """全屏检测 UI 元素。"""
        if not self._load():
            return []

        h, w = frame.shape[:2]
        # 预处理：缩放到模型输入尺寸
        blob = cv2.dnn.blobFromImage(frame, 1 / 255.0, self._input_size,
                                     swapRB=True, crop=False)
        self._net.setInput(blob)
        try:
            outputs = self._net.forward()[0]
        except Exception as e:
            print(f"[YoloDetector] 推理失败: {e}")
            return []

        # 解析 YOLOv5 输出
        boxes = []
        scale_x = w / self._input_size[0]
        scale_y = h / self._input_size[1]

        for det in outputs:
            # YOLOv5: [cx, cy, w, h, objectness, cls_scores...]
            cx, cy, bw, bh = det[:4]
            obj_conf = float(det[4])
            if obj_conf < 0.3:
                continue
            cls_scores = det[5:]
            cls_id = int(cls_scores.argmax())
            score = float(cls_scores[cls_id]) * obj_conf
            if score < confidence:
                continue
            x1 = int((cx - bw / 2) * scale_x)
            y1 = int((cy - bh / 2) * scale_y)
            bw = int(bw * scale_x)
            bh = int(bh * scale_y)
            # 只保留 UI 相关类别（家具/电子/书籍等）
            if cls_id not in (56, 57, 60, 62, 63, 64, 65, 66, 67, 74, 75):
                # 但保留高置信度的其它检测作为参考
                if score < 0.7:
                    continue
            boxes.append({
                "x": max(0, x1), "y": max(0, y1),
                "w": min(bw, w - x1), "h": min(bh, h - y1),
                "score": score,
                "class_id": cls_id,
            })

        # NMS 去重
        if not boxes:
            return []
        coords = np.array([[b["x"], b["y"], b["x"] + b["w"], b["y"] + b["h"]]
                           for b in boxes], dtype=np.float32)
        scores_arr = np.array([b["score"] for b in boxes], dtype=np.float32)
        keep = cv2.dnn.NMSBoxes(coords.tolist(), scores_arr.tolist(),
                                self.CONF_THRESH, self.NMS_THRESH)
        return [boxes[i] for i in keep]

    def detect_near(self, frame: np.ndarray, cx: int, cy: int,
                    confidence: float = 0.3) -> dict | None:
        """检测并返回离鼠标最近的元素。"""
        boxes = self.detect(frame, confidence)
        if not boxes:
            return None

        best = min(boxes, key=lambda b:
                   (b["x"] + b["w"] // 2 - cx) ** 2 +
                   (b["y"] + b["h"] // 2 - cy) ** 2)

        # 鼠标必须在元素内部或附近
        margin = 10
        if (best["x"] - margin <= cx <= best["x"] + best["w"] + margin and
                best["y"] - margin <= cy <= best["y"] + best["h"] + margin):
            return best
        return None
