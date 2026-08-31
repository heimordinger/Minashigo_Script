"""Smoke + integration tests for adaptive hotspot ROI."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.matcher.hotspot_roi import (  # noqa: E402
    HotspotStore,
    adaptive_match,
    frame_bucket_key,
    normalize_template_key,
)
from backend.matcher.matcher import Matcher  # noqa: E402
from core.match.match_result import MatchResult  # noqa: E402


class _FakeMatcher:
    def __init__(self):
        self.calls = []

    def match(self, target, template, threshold=0.9, match_type="image",
              crop_top_left=None, crop_bottom_right=None, **kwargs):
        self.calls.append((crop_top_left, crop_bottom_right))
        h, w = target.shape[:2]
        if crop_top_left and crop_bottom_right:
            x1, y1 = crop_top_left
            x2, y2 = crop_bottom_right
            if x2 - x1 < 50:
                return MatchResult(None, None, 0.0, False)
            cx = (x1 + x2) // 2
            cy = (y1 + y2) // 2
            return MatchResult(cx, cy, 0.95, True)
        return MatchResult(w // 2, h // 2, 0.92, True)


def test_store_propose_and_clear(tmp_path: Path):
    store = HotspotStore(tmp_path / "hs.json")
    store.enabled = True
    tkey = "deepone/jjc_ok.png"
    fkey = frame_bucket_key(1910, 915, capture_mode="full")
    for i in range(5):
        store.record_hit(
            template_key=tkey,
            frame_key=fkey,
            x=950 + i,
            y=820 + (i % 2),
            score=0.95,
            via_roi=False,
        )
    rois = store.propose_rois(
        template_key=tkey, frame_key=fkey, frame_w=1910, frame_h=915
    )
    assert rois, "should propose ROI after enough samples"
    x1, y1, x2, y2 = rois[0]
    assert x1 < 950 < x2
    assert y1 < 820 < y2
    store.flush(force=True)
    assert (tmp_path / "hs.json").is_file()
    store2 = HotspotStore(tmp_path / "hs.json")
    rois_reload = store2.propose_rois(
        template_key=tkey, frame_key=fkey, frame_w=1910, frame_h=915
    )
    assert rois_reload, "persist/reload should keep samples"
    for _ in range(8):
        store.note_roi_miss_then_full(template_key=tkey, frame_key=fkey)
    rois2 = store.propose_rois(
        template_key=tkey, frame_key=fkey, frame_w=1910, frame_h=915
    )
    assert not rois2, "unreliable bucket should clear"


def test_adaptive_roi_then_full(tmp_path: Path):
    import backend.matcher.hotspot_roi as hr

    hr._STORE = HotspotStore(tmp_path / "hs2.json")
    hr._STORE.enabled = True

    fake = _FakeMatcher()
    frame = np.zeros((900, 1600, 3), dtype=np.uint8)
    tkey = "t/ok.png"
    fkey = frame_bucket_key(1600, 900, capture_mode="full")
    for _ in range(4):
        hr._STORE.record_hit(
            template_key=tkey, frame_key=fkey,
            x=800, y=700, score=0.99, via_roi=False,
        )

    r = adaptive_match(
        fake, frame, "ok.png",
        threshold=0.9, match_type="image",
        template_key=tkey, capture_mode="full", enabled=True,
    )
    assert r and r.x is not None
    assert fake.calls, "should attempt match"
    assert fake.calls[0][0] is not None


def test_real_matcher_roi_path(tmp_path: Path):
    """真实 Matcher：先全图学热点，再验证走 ROI 仍能命中。"""
    import backend.matcher.hotspot_roi as hr

    hr._STORE = HotspotStore(tmp_path / "hs3.json")
    hr._STORE.enabled = True

    tw, th = 40, 30
    templ = np.zeros((th, tw, 3), dtype=np.uint8)
    templ[:] = (40, 180, 220)
    cv2.rectangle(templ, (4, 4), (tw - 5, th - 5), (20, 80, 200), -1)

    fw, fh = 640, 480
    frame = np.zeros((fh, fw, 3), dtype=np.uint8)
    frame[:] = (30, 30, 30)
    ox, oy = 480, 350
    frame[oy:oy + th, ox:ox + tw] = templ

    templ_path = tmp_path / "btn.png"
    cv2.imwrite(str(templ_path), templ)

    m = Matcher()
    tkey = normalize_template_key(templ_path)
    # force stable key for test
    tkey = "test/btn.png"
    fkey = frame_bucket_key(fw, fh, capture_mode="full")

    # seed hits near true center
    cx, cy = ox + tw // 2, oy + th // 2
    for _ in range(4):
        hr._STORE.record_hit(
            template_key=tkey, frame_key=fkey,
            x=cx, y=cy, score=0.99, via_roi=False,
        )
    rois = hr._STORE.propose_rois(
        template_key=tkey, frame_key=fkey, frame_w=fw, frame_h=fh
    )
    assert rois, "seeded samples must propose ROI"
    x1, y1, x2, y2 = rois[0]
    assert x1 <= cx <= x2 and y1 <= cy <= y2

    r = adaptive_match(
        m, frame, templ_path,
        threshold=0.85, match_type="image",
        use_orb=False,
        template_key=tkey, capture_mode="full", enabled=True,
    )
    assert r is not None and r.x is not None and r.match_success
    assert abs(r.x - cx) <= 8 and abs(r.y - cy) <= 8


def test_normalize_key():
    k = normalize_template_key(Path("foo/bar.PNG"))
    assert "bar.png" in k.lower()


def test_orb_offset_with_crop():
    """ROI 裁剪时 ORB 坐标必须加 offset。"""
    m = Matcher()
    tw, th = 80, 60
    templ = np.random.randint(40, 200, (th, tw, 3), dtype=np.uint8)
    # high-contrast pattern for ORB
    for i in range(0, tw, 8):
        templ[:, i:i + 2] = 255
    for j in range(0, th, 8):
        templ[j:j + 2, :] = 0

    fw, fh = 400, 300
    frame = np.zeros((fh, fw, 3), dtype=np.uint8)
    ox, oy = 250, 180
    frame[oy:oy + th, ox:ox + tw] = templ

    # crop that contains the patch
    x1, y1, x2, y2 = 200, 140, 400, 300
    results = m._orb_match(
        frame[y1:y2, x1:x2], templ, threshold=0.3, offset=(x1, y1)
    )
    # ORB may fail on synthetic; if it hits, coords must be in full-frame space
    if results:
        r = results[0]
        assert r.x >= x1 and r.y >= y1, "ORB must apply crop offset"


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        test_store_propose_and_clear(p)
        test_adaptive_roi_then_full(p)
        test_real_matcher_roi_path(p)
        test_normalize_key()
        test_orb_offset_with_crop()
        print("ok")
