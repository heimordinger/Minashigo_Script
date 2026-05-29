from pathlib import Path
from typing import Optional, Union, Tuple
from backend.ocr.ocr_engine import OCREngine
from core.match.match_result import MatchResult
import base64
import cv2
import numpy as np


class Matcher:
    def __init__(self):
        self.ocr_engine = OCREngine()

        self.orb = cv2.ORB_create(nfeatures=1200)
        self.bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)

        self._template_cache = {}   # path -> (img, gray)
        self._orb_cache = {}        # bytes -> (kp, des)

        # 多尺度搜索范围
        self.scales = [0.8, 0.9, 1.0, 1.1, 1.2]

    # =========================
    # 主入口
    # =========================
    def match(
            self,
            target: Union[np.ndarray, str, Path],
            template: Union[str, Path, np.ndarray, None] = None,
            text: str = None,
            color=None,
            threshold: Optional[float] = None,
            match_type: Optional[str] = None,
            crop_top_left: Tuple[int, int] = None,
            crop_bottom_right: Tuple[int, int] = None,
            *,
            match_select: str = "best",
            use_color_check: bool = False,
            color_tol: float = 30.0,
            min_dist: int = 20,
            max_count: Optional[int] = None,
            use_orb: bool = True,
    ):

        t = threshold if threshold is not None else 0.9

        # ---------- load frame ----------
        full_frame = self._to_bgr(target)
        H, W = full_frame.shape[:2]

        # ---------- ROI裁剪（仅手动）----------
        x1, y1 = crop_top_left if crop_top_left else (0, 0)
        x2, y2 = crop_bottom_right if crop_bottom_right else (W, H)

        x1, x2 = sorted((max(0, x1), min(W, x2)))
        y1, y2 = sorted((max(0, y1), min(H, y2)))

        frame = full_frame[y1:y2, x1:x2]
        frame_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        # ---------- match type ----------
        if match_type:
            mtype = match_type.lower()
        else:
            if template is not None:
                mtype = "image"
            elif text is not None:
                mtype = "text"
            elif color is not None:
                mtype = "color"
            else:
                raise ValueError("No valid match input")

        # =========================
        # IMAGE MATCH
        # =========================
        if mtype in ("image", "image_multi"):

            templ, templ_gray = self._load_template_cached(template)

            # 模板不能比图大
            if templ_gray.shape[0] > frame_gray.shape[0] or templ_gray.shape[1] > frame_gray.shape[1]:
                return [] if mtype == "image_multi" else MatchResult(None, None, 0.0, False)

            results = []

            results += self._template_multi_scale_match(
                frame_gray,
                templ_gray,
                threshold=t,
                offset=(x1, y1),
                use_color_check=use_color_check,
                frame_color=frame,
                templ_color=templ,
                color_tol=color_tol,
            )

            if use_orb:
                results += self._orb_match(frame, templ, t)

            if not results:
                return MatchResult(None, None, 0.0, False) if mtype == "image" else []

            results = self._deduplicate(results, min_dist)

            if mtype == "image_multi":
                return [
                    {"x": int(r.x), "y": int(r.y), "score": float(r.score)}
                    for r in results[:max_count] if not max_count or len(results) <= max_count
                ]

            best = max(results, key=lambda r: r.score)

            return MatchResult(int(best.x), int(best.y), float(best.score), best.score >= t)

        # =========================
        # TEXT
        # =========================
        elif mtype == "text":
            results = self.match_text_multi(frame, text)
            if not results:
                return MatchResult(None, None, 0.0, False)

            best = self._select_best(results, match_select)
            best["x"] += x1
            best["y"] += y1

            return MatchResult(best["x"], best["y"], best["score"], best["score"] >= t)

        # =========================
        # COLOR
        # =========================
        elif mtype == "color":
            x, y, score = self.match_color(frame, color, t)
            if x is None:
                return MatchResult(None, None, 0.0, False)
            return MatchResult(x + x1, y + y1, score, score >= t)

        raise ValueError("Unknown type")

    # =========================
    # TEMPLATE MATCH CORE
    # =========================
    def _template_multi_scale_match(
            self,
            frame_gray,
            templ_gray,
            threshold=0.9,
            offset=(0, 0),
            use_color_check=False,
            frame_color=None,
            templ_color=None,
            color_tol=30.0,
    ):
        """在多个尺度下执行模板匹配，返回全分辨率坐标"""
        results = []
        print(f"[_template_multi_scale_match] frame={frame_gray.shape}, template={templ_gray.shape}, threshold={threshold}")

        for s in self.scales:
            resized = cv2.resize(templ_gray, None, fx=s, fy=s)

            # 防崩：模板不能大于图（缩小后可能就合法了）
            if resized.shape[0] > frame_gray.shape[0] or resized.shape[1] > frame_gray.shape[1]:
                print(f"[_template_multi_scale_match] scale={s}: 模板({resized.shape}) > 帧({frame_gray.shape}), 跳过")
                continue

            res = cv2.matchTemplate(frame_gray, resized, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            print(f"[_template_multi_scale_match] scale={s}: max_val={max_val:.4f} (阈值={threshold}), at={max_loc}")

            if max_val < threshold:
                continue

            if use_color_check:
                if not self._color_check(frame_color, templ_color, max_loc, resized.shape, color_tol):
                    print(f"[_template_multi_scale_match] scale={s}: 颜色校验失败")
                    continue

            # 转回原始frame坐标
            x = max_loc[0] + resized.shape[1] // 2 + offset[0]
            y = max_loc[1] + resized.shape[0] // 2 + offset[1]

            # 高置信度直接返回（跳过剩余尺度）
            if max_val > 0.97:
                print(f"[_template_multi_scale_match] scale={s}: 高置信度({max_val:.4f})直接返回 ({x},{y})")
                return [MatchResult(x, y, float(max_val), True)]

            results.append(MatchResult(x, y, float(max_val), True))

        print(f"[_template_multi_scale_match] 总共找到 {len(results)} 个候选结果")
        return results

    # =========================
    # COLOR CHECK
    # =========================
    def _color_check(self, frame, templ, loc, size, tol):
        if frame is None or templ is None:
            return True

        x, y = loc
        h, w = size

        patch = frame[y:y + h, x:x + w]
        if patch.size == 0:
            return False

        templ = cv2.resize(templ, (patch.shape[1], patch.shape[0]))

        diff = np.abs(patch.astype(np.int16) - templ.astype(np.int16))
        return np.mean(diff) < tol

    # =========================
    # ORB MATCH
    # =========================
    def _orb_match(self, frame, templ, threshold):
        kp1, des1 = self._get_orb_template(templ)
        kp2, des2 = self.orb.detectAndCompute(frame, None)

        if des1 is None or des2 is None:
            return []

        matches = sorted(self.bf.match(des1, des2), key=lambda x: x.distance)

        if len(matches) < 10:
            return []

        good = matches[:len(matches) // 2]
        pts = [kp2[m.trainIdx].pt for m in good]

        x = int(np.mean([p[0] for p in pts]))
        y = int(np.mean([p[1] for p in pts]))

        score = len(good) / len(matches)

        return [MatchResult(x, y, float(score), score >= threshold)]

    # =========================
    # CACHE
    # =========================
    def _load_template_cached(self, template):
        key = str(template)
        if key in self._template_cache:
            return self._template_cache[key]

        img = self._load_template(template)

        gray = img if len(img.shape) == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        self._template_cache[key] = (img, gray)
        return img, gray

    def _get_orb_template(self, templ):
        key = templ.tobytes()
        if key in self._orb_cache:
            return self._orb_cache[key]

        kp, des = self.orb.detectAndCompute(templ, None)
        self._orb_cache[key] = (kp, des)
        return kp, des

    # =========================
    # UTIL
    # =========================
    def match_color(self, frame, color, threshold):
        target = np.array(color, dtype=np.int16)
        diff = np.linalg.norm(frame.astype(np.int16) - target, axis=2)
        sim = 1 - diff / np.sqrt(3 * 255 ** 2)

        _, max_val, _, max_loc = cv2.minMaxLoc(sim)

        if max_val >= threshold:
            return max_loc[0], max_loc[1], float(max_val)

        return None, None, float(max_val)

    def _deduplicate(self, results, min_dist):
        out = []
        for r in sorted(results, key=lambda x: -x.score):
            if all((r.x - o.x) ** 2 + (r.y - o.y) ** 2 > min_dist ** 2 for o in out):
                out.append(r)
        return out

    # =========================
    # IO
    # =========================
    def _to_bgr(self, img):
        if isinstance(img, np.ndarray):
            return img
        return self._load_image(Path(img))

    def _load_image(self, path: Path):
        path = self._resolve_image_path(path)
        data = np.fromfile(path, dtype=np.uint8)
        img = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError(f"图片解码失败: {path}")
        return img

    def _load_image_from_base64(self, data_url: str) -> np.ndarray:
        """从 base64 data URL 解码图片"""
        # data:image/png;base64,iVBOR...
        _, encoded = data_url.split(",", 1)
        img_bytes = base64.b64decode(encoded)
        arr = np.frombuffer(img_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("base64 图片解码失败")
        return img

    def _load_template(self, template):
        if isinstance(template, np.ndarray):
            print(f"[matcher._load_template] 已是numpy数组, shape={template.shape}")
            return template
        s = str(template)
        print(f"[matcher._load_template] template类型=字符串, 前缀={s[:60]}...")
        if s.startswith("data:image"):
            print(f"[matcher._load_template] 检测到base64 data URL, 长度={len(s)}")
            result = self._load_image_from_base64(s)
            print(f"[matcher._load_template] base64解码结果 shape={result.shape}")
            return result
        print(f"[matcher._load_template] 视为文件路径: {s}")
        return self._load_image(Path(template))

    def _resolve_image_path(self, path: Path) -> Path:
        if path.exists():
            return path
        for ext in [".png", ".jpg", ".jpeg"]:
            alt = path.with_suffix(ext)
            if alt.exists():
                return alt
        raise FileNotFoundError(f"图片不存在: {path}")


matcher = Matcher()
