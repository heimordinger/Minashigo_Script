"""match_ref（可选）与 scale_calibrate。"""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

from backend.matcher.match_ref import find_match_ref, resolve_scale_for_frame
from backend.matcher.scale_calibrate import (
    calibrate_scale,
    get_scale_cache,
    narrow_scales,
    resolve_scale_scales,
)


def test_match_ref_optional(tmp_path: Path):
    root = tmp_path / "pack"
    root.mkdir()
    tpl = root / "a.png"
    tpl.write_bytes(b"x")
    assert find_match_ref(tpl) is None
    (root / "match_ref.json").write_text(
        json.dumps({"ref_width": 1280, "ref_height": 960}),
        encoding="utf-8",
    )
    from backend.matcher import match_ref as mr

    mr._load_ref_file.cache_clear()
    ref = find_match_ref(tpl)
    assert ref is not None and ref.ref_width == 1280
    plan = resolve_scale_for_frame(tpl, 640, 480)
    assert plan is not None and abs(plan.scale - 0.5) < 1e-6


def test_calibrate_finds_challenge_scale():
    shot_path = Path(
        r"C:\Users\30241\.cursor\projects\f-Minashigo-script\assets"
        r"\c__Users_30241_AppData_Roaming_Cursor_User_workspaceStorage_"
        r"2663938f02b2e6086503f0cb1942200d_images_image-fb996671-b54e-4342-a2d9-a7ce1df0d700.jpg"
    )
    tpl_path = Path(r"f:/Minashigo_script/assets/images/yys/挑战.png")
    if not shot_path.is_file() or not tpl_path.is_file():
        return

    def imread_u(p: Path):
        return cv2.imdecode(np.frombuffer(p.read_bytes(), np.uint8), cv2.IMREAD_COLOR)

    s, sc = calibrate_scale(imread_u(shot_path), imread_u(tpl_path))
    assert s is not None
    assert 0.5 <= s <= 0.75
    assert sc >= 0.85


def test_resolve_scale_scales_caches():
    from backend.matcher.matcher import Matcher

    shot_path = Path(
        r"C:\Users\30241\.cursor\projects\f-Minashigo-script\assets"
        r"\c__Users_30241_AppData_Roaming_Cursor_User_workspaceStorage_"
        r"2663938f02b2e6086503f0cb1942200d_images_image-fb996671-b54e-4342-a2d9-a7ce1df0d700.jpg"
    )
    tpl_path = Path(r"f:/Minashigo_script/assets/images/yys/挑战.png")
    if not shot_path.is_file() or not tpl_path.is_file():
        return

    def imread_u(p: Path):
        return cv2.imdecode(np.frombuffer(p.read_bytes(), np.uint8), cv2.IMREAD_COLOR)

    shot = imread_u(shot_path)
    cache = get_scale_cache()
    cache.clear()
    m = Matcher()
    h, w = shot.shape[:2]
    s1, band1, src1 = resolve_scale_scales(
        matcher=m,
        frame=shot,
        template=str(tpl_path),
        template_key="yys/挑战",
        frame_w=w,
        frame_h=h,
    )
    assert src1 == "calibrate" and s1 is not None
    assert band1 == narrow_scales(s1)
    s2, _band2, src2 = resolve_scale_scales(
        matcher=m,
        frame=shot,
        template=str(tpl_path),
        template_key="yys/挑战",
        frame_w=w,
        frame_h=h,
    )
    assert src2 == "cache" and abs(s2 - s1) < 1e-9
    cache.clear()
