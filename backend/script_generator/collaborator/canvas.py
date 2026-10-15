"""协作共同视口：会话素材区 + 标注层（匹配框 / 用户标记 / 落点）。"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any, Optional

from core.path import PROJECT_ROOT

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
MAX_FOCUSED = 2
# 模型自己画出来的标注图（圈/框/编号）。进素材区给人看，但不是场景素材：
# 不能被当成待识别的界面内容，兜底匹配也不该拿它当模板。
ANNOTATION_GROUP = "标注"
ANNOTATION_ROLE = "annotation"
# 探针脚本 save_image 写出的图（标注图 / 拼版预览 / 自检图…）同样是"产出物"，
# 不是游戏界面本身。以前它们默认 role="template"，于是被当场景素材反复送识图，
# 形成"用自己画出来的图再去识图"的自反馈闭环。
# 显式引用（match_template asset_id / crop_asset）仍然可用，只是不再自动当界面内容。
PROBE_GROUP = "探针"
PROBE_GROUP_ALIASES = ("探针", "探针产出")
PROBE_ROLE = "probe_out"

_NON_SCENE_ROLES = frozenset({ANNOTATION_ROLE, PROBE_ROLE})
_NON_SCENE_GROUPS = frozenset({ANNOTATION_GROUP, *PROBE_GROUP_ALIASES})


def is_annotation_asset(asset: dict[str, Any] | None) -> bool:
    """模型画出来的标注图（圈/框/编号）。"""
    if not isinstance(asset, dict):
        return False
    return (
        str(asset.get("role") or "").strip() == ANNOTATION_ROLE
        or str(asset.get("group") or "").strip() == ANNOTATION_GROUP
    )


def non_scene_note(asset: dict[str, Any] | None) -> str:
    """非场景素材在清单里的一句话说明；是场景素材则返回空串。"""
    if is_scene_asset(asset):
        return ""
    if is_annotation_asset(asset):
        return "（标注图，不是场景素材）"
    return "（探针产出图，不是场景素材；要当模板请显式用它的 asset_id）"


def is_scene_asset(asset: dict[str, Any] | None) -> bool:
    """这张素材能不能当作"界面内容"来看/来匹配。

    Phase B 收口后，这条判据**只有一份**：``resource_binding.is_auto_scene``
    （产出物 ``probe_out``、标注图都不在其中 —— 它们只有被显式点名才会被使用）。
    """
    if not isinstance(asset, dict):
        return False
    from backend.script_generator.resource_binding import is_auto_scene

    return is_auto_scene(asset)


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
    if kind not in ("match", "ban", "mark", "click", "note"):
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
    shape = str(raw.get("shape") or "").strip()
    if shape in ("rect", "stroke"):
        out["shape"] = shape
    if shape == "stroke" and isinstance(raw.get("points"), list):
        points = []
        for point in raw["points"][:2000]:
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                continue
            try:
                points.append([
                    max(0.0, min(1.0, float(point[0]))),
                    max(0.0, min(1.0, float(point[1]))),
                ])
            except Exception:
                continue
        if len(points) >= 2:
            out["points"] = points
    if raw.get("color"):
        out["color"] = str(raw["color"])
    return out


_VIEWPORT_MARK_SETS = "viewport_mark_sets"


def _viewport_image_key(path: str | Path | None) -> str:
    if not path:
        return ""
    try:
        return str(Path(path).resolve()).replace("\\", "/").lower()
    except Exception:
        return str(path).replace("\\", "/").lower()


def save_current_viewport_marks(session) -> None:
    """把当前底图的标记顺序存入会话；顺序本身就是跨窗口的撤销栈。"""
    key = _viewport_image_key(getattr(session, "frame_path", None))
    if not key:
        return
    if not isinstance(getattr(session, "artifacts", None), dict):
        session.artifacts = {}
    sets = session.artifacts.setdefault(_VIEWPORT_MARK_SETS, {})
    if not isinstance(sets, dict):
        sets = {}
        session.artifacts[_VIEWPORT_MARK_SETS] = sets
    sets[key] = [
        dict(o)
        for o in (getattr(session, "overlays", None) or [])
        if isinstance(o, dict) and o.get("kind") == "mark"
    ]


def activate_viewport_image(session, path: str | Path) -> None:
    """切换共同视口底图，并恢复这张图自己的标记。"""
    new_path = Path(path)
    old_key = _viewport_image_key(getattr(session, "frame_path", None))
    new_key = _viewport_image_key(new_path)
    if old_key == new_key:
        session.frame_path = new_path
        return
    save_current_viewport_marks(session)
    art = getattr(session, "artifacts", None)
    sets = art.get(_VIEWPORT_MARK_SETS) if isinstance(art, dict) else None
    restored = sets.get(new_key, []) if isinstance(sets, dict) else []
    session.frame_path = new_path
    # 匹配框等属于旧画面，不跨图；只恢复用户对这张图的独立标记层。
    session.overlays = [dict(o) for o in restored if isinstance(o, dict)]


def replace_current_viewport_marks(session, marks: list[dict]) -> None:
    """替换当前图片的标记，同时保留当前图片的非标记覆盖层。"""
    preserved = [
        o for o in (getattr(session, "overlays", None) or [])
        if not (isinstance(o, dict) and o.get("kind") == "mark")
    ]
    session.overlays = preserved + [dict(o) for o in marks if isinstance(o, dict)]
    save_current_viewport_marks(session)
    session.touch()


_ASSET_REF_RE = re.compile(r"\[\[([^\]]+?)\]\]|⟦([^⟧]+?)⟧")
_BARE_ASSET_ID_RE = re.compile(r"\b(asset_[a-f0-9]{6,12})\b", re.IGNORECASE)


def asset_display_name(asset: dict[str, Any] | None) -> str:
    if not isinstance(asset, dict):
        return ""
    fname = str(asset.get("file_name") or Path(str(asset.get("path") or "")).name)
    return str(asset.get("name") or fname).strip() or fname or str(asset.get("id") or "")


def assets_summary_for_prompt(assets: list[dict], focused: list[str] | None = None) -> str:
    """给 LLM 看：稳定 id 为主；用户别名仅作理解用户话的对照。

    没有 focus 了，所以不标星；改为标出「看过/没看过」——这是模型下一步
    该不该 inspect_asset 的依据。
    """
    if not assets:
        return "（素材区是空的）"
    lines = []
    for a in assets[:40]:
        aid = str(a.get("id") or "")
        cap = str(a.get("caption") or "").strip()
        cap_s = f" — {cap[:40]}" if cap else ""
        user_name = asset_display_name(a)
        role = a.get("role") or "template"
        note = non_scene_note(a)
        # LLM 认 [[id]]；user= 只帮助听懂用户口头叫法，不是新图
        lines.append(
            f"- [[{aid}]] user=「{user_name}」 ({role}){cap_s}{note}"
        )
    more = f"\n…共 {len(assets)} 张，用 list_session_assets 查全部" if len(assets) > 40 else ""
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
        elif kind == "mark":
            if o.get("shape") == "stroke":
                pts = o.get("points") or []
                if pts:
                    xs = [float(p[0]) for p in pts]
                    ys = [float(p[1]) for p in pts]
                    stride = max(1, len(pts) // 8)
                    sampled = pts[::stride][:8]
                    lines.append(
                        f"· mark-stroke {label} candidate={o.get('id')} points={len(pts)} "
                        f"bbox=({min(xs):.3f},{min(ys):.3f})-({max(xs):.3f},{max(ys):.3f}) "
                        f"sample={sampled}"
                    )
            elif "nx0" in o:
                lines.append(
                    f"· mark-rect {label} candidate={o.get('id')} "
                    f"norm=({o.get('nx0'):.3f},{o.get('ny0'):.3f})"
                    f"-({o.get('nx1'):.3f},{o.get('ny1'):.3f})"
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
    use: str = "explicit_match",
) -> tuple[Optional[Path], Optional[dict], str]:
    """返回 (path, asset_or_None, error)。

    Phase B（设计 §4）：素材引用统一走 :class:`ResourceBinder`，错误一律带
    **稳定诊断码**（``RESOURCE_NOT_FOUND`` / ``RESOURCE_VERSION_EXPIRED`` /
    ``RESOURCE_TYPE_MISMATCH`` …）。这里不再自己做状态判断，也不猜名字/退回焦点素材。

    ``use`` 是**用途**（不是类型/角色）：显式点名的模板匹配用 ``explicit_match``，
    只是"看一眼"用 ``explicit_view``。``probe_out``/``crop`` 这类产出物有
    ``explicit_match``，所以显式按 id 指定时照样能用；标注图没有，仍然被挡住。
    """
    from backend.script_generator.resource_binding import (
        ResourceBinder,
        ResourceBindingError,
        SessionResourceSource,
    )

    assets = list(getattr(session, "assets", None) or [])
    if asset_id:
        binder = ResourceBinder(SessionResourceSource(session), origin="tool_call")
        try:
            bound = binder.bind(str(asset_id), uses=(str(use or "explicit_match"),))
        except ResourceBindingError as exc:
            return None, None, f"{exc.code}: {exc.failure.message}"
        a = find_asset(assets, asset_id=bound.resource_id)
        return Path(bound.path), a, ""
    raw = (template_path or "").strip()
    if not raw:
        # 不再静默回退到焦点素材（设计 §4.2）：要哪张就点名。
        return None, None, "请提供 asset_id 或 template_path"
    # 先当会话素材名
    a = find_asset(assets, name=raw)
    if a:
        p = Path(str(a.get("path") or ""))
        if p.is_file():
            return p, a, ""
    p = Path(raw)
    if p.is_file():
        from backend.script_generator.collaborator.resource_registry import resource_for_path

        known = resource_for_path(session, p)
        if known:
            # 路径能对上已登记资源 → 用同一套绑定规则判定（状态/内容/hash 一次判清）
            binder = ResourceBinder(SessionResourceSource(session), origin="tool_call")
            try:
                bound = binder.bind(
                    str(known.get("resource_id") or ""),
                    version=int(known.get("version") or 1),
                )
            except ResourceBindingError as exc:
                return None, None, f"{exc.code}: {exc.failure.message}"
            return Path(bound.path), known, ""
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
            from backend.script_generator.collaborator.resource_registry import register_asset

            register_asset(session, a, status="active")
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
    from backend.script_generator.collaborator.resource_registry import register_asset

    register_asset(session, item, status="active")
    session.touch()
    return item


#: 粘贴进素材区的图片统一命名：``图片_1``、``图片_2`` …
PASTE_ASSET_NAME_PREFIX = "图片_"


def next_paste_asset_names(
    assets: list[dict[str, Any]] | None,
    count: int,
    *,
    prefix: str = PASTE_ASSET_NAME_PREFIX,
) -> list[str]:
    """给"粘贴进素材区"的图片生成统一命名：``图片_1`` … ``图片_n``。

    序号是**单调递增**的：取现存同名样式里最大的编号 +1，而不是补空位
    （删掉中间某张再粘贴不会复用旧编号 —— 旧编号可能已经写进逻辑草稿 /
    历史消息的引用里，复用会指错图）。非本样式的名字（``图片``、``我的图片_3``）
    不参与计数。
    """
    used = {str(a.get("name") or "").strip() for a in (assets or [])}
    pattern = re.compile(rf"^{re.escape(str(prefix))}(\d+)$")
    highest = 0
    for name in used:
        m = pattern.match(name)
        if m:
            highest = max(highest, int(m.group(1)))
    names: list[str] = []
    n = highest
    for _ in range(max(0, int(count))):
        n += 1
        name = f"{prefix}{n}"
        while name in used:  # 兜底：绝不重名
            n += 1
            name = f"{prefix}{n}"
        used.add(name)
        names.append(name)
    return names


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
    from backend.script_generator.collaborator.resource_registry import archive_resource

    archive_resource(session, aid)
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
    from backend.script_generator.collaborator.resource_registry import register_asset

    register_asset(session, updated, status="active")
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


def show_asset_in_viewport(session, asset_id: str) -> dict:
    """把素材设为共同视口底图；仅改本地会话，不触发新的模型回合。"""
    from backend.script_generator.collaborator.resource_registry import (
        resolve_asset_resource,
    )

    resource, resource_error = resolve_asset_resource(session, asset_id)
    if resource_error:
        return {"ok": False, "error": resource_error}
    asset = find_asset(list(getattr(session, "assets", None) or []), asset_id=asset_id)
    if asset is None:
        return {"ok": False, "error": f"素材不存在：{asset_id}"}
    path = Path(str((resource or {}).get("path") or asset.get("path") or ""))
    activate_viewport_image(session, path)
    session.focused_asset_ids = [str(asset.get("id") or asset_id)]
    # 视觉定位缓存属于旧底图；标记由 activate_viewport_image 按图片恢复。
    if not isinstance(getattr(session, "artifacts", None), dict):
        session.artifacts = {}
    for key in (
        "assist_vision",
        "last_vision_desc",
        "vision_groundings",
        "repeated_region_fits",
        "canvas_dirty",
    ):
        session.artifacts.pop(key, None)
    session.artifacts["viewport_change"] = {
        "source": "tool",
        "asset_id": str(asset.get("id") or asset_id),
        "path": str(path),
        "name": str(asset.get("name") or path.name),
    }
    session.touch()
    return {
        "ok": True,
        "asset_id": str(asset.get("id") or asset_id),
        "name": str(asset.get("name") or path.name),
        "path": str(path),
        "note": "已在本地切换共同视口；未因此自动发送新消息",
    }


def clear_overlays(session, *, kinds: Optional[list[str]] = None) -> None:
    ov = list(getattr(session, "overlays", None) or [])
    if kinds:
        ks = set(kinds)
        ov = [o for o in ov if o.get("kind") not in ks]
    else:
        ov = []
    session.overlays = ov
    save_current_viewport_marks(session)
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


def replace_multi_match_overlays(
    session,
    *,
    asset: dict | None,
    matches: list[dict],
    threshold: float,
) -> list[dict]:
    """用一批多目标命中替换该模板之前的匹配框。"""
    asset_id = str((asset or {}).get("id") or "")
    label = str((asset or {}).get("name") or "模板")
    overlays = [
        o for o in (getattr(session, "overlays", None) or [])
        if not (o.get("kind") == "match" and asset_id and o.get("asset_id") == asset_id)
    ]
    added: list[dict] = []
    for index, match in enumerate(matches or [], 1):
        box = match.get("box") if isinstance(match, dict) else None
        if not isinstance(box, dict):
            continue
        item = normalize_overlay(
            {
                "id": _new_id("match"),
                "kind": "match",
                "label": f"{label} #{index}",
                "asset_id": asset_id,
                "x": float(box.get("x") or 0),
                "y": float(box.get("y") or 0),
                "w": max(1.0, float(box.get("w") or 1)),
                "h": max(1.0, float(box.get("h") or 1)),
                "score": match.get("score"),
                "threshold": threshold,
                "color": "#5b8def",
            }
        )
        if item:
            overlays.append(item)
            added.append(item)
    session.overlays = overlays
    session.touch()
    return added


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


def add_mark_rect_overlay(
    session,
    *,
    nx0: float,
    ny0: float,
    nx1: float,
    ny1: float,
    label: str = "矩形标记",
) -> dict:
    """添加通用矩形标记；不再默认解释为点击排除区。"""
    x0, x1 = sorted((float(nx0), float(nx1)))
    y0, y1 = sorted((float(ny0), float(ny1)))
    item = normalize_overlay(
        {
            "id": _new_id("mark"),
            "kind": "mark",
            "shape": "rect",
            "label": label,
            "nx0": max(0.0, min(1.0, x0)),
            "ny0": max(0.0, min(1.0, y0)),
            "nx1": max(0.0, min(1.0, x1)),
            "ny1": max(0.0, min(1.0, y1)),
            "color": "#f0a33b",
        }
    )
    if not item or item["nx1"] - item["nx0"] < 0.005 or item["ny1"] - item["ny0"] < 0.005:
        return {}
    session.overlays = list(getattr(session, "overlays", None) or []) + [item]
    save_current_viewport_marks(session)
    _register_user_mark_grounding(session, item)
    session.touch()
    return item


def add_mark_stroke_overlay(session, *, points: list, label: str = "画笔标记") -> dict:
    """添加归一化自由画笔轨迹。"""
    item = normalize_overlay(
        {
            "id": _new_id("mark"),
            "kind": "mark",
            "shape": "stroke",
            "label": label,
            "points": points,
            "color": "#f0a33b",
        }
    )
    if not item or len(item.get("points") or []) < 2:
        return {}
    session.overlays = list(getattr(session, "overlays", None) or []) + [item]
    save_current_viewport_marks(session)
    _register_user_mark_grounding(session, item)
    session.touch()
    return item


def mark_on_image(
    session,
    *,
    marks: Any,
    asset_id: str = "",
    template_path: str = "",
    name: str = "",
    title: str = "",
    show: bool = True,
) -> dict[str, Any]:
    """在指定图（默认共同视口底图）上圈出/框出给定坐标，产出新的标注图。

    这是"在图上标记"的正式入口：不必再手写临时探针脚本。坐标按原图像素理解
    （与 describe_current_frame 返回的 bbox 同一套）。产物登记进素材区
    （role=annotation），并在 show=True 时切到共同视口，用户立刻能看到。
    """
    from backend.script_generator.collaborator.annotation import render_marks
    from backend.script_generator.collaborator.asset_ops import probe_out_dir
    from backend.script_generator.vision_tools import _imread, _imwrite

    src, asset, err = resolve_template_path(
        session, asset_id=str(asset_id or ""), template_path=str(template_path or "")
    )
    if src is None and str(asset_id or "").strip() == "" and str(template_path or "").strip() == "":
        frame = getattr(session, "frame_path", None)
        if frame and Path(frame).is_file():
            src, asset, err = Path(frame), None, ""
    if err or src is None:
        return {"ok": False, "error": err or "找不到要标记的图（给 asset_id 或用共同视口）"}

    try:
        image = _imread(src)
    except Exception as e:
        return {"ok": False, "error": f"读不到图片：{e}"}

    canvas, dropped, items = render_marks(image, marks, title=str(title or ""))
    if not items:
        return {
            "ok": False,
            "error": "没有可画的标记；每项要给 rect=[x0,y0,x1,y1] 或 x/y（原图像素）",
            "dropped": dropped,
        }

    label = str(name or "").strip()
    if not label:
        stem = Path(str(src)).stem[:24] or "图"
        label = f"{stem}_标记{len(items)}处"
    if not Path(label).suffix:
        label = f"{label}.png"

    out_dir = probe_out_dir(session)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / Path(label).name
    try:
        _imwrite(dest, canvas)
    except Exception as e:
        return {"ok": False, "error": f"写入失败：{e}"}

    item = add_asset_from_path(
        session,
        dest,
        role=ANNOTATION_ROLE,
        caption=f"在图上标记 {len(items)} 处",
        group=ANNOTATION_GROUP,
    )
    from backend.script_generator.collaborator.resource_registry import register_asset

    register_asset(
        session,
        item,
        status="active",
        derived_from=[str(asset.get("id"))] if asset and asset.get("id") else [],
        provenance={
            "source": "mark_on_image",
            "source_path": str(src),
            "source_asset_id": str(asset.get("id") or "") if asset else "",
            "mark_count": len(items),
        },
    )
    if not isinstance(getattr(session, "artifacts", None), dict):
        session.artifacts = {}
    session.artifacts["canvas_dirty"] = True

    shown = False
    if show:
        res = show_asset_in_viewport(session, str(item.get("id") or ""))
        shown = bool(res.get("ok"))
    else:
        session.touch()

    return {
        "ok": True,
        "asset_id": item.get("id"),
        "name": item.get("name"),
        "path": str(dest),
        "count": len(items),
        "labels": [str(i.get("label") or "") for i in items][:12],
        "dropped": dropped,
        "in_viewport": shown,
        "note": (
            f"已画好 {len(items)} 处标记"
            + ("，并切到共同视口，用户能看到。" if shown else "。")
            + "标注图是给人看的，不是场景素材，别拿它当识别/匹配对象。"
        ),
    }


def _register_user_mark_grounding(session, item: dict) -> None:
    """用户手动画出的范围是高可信定位来源，可直接供 crop_asset 引用。"""
    frame_path = getattr(session, "frame_path", None)
    if not frame_path:
        return
    if item.get("shape") == "stroke":
        points = item.get("points") or []
        if len(points) < 2:
            return
        xs = [float(p[0]) for p in points]
        ys = [float(p[1]) for p in points]
        norm_bbox = [min(xs), min(ys), max(xs), max(ys)]
    else:
        norm_bbox = [item.get("nx0"), item.get("ny0"), item.get("nx1"), item.get("ny1")]
    try:
        from backend.script_generator.vision_tools import _imread

        image = _imread(Path(frame_path))
        height, width = image.shape[:2]
        bbox = [
            int(round(float(norm_bbox[0]) * width)),
            int(round(float(norm_bbox[1]) * height)),
            int(round(float(norm_bbox[2]) * width)),
            int(round(float(norm_bbox[3]) * height)),
        ]
    except Exception:
        return
    source_asset_id = ""
    for asset in list(getattr(session, "assets", None) or []):
        try:
            if Path(str(asset.get("path") or "")).resolve() == Path(frame_path).resolve():
                source_asset_id = str(asset.get("id") or "")
                break
        except Exception:
            continue
    if not isinstance(getattr(session, "artifacts", None), dict):
        session.artifacts = {}
    records = session.artifacts.setdefault("vision_groundings", [])
    if not isinstance(records, list):
        records = []
        session.artifacts["vision_groundings"] = records
    record = {
        "id": str(item.get("id") or ""),
        "candidate_id": str(item.get("id") or ""),
        "source_path": str(frame_path),
        "source_asset_id": source_asset_id,
        "bbox": bbox,
        "label": str(item.get("label") or "用户标记"),
        "confidence": 1.0,
        "mode": "user_mark",
    }
    records[:] = [r for r in records if not isinstance(r, dict) or r.get("id") != record["id"]]
    records.append(record)


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
