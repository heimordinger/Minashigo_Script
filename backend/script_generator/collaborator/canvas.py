"""协作共同视口：会话素材区 + 标注层（匹配框 / 排除区 / 落点）。"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any, Optional

from core.path import PROJECT_ROOT

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
MAX_FOCUSED = 2


def _new_id(prefix: str = "a") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def normalize_asset(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    path = str(raw.get("path") or "").strip()
    if not path:
        return None
    p = Path(path)
    aid = str(raw.get("id") or "").strip() or _new_id("asset")
    role = str(raw.get("role") or "template").strip() or "template"
    name = str(raw.get("name") or p.name).strip() or p.name
    return {
        "id": aid,
        "path": str(p),
        "name": name,
        "role": role,
        "caption": str(raw.get("caption") or "").strip(),
        "group": str(raw.get("group") or "").strip(),
        "file_name": str(raw.get("file_name") or p.name).strip() or p.name,
    }


def normalize_overlay(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or "").strip()
    if kind not in ("match", "ban", "click", "note"):
        return None
    oid = str(raw.get("id") or "").strip() or _new_id("ov")
    out: dict[str, Any] = {
        "id": oid,
        "kind": kind,
        "label": str(raw.get("label") or "").strip(),
        "asset_id": str(raw.get("asset_id") or "").strip(),
    }
    for k in ("x", "y", "w", "h", "score", "threshold"):
        if k in raw and raw[k] is not None:
            try:
                out[k] = float(raw[k])
            except Exception:
                pass
    # 归一化矩形（相对底图像素 0~1），画排除区优先用这个
    for k in ("nx0", "ny0", "nx1", "ny1"):
        if k in raw and raw[k] is not None:
            try:
                out[k] = float(raw[k])
            except Exception:
                pass
    if raw.get("color"):
        out["color"] = str(raw["color"])
    return out


_ASSET_REF_RE = re.compile(r"\[\[([^\]]+?)\]\]|⟦([^⟧]+?)⟧")
_BARE_ASSET_ID_RE = re.compile(r"\b(asset_[a-f0-9]{6,12})\b", re.IGNORECASE)


def asset_display_name(asset: dict[str, Any] | None) -> str:
    if not isinstance(asset, dict):
        return ""
    fname = str(asset.get("file_name") or Path(str(asset.get("path") or "")).name)
    return str(asset.get("name") or fname).strip() or fname or str(asset.get("id") or "")


def assets_summary_for_prompt(assets: list[dict], focused: list[str]) -> str:
    """给 LLM 看：稳定 id 为主；用户别名仅作理解用户话的对照。"""
    if not assets:
        return "（暂无会话素材；可用「导入素材」或工具挂模板）"
    focus = set(focused or [])
    lines = []
    for a in assets[:40]:
        mark = "★" if a.get("id") in focus else "·"
        aid = str(a.get("id") or "")
        cap = str(a.get("caption") or "").strip()
        cap_s = f" — {cap[:40]}" if cap else ""
        user_name = asset_display_name(a)
        role = a.get("role") or "template"
        # LLM 认 [[id]]；user= 只帮助听懂用户口头叫法，不是新图
        lines.append(
            f"{mark} [[{aid}]] user=「{user_name}」 ({role}){cap_s}"
        )
    more = f"\n…共 {len(assets)} 张" if len(assets) > 40 else ""
    return "\n".join(lines) + more


def render_asset_refs_for_user(text: str, assets: list[dict] | None) -> str:
    """把模型回复里的 [[asset_id]] / 裸 id 换成用户别名。"""
    raw = text or ""
    if not raw:
        return raw
    by_id: dict[str, str] = {}
    for a in assets or []:
        if not isinstance(a, dict):
            continue
        aid = str(a.get("id") or "").strip()
        if not aid:
            continue
        by_id[aid] = asset_display_name(a) or aid

    def _label(aid: str) -> str:
        name = by_id.get(aid) or by_id.get(aid.lower())
        if not name:
            # 容错：大小写
            for k, v in by_id.items():
                if k.lower() == aid.lower():
                    name = v
                    break
        return f"「{name}」" if name else aid

    def _bracket(m: re.Match) -> str:
        aid = (m.group(1) or m.group(2) or "").strip()
        return _label(aid) if aid else m.group(0)

    out = _ASSET_REF_RE.sub(_bracket, raw)

    def _bare(m: re.Match) -> str:
        aid = m.group(1)
        if aid in by_id or any(k.lower() == aid.lower() for k in by_id):
            return _label(aid)
        return aid

    out = _BARE_ASSET_ID_RE.sub(_bare, out)
    return out


def overlays_summary_for_prompt(overlays: list[dict]) -> str:
    if not overlays:
        return "（无标注）"
    lines = []
    for o in overlays[-20:]:
        kind = o.get("kind")
        label = o.get("label") or o.get("asset_id") or o.get("id")
        if kind == "match":
            lines.append(
                f"· match {label} score={o.get('score')} "
                f"@({o.get('x')},{o.get('y')}) {o.get('w')}x{o.get('h')}"
            )
        elif kind == "ban":
            if "nx0" in o:
                lines.append(
                    f"· ban {label} norm=({o.get('nx0'):.3f},{o.get('ny0'):.3f})"
                    f"-({o.get('nx1'):.3f},{o.get('ny1'):.3f})"
                )
            else:
                lines.append(
                    f"· ban {label} xywh=({o.get('x')},{o.get('y')},{o.get('w')},{o.get('h')})"
                )
        elif kind == "click":
            lines.append(f"· click {label} @({o.get('x')},{o.get('y')})")
        else:
            lines.append(f"· {kind} {label}")
    return "\n".join(lines)


def find_asset(assets: list[dict], asset_id: str | None = None, name: str = "") -> dict | None:
    aid = (asset_id or "").strip()
    if aid:
        for a in assets:
            if str(a.get("id")) == aid:
                return a
    nm = (name or "").strip().lower()
    if nm:
        for a in assets:
            if str(a.get("name") or "").lower() == nm:
                return a
            if str(a.get("file_name") or "").lower() == nm:
                return a
            if Path(str(a.get("path") or "")).name.lower() == nm:
                return a
            stem = Path(str(a.get("path") or "")).stem.lower()
            if stem == nm or stem == Path(nm).stem.lower():
                return a
    return None


def resolve_template_path(
    session,
    *,
    asset_id: str = "",
    template_path: str = "",
) -> tuple[Optional[Path], Optional[dict], str]:
    """返回 (path, asset_or_None, error)."""
    assets = list(getattr(session, "assets", None) or [])
    if asset_id:
        a = find_asset(assets, asset_id=asset_id)
        if a is None:
            return None, None, f"未知 asset_id：{asset_id}"
        p = Path(str(a.get("path") or ""))
        if not p.is_file():
            return None, a, f"素材文件不存在：{p}"
        return p, a, ""
    raw = (template_path or "").strip()
    if not raw:
        focused = list(getattr(session, "focused_asset_ids", None) or [])
        if focused:
            a = find_asset(assets, asset_id=focused[0])
            if a:
                p = Path(str(a.get("path") or ""))
                if p.is_file():
                    return p, a, ""
        return None, None, "请提供 asset_id 或 template_path"
    # 先当会话素材名
    a = find_asset(assets, name=raw)
    if a:
        p = Path(str(a.get("path") or ""))
        if p.is_file():
            return p, a, ""
    p = Path(raw)
    if p.is_file():
        return p, None, ""
    cand = PROJECT_ROOT / "assets" / raw
    if cand.is_file():
        return cand, None, ""
    # 相对 assets/images
    cand2 = PROJECT_ROOT / "assets" / "images" / raw
    if cand2.is_file():
        return cand2, None, ""
    return None, None, f"模板不存在：{raw}"


def add_asset_from_path(
    session,
    path: str | Path,
    *,
    role: str = "template",
    caption: str = "",
    group: str = "",
) -> dict[str, Any]:
    p = Path(path)
    assets = list(getattr(session, "assets", None) or [])
    for a in assets:
        if Path(str(a.get("path") or "")) == p:
            return a
    item = normalize_asset(
        {
            "id": _new_id("asset"),
            "path": str(p),
            "name": p.name,
            "role": role,
            "caption": caption,
            "group": group,
        }
    )
    assert item is not None
    # 尝试从识图目录补 caption
    if not item["caption"]:
        item["caption"] = _caption_from_catalog(p) or ""
    assets.append(item)
    session.assets = assets
    session.touch()
    return item


def remove_asset(session, asset_id: str) -> dict[str, Any] | None:
    """从素材区移出一张；同步清焦点与关联标注。返回被移除项，未找到则 None。"""
    aid = str(asset_id or "").strip()
    if not aid:
        return None
    assets = list(getattr(session, "assets", None) or [])
    removed = None
    kept = []
    for a in assets:
        if str(a.get("id")) == aid:
            removed = a
        else:
            kept.append(a)
    if removed is None:
        return None
    session.assets = kept
    focused = [
        x for x in (getattr(session, "focused_asset_ids", None) or []) if str(x) != aid
    ]
    session.focused_asset_ids = focused
    ov = list(getattr(session, "overlays", None) or [])
    session.overlays = [o for o in ov if str(o.get("asset_id") or "") != aid]
    session.touch()
    return removed


def rename_asset(session, asset_id: str, new_name: str) -> dict[str, Any] | None:
    """只改展示别名，不改 path/id/文件，不触碰识图缓存。

    返回更新后的素材 dict；未找到或空名则 None。
    同步：overlays label、逻辑步骤里的旧文件名/旧别名。
    """
    aid = str(asset_id or "").strip()
    name = str(new_name or "").strip()
    # 去掉路径分隔与首尾空白，避免当成换文件
    name = name.replace("\\", "/").split("/")[-1].strip()
    if not aid or not name:
        return None
    # 过长截断，方便气泡显示
    if len(name) > 64:
        name = name[:64].rstrip()
    assets = list(getattr(session, "assets", None) or [])
    updated = None
    old_name = ""
    file_name = ""
    for a in assets:
        if str(a.get("id")) != aid:
            continue
        # 保留磁盘文件名，便于提示词与找回
        path = Path(str(a.get("path") or ""))
        if not a.get("file_name"):
            a["file_name"] = path.name
        file_name = str(a.get("file_name") or path.name)
        old_name = str(a.get("name") or file_name)
        a["name"] = name
        updated = a
        break
    if updated is None:
        return None
    # 同步标注层上的口语 label，避免仍显示旧名
    ov = list(getattr(session, "overlays", None) or [])
    changed_ov = False
    for o in ov:
        if str(o.get("asset_id") or "") != aid:
            continue
        lab = str(o.get("label") or "")
        if not lab or lab == old_name or lab == file_name:
            o["label"] = name
            changed_ov = True
    if changed_ov:
        session.overlays = ov
    # 同步逻辑步骤文案（文件名 / 旧别名 → 新别名），并记下 asset_id
    _rewrite_logic_asset_labels(
        session,
        asset_id=aid,
        old_name=old_name,
        file_name=file_name,
        new_name=name,
    )
    session.assets = assets
    session.touch()
    return updated


def _rewrite_logic_asset_labels(
    session,
    *,
    asset_id: str,
    old_name: str,
    file_name: str,
    new_name: str,
) -> None:
    logic = getattr(session, "logic", None)
    if logic is None or not getattr(logic, "steps", None):
        return
    olds = [x for x in (old_name, file_name, Path(file_name).stem) if x]
    # 去重保序
    seen = set()
    keys: list[str] = []
    for x in olds:
        s = str(x).strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            keys.append(s)
    if not keys:
        return
    changed = False
    for step in logic.steps:
        for attr in ("label", "hint", "when"):
            text = str(getattr(step, attr, "") or "")
            if not text:
                continue
            new_text = text
            for k in keys:
                if k in new_text:
                    new_text = new_text.replace(k, new_name)
                # 「旧名」形式
                quoted = f"「{k}」"
                if quoted in new_text:
                    new_text = new_text.replace(quoted, f"「{new_name}」")
            if new_text != text:
                setattr(step, attr, new_text)
                changed = True
        # bind.images 记下 id，供缩略图
        bind = getattr(step, "bind", None)
        if bind is not None:
            imgs = list(getattr(bind, "images", None) or [])
            if asset_id not in imgs:
                # 步骤文案里若提到该素材，挂上 id
                blob = f"{getattr(step, 'label', '')} {getattr(step, 'hint', '')}"
                if any(k in blob for k in keys) or new_name in blob:
                    imgs = ([asset_id] + imgs)[:4]
                    bind.images = imgs
                    changed = True
    if changed:
        session.touch()


def enrich_logic_steps_for_ui(
    steps: list[dict[str, Any]] | None,
    assets: list[dict] | None,
) -> list[dict[str, Any]]:
    """逻辑卡展示：别名替换 + 挂上缩略图路径。"""
    from backend.script_generator.logic_graph import (
        display_action,
        param_for_ui,
        parse_step_params,
    )

    assets = list(assets or [])
    by_id = {str(a.get("id")): a for a in assets if a.get("id")}
    # 文件名 / stem / 别名 → asset
    by_key: dict[str, dict] = {}
    for a in assets:
        keys = [
            str(a.get("id") or ""),
            str(a.get("name") or ""),
            str(a.get("file_name") or ""),
            Path(str(a.get("path") or "")).name,
            Path(str(a.get("file_name") or a.get("path") or "")).stem,
        ]
        for k in keys:
            kk = k.strip().lower()
            if kk and kk not in by_key:
                by_key[kk] = a

    out: list[dict[str, Any]] = []
    for raw in steps or []:
        card = dict(raw)
        label = str(card.get("label") or "")
        hint = str(card.get("hint") or "")
        # [[id]] → 「别名」
        label = render_asset_refs_for_user(label, assets)
        hint = render_asset_refs_for_user(hint, assets)
        # 裸文件名 → 「别名」
        for a in assets:
            fname = str(a.get("file_name") or Path(str(a.get("path") or "")).name)
            disp = asset_display_name(a)
            if not fname or not disp:
                continue
            if fname in label:
                label = label.replace(fname, disp)
            if fname in hint:
                hint = hint.replace(fname, disp)
            stem = Path(fname).stem
            if stem and stem != disp and stem in label and f"「{stem}」" not in label:
                # 避免误伤过短 stem
                if len(stem) >= 6:
                    label = label.replace(stem, disp)
        card["label"] = label
        if hint:
            card["hint"] = hint
        params = parse_step_params(label, card.get("params") if isinstance(card.get("params"), dict) else None)
        card["params"] = params
        card["display_label"] = display_action(label)
        card["param"] = param_for_ui(params, label=label)

        aid = ""
        imgs = card.get("images") or card.get("bind_images") or []
        if isinstance(imgs, list) and imgs:
            aid = str(imgs[0] or "").strip()
        if not aid:
            # 从文案猜
            blob = f"{label} {hint}"
            m = re.search(r"\[\[([^\]]+)\]\]", blob)
            if m:
                aid = m.group(1).strip()
            else:
                for token in re.findall(
                    r"「([^」]+)」|(?:snip_\d[\w.-]+)|([\w.-]+\.(?:png|jpg|jpeg|webp))",
                    blob,
                    flags=re.IGNORECASE,
                ):
                    if isinstance(token, tuple):
                        cand = next((t for t in token if t), "")
                    else:
                        cand = token
                    a = by_key.get(str(cand).strip().lower())
                    if a:
                        aid = str(a.get("id") or "")
                        break
        asset = by_id.get(aid) if aid else None
        if asset is None and aid:
            asset = by_key.get(aid.lower())
        if asset:
            card["asset_id"] = str(asset.get("id") or "")
            card["thumb_path"] = str(asset.get("path") or "")
            card["thumb_name"] = asset_display_name(asset)
        out.append(card)
    return out


def import_assets_dir(session, directory: str | Path, *, limit: int = 80) -> list[dict]:
    root = Path(directory)
    if not root.is_dir():
        return []
    catalog = _load_catalog_map(root)
    added = []
    for p in sorted(root.iterdir()):
        if not p.is_file() or p.suffix.lower() not in IMAGE_EXTS:
            continue
        cap = catalog.get(p.name.lower()) or catalog.get(p.stem.lower()) or ""
        item = add_asset_from_path(session, p, caption=cap, group=root.name)
        added.append(item)
        if len(added) >= limit:
            break
    return added


def set_focused(session, asset_ids: list[str]) -> list[str]:
    assets = {str(a.get("id")) for a in (getattr(session, "assets", None) or [])}
    out = []
    for aid in asset_ids or []:
        s = str(aid).strip()
        if s in assets and s not in out:
            out.append(s)
        if len(out) >= MAX_FOCUSED:
            break
    session.focused_asset_ids = out
    session.touch()
    return out


def clear_overlays(session, *, kinds: Optional[list[str]] = None) -> None:
    ov = list(getattr(session, "overlays", None) or [])
    if kinds:
        ks = set(kinds)
        ov = [o for o in ov if o.get("kind") not in ks]
    else:
        ov = []
    session.overlays = ov
    session.touch()


def upsert_match_overlay(
    session,
    *,
    asset: dict | None,
    match: dict,
    template_wh: tuple[int, int] | None = None,
) -> dict:
    """根据 match 结果写 match 标注（中心点 + 模板宽高估框）。"""
    x = match.get("x")
    y = match.get("y")
    tw, th = template_wh or (40, 40)
    try:
        cx, cy = float(x), float(y)
    except Exception:
        return {}
    box = {
        "id": _new_id("match"),
        "kind": "match",
        "label": (asset or {}).get("name") or Path(str(match.get("template") or "")).name,
        "asset_id": (asset or {}).get("id") or "",
        "x": cx - tw / 2,
        "y": cy - th / 2,
        "w": float(tw),
        "h": float(th),
        "score": match.get("score"),
        "threshold": match.get("threshold"),
        "color": "#5b8def",
    }
    # 同 asset 只保留最新 match
    ov = [
        o
        for o in (getattr(session, "overlays", None) or [])
        if not (
            o.get("kind") == "match"
            and box["asset_id"]
            and o.get("asset_id") == box["asset_id"]
        )
    ]
    item = normalize_overlay(box)
    if item:
        ov.append(item)
        session.overlays = ov
        session.touch()
        return item
    return {}


def add_ban_overlay(
    session,
    *,
    nx0: float,
    ny0: float,
    nx1: float,
    ny1: float,
    label: str = "排除区",
) -> dict:
    x0, x1 = sorted((float(nx0), float(nx1)))
    y0, y1 = sorted((float(ny0), float(ny1)))
    x0, x1 = max(0.0, min(1.0, x0)), max(0.0, min(1.0, x1))
    y0, y1 = max(0.0, min(1.0, y0)), max(0.0, min(1.0, y1))
    if x1 - x0 < 0.005 or y1 - y0 < 0.005:
        return {}
    item = normalize_overlay(
        {
            "id": _new_id("ban"),
            "kind": "ban",
            "label": label or "排除区",
            "nx0": x0,
            "ny0": y0,
            "nx1": x1,
            "ny1": y1,
            "color": "#e85d5d",
        }
    )
    if not item:
        return {}
    ov = list(getattr(session, "overlays", None) or [])
    ov.append(item)
    session.overlays = ov
    session.touch()
    return item


def ban_overlays_as_script_snippet(overlays: list[dict]) -> str:
    """把排除区导出成可粘进脚本的归一化矩形常量。"""
    bans = [o for o in overlays if o.get("kind") == "ban" and "nx0" in o]
    if not bans:
        return ""
    lines = [
        "# 协作共同视口：用户标注排除区（归一化 x0,x1,y0,y1）",
        "_COLLAB_BAN_ZONES = [",
    ]
    for i, o in enumerate(bans):
        lab = str(o.get("label") or f"ban{i}").replace('"', "")
        lines.append(
            f"    ({o['nx0']:.4f}, {o['nx1']:.4f}, {o['ny0']:.4f}, {o['ny1']:.4f}),  # {lab}"
        )
    lines.append("]")
    lines.append("")
    lines.append(
        "def _collab_banned(x, y, w, h):\n"
        "    for x0, x1, y0, y1 in _COLLAB_BAN_ZONES:\n"
        "        if (w * x0) <= x <= (w * x1) and (h * y0) <= y <= (h * y1):\n"
        "            return True\n"
        "    return False"
    )
    return "\n".join(lines)


def _load_catalog_map(directory: Path) -> dict[str, str]:
    """粗解析 识图目录.txt → name.lower() -> caption。"""
    path = directory / "识图目录.txt"
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return {}
    out: dict[str, str] = {}
    # ### name.png / name.png：
    for m in re.finditer(
        r"(?:###\s*)?([^\s：:\n]+\.(?:png|jpg|jpeg|webp|bmp))\s*[：:]\s*(.+)",
        text,
        re.I,
    ):
        name, cap = m.group(1), m.group(2).strip()
        out[name.lower()] = cap[:120]
        out[Path(name).stem.lower()] = cap[:120]
    return out


def _caption_from_catalog(path: Path) -> str:
    return _load_catalog_map(path.parent).get(path.name.lower()) or ""


def template_size(path: Path) -> tuple[int, int]:
    try:
        import cv2
        import numpy as np

        data = np.fromfile(str(path), dtype=np.uint8)
        img = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if img is None:
            return 40, 40
        h, w = img.shape[:2]
        return int(w), int(h)
    except Exception:
        return 40, 40
