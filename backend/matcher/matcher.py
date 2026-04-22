# backend/matcher/matcher.py
from pathlib import Path
from typing import Optional, Union, Tuple
from backend.ocr.ocr_engine import OCREngine
from core.match.match_result import MatchResult
import cv2
import numpy as np


class Matcher:
    def __init__(self):
        self.ocr_engine = OCREngine()

        self.orb = cv2.ORB_create(nfeatures=1200)
        self.bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)

        self.scales = [0.8, 0.9, 1.0, 1.1, 1.2]

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

        # 🔥 关键：兜底 threshold
        t = threshold if threshold is not None else 0.9

        full_frame = target if isinstance(target, np.ndarray) else self._load_image(Path(target))
        H, W = full_frame.shape[:2]

        x1, y1 = crop_top_left if crop_top_left else (0, 0)
        x2, y2 = crop_bottom_right if crop_bottom_right else (W, H)

        x1, x2 = sorted((max(0, x1), min(W, x2)))
        y1, y2 = sorted((max(0, y1), min(H, y2)))

        frame = full_frame[y1:y2, x1:x2]
        frame_gray = frame if len(frame.shape) == 2 else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

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

        # ================= IMAGE =================
        if mtype in ("image", "image_multi"):

            templ = self._load_template(template)
            templ_gray = templ if len(templ.shape) == 2 else cv2.cvtColor(templ, cv2.COLOR_BGR2GRAY)

            results: list[MatchResult] = []

            results += self._template_multi_scale_match(
                frame_gray,
                templ_gray,
                threshold=t,
                use_color_check=use_color_check,
                frame_color=frame,
                templ_color=templ,
                color_tol=color_tol
            )

            if use_orb:
                results += self._orb_match(frame, templ, t)

            if not results:
                if mtype == "image":
                    return MatchResult(None, None, 0.0, False)
                return []

            results = self._deduplicate(results, min_dist)

            if mtype == "image_multi":
                return [
                    {
                        "x": int(r.x + x1),
                        "y": int(r.y + y1),
                        "score": float(r.score)
                    }
                    for r in results[:max_count] if not max_count or len(results) <= max_count
                ]

            if not results:
                return MatchResult(None, None, 0.0, False)

            best = max(results, key=lambda r: r.score)

            return MatchResult(
                int(best.x + x1),
                int(best.y + y1),
                float(best.score),
                best.score >= t
            )

        # ================= TEXT =================
        elif mtype == "text":
            results = self.match_text_multi(frame, text)
            if not results:
                return MatchResult(None, None, 0.0, False)

            best = self._select_best(results, match_select)
            best["x"] += x1
            best["y"] += y1

            return MatchResult(
                best["x"],
                best["y"],
                best["score"],
                best["score"] >= t
            )

        # ================= COLOR =================
        elif mtype == "color":
            x, y, score = self.match_color(frame, color, t)
            if x is None:
                return MatchResult(None, None, 0.0, False)

            return MatchResult(x + x1, y + y1, score, score >= t)

        raise ValueError("Unknown type")

    # ================= TEMPLATE =================
    def _template_multi_scale_match(
            self,
            frame_gray,
            templ_gray,
            threshold=0.9,
            use_color_check=False,
            frame_color=None,
            templ_color=None,
            color_tol=30.0
    ):
        results = []

        for s in self.scales:
            resized = cv2.resize(templ_gray, None, fx=s, fy=s)

            if resized.shape[0] > frame_gray.shape[0]:
                continue

            res = cv2.matchTemplate(frame_gray, resized, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)

            if max_val < threshold:
                continue

            if use_color_check:
                if not self._color_check(frame_color, templ_color, max_loc, color_tol, resized.shape):
                    continue

            results.append(
                MatchResult(
                    max_loc[0] + resized.shape[1] // 2,
                    max_loc[1] + resized.shape[0] // 2,
                    float(max_val),
                    True  # 这里一定 >= threshold
                )
            )

        return results

    # ================= ORB =================
    def _orb_match(self, frame, templ, threshold):
        kp1, des1 = self.orb.detectAndCompute(templ, None)
        kp2, des2 = self.orb.detectAndCompute(frame, None)

        if des1 is None or des2 is None:
            return []

        matches = self.bf.match(des1, des2)
        matches = sorted(matches, key=lambda x: x.distance)

        if len(matches) < 10:
            return []

        good = matches[:max(1, len(matches) // 2)]
        pts = [kp2[m.trainIdx].pt for m in good]

        x = int(np.mean([p[0] for p in pts]))
        y = int(np.mean([p[1] for p in pts]))

        score = len(good) / len(matches)

        return [MatchResult(x, y, float(score), score >= threshold)]

    # ================= COLOR =================
    def match_color(self, frame, color, threshold):
        target = np.array(color, dtype=np.int16)
        diff = np.linalg.norm(frame.astype(np.int16) - target, axis=2)
        sim = 1 - diff / np.sqrt(3 * 255 ** 2)
        _, max_val, _, max_loc = cv2.minMaxLoc(sim)

        if max_val >= threshold:
            return max_loc[0], max_loc[1], float(max_val)

        return None, None, max_val

    # ================= DEDUP =================
    def _deduplicate(self, results, min_dist):

        def normalize(r):
            if isinstance(r, MatchResult):
                return r
            if isinstance(r, dict):
                return MatchResult(
                    r.get("x"),
                    r.get("y"),
                    r.get("score", r.get("max_val", 0.0)),
                    True
                )
            return None

        results = [normalize(r) for r in results]
        results = [r for r in results if r and r.x is not None and r.y is not None]

        filtered = []

        for r in sorted(results, key=lambda x: -x.score):
            if all(
                    (r.x - f.x) ** 2 + (r.y - f.y) ** 2 > min_dist ** 2
                    for f in filtered
            ):
                filtered.append(r)

        return filtered

    # ================= IO =================
    def _load_image(self, path: Path):
        path = self._resolve_image_path(path)
        data = np.fromfile(path, dtype=np.uint8)
        img = cv2.imdecode(data, cv2.IMREAD_COLOR)

        if img is None:
            raise ValueError(f"图片解码失败: {path}")

        return img

    def _load_template(self, template):
        return template if isinstance(template, np.ndarray) else self._load_image(Path(template))

    def _resolve_image_path(self, path: Path) -> Path:
        if path.exists():
            return path

        if path.suffix == "":
            for ext in [".png", ".jpg", ".jpeg"]:
                alt = path.with_suffix(ext)
                if alt.exists():
                    return alt
        else:
            for ext in [".png", ".jpg", ".jpeg"]:
                alt = path.with_suffix(ext)
                if alt.exists():
                    return alt

        raise FileNotFoundError(f"图片不存在: {path}")

    """
    老代码的技术债
    """
    def _load_and_normalize_template(self, template):
        img = self._load_template(template)
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    def _load_and_normalize_target(self, target, gray=True):
        img = target if isinstance(target, np.ndarray) else self._load_image(Path(target))
        if gray:
            return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        return img

matcher = Matcher()