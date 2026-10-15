"""会话工作区：一次会话一个目录，收拢素材、产出与副轮轨迹，便于复现问题。

目录结构（``user_data/collab_workspaces/<session_id>/``）::

    meta.json               会话元信息 + 已缓存文件清单（来源路径 → 副本）
    assets/                 素材区图片副本（粘贴 / 截图 / 导入）
    generated/              生成过的图片（探针产出、裁剪、标注、拼版）
    trace/subturns.jsonl    每个副轮一行：请求 + 模型动作 + 结果
    trace/events.jsonl      主轮生命周期事件
    trace/turns.jsonl       用户可见轮次：用户话 + agent 回话 + meta
    trace/session.json      会话快照（便于离线复现）

两条不变量：

1. **只在用户发出第一条消息时创建。** 打开协作页、切 Tab、换运行模式都不建目录；
   写入类接口在工作区不存在时直接跳过，不会偷偷把它建出来。
2. **只缓存副本，不动原件。** 素材区/探针产出的原始文件仍在
   ``screenshots/collab_snips`` 与 ``screenshots/collab_probe`` 下，已保存脚本里
   引用的路径继续有效；工作区里是便于打包带走的一份快照。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from core.path import PROJECT_ROOT

WORKSPACE_ROOT = PROJECT_ROOT / "user_data" / "collab_workspaces"

#: 生成物（产出图）的角色标记：这些是模型画出来的，不是游戏界面本身。
GENERATED_ROLES = {"probe_out", "annotation"}
SUBTURN_TRACE = "subturns.jsonl"
EVENT_TRACE = "events.jsonl"
TURN_TRACE = "turns.jsonl"
SNAPSHOT_FILE = "session.json"
META_FILE = "meta.json"

_SAFE = re.compile(r"[^\w\u4e00-\u9fff\-]+")
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _safe_name(value: str, *, fallback: str = "item", limit: int = 40) -> str:
    cleaned = _SAFE.sub("_", str(value or "").strip()).strip("_")
    cleaned = re.sub(r"_+", "_", cleaned)[:limit]
    return cleaned or fallback


def is_image(path: Path) -> bool:
    try:
        return Path(path).suffix.lower() in _IMAGE_EXTS
    except Exception:
        return False


def workspace_root() -> Path:
    return WORKSPACE_ROOT


def workspace_dir(session_id: str) -> Path:
    sid = _safe_name(session_id, fallback="session", limit=80)
    return WORKSPACE_ROOT / sid


def exists(session_id: str) -> bool:
    sid = str(session_id or "").strip()
    if not sid:
        return False
    return workspace_dir(sid).is_dir()


def ensure(session_id: str, *, goal_text: str = "") -> Optional[Path]:
    """幂等创建会话工作区。

    调用点只有一处：用户在某次会话里发出**第一条消息**时。会话已存在时只补
    元信息，不做任何清空。
    """
    sid = str(session_id or "").strip()
    if not sid:
        return None
    root = workspace_dir(sid)
    first_time = not root.is_dir()
    (root / "assets").mkdir(parents=True, exist_ok=True)
    (root / "generated").mkdir(parents=True, exist_ok=True)
    (root / "trace").mkdir(parents=True, exist_ok=True)
    meta = _read_meta(root)
    meta["session_id"] = sid
    meta.setdefault("created_at", _now())
    meta["updated_at"] = _now()
    if goal_text and not meta.get("goal_text"):
        meta["goal_text"] = str(goal_text)
    meta.setdefault("counters", {"subturns": 0, "events": 0, "turns": 0})
    meta.setdefault("cached", {})
    if first_time:
        meta["created_by"] = "first_user_message"
    _write_meta(root, meta)
    return root


def _read_meta(root: Path) -> dict[str, Any]:
    path = Path(root) / META_FILE
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_meta(root: Path, meta: dict[str, Any]) -> None:
    try:
        (Path(root) / META_FILE).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError:
        pass


def _append_jsonl(session_id: str, filename: str, record: dict[str, Any]) -> bool:
    """追加一行轨迹。工作区不存在时**不创建**它（见模块头的不变量 1）。"""
    if not exists(session_id):
        return False
    root = workspace_dir(session_id)
    if not root.is_dir():
        return False
    try:
        with (root / "trace" / filename).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError:
        return False
    _bump_counter(session_id, filename)
    return True


def _bump_counter(session_id: str, filename: str) -> None:
    key = {
        SUBTURN_TRACE: "subturns",
        EVENT_TRACE: "events",
        TURN_TRACE: "turns",
    }.get(filename)
    if key is None:
        return
    root = workspace_dir(session_id)
    meta = _read_meta(root)
    counters = meta.setdefault("counters", {"subturns": 0, "events": 0, "turns": 0})
    counters[key] = int(counters.get(key, 0)) + 1
    meta["updated_at"] = _now()
    _write_meta(root, meta)


# ── 副轮 / 事件 / 轮次轨迹 ─────────────────────────────────────────────

def record_subturn(session_id: str, record: dict[str, Any]) -> bool:
    """记一个副轮：一次模型往返 + 一个动作 + 它的结果。

    这是复现 bug 最关键的一份数据——``ActionRequested`` 事件只记了动作类型，
    这里带上送进模型的载荷和模型真正返回的动作。
    """
    payload = dict(record)
    payload.setdefault("at", _now())
    return _append_jsonl(session_id, SUBTURN_TRACE, payload)


def record_event(session_id: str, event_type: str, payload: dict[str, Any] | None = None) -> bool:
    """记一条主轮生命周期事件（开始/暂停/失败/提交）。"""
    return _append_jsonl(
        session_id,
        EVENT_TRACE,
        {"at": _now(), "type": str(event_type or ""), "payload": dict(payload or {})},
    )


def record_turn(session_id: str, record: dict[str, Any]) -> bool:
    """记一次用户可见轮次：用户话 + agent 回话 + meta。"""
    payload = dict(record)
    payload.setdefault("at", _now())
    return _append_jsonl(session_id, TURN_TRACE, payload)


def save_snapshot(session_id: str, session: Any) -> bool:
    """把会话快照写进工作区，便于脱离 GUI 复现。"""
    if not exists(session_id):
        return False
    root = workspace_dir(session_id)
    try:
        data = session.to_dict() if hasattr(session, "to_dict") else dict(session or {})
    except Exception:
        return False
    try:
        (root / "trace" / SNAPSHOT_FILE).write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError:
        return False
    return True


# ── 图片缓存 ──────────────────────────────────────────────────────────

def _file_digest(path: Path) -> Optional[str]:
    try:
        digest = hashlib.sha256()
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _cache_one(
    session_id: str,
    source: Path,
    *,
    bucket: str,
    label: str = "",
    meta: dict[str, Any] | None = None,
) -> Optional[str]:
    """把一个图片复制进工作区；同一份内容只存一份，返回相对路径。"""
    source = Path(source)
    if not source.is_file() or not is_image(source):
        return None
    root = workspace_dir(session_id)
    target_dir = root / bucket
    meta = meta if meta is not None else _read_meta(root)
    cached = meta.setdefault("cached", {})
    key = str(source)

    try:
        stat = source.stat()
    except OSError:
        return None
    known = cached.get(key)
    if isinstance(known, dict):
        dest = root / str(known.get("dest") or "")
        if dest.is_file() and int(known.get("size") or -1) == stat.st_size:
            return str(Path(bucket) / dest.name)

    digest = _file_digest(source)
    if digest is None:
        return None
    # 内容相同的图（例：同一张素材换了个名字）复用同一份副本
    for entry in cached.values():
        if isinstance(entry, dict) and entry.get("sha256") == digest:
            dest = root / str(entry.get("dest") or "")
            if dest.is_file():
                cached[key] = {
                    "dest": str(entry.get("dest")),
                    "sha256": digest,
                    "size": stat.st_size,
                    "bucket": entry.get("bucket") or bucket,
                    "source": str(source),
                }
                return str(Path(bucket) / dest.name)

    prefix = _safe_name(label or source.stem, fallback="image", limit=32)
    dest_name = f"{prefix}__{digest[:10]}{source.suffix.lower()}"
    dest = target_dir / dest_name
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        if not dest.is_file():
            shutil.copyfile(source, dest)
    except OSError:
        return None
    rel = str(Path(bucket) / dest_name)
    cached[key] = {
        "dest": rel,
        "sha256": digest,
        "size": stat.st_size,
        "bucket": bucket,
        "source": str(source),
    }
    return rel


def sync_assets(session_id: str, assets: Iterable[Any]) -> list[str]:
    """把素材区图片缓存进 ``assets/``。返回本次确认缓存的相对路径。"""
    if not exists(session_id):
        return []
    root = workspace_dir(session_id)
    meta = _read_meta(root)
    out: list[str] = []
    for asset in assets or []:
        if not isinstance(asset, dict):
            continue
        if str(asset.get("role") or "") in GENERATED_ROLES:
            continue  # 产出图归 generated/
        rel = _cache_one(
            session_id,
            Path(str(asset.get("path") or "")),
            bucket="assets",
            label=str(asset.get("name") or "") or str(asset.get("id") or ""),
            meta=meta,
        )
        if rel:
            out.append(rel)
    meta["updated_at"] = _now()
    _write_meta(root, meta)
    return out


def sync_generated(
    session_id: str,
    assets: Iterable[Any] = (),
    *,
    probe_dir: Path | None = None,
) -> list[str]:
    """把生成过的图片缓存进 ``generated/``。

    来源有两处：素材区里 role 为 ``probe_out``/``annotation`` 的产出图，
    以及本次会话的探针输出目录（``screenshots/collab_probe/<session_id>/``）。
    """
    if not exists(session_id):
        return []
    root = workspace_dir(session_id)
    meta = _read_meta(root)
    out: list[str] = []
    for asset in assets or []:
        if not isinstance(asset, dict):
            continue
        if str(asset.get("role") or "") not in GENERATED_ROLES:
            continue
        rel = _cache_one(
            session_id,
            Path(str(asset.get("path") or "")),
            bucket="generated",
            label=str(asset.get("name") or "") or str(asset.get("id") or ""),
            meta=meta,
        )
        if rel:
            out.append(rel)
    directory = Path(probe_dir) if probe_dir is not None else _default_probe_dir(session_id)
    if directory is not None and directory.is_dir():
        try:
            files = sorted(p for p in directory.rglob("*") if is_image(p))
        except OSError:
            files = []
        for path in files:
            rel = _cache_one(session_id, path, bucket="generated", meta=meta)
            if rel:
                out.append(rel)
    meta["updated_at"] = _now()
    _write_meta(root, meta)
    return sorted(set(out))


def _default_probe_dir(session_id: str) -> Optional[Path]:
    try:
        from backend.script_generator.collaborator.probe_script import PROBE_OUT_ROOT

        return PROBE_OUT_ROOT / str(session_id)
    except Exception:
        return None


def sync_all(session_id: str, session: Any) -> dict[str, list[str]]:
    """一次把素材、产出、快照都同步好（落盘防抖时调用）。"""
    if not exists(session_id):
        return {"assets": [], "generated": [], "snapshot": False}
    assets = list(getattr(session, "assets", None) or [])
    return {
        "assets": sync_assets(session_id, assets),
        "generated": sync_generated(session_id, assets),
        "snapshot": save_snapshot(session_id, session),
    }


def summary(session_id: str) -> dict[str, Any]:
    """看一眼工作区里有什么（诊断用）。"""
    root = workspace_dir(session_id)
    if not root.is_dir():
        return {"exists": False, "path": str(root)}
    meta = _read_meta(root)

    def _count(path: Path) -> int:
        try:
            return sum(1 for _ in path.open(encoding="utf-8"))
        except OSError:
            return 0

    return {
        "exists": True,
        "path": str(root),
        "created_at": meta.get("created_at"),
        "updated_at": meta.get("updated_at"),
        "goal_text": meta.get("goal_text"),
        "assets": len(list((root / "assets").glob("*"))) if (root / "assets").is_dir() else 0,
        "generated": len(list((root / "generated").glob("*")))
        if (root / "generated").is_dir()
        else 0,
        "subturns": _count(root / "trace" / SUBTURN_TRACE),
        "events": _count(root / "trace" / EVENT_TRACE),
        "turns": _count(root / "trace" / TURN_TRACE),
        "snapshot": (root / "trace" / SNAPSHOT_FILE).is_file(),
    }


def delete(session_id: str) -> bool:
    """删除整个工作区（用户明确要求时才调用）。"""
    root = workspace_dir(session_id)
    if not root.is_dir():
        return True
    try:
        shutil.rmtree(root)
        return True
    except OSError:
        return False


class SessionWorkspace:
    """绑定一个会话 id 的薄封装，供 GUI 与适配器复用。"""

    def __init__(self, session_id: str) -> None:
        self.session_id = str(session_id or "")

    @property
    def path(self) -> Path:
        return workspace_dir(self.session_id)

    def exists(self) -> bool:
        return exists(self.session_id)

    def ensure(self, *, goal_text: str = "") -> Optional[Path]:
        return ensure(self.session_id, goal_text=goal_text)

    def sync_all(self, session: Any) -> dict[str, list[str]]:
        return sync_all(self.session_id, session)

    def record_subturn(self, record: dict[str, Any]) -> bool:
        return record_subturn(self.session_id, record)

    def record_event(self, event_type: str, payload: dict[str, Any] | None = None) -> bool:
        return record_event(self.session_id, event_type, payload)

    def record_turn(self, record: dict[str, Any]) -> bool:
        return record_turn(self.session_id, record)

    def summary(self) -> dict[str, Any]:
        return summary(self.session_id)
