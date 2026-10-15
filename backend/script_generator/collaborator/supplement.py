"""用户「补充」：模型思考途中追加的话，在下一个检查点注入当轮上下文。

GUI 线程 submit，worker 线程 drain——两边不共享其它状态，所以只靠一把锁。

关键约束：**只有即将真的再发一次请求时才消费**。若在判断「还要不要继续」之前
就把队列清空，而这一轮恰好收尾（turn=stop），补充就凭空消失了。所以消费点放在
`_plan_followups` 决定继续、组 followup 消息之前。没被消费的一直留在队列里，
由 GUI 在轮结束时当普通新消息发出去。
"""

from __future__ import annotations

import threading


class SupplementBox:
    """线程安全的补充信箱：GUI 线程写，worker 线程读。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: list[str] = []
        self._consumed: list[str] = []
        self._context_by_text: list[dict] = []

    def submit(self, text: str) -> bool:
        text = (text or "").strip()
        if not text:
            return False
        with self._lock:
            self._pending.append(text)
            self._context_by_text.append({})
        return True

    def submit_context(self, text: str, context: dict | None = None) -> bool:
        """V3 side channel: enqueue text with the viewport context at submit time."""
        text = (text or "").strip()
        if not text:
            return False
        with self._lock:
            self._pending.append(text)
            self._context_by_text.append(dict(context or {}))
        return True

    def drain(self) -> list[str]:
        """取走待注入项并记为已消费。由 worker 在检查点调用。"""
        with self._lock:
            if not self._pending:
                return []
            items = self._pending
            self._pending = []
            self._context_by_text = []
            self._consumed.extend(items)
            return items

    def drain_context(self) -> list[dict]:
        """Consume pending items as structured V3 supplements."""
        with self._lock:
            if not self._pending:
                return []
            texts = self._pending
            contexts = self._context_by_text
            self._pending = []
            self._context_by_text = []
            self._consumed.extend(texts)
            return [
                {"text": text, "context": dict(contexts[i] if i < len(contexts) else {})}
                for i, text in enumerate(texts)
            ]

    def pending(self) -> list[str]:
        """还没被 worker 取走的（轮结束时按普通新消息补发）。"""
        with self._lock:
            return list(self._pending)

    def take_pending(self) -> list[str]:
        """取走待发的并清空：调用方负责把它变成新消息，重复调用不会重发。"""
        with self._lock:
            items = self._pending
            self._pending = []
            self._context_by_text = []
            return items

    def consumed(self) -> list[str]:
        """已经被注入当轮上下文的（轮结束时并入会话记录）。"""
        with self._lock:
            return list(self._consumed)

    def take_consumed(self) -> list[str]:
        """取走已注入的并清空：并入会话记录时用，避免二次合并。"""
        with self._lock:
            items = self._consumed
            self._consumed = []
            return items

    def reset(self) -> None:
        with self._lock:
            self._pending = []
            self._consumed = []
            self._context_by_text = []
