"""全过程伪录制：时间线事件 + 稀疏关键帧，用于效率分析。

启用方式：
  - 环境变量 MINASHIGO_PSEUDO_RECORD=1
  - 或 browser.enable_pseudo_record(script_name=...)
输出：
  screenshots/pseudo_record/{account}_{ts}_{script}/
    timeline.jsonl   每行一个事件
    summary.json     分类耗时汇总
    summary.txt      人类可读摘要
    frames/          稀疏 JPEG 关键帧（可选）
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from core.path import PROJECT_ROOT

PSEUDO_RECORD_ROOT = PROJECT_ROOT / "screenshots" / "pseudo_record"
ENV_FLAG = "MINASHIGO_PSEUDO_RECORD"


def env_pseudo_record_enabled() -> bool:
    v = (os.getenv(ENV_FLAG) or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def analyze_frame_blackness(
    frame,
    *,
    mean_max: float = 22.0,
    dark_ratio_min: float = 0.90,
    dark_thresh: int = 32,
) -> tuple[bool, dict]:
    """判断帧中心区是否「基本黑屏」（忽略顶栏/右下角广告）。

    只用于计时对比，不驱动业务点击。
    """
    try:
        import numpy as np
    except Exception:
        return False, {"error": "numpy missing"}
    if frame is None or not hasattr(frame, "shape"):
        return False, {"error": "no frame"}
    try:
        img = np.asarray(frame)
        if img.ndim == 3:
            # BGR → 灰度近似
            gray = img.mean(axis=2)
        else:
            gray = img.astype(float)
        h, w = gray.shape[:2]
        if h < 8 or w < 8:
            return False, {"error": "too small", "shape": [int(w), int(h)]}
        # 避开 FANZA 顶栏与右下角弹窗
        y0, y1 = int(h * 0.12), int(h * 0.82)
        x0, x1 = int(w * 0.06), int(w * 0.72)
        roi = gray[y0:y1, x0:x1]
        mean = float(roi.mean())
        dark_ratio = float((roi < dark_thresh).mean())
        is_black = mean <= mean_max or dark_ratio >= dark_ratio_min
        return is_black, {
            "mean": round(mean, 2),
            "dark_ratio": round(dark_ratio, 4),
            "roi": [x0, y0, x1, y1],
        }
    except Exception as e:
        return False, {"error": str(e)}


def frame_is_mostly_black(frame, **kwargs) -> bool:
    ok, _ = analyze_frame_blackness(frame, **kwargs)
    return ok


def _safe_name(s: str) -> str:
    s = str(s or "unknown").strip() or "unknown"
    return re.sub(r'[<>:"/\\|?*\s]+', "_", s)[:80]


@dataclass
class _Span:
    kind: str
    name: str
    t0: float
    meta: dict = field(default_factory=dict)


class PseudoRecorder:
    """线程安全的轻量伪录制器（挂在 UserBrowser 上）。"""

    def __init__(
        self,
        *,
        account: str,
        script_name: str,
        save_keyframes: bool = True,
        keyframe_min_interval_s: float = 2.5,
        keyframe_max_side: int = 960,
        keyframe_jpeg_quality: int = 70,
    ):
        self.account = account
        self.script_name = script_name
        self.save_keyframes = save_keyframes
        self.keyframe_min_interval_s = keyframe_min_interval_s
        self.keyframe_max_side = keyframe_max_side
        self.keyframe_jpeg_quality = keyframe_jpeg_quality

        self._lock = threading.Lock()
        self._t0 = time.perf_counter()
        self._wall0 = time.time()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.dir = PSEUDO_RECORD_ROOT / f"{_safe_name(account)}_{ts}_{_safe_name(script_name)}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.frames_dir = self.dir / "frames"
        if save_keyframes:
            self.frames_dir.mkdir(parents=True, exist_ok=True)

        self._timeline_path = self.dir / "timeline.jsonl"
        self._fh = open(self._timeline_path, "a", encoding="utf-8")
        self._n_events = 0
        self._last_keyframe_mono = 0.0
        self._keyframe_i = 0
        self._spans: list[_Span] = []
        self._closed = False
        self._black_on: bool = False
        self._black_since_mono: float | None = None

        self.event(
            "session_start",
            account=account,
            script=script_name,
            wall_time=datetime.now().isoformat(timespec="seconds"),
        )

    @property
    def elapsed_s(self) -> float:
        return time.perf_counter() - self._t0

    def event(self, kind: str, **payload: Any) -> None:
        if self._closed:
            return
        row = {
            "t": round(self.elapsed_s, 4),
            "kind": kind,
            **{k: v for k, v in payload.items() if v is not None},
        }
        line = json.dumps(row, ensure_ascii=False, default=str)
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()
            self._n_events += 1

    def begin(self, kind: str, name: str = "", **meta: Any) -> int:
        """开始一段计时，返回 span id（栈下标）。"""
        sp = _Span(kind=kind, name=name or kind, t0=time.perf_counter(), meta=dict(meta))
        with self._lock:
            self._spans.append(sp)
            return len(self._spans) - 1

    def end(self, span_id: int | None = None, **extra: Any) -> float:
        """结束一段计时并写入事件；返回耗时秒。"""
        with self._lock:
            if not self._spans:
                return 0.0
            if span_id is None or span_id >= len(self._spans):
                sp = self._spans.pop()
            else:
                # 允许非严格嵌套：从末尾找
                sp = self._spans.pop()
        dt = time.perf_counter() - sp.t0
        payload = {**sp.meta, **extra, "name": sp.name, "dt_ms": round(dt * 1000, 1)}
        self.event(sp.kind, **payload)
        return dt

    def maybe_keyframe(self, frame, *, reason: str = "") -> Optional[str]:
        if self._closed or not self.save_keyframes or frame is None:
            return None
        now = time.perf_counter()
        if (now - self._last_keyframe_mono) < self.keyframe_min_interval_s:
            # 关键动作仍允许保存；diag 走间隔，避免等待期刷盘
            if reason not in ("click", "step", "error", "manual"):
                return None
        try:
            import cv2
            import numpy as np

            img = frame
            if not isinstance(img, np.ndarray):
                return None
            h, w = img.shape[:2]
            scale = min(1.0, self.keyframe_max_side / max(h, w))
            if scale < 1.0:
                img = cv2.resize(
                    img,
                    (int(w * scale), int(h * scale)),
                    interpolation=cv2.INTER_AREA,
                )
            self._keyframe_i += 1
            name = f"{self._keyframe_i:04d}_{reason or 'frame'}_{self.elapsed_s:.1f}s.jpg"
            path = self.frames_dir / name
            # Windows 上 cv2.imwrite 遇中文路径常静默失败，改 imencode + 写字节
            ok, buf = cv2.imencode(
                ".jpg",
                img,
                [int(cv2.IMWRITE_JPEG_QUALITY), int(self.keyframe_jpeg_quality)],
            )
            if not ok:
                self.event("keyframe_error", error="imencode failed")
                return None
            path.write_bytes(buf.tobytes())
            self._last_keyframe_mono = now
            rel = f"frames/{name}"
            self.event(
                "keyframe",
                path=rel,
                reason=reason,
                shape=[int(frame.shape[1]), int(frame.shape[0])],
            )
            return rel
        except Exception as e:
            self.event("keyframe_error", error=str(e))
            return None

    def note_black_frame(self, is_black: bool, **metrics: Any) -> Optional[dict]:
        """黑屏状态机：仅边沿写 black_on / black_off。返回边沿信息供打日志。"""
        if self._closed:
            return None
        is_black = bool(is_black)
        if is_black and not self._black_on:
            self._black_on = True
            self._black_since_mono = time.perf_counter()
            self.event("black_on", **metrics)
            return {"edge": "on", **metrics}
        if (not is_black) and self._black_on:
            dt = 0.0
            if self._black_since_mono is not None:
                dt = time.perf_counter() - self._black_since_mono
            self._black_on = False
            self._black_since_mono = None
            payload = {"dt_ms": round(dt * 1000, 1), **metrics}
            self.event("black_off", **payload)
            return {"edge": "off", "dt_s": round(dt, 2), **metrics}
        return None

    def finish(self, *, status: str = "ok") -> Path:
        if self._closed:
            return self.dir
        # 收尾未闭合黑屏段
        if self._black_on:
            self.note_black_frame(False, truncated=True)
        # 未闭合 span 全部冲掉
        while True:
            with self._lock:
                if not self._spans:
                    break
                sp = self._spans.pop()
            dt = time.perf_counter() - sp.t0
            self.event(
                sp.kind,
                name=sp.name,
                dt_ms=round(dt * 1000, 1),
                **sp.meta,
                truncated=True,
            )

        total = self.elapsed_s
        self.event("session_end", status=status, total_s=round(total, 3))
        summary = build_summary(self._timeline_path, total_s=total)
        (self.dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (self.dir / "summary.txt").write_text(
            format_summary_text(summary) + "\n",
            encoding="utf-8",
        )
        with self._lock:
            try:
                self._fh.close()
            except Exception:
                pass
            self._closed = True
        return self.dir


def build_summary(timeline_path: Path, *, total_s: float | None = None) -> dict:
    events: list[dict] = []
    if timeline_path.is_file():
        for line in timeline_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except Exception:
                continue

    if total_s is None and events:
        total_s = float(events[-1].get("t") or 0.0)
    total_s = float(total_s or 0.0)

    by_kind: dict[str, dict] = {}
    match_fail = 0
    match_ok = 0
    sleep_s = 0.0
    capture_s = 0.0
    match_s = 0.0
    click_n = 0
    long_sleeps: list[dict] = []
    long_matches: list[dict] = []
    log_marks: list[dict] = []

    for e in events:
        kind = str(e.get("kind") or "")
        bucket = by_kind.setdefault(kind, {"count": 0, "dt_ms": 0.0})
        bucket["count"] += 1
        dt_ms = float(e.get("dt_ms") or 0.0)
        bucket["dt_ms"] += dt_ms

        if kind == "sleep":
            sleep_s += dt_ms / 1000.0
            if dt_ms >= 1500:
                long_sleeps.append(e)
        elif kind == "capture":
            capture_s += dt_ms / 1000.0
        elif kind == "match":
            match_s += dt_ms / 1000.0
            if e.get("ok"):
                match_ok += 1
            else:
                match_fail += 1
            if dt_ms >= 400:
                long_matches.append(e)
        elif kind in ("click", "click_image"):
            click_n += 1
        elif kind == "log":
            log_marks.append({"t": e.get("t"), "msg": e.get("msg")})

    # 相邻 log 之间的墙钟间隔 → 找「黑段」
    gaps: list[dict] = []
    for a, b in zip(log_marks, log_marks[1:]):
        try:
            dt = float(b["t"]) - float(a["t"])
        except Exception:
            continue
        if dt >= 8.0:
            gaps.append(
                {
                    "dt_s": round(dt, 2),
                    "from": a.get("msg"),
                    "to": b.get("msg"),
                    "t0": a.get("t"),
                    "t1": b.get("t"),
                }
            )
    gaps.sort(key=lambda x: -x["dt_s"])

    # 黑屏段：black_on → black_off（dt_ms）累加
    black_s = 0.0
    black_spans: list[dict] = []
    pending_on: dict | None = None
    for e in events:
        kind = str(e.get("kind") or "")
        if kind == "black_on":
            pending_on = e
        elif kind == "black_off":
            dt = float(e.get("dt_ms") or 0.0) / 1000.0
            black_s += dt
            black_spans.append(
                {
                    "t0": (pending_on or {}).get("t"),
                    "t1": e.get("t"),
                    "dt_s": round(dt, 2),
                }
            )
            pending_on = None

    effective_s = max(0.0, total_s - black_s)
    accounted = sleep_s + capture_s + match_s
    return {
        "total_s": round(total_s, 2),
        "black_s": round(black_s, 2),
        "effective_s": round(effective_s, 2),
        "event_count": len(events),
        "by_kind": {
            k: {
                "count": v["count"],
                "dt_s": round(v["dt_ms"] / 1000.0, 2),
            }
            for k, v in sorted(by_kind.items(), key=lambda kv: -kv[1]["dt_ms"])
        },
        "totals": {
            "sleep_s": round(sleep_s, 2),
            "capture_s": round(capture_s, 2),
            "match_s": round(match_s, 2),
            "accounted_s": round(accounted, 2),
            "unaccounted_s": round(max(0.0, total_s - accounted), 2),
            "match_ok": match_ok,
            "match_fail": match_fail,
            "clicks": click_n,
            "black_s": round(black_s, 2),
            "effective_s": round(effective_s, 2),
        },
        "black_spans": black_spans[:20],
        "top_log_gaps": gaps[:15],
        "long_sleeps": long_sleeps[:20],
        "long_matches": long_matches[:20],
    }


def format_summary_text(summary: dict) -> str:
    lines = []
    lines.append(
        f"伪录制摘要  total={summary.get('total_s')}s  "
        f"black={summary.get('black_s')}s  "
        f"effective={summary.get('effective_s')}s  "
        f"events={summary.get('event_count')}"
    )
    tot = summary.get("totals") or {}
    lines.append(
        "分类: "
        f"sleep={tot.get('sleep_s')}s  "
        f"capture={tot.get('capture_s')}s  "
        f"match={tot.get('match_s')}s  "
        f"unaccounted={tot.get('unaccounted_s')}s  "
        f"match_ok/fail={tot.get('match_ok')}/{tot.get('match_fail')}  "
        f"clicks={tot.get('clicks')}"
    )
    lines.append("")
    lines.append("黑屏段（已从 effective 剔除）:")
    spans = summary.get("black_spans") or []
    if not spans:
        lines.append("  （无）")
    for sp in spans:
        lines.append(
            f"  +{sp.get('dt_s'):6.1f}s  [{sp.get('t0')}→{sp.get('t1')}]"
        )
    lines.append("")
    lines.append("by_kind:")
    for k, v in (summary.get("by_kind") or {}).items():
        lines.append(f"  {k:16s} n={v['count']:5d}  dt={v['dt_s']:8.2f}s")
    lines.append("")
    lines.append("最长 log 间隔（疑似空等/战斗）:")
    for g in summary.get("top_log_gaps") or []:
        lines.append(
            f"  +{g['dt_s']:6.1f}s  [{g.get('t0')}→{g.get('t1')}]  "
            f"{g.get('from')}  =>  {g.get('to')}"
        )
    lines.append("")
    lines.append("长 sleep (≥1.5s):")
    for e in summary.get("long_sleeps") or []:
        lines.append(
            f"  t={e.get('t')}  {e.get('dt_ms')}ms  "
            f"planned={e.get('planned_s')}"
        )
    return "\n".join(lines)


def summarize_dir(path: str | Path) -> dict:
    p = Path(path)
    timeline = p / "timeline.jsonl" if p.is_dir() else p
    summary = build_summary(timeline)
    if p.is_dir():
        (p / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (p / "summary.txt").write_text(format_summary_text(summary) + "\n", encoding="utf-8")
    return summary


if __name__ == "__main__":
    import sys

    target = Path(sys.argv[1]) if len(sys.argv) > 1 else PSEUDO_RECORD_ROOT
    if target.is_dir() and (target / "timeline.jsonl").is_file():
        s = summarize_dir(target)
        print(format_summary_text(s))
    elif target.is_dir():
        # 最新一场
        subs = sorted(
            [d for d in target.iterdir() if d.is_dir() and (d / "timeline.jsonl").is_file()],
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
        if not subs:
            print("no recordings under", target)
            raise SystemExit(1)
        s = summarize_dir(subs[0])
        print("dir:", subs[0])
        print(format_summary_text(s))
    else:
        print("usage: python -m backend.automation.run_recorder [record_dir]")
        raise SystemExit(2)
