"""第二层B：统一视觉证据调度（设计 §5，Phase B2）。

设计原文（§5.1/§5.3/§5.5–§5.8）要点：

- **按证据缺口调用**，不是"每个主轮自动识图"：下一步依赖图片事实、且 Runtime 没有
  兼容证据时才调用视觉；
- 顺序固定：**查缓存 → 本地 CV 够用就先用本地 CV → 主逻辑模型视觉 → 不可用/结果
  不合格才用辅助识图模型 → 最多一次针对性复核**；
- 缓存键是**指纹**（content_hash + mark_revision + 封闭 question_kind + 归一化目标 +
  区域 + 视觉协议版本 + 模型/能力版本），**不用整句自然语言问题**；主轮变化本身
  不使证据失效；
- 跨主轮证据落在 V3 ``evidence`` 表（L2 权威源），V2 ``session.artifacts["vision_cache"]``
  只是 L1；先写 L2 再写 L1，失效先改 L2 再清 L1；
- 预算：每个新证据缺口默认 1 次新调用，允许 1 次针对性复核，自动上限 2 次，
  用户明确"重新识图/再看一次"额外 +1；缓存命中不计数；到上限就报告已有证据与冲突，
  不再循环调用。

本模块是**中性**的：不 import V2/V3 的任何东西。真实模型调用、本地 CV、L1 缓存与
L2 存储都以**适配器**注入（见 :class:`VisionBackend`、:class:`LocalCvProbe`、
:class:`L1Cache`、:class:`EvidenceStore`）。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol, Sequence

# ── 诊断码（稳定，不随措辞变化）────────────────────────────────────────

#: 闸门：没有兼容的视觉证据就不许下"图片事实"结论 / 让依赖图片的逻辑草稿过关 / 交付。
VISUAL_EVIDENCE_REQUIRED = "VISUAL_EVIDENCE_REQUIRED"
#: 预算用尽（含针对性复核）：必须报告已有证据与冲突，不许继续循环调用。
VISUAL_BUDGET_EXHAUSTED = "VISUAL_BUDGET_EXHAUSTED"
#: 视觉输出结构不合格（缺尺寸/缺候选/坐标越界…），只能作为"未通过"的证据。
VISUAL_RESULT_UNSTRUCTURED = "VISUAL_RESULT_UNSTRUCTURED"
#: 视觉自报低置信（有结构化结果但不足以支撑结论）。
VISUAL_RESULT_LOW_CONFIDENCE = "VISUAL_RESULT_LOW_CONFIDENCE"
#: 视觉结论与本地 CV 冲突 → 允许**一次**针对性复核。
VISUAL_RESULT_CONFLICTS_LOCAL_CV = "VISUAL_RESULT_CONFLICTS_LOCAL_CV"

VISUAL_DIAGNOSTIC_CODES: tuple[str, ...] = (
    VISUAL_EVIDENCE_REQUIRED,
    VISUAL_BUDGET_EXHAUSTED,
    VISUAL_RESULT_UNSTRUCTURED,
    VISUAL_RESULT_LOW_CONFIDENCE,
    VISUAL_RESULT_CONFLICTS_LOCAL_CV,
)

#: 证据来源（遥测用）。
SOURCE_CACHE = "cache"
SOURCE_LOCAL_CV = "local_cv"
SOURCE_PRIMARY = "primary"
SOURCE_ASSIST = "assist"

#: 视觉协议版本：任何会改变"请求语义或输出语义"的改动都要 +1（进指纹）。
VISION_PROTOCOL_VERSION = "vision.protocol.v1"

#: 实际送入图片的最长边（与 V2 ``_VISION_MAX_SIDE`` 同一口径）。
VISION_MAX_SIDE = 1280

#: 低置信阈值：结构化结果的最高候选置信度低于它就算"不足以支撑结论"。
LOW_CONFIDENCE = 0.5

#: 自动视觉调用上限（每个主轮）：覆盖原图、参考模板与最终产物复核。
# 标图工作流的常见最小闭环是：原图 + 两张参考模板 + 最终成图复核。
# 旧值 2 会与 Runtime 的强制证据闸门形成必然死锁；仍由缓存、同素材复用和
# 主轮级预算限制防止空转，不把视觉调用放成无限。
AUTO_CALL_LIMIT = 4
#: 用户明确要求"重新识图/再看一次"时可额外增加的一次。
USER_EXTRA_CALLS = 1

# ── 封闭 question_kind（设计 §5.9）────────────────────────────────────

QUESTION_OBJECT_PRESENCE = "object_presence"
QUESTION_OBJECT_COUNT = "object_count"
QUESTION_OBJECT_LOCATION = "object_location"
QUESTION_LAYOUT = "layout"
QUESTION_APPEARANCE = "appearance"
QUESTION_DIFFERENCE = "difference"
QUESTION_TEMPLATE_SUITABILITY = "template_suitability"
QUESTION_REGION_CONTENT = "region_content"

#: 封闭集合：不在这八类里的问题**不许**建缓存键（宁可每次都重新取证）。
QUESTION_KINDS: tuple[str, ...] = (
    QUESTION_OBJECT_PRESENCE,
    QUESTION_OBJECT_COUNT,
    QUESTION_OBJECT_LOCATION,
    QUESTION_LAYOUT,
    QUESTION_APPEARANCE,
    QUESTION_DIFFERENCE,
    QUESTION_TEMPLATE_SUITABILITY,
    QUESTION_REGION_CONTENT,
)

#: 一份证据能回答的**问题类型**（兼容性）。同类型永远兼容；跨类型只有在语义上
#: 真的蕴含时才兼容（例如"逐目标框"也证明了"存在"）。
_COVERS: dict[str, frozenset[str]] = {
    QUESTION_OBJECT_PRESENCE: frozenset({QUESTION_OBJECT_PRESENCE}),
    # 总数证据**不**蕴含"在哪"或"每个区域是什么" —— 那正是"只补缺失部分"的场合。
    QUESTION_OBJECT_COUNT: frozenset({QUESTION_OBJECT_COUNT, QUESTION_OBJECT_PRESENCE}),
    # 逐目标坐标天然包含存在性与数量（候选完整性由结构化结果保证）。
    QUESTION_OBJECT_LOCATION: frozenset(
        {QUESTION_OBJECT_LOCATION, QUESTION_OBJECT_PRESENCE, QUESTION_OBJECT_COUNT}
    ),
    QUESTION_LAYOUT: frozenset({QUESTION_LAYOUT, QUESTION_APPEARANCE}),
    QUESTION_APPEARANCE: frozenset({QUESTION_APPEARANCE}),
    QUESTION_DIFFERENCE: frozenset({QUESTION_DIFFERENCE}),
    # "这块能不能当模板"包含外观判断。
    QUESTION_TEMPLATE_SUITABILITY: frozenset(
        {QUESTION_TEMPLATE_SUITABILITY, QUESTION_APPEARANCE}
    ),
    QUESTION_REGION_CONTENT: frozenset({QUESTION_REGION_CONTENT}),
}

#: 关键词 → question_kind（**只用来把自然语言归到封闭类型**，绝不进指纹）。
_KIND_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (QUESTION_OBJECT_COUNT, ("多少", "几个", "数量", "总数", "count", "how many")),
    (QUESTION_OBJECT_LOCATION, ("哪里", "在哪", "位置", "坐标", "定位", "where", "locate")),
    (QUESTION_DIFFERENCE, ("差异", "区别", "不同", "不一样", "difference", "compare")),
    (QUESTION_TEMPLATE_SUITABILITY, ("模板", "能不能识别", "适合", "稳定性", "template")),
    (QUESTION_LAYOUT, ("布局", "排列", "排布", "结构", "layout", "arrangement")),
    (QUESTION_APPEARANCE, ("外观", "颜色", "样式", "长什么样", "appearance", "look")),
    (QUESTION_OBJECT_PRESENCE, ("有没有", "是否", "存在", "presence", "whether")),
    (QUESTION_REGION_CONTENT, ("这块", "区域", "裁剪", "选区", "region", "crop")),
)


class UnknownQuestionKind(ValueError):
    """问题类型不在封闭集合里。"""


def question_kind_from(*candidates: Any, mode: str = "", default: str = QUESTION_APPEARANCE) -> str:
    """把（可选的显式声明 + 模式 + 自然语言线索）归到一个**封闭** question_kind。

    - 任一候选本身已经是合法的 question_kind → 直接用它；
    - ``mode == "verify"``（裁剪/区域复核）优先归到 ``region_content``；
      只有明确问"能不能当模板"时才归到 ``template_suitability``；
    - 否则按关键词表归类；
    - 都归不出来 → ``default``（通用外观描述）。
    """
    for item in candidates:
        text = str(item or "").strip()
        if text in QUESTION_KINDS:
            return text
    blob = " ".join(str(x or "") for x in candidates).lower()
    if str(mode or "").strip().lower() == "verify":
        if any(word in blob for word in ("模板", "template")):
            return QUESTION_TEMPLATE_SUITABILITY
        return QUESTION_REGION_CONTENT
    for kind, keywords in _KIND_PATTERNS:
        if any(word in blob for word in keywords):
            return kind
    return default


def covers(stored_kind: str, wanted_kind: str) -> bool:
    """存下的证据类型能不能回答 ``wanted_kind``。"""
    return wanted_kind in _COVERS.get(str(stored_kind or ""), frozenset())


# ── 归一化 ────────────────────────────────────────────────────────────

_SPACE_RE = re.compile(r"\s+")


def normalize_target(target: str, *, resource_id: str = "", content_hash: str = "") -> str:
    """归一化"看的是什么"：**资源 id > 内容摘要 > 目标文本**。

    顺序是刻意的（人工要求）：

    - 有资源 id → 用它（同一资源换别名/换路径都算同一份证据）；
    - 没有 id 但有内容摘要 → 用 ``sha:<摘要尾>``：**同内容换路径 = 同一份证据**，
      同路径改了内容 = 新指纹（并因此让旧证据失效）；
    - 都没有 → 退到去路径/空白的稳定短标识（不含整句自然语言）。
    """
    if str(resource_id or "").strip():
        return str(resource_id).strip().lower()
    digest = str(content_hash or "").strip()
    if digest:
        return "sha:" + digest.rsplit(":", 1)[-1][:16]
    text = _SPACE_RE.sub(" ", str(target or "").strip()).lower()
    if not text:
        return "viewport"
    # 路径 → 文件名（同一张图换目录不该换指纹）
    if "/" in text or "\\" in text:
        text = re.split(r"[\\/]", text)[-1]
    return text[:120]


def normalize_region(region: Any) -> tuple[int, int, int, int] | tuple[()]:
    """归一化选区：空 → ``()``；四元组 → 取整的 ``(x0,y0,x1,y1)``。"""
    if region in (None, "", [], ()):
        return ()
    try:
        values = [int(round(float(x))) for x in list(region)[:4]]
    except (TypeError, ValueError):
        return ()
    if len(values) != 4:
        return ()
    x0, y0, x1, y1 = values
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    return (x0, y0, x1, y1)


def marks_revision(marks: Any) -> str:
    """标记/选区的版本：内容变了才变（与主轮、时间无关）。"""
    if not marks:
        return ""
    try:
        blob = json.dumps(marks, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        blob = repr(marks)
    return "mk_" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def fed_size_for(width: int, height: int, *, max_side: int = VISION_MAX_SIDE) -> tuple[int, int]:
    """实际送入模型前等比缩放后的尺寸（**不信模型自报尺寸**，自己算）。"""
    if width <= 0 or height <= 0:
        return 0, 0
    longest = max(int(width), int(height))
    if longest <= max_side:
        return int(width), int(height)
    scale = max_side / float(longest)
    return int(width * scale), int(height * scale)


def content_hash_of(path: str, *, chunk: int = 1024 * 1024) -> str:
    """文件**字节**内容 hash（SHA-256）；读不到返回空串（调用方按"没有 hash"处理）。"""
    try:
        from pathlib import Path

        target = Path(str(path or ""))
        if not target.is_file():
            return ""
        digest = hashlib.sha256()
        with target.open("rb") as stream:
            for block in iter(lambda: stream.read(chunk), b""):
                digest.update(block)
        return "sha256:" + digest.hexdigest()
    except OSError:
        return ""


def resolve_content_hash(
    path: str,
    *,
    bound_hash: str = "",
    hasher: Optional[Callable[[str], str]] = None,
) -> tuple[str, str]:
    """取"这张图的内容摘要"，返回 ``(content_hash, source)``。

    **规则（人工要求）**：

    1. 优先用 Phase B :class:`ResourceBinder` 已经**登记并核验**过的
       ``BoundResource.content_hash`` —— Binder 校验时已经读过字节（不一致会报
       ``RESOURCE_CONTENT_CHANGED``），这里不重复读盘；
    2. 没有绑定资源的临时图片 → **读文件字节**算 SHA-256；
    3. ``(path, mtime, size)`` **不能**充当内容摘要，也不能进指纹代替字节哈希：
       伪造 mtime / 同大小改写都能骗过 stat，所以临时图片一律真读。

    ``source`` 取值 ``bound_resource`` / ``file_bytes`` / ``none``，只作遥测。
    """
    recorded = str(bound_hash or "").strip()
    if recorded:
        return recorded, "bound_resource"
    digest = str((hasher or content_hash_of)(str(path or "")) or "")
    if digest:
        return digest, "file_bytes"
    return "", "none"


@dataclass(frozen=True)
class VisualEvidenceRequest:
    """一次"要不要取视觉证据"的请求（指纹由它算出来）。"""

    question_kind: str = QUESTION_APPEARANCE
    resource_id: str = ""
    content_hash: str = ""
    target: str = ""
    region: Any = ()
    mark_revision: str = ""
    vision_protocol: str = VISION_PROTOCOL_VERSION
    backend_version: str = ""
    #: 自然语言问题：**只发给模型**，不进指纹（要求 §4）。
    question: str = ""
    mode: str = "locate"
    distinguish_from: tuple[str, ...] = ()
    path: str = ""
    origin: str = ""
    #: 上一份证据只覆盖了总数、这次要逐区域明细时，把缺的部分写在这里。
    missing: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        kind = str(self.question_kind or "").strip()
        if kind not in QUESTION_KINDS:
            raise UnknownQuestionKind(kind or "<empty>")
        object.__setattr__(self, "question_kind", kind)
        object.__setattr__(self, "region", normalize_region(self.region))
        object.__setattr__(
            self,
            "distinguish_from",
            tuple(str(x) for x in (self.distinguish_from or ()) if str(x).strip()),
        )

    @property
    def normalized_target(self) -> str:
        return normalize_target(
            self.target, resource_id=self.resource_id, content_hash=self.content_hash
        )

    def fingerprint(self) -> str:
        """证据指纹（设计 §5.5）：**不含**主轮 id、不含自然语言整句。"""
        raw = json.dumps(
            {
                "content_hash": str(self.content_hash or ""),
                "mark_revision": str(self.mark_revision or ""),
                "question_kind": self.question_kind,
                "target": self.normalized_target,
                "region": list(self.region),
                "vision_protocol": str(self.vision_protocol or ""),
                "backend_version": str(self.backend_version or ""),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return "ve_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def to_dict(self) -> dict[str, Any]:
        return {
            "question_kind": self.question_kind,
            "resource_id": self.resource_id,
            "content_hash": self.content_hash,
            "target": self.normalized_target,
            "region": list(self.region),
            "mark_revision": self.mark_revision,
            "vision_protocol": self.vision_protocol,
            "backend_version": self.backend_version,
            "mode": self.mode,
            "origin": self.origin,
        }


@dataclass(frozen=True)
class VisualEvidence:
    """一份可复用的视觉证据（L1 存它的 dict，L2 存 evidence 行）。"""

    fingerprint: str
    question_kind: str
    resource_id: str
    content_hash: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    backend: str = ""
    source: str = ""
    structured: bool = False
    confidence: Optional[float] = None
    status: str = "active"
    evidence_id: str = ""
    created_at: str = ""
    region: tuple[int, int, int, int] | tuple[()] = ()
    mark_revision: str = ""

    def covers(self, question_kind: str) -> bool:
        return covers(self.question_kind, question_kind)

    def covers_region(self, region: Any) -> bool:
        """选区覆盖：空选区（整图）覆盖任何请求；请求空选区要求整图证据。"""
        want = normalize_region(region)
        have = normalize_region(self.region)
        if not want:
            return not have
        if not have:
            return True
        return have[0] <= want[0] and have[1] <= want[1] and have[2] >= want[2] and have[3] >= want[3]

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "question_kind": self.question_kind,
            "resource_id": self.resource_id,
            "content_hash": self.content_hash,
            "payload": dict(self.payload),
            "backend": self.backend,
            "source": self.source,
            "structured": bool(self.structured),
            "confidence": self.confidence,
            "status": self.status,
            "evidence_id": self.evidence_id,
            "created_at": self.created_at,
            "region": list(self.region),
            "mark_revision": self.mark_revision,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "VisualEvidence":
        raw = dict(data or {})
        return cls(
            fingerprint=str(raw.get("fingerprint") or ""),
            question_kind=str(raw.get("question_kind") or ""),
            resource_id=str(raw.get("resource_id") or ""),
            content_hash=str(raw.get("content_hash") or ""),
            payload=dict(raw.get("payload") or {}),
            backend=str(raw.get("backend") or ""),
            source=str(raw.get("source") or ""),
            structured=bool(raw.get("structured")),
            confidence=(
                None if raw.get("confidence") is None else float(raw.get("confidence"))
            ),
            status=str(raw.get("status") or "active"),
            evidence_id=str(raw.get("evidence_id") or ""),
            created_at=str(raw.get("created_at") or ""),
            region=normalize_region(raw.get("region")),
            mark_revision=str(raw.get("mark_revision") or ""),
        )


# ── 适配器接口（V2/V3 各自实现，规则同一份）──────────────────────────

class L1Cache(Protocol):
    """会话级 L1（V2：``session.artifacts["vision_cache"]``）。"""

    def get(self, fingerprint: str) -> Optional[dict[str, Any]]: ...

    def put(self, fingerprint: str, value: Mapping[str, Any]) -> None: ...

    def clear(self, *, fingerprints: Iterable[str] = (), keep: int = 24) -> int: ...


class EvidenceStore(Protocol):
    """跨主轮 L2（V3：``evidence`` 表）。"""

    def find(self, *, fingerprint: str, question_kind: str) -> Optional[dict[str, Any]]: ...

    def put(self, evidence: VisualEvidence, *, turn_id: str = "") -> str: ...

    def invalidate(
        self,
        *,
        resource_id: str = "",
        content_hash: str = "",
        mark_revision: str = "",
        reason: str = "",
    ) -> int: ...


class NullEvidenceStore:
    """没有 V3 store 时的 L2 占位：**不假装有权威源**，一切按"L2 缺失"处理。"""

    label = "none"
    #: 不是权威源 → L1 命中时不做 L2 校验（V2 独立运行时只有 L1 这一层缓存）。
    authoritative = False

    def find(self, *, fingerprint: str, question_kind: str) -> Optional[dict[str, Any]]:
        return None

    def put(self, evidence: VisualEvidence, *, turn_id: str = "") -> str:
        return ""

    def invalidate(self, **kwargs: Any) -> int:
        return 0


class VisionBackend(Protocol):
    """一个视觉后端（主逻辑模型自带视觉 / 辅助识图模型）。"""

    @property
    def label(self) -> str: ...

    @property
    def available(self) -> bool: ...

    def describe(self, request: VisualEvidenceRequest, *, protocol: Mapping[str, Any]) -> Mapping[str, Any]: ...


class LocalCvProbe(Protocol):
    """本地 CV：精确算法够用时**不必**花视觉调用。"""

    def __call__(self, request: VisualEvidenceRequest) -> Optional[Mapping[str, Any]]: ...


# ── 预算 ──────────────────────────────────────────────────────────────


@dataclass
class VisualBudget:
    """每个主轮一份的视觉预算（缓存命中不计数）。"""

    auto_limit: int = AUTO_CALL_LIMIT
    user_extra_limit: int = USER_EXTRA_CALLS
    new_calls: int = 0
    rechecks: int = 0
    user_extra: int = 0
    cache_hits: int = 0

    @property
    def auto_calls(self) -> int:
        return self.new_calls + self.rechecks

    @property
    def total_calls(self) -> int:
        return self.auto_calls + self.user_extra

    def can_call(self, *, targeted: bool = False) -> bool:
        if targeted:
            return self.auto_calls < self.auto_limit and self.rechecks < 1
        return self.auto_calls < self.auto_limit

    def spend(self, *, targeted: bool = False) -> None:
        if targeted:
            self.rechecks += 1
        else:
            self.new_calls += 1

    def grant_user_extra(self, count: int = 1) -> int:
        """用户明确要求"重新识图/再看一次" → 额外放行（最多 ``user_extra_limit`` 次）。"""
        room = max(0, int(self.user_extra_limit) - self.user_extra)
        granted = min(room, max(0, int(count)))
        self.user_extra += granted
        return granted

    def snapshot(self) -> dict[str, int]:
        return {
            "new_calls": self.new_calls,
            "rechecks": self.rechecks,
            "user_extra": self.user_extra,
            "cache_hits": self.cache_hits,
            "auto_calls": self.auto_calls,
            "total_calls": self.total_calls,
            "auto_limit": self.auto_limit,
            "user_extra_limit": self.user_extra_limit,
        }


# ── 结构化结果 ────────────────────────────────────────────────────────


def build_structured_payload(
    parsed: Mapping[str, Any],
    *,
    request: VisualEvidenceRequest,
    source_size: tuple[int, int] = (0, 0),
    fed_size: tuple[int, int] = (0, 0),
) -> dict[str, Any]:
    """把视觉模型的原始 JSON 整理成**结构化证据**（设计 §5.8）。

    包含：实际送入尺寸、observations、candidates（含 bbox/confidence）、与
    question_kind 对应的结果、uncertainties、推荐的本地验证方式。
    坐标换算仍遵守既有不变量：按**实际送入尺寸**换算，自报尺寸只作诊断。
    """
    raw = dict(parsed or {})
    candidates: list[dict[str, Any]] = []
    for item in raw.get("candidates") or []:
        if not isinstance(item, Mapping):
            continue
        bbox = item.get("bbox")
        candidates.append(
            {
                "id": str(item.get("id") or ""),
                "label": str(item.get("label") or ""),
                "bbox": [int(v) for v in bbox] if isinstance(bbox, (list, tuple)) and len(bbox) == 4 else [],
                "confidence": (
                    None if item.get("confidence") is None else float(item.get("confidence"))
                ),
                "observations": str(item.get("observations") or ""),
                "interpretation": str(item.get("interpretation") or ""),
                "grounding_id": str(item.get("grounding_id") or ""),
            }
        )
    reported = raw.get("image_size")
    kind = request.question_kind
    result: dict[str, Any] = {"question_kind": kind}
    uncertainties = [str(x) for x in (raw.get("uncertainties") or []) if str(x).strip()]
    if kind == QUESTION_OBJECT_COUNT:
        declared = raw.get("count")
        if declared is None:
            # 模型没显式给总数：用候选列表长度兜底，但**标注来源**，
            # 免得下游把"列了几个"当成"一共有几个"。
            declared = len(candidates)
            result["count_source"] = "candidates"
            uncertainties.append("总数由候选列表长度推出（模型未显式给出）")
        else:
            result["count_source"] = "model"
        result["count"] = declared
    elif kind in (QUESTION_OBJECT_LOCATION, QUESTION_OBJECT_PRESENCE):
        result["found"] = bool(candidates)
        result["locations"] = [
            {"id": item["id"], "bbox": item["bbox"]} for item in candidates if item["bbox"]
        ]
    elif kind in (QUESTION_LAYOUT, QUESTION_APPEARANCE, QUESTION_REGION_CONTENT):
        result["summary"] = str(raw.get("summary") or "")
        result["observations"] = [item["observations"] for item in candidates if item["observations"]]
    elif kind == QUESTION_DIFFERENCE:
        result["summary"] = str(raw.get("summary") or "")
        result["differences"] = [
            item["observations"] for item in candidates if item["observations"]
        ]
    elif kind == QUESTION_TEMPLATE_SUITABILITY:
        result["verified"] = raw.get("verified")
        result["reason"] = str(raw.get("verification_reason") or "")
    return {
        "fed_size": [int(fed_size[0]), int(fed_size[1])],
        "source_size": [int(source_size[0]), int(source_size[1])],
        "vision_reported_size": (
            [int(reported[0]), int(reported[1])]
            if isinstance(reported, (list, tuple)) and len(reported) == 2
            else []
        ),
        "observations": str(raw.get("summary") or ""),
        "candidates": candidates,
        "result": result,
        "uncertainties": uncertainties,
        "recommended_local_verification": recommend_local_verification(kind, candidates),
        "structured": bool(raw.get("structured")),
        "validation_errors": [str(x) for x in (raw.get("validation_errors") or [])],
    }


def recommend_local_verification(
    question_kind: str, candidates: Sequence[Mapping[str, Any]]
) -> list[str]:
    """推荐的本地验证方式：视觉只给"往哪儿看"，精确结论用本地 CV 定。"""
    has_bbox = any(item.get("bbox") for item in candidates)
    hints: list[str] = []
    if question_kind in (QUESTION_OBJECT_COUNT, QUESTION_OBJECT_LOCATION):
        hints.append("用 match_template_multi 在局部做精确计数/定位")
    if question_kind == QUESTION_TEMPLATE_SUITABILITY:
        hints.append("用模板诊断（灰度标准差/峰值间隔）确认模板区分度")
    if question_kind == QUESTION_REGION_CONTENT and has_bbox:
        hints.append("按 bbox 裁剪后 inspect_asset(mode=verify) 复核")
    if not hints:
        hints.append("需要精确数字时用本地 CV 复算，不要直接引用视觉估计")
    return hints


def validate_structured(
    payload: Mapping[str, Any], *, question_kind: str
) -> tuple[bool, list[str]]:
    """结构化结果的合格性判定（不合格只允许一次针对性复核，不算有效证据）。"""
    data = dict(payload or {})
    errors = [str(x) for x in (data.get("validation_errors") or [])]
    if not data.get("structured"):
        errors.append("视觉输出未通过结构化校验")
    if not data.get("fed_size") or min(int(x) for x in data.get("fed_size") or [0, 0]) <= 0:
        errors.append("缺少实际送入尺寸")
    result = dict(data.get("result") or {})
    if question_kind == QUESTION_OBJECT_COUNT and result.get("count") in (None, ""):
        errors.append("数量类结果缺少 count")
    return (not errors), errors


def max_confidence(payload: Mapping[str, Any]) -> Optional[float]:
    values = [
        float(item["confidence"])
        for item in (payload.get("candidates") or [])
        if isinstance(item, Mapping) and item.get("confidence") is not None
    ]
    return max(values) if values else None


# ── Broker ────────────────────────────────────────────────────────────


def should_invalidate(
    facts: Mapping[str, Any],
    *,
    resource_id: str = "",
    content_hash: str = "",
    mark_revision: str = "",
) -> bool:
    """这条证据该不该因为"当前状态变了"而失效（V2/V3 共用**同一份**判据）。

    传进来的是**当前**状态（设计 §5.6）：

    - ``resource_id``：只在该资源范围内比较（空 → 只管"没有资源 id 的视口级证据"）；
    - ``content_hash``：图片内容变了才算变；证据没记 hash（老行）时无法证明一致 → 失效；
    - ``mark_revision``：**只有"当初带标记"的证据**才因标记变化失效；没标记的证据
      描述的是原始像素，标记变化不该误伤它。

    主轮变化、界面缩放、无关资源变化都不在判据里 → 不误伤。
    """
    row_resource = str(facts.get("resource_id") or facts.get("subject_id") or "")
    if str(resource_id or "") != row_resource:
        return False
    if content_hash:
        stored = str(facts.get("content_hash") or "")
        if stored != str(content_hash):
            return True
    if mark_revision:
        stored_mark = str(facts.get("mark_revision") or "")
        if stored_mark and stored_mark != str(mark_revision):
            return True
    return False


@dataclass
class LookupResult:
    hit: Optional[VisualEvidence] = None
    reason: str = ""
    l1_hit: bool = False
    l2_checked: bool = False
    l1_dropped: bool = False
    missing: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return self.hit is not None and not self.missing


@dataclass
class AcquireResult:
    ok: bool
    source: str = ""
    evidence: Optional[VisualEvidence] = None
    text: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    calls: int = 0
    cached: bool = False
    code: str = ""
    errors: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    attempts: tuple[dict[str, Any], ...] = ()
    budget: Mapping[str, int] = field(default_factory=dict)
    #: 缓存作用域：``cross_turn_authoritative``（有 L2）/ ``session_l1_only``（只有 L1）。
    cache_scope: str = "session_l1_only"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": bool(self.ok),
            "source": self.source,
            "cached": bool(self.cached),
            "cache_scope": self.cache_scope,
            "code": self.code,
            "tokens_in": int(self.tokens_in),
            "tokens_out": int(self.tokens_out),
            "calls": int(self.calls),
            "errors": list(self.errors),
            "conflicts": list(self.conflicts),
            "attempts": [dict(x) for x in self.attempts],
            "budget": dict(self.budget),
            "evidence": self.evidence.to_dict() if self.evidence else None,
        }


class VisualEvidenceBroker:
    """唯一视觉证据调度实现：指纹 / L1+L2 缓存 / 失效 / 预算 / 顺序 / 遥测。"""

    def __init__(
        self,
        *,
        l1: Optional[L1Cache] = None,
        l2: Optional[EvidenceStore] = None,
        budget: Optional[VisualBudget] = None,
        telemetry: Optional[Callable[[dict[str, Any]], None]] = None,
        vision_protocol: str = VISION_PROTOCOL_VERSION,
        clock: Callable[[], float] = time.time,
        max_side: int = VISION_MAX_SIDE,
    ) -> None:
        self.l1 = l1
        self.l2 = l2 or NullEvidenceStore()
        self.budget = budget or VisualBudget()
        self.telemetry = telemetry
        self.vision_protocol = str(vision_protocol or VISION_PROTOCOL_VERSION)
        self.clock = clock
        self.max_side = int(max_side)
        self._invalidate_log: list[dict[str, Any]] = []
        self._l2_notice_emitted = False

    # ── 能力/降级说明（不许把"只有 L1"说成跨主轮权威缓存）──

    @property
    def l2_authoritative(self) -> bool:
        """L2 是不是权威源（没有 V3 store 时为 False）。"""
        return bool(getattr(self.l2, "authoritative", True))

    @property
    def cache_scope(self) -> str:
        """缓存作用域：``cross_turn_authoritative`` / ``session_l1_only``。"""
        return "cross_turn_authoritative" if self.l2_authoritative else "session_l1_only"

    def _note_l2_unavailable(self) -> None:
        """只有 L1 时明确记一条降级遥测（不静默假装有权威缓存）。"""
        if self.l2_authoritative or self._l2_notice_emitted:
            return
        self._l2_notice_emitted = True
        self._emit(
            {
                "event": "l2_unavailable",
                "code": "VISUAL_L2_UNAVAILABLE",
                "cache_scope": self.cache_scope,
                "note": (
                    "没有 V3 evidence store：视觉证据只存在本会话 L1 缓存里，"
                    "**不是跨主轮权威缓存**（换会话/换库后不作数）"
                ),
            }
        )

    # ── 缓存 ──

    def lookup(self, request: VisualEvidenceRequest) -> LookupResult:
        """先 L1（命中要校验 L2 仍有效），再 L2。

        没有权威 L2 时（V2 独立运行）L1 命中即可用 —— 但那只代表**本会话缓存**，
        调用方必须按 ``cache_scope == "session_l1_only"`` 描述它。
        """
        fingerprint = request.fingerprint()
        result = LookupResult()
        self._note_l2_unavailable()
        l1_value: Optional[dict[str, Any]] = None
        if self.l1 is not None:
            raw = self.l1.get(fingerprint)
            if isinstance(raw, Mapping):
                l1_value = dict(raw)
        if l1_value is not None:
            result.l1_hit = True
            evidence = VisualEvidence.from_dict({**l1_value, "fingerprint": fingerprint})
            # 有**权威 L2** 时，L1 命中必须校验 L2 仍有效（设计 §5.11）；
            # V2 独立运行没有 L2（NullEvidenceStore）时，L1 就是唯一一层缓存。
            if getattr(self.l2, "authoritative", True):
                result.l2_checked = True
                row = self.l2.find(fingerprint=fingerprint, question_kind=request.question_kind)
                if row is None:
                    # L1 有、L2 没有权威记录（例如跨主轮换了库）→ 不能当命中
                    self.l1.clear(fingerprints=(fingerprint,))
                    result.l1_dropped = True
                    result.reason = "L1 命中但 L2 无权威记录"
                    result.hit = None
                    return result
                if str(row.get("status") or "") != "active":
                    self.l1.clear(fingerprints=(fingerprint,))
                    result.l1_dropped = True
                    result.reason = f"L1 命中但 L2 证据状态为 {row.get('status')}"
                    result.hit = None
                    return result
                evidence = replace(
                    evidence,
                    status=str(row.get("status") or "active"),
                    evidence_id=str(row.get("id") or ""),
                )
            if not evidence.covers(request.question_kind):
                result.reason = (
                    f"命中证据是 {evidence.question_kind}，回答不了 {request.question_kind}"
                )
                result.hit = None
                result.missing = (f"question_kind:{request.question_kind}",)
                return result
            if not evidence.covers_region(request.region):
                result.reason = "命中证据的选区不覆盖本次请求"
                result.hit = None
                result.missing = (f"region:{normalize_region(request.region)}",)
                return result
            result.hit = evidence
            result.reason = "L1 命中且 L2 有效"
            return result
        row = self.l2.find(fingerprint=fingerprint, question_kind=request.question_kind)
        result.l2_checked = True
        if row is None:
            result.reason = "L1/L2 都没有该指纹的证据"
            return result
        if str(row.get("status") or "") != "active":
            result.reason = f"L2 证据状态为 {row.get('status')}"
            return result
        evidence = VisualEvidence.from_dict({**row, "fingerprint": fingerprint})
        if not evidence.covers(request.question_kind):
            result.reason = (
                f"命中证据是 {evidence.question_kind}，回答不了 {request.question_kind}"
            )
            result.missing = (f"question_kind:{request.question_kind}",)
            return result
        if not evidence.covers_region(request.region):
            result.reason = "命中证据的选区不覆盖本次请求"
            result.missing = (f"region:{normalize_region(request.region)}",)
            return result
        result.hit = evidence
        result.reason = "L2 命中"
        if self.l1 is not None:
            self.l1.put(fingerprint, evidence.to_dict())
        return result

    def record(
        self,
        request: VisualEvidenceRequest,
        payload: Mapping[str, Any],
        *,
        backend: str,
        source: str,
        structured: bool,
        confidence: Optional[float] = None,
        text: str = "",
        evidence_id: str = "",
        turn_id: str = "",
    ) -> VisualEvidence:
        """**先写 L2，再写 L1**（跨主轮权威源优先）。"""
        fingerprint = request.fingerprint()
        # 身份字段同时写进 payload：L2 行没有单独的列，失效/复用判定都要靠它们。
        body = {
            **dict(payload),
            "resource_id": str(request.resource_id or ""),
            "content_hash": str(request.content_hash or ""),
            "mark_revision": str(request.mark_revision or ""),
            "question_kind": request.question_kind,
            "region": list(normalize_region(request.region)),
            "vision_protocol": str(request.vision_protocol or ""),
            "backend_version": str(request.backend_version or ""),
            "origin": str(request.origin or ""),
            "text": str(text or ""),
        }
        evidence = VisualEvidence(
            fingerprint=fingerprint,
            question_kind=request.question_kind,
            resource_id=request.resource_id,
            content_hash=request.content_hash,
            payload=body,
            backend=str(backend or ""),
            source=str(source or ""),
            structured=bool(structured),
            confidence=confidence,
            status="active",
            evidence_id=str(evidence_id or ""),
            created_at=str(int(self.clock())),
            region=normalize_region(request.region),
            mark_revision=str(request.mark_revision or ""),
        )
        new_id = self.l2.put(evidence, turn_id=turn_id)
        if new_id:
            evidence = replace(evidence, evidence_id=new_id)
        if self.l1 is not None:
            self.l1.put(fingerprint, evidence.to_dict())
        return evidence

    def invalidate(
        self,
        *,
        resource_id: str = "",
        content_hash: str = "",
        mark_revision: str = "",
        reason: str = "",
    ) -> int:
        """**先更新 L2，再清 L1**（要求 §3）。返回受影响的 L2 行数。"""
        changed = int(
            self.l2.invalidate(
                resource_id=resource_id,
                content_hash=content_hash,
                mark_revision=mark_revision,
                reason=reason,
            )
            or 0
        )
        if self.l1 is not None:
            self.l1.clear()
        event = {
            "resource_id": str(resource_id or ""),
            "content_hash": str(content_hash or ""),
            "mark_revision": str(mark_revision or ""),
            "reason": str(reason or ""),
            "l2_rows": changed,
        }
        self._invalidate_log.append(event)
        del self._invalidate_log[:-20]
        self._emit({"event": "invalidate", **event})
        return changed

    # ── 调度 ──

    def acquire(
        self,
        request: VisualEvidenceRequest,
        *,
        describe: Callable[[str], Mapping[str, Any]],
        primary: Optional[VisionBackend] = None,
        assist: Optional[VisionBackend] = None,
        local_cv: Optional[LocalCvProbe] = None,
        user_requested: bool = False,
        source_size: tuple[int, int] = (0, 0),
        turn_id: str = "",
        trigger: str = "",
        recheck: Optional[VisualEvidenceRequest] = None,
    ) -> AcquireResult:
        """固定顺序：缓存 → 本地 CV → 主模型视觉 → 辅助识图 → 最多一次针对性复核。

        ``describe(backend_label)`` 负责**真正**调用模型（返回 text/tokens/latency/error），
        Broker 只决定"要不要调、调哪一个、还能不能再调"。
        """
        attempts: list[dict[str, Any]] = []
        errors: list[str] = []
        conflicts: list[str] = []
        if user_requested:
            # 用户明确"重新识图/再看一次"：额外放行一次（设计 §5.7）
            self.budget.grant_user_extra()

        lookup = self.lookup(request)
        if lookup.usable and lookup.hit is not None and not user_requested:
            self.budget.cache_hits += 1
            self._emit(
                {
                    "event": "acquire",
                    "source": SOURCE_CACHE,
                    "cached": True,
                    "fingerprint": lookup.hit.fingerprint,
                    "question_kind": request.question_kind,
                    "trigger": trigger,
                    "budget": self.budget.snapshot(),
                }
            )
            return AcquireResult(
                cache_scope=self.cache_scope,
                ok=True,
                source=SOURCE_CACHE,
                evidence=lookup.hit,
                text=str(lookup.hit.payload.get("text") or ""),
                cached=True,
                attempts=tuple(attempts),
                budget=self.budget.snapshot(),
            )

        # ① 本地 CV：精确算法够用就不花视觉调用
        if local_cv is not None:
            try:
                local = local_cv(request)
            except Exception as exc:  # noqa: BLE001 - 本地 CV 失败不该打死主轮
                local = None
                errors.append(f"本地 CV 异常：{exc}")
            if isinstance(local, Mapping) and local.get("sufficient"):
                payload = dict(local.get("payload") or {})
                evidence = self.record(
                    request,
                    payload,
                    backend=str(local.get("backend") or "local_cv"),
                    source=SOURCE_LOCAL_CV,
                    structured=True,
                    confidence=local.get("confidence"),
                    text=str(local.get("text") or ""),
                    turn_id=turn_id,
                )
                attempts.append({"source": SOURCE_LOCAL_CV, "ok": True})
                self._emit(
                    {
                        "event": "acquire",
                        "source": SOURCE_LOCAL_CV,
                        "cached": False,
                        "fingerprint": evidence.fingerprint,
                        "question_kind": request.question_kind,
                        "trigger": trigger or "local_cv_sufficient",
                        "budget": self.budget.snapshot(),
                    }
                )
                return AcquireResult(
                    cache_scope=self.cache_scope,
                    ok=True,
                    source=SOURCE_LOCAL_CV,
                    evidence=evidence,
                    text=str(local.get("text") or ""),
                    attempts=tuple(attempts),
                    budget=self.budget.snapshot(),
                )
            if isinstance(local, Mapping) and local.get("conflict"):
                conflicts.extend(str(x) for x in (local.get("conflict") or []))

        # ② 主逻辑模型视觉 → ③ 辅助识图（同一后端不重复打）
        protocol = {"vision_protocol": self.vision_protocol, "request": request.to_dict()}
        backends: list[VisionBackend] = []
        if primary is not None and primary.available:
            backends.append(primary)
        if assist is not None and assist.available and assist.label not in {b.label for b in backends}:
            backends.append(assist)

        last_payload: Optional[dict[str, Any]] = None
        for backend in backends:
            if not self.budget.can_call():
                break
            attempt = self._call_backend(
                backend,
                request,
                describe=describe,
                protocol=protocol,
                source=SOURCE_PRIMARY if backend is primary else SOURCE_ASSIST,
                source_size=source_size,
                turn_id=turn_id,
                trigger=trigger,
                attempts=attempts,
            )
            if attempt["ok"]:
                last_payload = attempt["payload"]
                evidence = attempt["evidence"]
                return AcquireResult(
                    cache_scope=self.cache_scope,
                    ok=True,
                    source=attempt["source"],
                    evidence=evidence,
                    text=attempt["text"],
                    tokens_in=int(attempt["tokens_in"]),
                    tokens_out=int(attempt["tokens_out"]),
                    calls=self.budget.total_calls,
                    errors=tuple(errors),
                    conflicts=tuple(conflicts),
                    attempts=tuple(attempts),
                    budget=self.budget.snapshot(),
                )
            last_payload = attempt.get("payload")
            if attempt.get("needs_recheck"):
                break
            if attempt.get("error"):
                errors.append(str(attempt["error"]))

        # ④ 最多一次针对性复核：结构不合格 / 低置信 / 与本地 CV 冲突。
        #    **同一后端不得重复调用**：复核只发给还没试过的后端；要复用同一后端时，
        #    调用方必须给一个**真正不同**的请求（``recheck``，例如按候选框裁小区域），
        #    否则同一指纹打第二遍只是白烧 token。
        needs_recheck = bool(errors) or bool(conflicts)
        if last_payload is not None:
            ok_struct, _ = validate_structured(last_payload, question_kind=request.question_kind)
            conf = max_confidence(last_payload)
            needs_recheck = needs_recheck or (not ok_struct) or (
                conf is not None and conf < LOW_CONFIDENCE
            )
        tried_labels = {str(item.get("backend") or "") for item in attempts}
        recheck_backend = next(
            (b for b in backends if b.label not in tried_labels),
            None,
        )
        recheck_request = request
        if recheck is not None:
            recheck_request = recheck
            if recheck_backend is None and recheck.fingerprint() != request.fingerprint():
                # 请求本身变了（裁小区域/换问题类型）→ 允许同一个后端再打一次
                recheck_backend = backends[-1] if backends else None
        if needs_recheck and recheck_backend is not None and self.budget.can_call(targeted=True):
            targeted_request = replace(
                recheck_request,
                missing=tuple(recheck_request.missing) + ("targeted_recheck",),
            )
            attempt = self._call_backend(
                recheck_backend,
                targeted_request,
                describe=describe,
                protocol=protocol,
                source=SOURCE_ASSIST if recheck_backend is not primary else SOURCE_PRIMARY,
                source_size=source_size,
                turn_id=turn_id,
                trigger=trigger or "targeted_recheck",
                attempts=attempts,
                targeted=True,
            )
            if attempt["ok"]:
                return AcquireResult(
                    cache_scope=self.cache_scope,
                    ok=True,
                    source=attempt["source"],
                    evidence=attempt["evidence"],
                    text=attempt["text"],
                    tokens_in=int(attempt["tokens_in"]),
                    tokens_out=int(attempt["tokens_out"]),
                    calls=self.budget.total_calls,
                    errors=tuple(errors),
                    conflicts=tuple(conflicts),
                    attempts=tuple(attempts),
                    budget=self.budget.snapshot(),
                )
            if attempt.get("error"):
                errors.append(str(attempt["error"]))

        code = (
            VISUAL_BUDGET_EXHAUSTED
            if not self.budget.can_call() and not self.budget.can_call(targeted=True)
            else (errors[0] if errors else VISUAL_RESULT_UNSTRUCTURED)
        )
        # 已达上限/复核也没成功：**报告已有证据与冲突**，不许继续循环调用（设计 §5.7）。
        weak = next(
            (
                item.get("evidence")
                for item in reversed(attempts)
                if item.get("evidence") is not None
            ),
            None,
        )
        self._emit(
            {
                "event": "acquire_failed",
                "code": code,
                "errors": list(errors),
                "conflicts": list(conflicts),
                "question_kind": request.question_kind,
                "trigger": trigger,
                "budget": self.budget.snapshot(),
            }
        )
        return AcquireResult(
            cache_scope=self.cache_scope,
            ok=False,
            code=code,
            evidence=weak,
            errors=tuple(errors),
            conflicts=tuple(conflicts),
            calls=self.budget.total_calls,
            attempts=tuple(attempts),
            budget=self.budget.snapshot(),
        )

    # ── 内部 ──

    def _call_backend(
        self,
        backend: VisionBackend,
        request: VisualEvidenceRequest,
        *,
        describe: Callable[[str], Mapping[str, Any]],
        protocol: Mapping[str, Any],
        source: str,
        source_size: tuple[int, int],
        turn_id: str,
        trigger: str,
        attempts: list[dict[str, Any]],
        targeted: bool = False,
    ) -> dict[str, Any]:
        started = float(self.clock())
        self.budget.spend(targeted=targeted)
        outcome: dict[str, Any] = {
            "backend": backend.label,
            "source": source,
            "targeted": bool(targeted),
            "trigger": trigger,
            "question_kind": request.question_kind,
            "resource_id": request.resource_id,
            "region": list(request.region),
            "fingerprint": request.fingerprint(),
            "cached": False,
            "protocol": str(protocol.get("vision_protocol") or ""),
        }
        try:
            raw = dict(describe(backend.label) or {})
        except Exception as exc:  # noqa: BLE001 - 后端异常一律记成失败尝试
            raw = {"error": str(exc)}
        latency_ms = int((float(self.clock()) - started) * 1000)
        outcome.update(
            {
                "tokens_in": int(raw.get("tokens_in") or 0),
                "tokens_out": int(raw.get("tokens_out") or 0),
                "latency_ms": latency_ms,
                "error": str(raw.get("error") or ""),
            }
        )
        if raw.get("error") or not str(raw.get("text") or "").strip():
            outcome["ok"] = False
            outcome["valid"] = False
            outcome.setdefault("error", "后端返回为空")
            attempts.append(outcome)
            self._emit({"event": "attempt", **outcome})
            return outcome

        parsed = dict(raw.get("parsed") or {})
        fed = fed_size_for(*source_size, max_side=self.max_side) if source_size else (0, 0)
        payload = build_structured_payload(
            parsed, request=request, source_size=source_size, fed_size=fed
        )
        ok_struct, validation_errors = validate_structured(
            payload, question_kind=request.question_kind
        )
        confidence = max_confidence(payload)
        low_confidence = confidence is not None and confidence < LOW_CONFIDENCE
        outcome.update(
            {
                "structured": bool(payload.get("structured")),
                "valid": bool(ok_struct and not low_confidence),
                "confidence": confidence,
                "low_confidence": bool(low_confidence),
                "validation_errors": list(validation_errors),
            }
        )
        if not ok_struct:
            outcome["ok"] = False
            outcome["payload"] = payload
            outcome["needs_recheck"] = True
            outcome["error"] = VISUAL_RESULT_UNSTRUCTURED
            outcome["text"] = str(raw.get("text") or "")
            attempts.append(outcome)
            self._emit({"event": "attempt", **outcome})
            return outcome
        # 结构化但低置信：**先落库**（后续主轮还能用），但这次不算取到合格证据，
        # 允许一次针对性复核（设计 §5.7）。
        evidence = self.record(
            request,
            payload,
            backend=backend.label,
            source=source,
            structured=True,
            confidence=confidence,
            text=str(raw.get("text") or ""),
            turn_id=turn_id,
        )
        outcome["payload"] = payload
        outcome["text"] = str(raw.get("text") or "")
        outcome["evidence"] = evidence
        if low_confidence:
            outcome["ok"] = False
            outcome["needs_recheck"] = True
            outcome["error"] = VISUAL_RESULT_LOW_CONFIDENCE
            attempts.append(outcome)
            self._emit({"event": "attempt", **outcome})
            return outcome
        outcome["ok"] = True
        attempts.append(outcome)
        self._emit(
            {
                "event": "acquire",
                "source": source,
                "cached": False,
                "fingerprint": evidence.fingerprint,
                "question_kind": request.question_kind,
                "resource_id": request.resource_id,
                "region": list(request.region),
                "backend": backend.label,
                "tokens_in": outcome["tokens_in"],
                "tokens_out": outcome["tokens_out"],
                "latency_ms": latency_ms,
                "valid": True,
                "confidence": confidence,
                "trigger": trigger,
                "budget": self.budget.snapshot(),
            }
        )
        return outcome

    def _emit(self, event: Mapping[str, Any]) -> None:
        if self.telemetry is None:
            return
        try:
            self.telemetry(dict(event))
        except Exception:  # noqa: BLE001 - 遥测失败绝不打断取证
            pass


# ── 兼容性判定：一组已有证据能不能支撑"图片事实" ───────────────────


def evidence_row_facts(row: Mapping[str, Any]) -> dict[str, Any]:
    """把一行证据（L2 行 / L1 dict）拍平成"判定用的字段"。

    L2 行把身份字段放在 payload 里（表结构只有一列 fingerprint），所以这里做一次
    归一：``status/kind/id`` 取自外层，``question_kind/resource_id/content_hash/region``
    取自 payload（外层有则优先）。
    """
    data = dict(row or {})
    payload = dict(data.get("payload") or {})
    out = {**payload, **{k: v for k, v in data.items() if k != "payload"}}
    out.setdefault("question_kind", str(payload.get("question_kind") or ""))
    out.setdefault("region", payload.get("region") or [])
    return out


def evidence_supports(
    evidence_rows: Iterable[Mapping[str, Any]],
    *,
    question_kind: str,
    resource_id: str = "",
    content_hash: str = "",
    region: Any = (),
) -> tuple[bool, str]:
    """给定若干证据行，判断它们能不能支撑某个图片事实（闸门用）。

    只要有一条**同资源/同内容、状态 active、类型兼容、选区覆盖**的视觉证据即可。
    """
    for row in evidence_rows or ():
        facts = evidence_row_facts(row)
        if str(facts.get("status") or "active") != "active":
            continue
        kind = str(facts.get("question_kind") or facts.get("kind") or "")
        if not covers(kind, question_kind):
            continue
        row_resource = str(facts.get("resource_id") or facts.get("subject_id") or "")
        if resource_id and row_resource and row_resource != str(resource_id):
            continue
        row_hash = str(facts.get("content_hash") or "")
        if content_hash and row_hash and row_hash != str(content_hash):
            continue
        if not VisualEvidence.from_dict(facts).covers_region(region):
            continue
        return True, ""
    return False, VISUAL_EVIDENCE_REQUIRED
