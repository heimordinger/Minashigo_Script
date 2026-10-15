"""V3 侧视觉证据适配器：把统一的 VisualEvidenceBroker 接到 ``evidence`` 表（L2 权威源）。

设计 §5.11：V2/V3 共用同一个 Broker；V3 这边只负责"L2 怎么存/怎么查/怎么失效"，
规则（指纹、失效条件、预算、顺序）不在这里重写。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional
from uuid import uuid4

from backend.script_generator.visual_evidence import VisualEvidence

#: 视觉证据在 ``evidence.kind`` 里的取值（与 EvidenceKind.VISION_OBSERVATION 一致）。
EVIDENCE_KIND_VISUAL = "vision_observation"


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class V3EvidenceStore:
    """``evidence`` 表适配器：实现 ``visual_evidence.EvidenceStore`` 协议。"""

    label = "v3_evidence"
    #: 权威源：L1 命中必须与这里对齐（设计 §5.11）。
    authoritative = True

    def __init__(self, store: Any, *, turn_id: str = "", task_id: str = "") -> None:
        self.store = store
        self.turn_id = str(turn_id or "")
        self.task_id = str(task_id or "")

    # ── EvidenceStore ──

    def find(self, *, fingerprint: str, question_kind: str = "") -> Optional[dict[str, Any]]:
        row = self.store.find_evidence_by_fingerprint(
            str(fingerprint or ""), kind=EVIDENCE_KIND_VISUAL, status="active"
        )
        if row is None:
            return None
        return row

    def put(self, evidence: VisualEvidence, *, turn_id: str = "") -> str:
        from ..domain.enums import EvidenceKind
        from ..domain.models import EvidenceRecord, ResourceRef

        evidence_id = str(evidence.evidence_id or "") or f"ev_vision_{uuid4().hex[:16]}"
        subject = None
        if str(evidence.resource_id or "").strip():
            try:
                subject = ResourceRef(str(evidence.resource_id), int(1))
            except Exception:  # noqa: BLE001 - 版本未知时不伪造 subject
                subject = None
        record = EvidenceRecord(
            id=evidence_id,
            kind=EvidenceKind.VISION_OBSERVATION,
            payload=dict(evidence.payload or {}),
            turn_id=str(turn_id or self.turn_id or "unknown"),
            subject=subject,
            producer_capability_id=str(evidence.backend or ""),
            producer_version=str(evidence.payload.get("vision_protocol") or ""),
            confidence=evidence.confidence,
            status="active",
            created_at=evidence.created_at or _now(),
        )
        self.store.put_evidence(record, fingerprint=evidence.fingerprint)
        return evidence_id

    def invalidate(
        self,
        *,
        resource_id: str = "",
        content_hash: str = "",
        mark_revision: str = "",
        reason: str = "",
    ) -> int:
        """按**当前状态**失效旧证据（与 V2 共用 ``should_invalidate`` 同一判据）。

        - 只动 ``resource_id`` 这一条资源的证据（空 → 只管视口级证据）；
        - 图片内容变了（或老行没记 hash）→ 失效；
        - **当初带标记**的证据，标记变了才失效；没标记的证据不因标记变化误伤；
        - 主轮变化、界面缩放、无关资源变化都不在判据里。
        """
        from backend.script_generator.visual_evidence import evidence_row_facts, should_invalidate

        rows = self.store.list_evidence_with_fingerprint(status="active", limit=200)
        targets: list[str] = []
        for row in rows:
            facts = evidence_row_facts(row)
            if should_invalidate(
                facts,
                resource_id=resource_id,
                content_hash=content_hash,
                mark_revision=mark_revision,
            ):
                targets.append(str(row.get("id") or ""))
        return int(self.store.invalidate_evidence_rows(targets, reason=reason) or 0)

    def active_rows(self, *, limit: int = 200) -> list[dict[str, Any]]:
        return list(self.store.list_evidence_with_fingerprint(status="active", limit=limit))
