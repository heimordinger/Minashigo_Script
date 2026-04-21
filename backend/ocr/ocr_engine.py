# backend/ocr/ocr_engine.py
import cv2
import pytesseract
import numpy as np
from typing import List, Dict

from core.path import MODELS_PATH


class OCREngine:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def _init_ocr(self):
        """
        初始化 Tesseract（懒初始化，只会执行一次）
        """
        if self._initialized:
            return

        pytesseract.pytesseract.tesseract_cmd = str(
            MODELS_PATH / "Tesseract_OCR" / "tesseract.exe"
        )

        self.lang = "jpn+eng"
        self.config = "--oem 3 --psm 11"
        self._initialized = True

    def ocr_image(self, frame: np.ndarray) -> List[Dict]:
        """
        :param frame: np.ndarray (BGR)
        :return: OCR 结果列表
        """
        self._init_ocr()

        if frame is None or frame.size == 0:
            return []

        # BGR -> RGB
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        data = pytesseract.image_to_data(
            rgb,
            lang=self.lang,
            config=self.config,
            output_type=pytesseract.Output.DICT
        )

        results = []
        n = len(data["text"])

        for i in range(n):
            text = data["text"][i].strip()

            try:
                conf = int(data["conf"][i])
            except (ValueError, TypeError):
                continue

            if not text:
                continue

            x, y, w, h = (
                data["left"][i],
                data["top"][i],
                data["width"][i],
                data["height"][i],
            )

            results.append({
                "text": text,
                "confidence": conf,
                "bbox": (x, y, x + w, y + h)
            })

        return results


if __name__ == "__main__":
    img = cv2.imread("test.png")
    ocr = OCREngine()
    res = ocr.ocr_image(img)
    for r in res:
        print(r)
