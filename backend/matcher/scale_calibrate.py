"""模板尺度自动定标与缓存。

不依赖固定分辨率 / 宽高比：按 (template_key, frame_w×frame_h, capture_mode)
缓存最佳尺度 s*；未命中时对数粗搜 + 峰值附近细搜。
"""

from __future__ import annotations

import inspect
import math
import threading
import time
import weakref
from dataclasses import dataclass
from typing import Optional, Union

import cv2
import numpy as np

# 连续未达阈值次数达到此值则作废缓存并重定标
MISS_RECALIBRATE_AFTER = 3

# 定标得分低于此值视为「没看到目标」：缓存默认尺度，但允许限流的宽搜兜底
WEAK_CACHE_SCORE = 0.25
# 同一 key 的重定标 / 宽搜冷却（秒）：未命中不等于尺度错，别每次都全搜
RECALIBRATE_COOLDOWN_S = 15.0
WIDE_SEARCH_COOLDOWN_S = 8.0
# 连续宽搜未命中的退避：8s → 30s → 120s（封顶）
WIDE_BACKOFF_BASE = 3.75
WIDE_BACKOFF_MAX_STEPS = 2

NARROW_BAND = (0.97, 1.0, 1.03)
COARSE_MIN = 0.30
COARSE_MAX = 1.80
COARSE_STEPS = 16
FINE_SPAN = 0.10
FINE_STEPS = 11


@dataclass
class ScaleEntry:
    scale: float
    score: float
    misses: int = 0
    hits: int = 0
    weak: bool = False
    calibrated_at: float = 0.0
    # 这个 key 一共真跑过几次 27 步定标。**这是"到底有没有定标风暴"的唯一可信证据**：
    # 日志可能被重复播报（踩过这个坑），但计数器不会。
    calibrations: int = 0


def cache_key(
    template_key: str,
    frame_w: int,
    frame_h: int,
    capture_mode: str = "full",
) -> str:
    return f"{template_key}|{int(frame_w)}x{int(frame_h)}|{capture_mode or 'full'}"


def narrow_scales(scale: float, band: tuple[float, ...] = NARROW_BAND) -> list[float]:
    s = float(scale)
    if s <= 0:
        return [1.0]
    return [s * f for f in band if s * f > 0]


def _logspace(lo: float, hi: float, n: int) -> list[float]:
    if n <= 1:
        return [float(lo)]
    lo = max(1e-3, float(lo))
    hi = max(lo, float(hi))
    return [float(math.exp(math.log(lo) + i * (math.log(hi) - math.log(lo)) / (n - 1))) for i in range(n)]


def _peak_at_scale(frame_gray: np.ndarray, templ_gray: np.ndarray, scale: float) -> float:
    th, tw = templ_gray.shape[:2]
    nh = max(1, int(round(th * scale)))
    nw = max(1, int(round(tw * scale)))
    fh, fw = frame_gray.shape[:2]
    if nh >= fh or nw >= fw or nh < 4 or nw < 4:
        return -1.0
    resized = cv2.resize(templ_gray, (nw, nh), interpolation=cv2.INTER_AREA)
    res = cv2.matchTemplate(frame_gray, resized, cv2.TM_CCOEFF_NORMED)
    return float(cv2.minMaxLoc(res)[1])


def calibrate_scale(
    frame_bgr: np.ndarray,
    templ_bgr: np.ndarray,
    *,
    s_min: float = COARSE_MIN,
    s_max: float = COARSE_MAX,
    coarse_steps: int = COARSE_STEPS,
    fine_span: float = FINE_SPAN,
    fine_steps: int = FINE_STEPS,
    coarse_downscale: float = 0.5,
) -> tuple[Optional[float], float]:
    """粗搜 + 细搜，返回 (best_scale, best_score)。失败返回 (None, 0)。

    粗搜在降采样帧上做（相对尺度不变，成本降到 1/k²），细搜仍在原分辨率上做，
    精度不受影响。
    """
    if frame_bgr is None or templ_bgr is None:
        return None, 0.0
    frame_gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    templ_gray = cv2.cvtColor(templ_bgr, cv2.COLOR_BGR2GRAY)

    coarse_frame, coarse_templ = frame_gray, templ_gray
    k = float(coarse_downscale or 1.0)
    if 0.0 < k < 0.999 and min(templ_gray.shape[:2]) * k >= 4:
        coarse_frame = cv2.resize(
            frame_gray, None, fx=k, fy=k, interpolation=cv2.INTER_AREA
        )
        coarse_templ = cv2.resize(
            templ_gray, None, fx=k, fy=k, interpolation=cv2.INTER_AREA
        )

    best_s: Optional[float] = None
    best_score = -1.0
    for s in _logspace(s_min, s_max, coarse_steps):
        sc = _peak_at_scale(coarse_frame, coarse_templ, s)
        if sc > best_score:
            best_score = sc
            best_s = s

    if best_s is None or best_score < 0:
        return None, 0.0

    lo = max(s_min, best_s * (1.0 - fine_span))
    hi = min(s_max, best_s * (1.0 + fine_span))
    for s in _logspace(lo, hi, fine_steps):
        sc = _peak_at_scale(frame_gray, templ_gray, s)
        if sc > best_score:
            best_score = sc
            best_s = s

    return best_s, float(best_score)


class ScaleCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, ScaleEntry] = {}
        self._wide_at: dict[str, float] = {}
        self._wide_miss: dict[str, int] = {}

    def get(self, key: str) -> Optional[ScaleEntry]:
        with self._lock:
            return self._entries.get(key)

    def set(self, key: str, scale: float, score: float, *, weak: bool = False) -> None:
        with self._lock:
            prev = self._entries.get(key)
            self._entries[key] = ScaleEntry(
                scale=float(scale),
                score=float(score),
                misses=0,
                hits=0,
                weak=bool(weak),
                calibrated_at=time.time(),
                # 累计次数要保留：否则计数器会被每次重定标清零，就失去意义了
                calibrations=(prev.calibrations + 1) if prev is not None else 1,
            )

    def calibration_count(self, key: str) -> int:
        with self._lock:
            ent = self._entries.get(key)
            return int(ent.calibrations) if ent is not None else 0

    def totals(self) -> dict:
        """{key: 定标次数}，只列真的定标过的（自检/排查用）。"""
        with self._lock:
            return {k: int(v.calibrations) for k, v in self._entries.items() if v.calibrations}

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def note_miss(self, key: str) -> bool:
        """记录一次未达阈值；是否应重定标。

        未命中大多只是「目标还没出现」，不是尺度错了：
        只有**曾经命中过**、且连续未命中够多、且已过冷却期，才值得重定标。
        """
        with self._lock:
            ent = self._entries.get(key)
            if ent is None:
                return True
            ent.misses += 1
            if ent.hits <= 0:
                return False  # 从未命中：目标不在画面上，重定标没意义
            if ent.misses < MISS_RECALIBRATE_AFTER:
                return False
            if (time.time() - float(ent.calibrated_at or 0.0)) < RECALIBRATE_COOLDOWN_S:
                return False
            self._entries.pop(key, None)
            return True

    def note_hit(self, key: str) -> None:
        with self._lock:
            ent = self._entries.get(key)
            if ent is not None:
                ent.misses = 0
                ent.hits += 1

    def allow_wide(self, key: str, *, cooldown_s: float = WIDE_SEARCH_COOLDOWN_S) -> bool:
        """宽搜（10 个尺度）限流：同一 key 每 cooldown_s 最多一次。

        连续宽搜都没找到目标时**指数退避**：宽搜很贵（实测 1730x974 上一次
        1.5~1.8s，是窄带未命中的 3~4 倍），而「连续几次全尺度都没有」基本就
        说明目标不在画面上（过场/战斗帧），再把冷却固定在 8s 只会每 8 秒白花
        1.5s。真命中时由 `note_hit` 立刻解除退避。
        """
        now = time.time()
        with self._lock:
            misses = self._wide_miss.get(key, 0)
            cd = float(cooldown_s) * (WIDE_BACKOFF_BASE ** min(misses, WIDE_BACKOFF_MAX_STEPS))
            last = self._wide_at.get(key)
            if last is not None and (now - float(last)) < cd:
                return False
            self._wide_at[key] = now
            return True

    def note_wide_result(self, key: str, *, found: bool) -> None:
        """宽搜结果回报：没找到就加一级退避，找到了就立刻复位。"""
        with self._lock:
            if found:
                self._wide_miss.pop(key, None)
            else:
                self._wide_miss[key] = min(
                    self._wide_miss.get(key, 0) + 1, WIDE_BACKOFF_MAX_STEPS
                )

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._wide_at.clear()
            self._wide_miss.clear()

    def clear_frame_size(self, frame_w: int, frame_h: int) -> None:
        token = f"|{int(frame_w)}x{int(frame_h)}|"
        with self._lock:
            drop = [k for k in self._entries if token in k]
            for k in drop:
                del self._entries[k]
            for k in [k for k in self._wide_at if token in k]:
                del self._wide_at[k]
            for k in [k for k in self._wide_miss if token in k]:
                del self._wide_miss[k]


_CACHE = ScaleCache()


def get_scale_cache() -> ScaleCache:
    return _CACHE


# ── 冷定标日志出口 ───────────────────────────────────────────────────
# matcher 层不持有 GUI/脚本上下文，所以这里只放一组"日志输出槽"，
# 由 UserWindow 登记 (账号名, script_log)。
#
# ⚠️ 槽里**必须带账号名**：不能向所有槽广播。双开时两个窗口各登记一个槽，
# 广播会让同一条冷定标被播报两次、且分别挂到两个账号名下（实测
# 20260921_024328 那次运行里每个 `[#N]` 都出现两行、账号不同）。
# 冷定标是全局共享缓存触发的，但"这次定标是谁引起的"要落到对应账号的日志里。
_LOG_SINKS: dict = {}
# 槽 -> 归属账号（用于把定标消息只发给引起它的那个账号）
_SINK_ACCOUNT: dict = {}
_SINK_LOCK = threading.Lock()

# 冷定标日志的洪泛保护：同一模板 + 帧尺寸每秒最多播报一条，被压掉的计数
# 在下一条里补报。加这个是因为真出过一次"一条日志被播报几百次"，
# 直接把 GUI 日志刷爆 —— 日志本身不该有能力拖慢主流程。
NOTE_MIN_INTERVAL_S = 1.0
_NOTE_LAST: dict = {}
_NOTE_SUPPRESSED: dict = {}
# 冷定标序号：只增不减。日志里序号该是 1,2,3…；如果同一序号出现很多行，
# 就说明是"一条消息被多个日志槽重复播报"，而不是真的定标了很多次。
_CALIB_SEQ = [0]


def _sink_key(fn):
    """给一个日志槽算稳定去重键。

    绑定方法每次取属性都是**新对象**，`weakref.WeakMethod` 每次比较也不相等，
    所以不能拿对象身份去重 —— 第一版就是这么错的：`UserWindow.script_log`
    每次被调用都登记一次自己，同一个方法被登记了几百遍，一条冷定标日志
    就被广播几百次（用户那次运行 15 秒里刷了 1057 条，看起来像卡死）。
    这里用 (实例 id, 函数名) 当键。
    """
    owner = getattr(fn, "__self__", None)
    name = getattr(fn, "__name__", None) or repr(fn)
    if owner is not None:
        return (id(owner), name)
    return (id(fn), name)


def _alive_sinks() -> list:
    """取出仍存活的 ``(key, account, fn)``；顺带清掉实例已没了的条目。"""
    out = []
    dead = []
    for key, ref in list(_LOG_SINKS.items()):
        try:
            fn = ref() if isinstance(ref, weakref.WeakMethod) else ref
        except Exception:
            fn = None
        if fn is None:
            dead.append(key)
            continue
        account = _SINK_ACCOUNT.get(key, "")
        out.append((key, account, fn))
    if dead:
        with _SINK_LOCK:
            for key in dead:
                _LOG_SINKS.pop(key, None)
                _SINK_ACCOUNT.pop(key, None)
    return out


def register_cold_calibration_sink(fn, account: str = "") -> None:
    """登记一个日志输出槽（`fn(str)`）及其归属账号；同一个槽重复登记只生效一次。"""
    if fn is None:
        return
    try:
        ref = weakref.WeakMethod(fn) if inspect.ismethod(fn) else fn
    except Exception:
        ref = fn
    key = _sink_key(fn)
    with _SINK_LOCK:
        _LOG_SINKS[key] = ref
        if account:
            _SINK_ACCOUNT[key] = str(account)


def unregister_cold_calibration_sink(fn) -> None:
    key = _sink_key(fn)
    with _SINK_LOCK:
        _LOG_SINKS.pop(key, None)
        _SINK_ACCOUNT.pop(key, None)


def cold_calibration_sink_count() -> int:
    """当前登记的日志槽数量（自检用：正常应该等于"正在跑的窗口数"，个位数）。"""
    return len(_LOG_SINKS)


def _note_cold_calibration(msg: str, account: str = "") -> None:
    print(msg)
    sinks = _alive_sinks()
    if account and len(sinks) > 1:
        # 只发给**引起这次定标**的那个账号的槽。
        # 双开时向所有槽广播会让同一条定标在两份日志里各出现一次、
        # 还各挂一个账号名（实测 20260921_024328：每个 `[#N]` 都是两行）。
        mine = [s for s in sinks if s[1] == account]
        if mine:
            sinks = mine
    for _key, _acct, fn in sinks:
        try:
            fn(msg)
        except Exception:
            pass


# 全局静音。用户报过"脚本停了冷标定还在刷"：任务结束后
# `TaskController` 并不会注销日志槽，而 `adaptive_match` 有可能还留在
# `asyncio.to_thread` 的线程里跑完（协程被 cancel 了，线程取消不掉），
# 于是任务停了以后仍在往 GUI 日志吐消息。
# 所以除了注销槽，再加一道总闸：没有任务在跑就直接不出声。
_MUTED = [False]


def set_cold_calibration_muted(muted: bool) -> None:
    _MUTED[0] = bool(muted)


def is_cold_calibration_muted() -> bool:
    return bool(_MUTED[0])


def _note_cold_calibration_rate_limited(key: str, msg: str, account: str = "") -> None:
    """按 key 限流的冷定标播报（被压掉的条数在下一条里补报）。"""
    if _MUTED[0]:
        return
    now = time.time()
    with _SINK_LOCK:
        last = _NOTE_LAST.get(key)
        if last is not None and (now - last) < NOTE_MIN_INTERVAL_S:
            _NOTE_SUPPRESSED[key] = _NOTE_SUPPRESSED.get(key, 0) + 1
            return
        skipped = _NOTE_SUPPRESSED.pop(key, 0)
        _NOTE_LAST[key] = now
        n_sinks = len(_LOG_SINKS)
    if skipped:
        msg = f"{msg}  （另有 {skipped} 次同类定标被限流折叠）"
    # 带上序号和槽数：这两个数是排查"日志被重复播报"的关键证据。
    # 真出过一次「日志槽泄漏 → 一条定标刷几百行、GUI 看着像卡死」，
    # 有这两个数一眼就能看出来（序号该 +1，槽数该是个位数且稳定）。
    _CALIB_SEQ[0] += 1
    _note_cold_calibration(f"{msg}  [#{_CALIB_SEQ[0]} 槽={n_sinks}]", account=account)


def resolve_scale_scales(
    *,
    matcher,
    frame,
    template: Union[str, object],
    template_key: str,
    frame_w: int,
    frame_h: int,
    capture_mode: str = "full",
    force_recalibrate: bool = False,
    log_account: str = "",
) -> tuple[Optional[float], Optional[list[float]], str]:
    """
    解析本帧应使用的尺度。
    返回 (scale_hint, scales, source)；source ∈ match_ref|cache|calibrate|wide。

    ``log_account``：这次匹配属于哪个账号，只用来把冷定标日志落进对的那份日志。
    """
    from backend.matcher.match_ref import resolve_scale_for_frame, scales_from_plan

    # 可选快路径：素材目录提供了 match_ref（强制重定标时跳过）
    if not force_recalibrate:
        plan = resolve_scale_for_frame(template, frame_w, frame_h)
        if plan is not None:
            return plan.scale, scales_from_plan(plan), "match_ref"

    key = cache_key(template_key, frame_w, frame_h, capture_mode)
    cache = get_scale_cache()

    if not force_recalibrate:
        ent = cache.get(key)
        if ent is not None and ent.scale > 0:
            return ent.scale, narrow_scales(ent.scale), "cache"

    templ, _ = matcher._load_template_cached(template)
    _t0 = time.perf_counter()
    best_s, best_sc = calibrate_scale(frame, templ)
    _ms = (time.perf_counter() - _t0) * 1000.0
    # 冷定标是本仓库最贵的单次匹配动作（实测 1730x974 上 6.6s、1158x651 上 1.4s，
    # 而热窄带未命中只要 0.13~0.46s）。打一条带耗时的日志，便于事后一眼看出
    # "这次慢是不是又在重定标"，而不是靠猜。
    _note_cold_calibration_rate_limited(
        key,
        f"[vision] 冷定标 {template_key} @ {frame_w}x{frame_h} "
        f"耗时 {_ms:.0f}ms → s={best_s if best_s is not None else float('nan'):.4f} "
        f"score={float(best_sc or 0.0):.4f}",
        account=log_account,
    )
    if best_s is not None and best_sc >= WEAK_CACHE_SCORE:
        cache.set(key, best_s, best_sc)
        print(
            f"[scale_calibrate] {template_key} @ {frame_w}x{frame_h} "
            f"s={best_s:.4f} score={best_sc:.4f}"
        )
        return best_s, narrow_scales(best_s), "calibrate"

    # 定标拿不到可信分数（多半是目标还没出现）：缓存默认尺度，别每次重跑 27 步定标；
    # 宽搜交给 adaptive_match 按冷却期限流兜底。
    cache.set(key, 1.0, float(best_sc or 0.0), weak=True)
    return 1.0, narrow_scales(1.0), "weak"

    # 定标拿不到可信分数（多半是目标还没出现）：缓存默认尺度，别每次重跑 27 步定标；
    # 宽搜交给 adaptive_match 按冷却期限流兜底。
    cache.set(key, 1.0, float(best_sc or 0.0), weak=True)
    return 1.0, narrow_scales(1.0), "weak"
