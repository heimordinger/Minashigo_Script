"""Lightweight one-position image comparison for post-click feedback."""

from __future__ import annotations

import math

import cv2
import numpy as np


def effect_match_score(matcher, region: np.ndarray, template) -> float:
    """Compare an already-located region with a template, without searching."""
    if region is None or region.size == 0:
        return 0.0
    _templ, templ_gray = matcher._load_template_cached(template)
    height, width = region.shape[:2]
    if templ_gray.shape[:2] != (height, width):
        templ_gray = cv2.resize(
            templ_gray, (width, height), interpolation=cv2.INTER_AREA,
        )
    gray = (
        region if len(region.shape) == 2
        else cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    )
    score = float(
        cv2.matchTemplate(gray, templ_gray, cv2.TM_CCOEFF_NORMED)[0, 0]
    )
    return score if math.isfinite(score) else 0.0
