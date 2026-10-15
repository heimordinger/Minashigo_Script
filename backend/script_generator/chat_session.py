"""生成 ↔ 修订同会话：保存/续写多轮 messages（文本，不含图片二进制）。"""

from __future__ import annotations

import json
from typing import Any, Optional


SESSION_VERSION = 1
# 单条 user/assistant 文本上限，避免续写爆窗
_MAX_MSG_CHARS = 80_000
_MAX_MESSAGES = 12
# 整段历史的总字符预算（不含 system）；超了就保留首轮介绍 + 最近若干轮
_MAX_TOTAL_CHARS = 60_000
# 预算内必须保住的前若干条（首轮介绍 + 首版代码）
_HEAD_KEEP = 2


def _clip(text: str, limit: int = _MAX_MSG_CHARS) -> str:
    t = text or ""
    if len(t) <= limit:
        return t
    return t[: limit - 20] + "\n\n# ...[truncated]...\n"


def build_generate_session(
    *,
    system: str,
    user_text: str,
    assistant_code: str,
    allowed_images: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "version": SESSION_VERSION,
        "system": system or "",
        "allowed_images": list(allowed_images or []),
        "messages": [
            {"role": "user", "content": _clip(user_text)},
            {"role": "assistant", "content": _clip(assistant_code)},
        ],
    }


def session_to_llm_messages(
    session: Optional[dict[str, Any]],
    *,
    max_chars: int = _MAX_TOTAL_CHARS,
    head_keep: int = _HEAD_KEEP,
) -> list[dict]:
    """转为 call_llm 用的 messages（content 为 text parts）。

    超过总预算时保留「首轮介绍 + 最近若干轮」，中间轮次丢弃：
    历史事故是整段会话每轮重发，单轮输入冲到十几万 token、一次重修 53 万。
    """
    if not isinstance(session, dict):
        return []
    out: list[dict] = []
    for msg in session.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "").strip()
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content")
        if isinstance(content, list):
            texts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    texts.append(str(part.get("text") or ""))
                elif isinstance(part, str):
                    texts.append(part)
            text = "\n".join(texts)
        else:
            text = str(content or "")
        if not text.strip():
            continue
        out.append({
            "role": role,
            "content": [{"type": "text", "text": _clip(text)}],
        })
    return _trim_to_budget(out, max_chars=max_chars, head_keep=head_keep)


def _msg_chars(msg: dict) -> int:
    content = msg.get("content")
    if isinstance(content, list) and content:
        return len(str(content[0].get("text") or ""))
    return len(str(content or ""))


def _trim_to_budget(msgs: list[dict], *, max_chars: int, head_keep: int) -> list[dict]:
    """总量裁剪：先保头部，再从尾部往回补，中间丢掉。"""
    if not msgs or max_chars <= 0:
        return msgs
    if sum(_msg_chars(m) for m in msgs) <= max_chars:
        return msgs

    head = msgs[: max(1, head_keep)]
    head_chars = sum(_msg_chars(m) for m in head)
    half = max(2_000, int(max_chars * 0.5))
    if head_chars > half:
        # 头部自己就超预算：只留第一条并裁到一半预算
        first = msgs[0]
        text = str(first["content"][0].get("text") or "")
        head = [
            {
                "role": first.get("role"),
                "content": [{"type": "text", "text": _clip(text, half)}],
            }
        ]
        head_chars = _msg_chars(head[0])

    budget = max_chars - head_chars
    tail: list[dict] = []
    for msg in reversed(msgs[head_keep:]):
        n = _msg_chars(msg)
        if n > budget:
            if tail:
                break
            text = str(msg["content"][0].get("text") or "")
            tail = [
                {
                    "role": msg.get("role"),
                    "content": [
                        {"type": "text", "text": _clip(text, max(1_000, budget))}
                    ],
                }
            ]
            break
        tail.insert(0, msg)
        budget -= n
        if budget <= 0:
            break
    return head + tail


def sync_last_assistant_code(
    session: dict[str, Any],
    code: str,
) -> dict[str, Any]:
    """用当前编辑器代码覆盖最后一条 assistant，避免和磁盘/试跑版漂移。"""
    msgs = list(session.get("messages") or [])
    code = _clip(code or "")
    if msgs and msgs[-1].get("role") == "assistant":
        msgs[-1] = {"role": "assistant", "content": code}
    else:
        msgs.append({"role": "assistant", "content": code})
    return {**session, "messages": msgs}


def append_turn(
    session: dict[str, Any],
    *,
    user_text: str,
    assistant_text: str,
) -> dict[str, Any]:
    msgs = list(session.get("messages") or [])
    msgs.append({"role": "user", "content": _clip(user_text)})
    msgs.append({"role": "assistant", "content": _clip(assistant_text)})
    # 保留首轮 user + 其后最近若干轮
    if len(msgs) > _MAX_MESSAGES:
        head = msgs[:1]
        tail = msgs[-( _MAX_MESSAGES - 1) :]
        msgs = head + tail
    return {**session, "messages": msgs}


def dumps_session(session: dict[str, Any]) -> str:
    return json.dumps(session, ensure_ascii=False)


def loads_session(raw: str | dict | None) -> Optional[dict[str, Any]]:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw if raw.get("messages") else None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if isinstance(data, dict) and data.get("messages"):
        return data
    return None


REVISE_CONTINUATION_ADDENDUM = """
## Continuation: REVISE mode (same conversation)
The previous assistant message is the current script. The new user message is trial feedback.
You MUST revise that script — do not regenerate from scratch unless the code is unusable.
Output format (STRICT):
<<<SUMMARY>>>
Short Chinese checklist (3-8 lines): 已改/未改 per constraint.
<<<CODE>>>
FULL corrected Python source only (no markdown fences).
Keep FSM shape and allowed browser APIs from the original system rules.
Fix syntax if broken. Prefer minimal surgical edits that compile.
NEVER invent _img() filenames; only use the ALLOWED image files listed in the new user message.
""".strip()
