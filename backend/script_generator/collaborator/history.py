"""协作会话本地历史（对话 + LogicGraph + 探针摘要）。

与经典轨 SessionArchive（生成/试跑材料）分开：这里是「可恢复的聊天会话」。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from backend.script_generator.collaborator.session import CollabSession

COLLAB_HISTORY_ROOT = (
    Path(__file__).resolve().parent.parent / "corpus" / "collab_sessions"
)
_MAX_KEEP = 40
_MAX_CODE_CHARS = 400_000


class CollabHistoryStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or COLLAB_HISTORY_ROOT)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, session_id: str) -> Path:
        safe = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", session_id or "chat")
        return self.root / f"{safe}.json"

    def save(self, session: CollabSession, *, require_content: bool = True) -> Optional[Path]:
        """写入会话文件。

        ``require_content=True``（默认）时，**还没有任何用户消息的会话不落盘**：
        打开协作页/切 Tab/切运行模式都不该凭空产生一个空会话，否则历史列表里
        会被这些"只带一句欢迎语"的空壳占满，`load_latest` 也会指到它们。
        """
        if require_content and not has_user_content(session):
            return None
        if not session.session_id:
            session.ensure_id()
        session.touch()
        data = session.to_dict()
        code = str(data.get("generated_code") or "")
        if len(code) > _MAX_CODE_CHARS:
            data["generated_code"] = code[:_MAX_CODE_CHARS] + "\n# ...[truncated]...\n"
        path = self.path_for(session.session_id)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self._gc()
        return path

    def load(self, session_id: str) -> Optional[CollabSession]:
        path = self.path_for(session_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        return CollabSession.from_dict(data)

    def load_latest(self) -> Optional[CollabSession]:
        """按文件 mtime 取最近一次**真正聊过**的会话；无则 None。

        跳过没有用户消息的空壳：它们会让"打开协作页"接到一个空白会话，
        看起来就像每次都在新建会话。
        """
        files = sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in files:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            if not _has_user_text(data.get("messages")):
                continue
            try:
                return CollabSession.from_dict(data)
            except Exception:
                continue
        return None

    def list_summaries(self, limit: int = 20) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        files = sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in files[: max(1, limit)]:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            sid = str(data.get("session_id") or p.stem)
            goal = str(data.get("goal_text") or "").strip()
            msgs = data.get("messages") or []
            n_msg = len(msgs) if isinstance(msgs, list) else 0
            steps = ((data.get("logic") or {}).get("steps") or []) if isinstance(data.get("logic"), dict) else []
            title = goal or _first_user_text(msgs) or "未命名会话"
            items.append(
                {
                    "id": sid,
                    "title": title[:48],
                    "phase": str(data.get("phase") or ""),
                    "updated_at": str(data.get("updated_at") or ""),
                    "n_messages": n_msg,
                    "n_steps": len(steps) if isinstance(steps, list) else 0,
                    "script_name": str(data.get("script_name") or ""),
                }
            )
        return items

    def delete(self, session_id: str) -> bool:
        """删除指定会话文件。成功或不存在均返回 True；失败返回 False。"""
        path = self.path_for(session_id)
        if not path.is_file():
            # 再试 stem 直接匹配（防 id 与文件名轻微不一致）
            alt = self.root / f"{session_id}.json"
            path = alt if alt.is_file() else path
        if not path.is_file():
            return True
        try:
            path.unlink()
            return True
        except Exception:
            return False

    def delete_all(self) -> int:
        """删除全部历史会话，返回删除个数。"""
        n = 0
        for p in list(self.root.glob("*.json")):
            try:
                p.unlink()
                n += 1
            except Exception:
                pass
        return n

    def _gc(self) -> None:
        files = sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in files[_MAX_KEEP:]:
            try:
                p.unlink()
            except Exception:
                pass


def _first_user_text(msgs: Any) -> str:
    if not isinstance(msgs, list):
        return ""
    for m in msgs:
        if isinstance(m, dict) and m.get("role") == "user":
            return str(m.get("text") or "").strip()
    return ""


def has_user_content(session: Any) -> bool:
    """会话里是否已经有过用户发言——没有就不值得落盘。"""
    return _has_user_text(getattr(session, "messages", None))


def _has_user_text(messages: Any) -> bool:
    if not isinstance(messages, list):
        return False
    for item in messages:
        if not isinstance(item, dict):
            continue
        if str(item.get("role") or "") == "user" and str(item.get("text") or "").strip():
            return True
    return False
