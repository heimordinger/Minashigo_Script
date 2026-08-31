"""从现有脚本 / 介绍 txt / JSON 导入 ScriptSpec。"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from script_spec.model import (
    ROLE_BUTTON,
    ROLE_ID,
    ROLE_OTHER,
    HelperSpec,
    ImageEntry,
    ScriptSpec,
    TaskSpec,
    ensure_image_name,
    refresh_dir_image_map,
)

_INTRO_NAMES = ("脚本介绍.txt", "脚本解释.txt")
_ID_NAME_RE = re.compile(r"(?i)^(logo|rank|.+_logo|.+_id)$")
_SKIP_FUNCS = {
    "do_work", "run_task", "check_guards", "register_guard",
    "run_step_chain", "_img", "_unpack_step",
}


def import_from_path(path: Path) -> tuple[ScriptSpec, str]:
    """返回 (spec, 说明)。path 可以是 .py / .txt / .json。"""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(str(path))
    suffix = path.suffix.lower()
    if suffix == ".json":
        spec = ScriptSpec.load_json(path)
        return spec, f"已加载 JSON：{path.name}"
    if suffix in {".txt", ".md"}:
        spec = ScriptSpec.from_explanation_text(
            path.read_text(encoding="utf-8"),
            source_dir=_infer_dir_from_intro(path),
        )
        return spec, f"已导入介绍：{path.name}"
    if suffix == ".py":
        return _import_python(path)
    raise ValueError(f"不支持的文件类型：{path.suffix}（请选 .py / .txt / .json）")


def _infer_dir_from_intro(path: Path) -> str:
    folder = path.parent
    try:
        from core.path import IMG_PATH
        folder.resolve().relative_to(Path(IMG_PATH).resolve())
        return str(folder)
    except Exception:
        pass
    if any(p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp"} for p in folder.iterdir() if p.is_file()):
        return str(folder)
    return ""


def _import_python(path: Path) -> tuple[ScriptSpec, str]:
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        raise ValueError(f"脚本语法错误：{e.msg}（第 {e.lineno} 行）") from e

    img_dir = _extract_image_dir(tree, path)
    intro = _find_intro_file(path, img_dir)
    if intro is not None:
        spec = ScriptSpec.from_explanation_text(
            intro.read_text(encoding="utf-8"),
            source_dir=str(img_dir) if img_dir else _infer_dir_from_intro(intro),
        )
        if img_dir and not spec.source_dir:
            spec.source_dir = str(img_dir)
        return spec, f"已导入 {path.name}（使用 {intro.name}）"

    spec = _spec_from_python(tree, path, img_dir)
    hint = "未找到脚本介绍.txt，已从代码摘要导入"
    return spec, f"已导入 {path.name}（{hint}）"


def _find_intro_file(py_path: Path, img_dir: Path | None) -> Path | None:
    candidates = [py_path.parent]
    if img_dir is not None:
        candidates.insert(0, img_dir)
    seen: set[str] = set()
    for folder in candidates:
        key = str(folder)
        if key in seen or not folder.is_dir():
            continue
        seen.add(key)
        for name in _INTRO_NAMES:
            p = folder / name
            if p.is_file():
                return p
    return None


def _const_str(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _img_path_parts(node: ast.AST) -> list[str] | None:
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.BinOp) and isinstance(cur.op, ast.Div):
        right = _const_str(cur.right)
        if right is None:
            return None
        parts.append(right)
        cur = cur.left
    if isinstance(cur, ast.Name) and cur.id == "IMG_PATH":
        parts.reverse()
        return parts
    return None


def _extract_image_dir(tree: ast.AST, py_path: Path) -> Path | None:
    from core.path import IMG_PATH

    named = (
        "IMG_DIR", "SCRIPT_PATH", "img_path", "img_dir", "my_path", "image_dir",
    )
    found: list[Path] = []
    for node in tree.body:
        target = None
        value = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target, value = node.targets[0].id, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        if target not in named or value is None:
            continue
        parts = _img_path_parts(value)
        if parts:
            found.append(Path(IMG_PATH, *parts))
            continue
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) and value.func.attr == "Path":
            if value.args:
                s = _const_str(value.args[0])
                if s:
                    found.append(Path(s))
    for p in found:
        if p.is_dir():
            return p

    # dataclass Config.img_dir = IMG_PATH / ...
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id in named and node.value is not None:
                parts = _img_path_parts(node.value)
                if parts:
                    p = Path(IMG_PATH, *parts)
                    if p.is_dir():
                        return p

    # 函数里 IMG_PATH / game / script / file  → 取出现最多的目录前缀
    counts: dict[str, int] = {}
    for node in ast.walk(tree):
        parts = _img_path_parts(node)
        if not parts:
            continue
        folder_parts = parts[:-1] if len(parts) >= 2 else parts
        folder = Path(IMG_PATH, *folder_parts)
        counts[str(folder)] = counts.get(str(folder), 0) + 1
    if counts:
        best = Path(max(counts, key=counts.get))
        if best.is_dir():
            return best

    # 文件名对图片目录
    stem = py_path.stem
    for root in (Path(IMG_PATH),):
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if p.is_dir() and p.name == stem:
                return p
    return None


def _call_img_name(call: ast.Call) -> str | None:
    if not isinstance(call.func, ast.Name) or call.func.id != "_img" or not call.args:
        return None
    return _const_str(call.args[0])


def _browser_call(node: ast.AST) -> tuple[str, ast.Call] | None:
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "browser":
        return func.attr, node
    return None


def _first_path_arg_name(call: ast.Call) -> str | None:
    if not call.args:
        return None
    arg0 = call.args[0]
    n = _call_img_name(arg0) if isinstance(arg0, ast.Call) else None
    if n:
        return n
    parts = _img_path_parts(arg0)
    if parts:
        return parts[-1]
    s = _const_str(arg0)
    if s:
        return Path(s).name
    if isinstance(arg0, ast.BinOp) and isinstance(arg0.op, ast.Div):
        right = _const_str(arg0.right)
        if right:
            return right
    return None


def _kw_num(call: ast.Call, name: str) -> str | None:
    for kw in call.keywords:
        if kw.arg == name:
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, (int, float)):
                return str(kw.value.value)
            if isinstance(kw.value, ast.Name):
                return kw.value.id
    return None


def _spec_from_python(
    tree: ast.Module,
    py_path: Path,
    img_dir: Path | None,
) -> ScriptSpec:
    goal = ast.get_docstring(tree) or ""
    goal = goal.strip().splitlines()[0].strip() if goal.strip() else py_path.stem

    click_imgs: set[str] = set()
    match_imgs: set[str] = set()
    for node in ast.walk(tree):
        hit = _browser_call(node)
        if not hit:
            continue
        method, call = hit
        name = _first_path_arg_name(call)
        if not name:
            continue
        name = ensure_image_name(name)
        if method in {"click_image", "click_until_gone"}:
            click_imgs.add(name)
        elif method in {"match_image", "match_image_multi", "wait_image"}:
            match_imgs.add(name)

    images: list[ImageEntry] = []
    seen: set[str] = set()
    folder_names: list[str] = []
    if img_dir and img_dir.is_dir():
        folder_names = list(refresh_dir_image_map(img_dir).values())

    def add_image(name: str, role: str, state: str = "", note: str = ""):
        key = ensure_image_name(name).lower()
        if not key or key in seen:
            return
        seen.add(key)
        stem = Path(ensure_image_name(name)).stem
        st = state
        if role == ROLE_ID and not st:
            if stem.lower() == "rank":
                st = "主界面"
            elif "logo" in stem.lower():
                st = stem.replace("_logo", "").replace("logo", "") or "场景"
        images.append(ImageEntry(
            image=ensure_image_name(name),
            role=role,
            state=st,
            note=note,
        ))

    for name in sorted(match_imgs | click_imgs, key=str.lower):
        stem = Path(name).stem
        if _ID_NAME_RE.match(stem):
            role = ROLE_ID
        elif name in click_imgs:
            role = ROLE_BUTTON
        else:
            role = ROLE_OTHER
        add_image(name, role)

    for name in folder_names:
        add_image(name, ROLE_ID if _ID_NAME_RE.match(Path(name).stem) else ROLE_OTHER)

    funcs = {
        n.name: n
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    task_order = _tasks_from_do_work(funcs.get("do_work"))
    helper_names = [
        n for n in funcs
        if n not in _SKIP_FUNCS
        and not n.startswith("_")
        and n not in {t[0] for t in task_order}
        and ("返回" in n or "unknown" in n.lower() or n in ("未知", "未知状态"))
    ]
    # 中文公开函数：既不在任务列表也不像 unknown
    if not helper_names:
        helper_names = [
            n for n in funcs
            if n not in _SKIP_FUNCS
            and not n.startswith("_")
            and n not in {t[0] for t in task_order}
            and any("\u4e00" <= ch <= "\u9fff" for ch in n)
            and n != "do_work"
        ]

    helpers = [
        HelperSpec(name=n, steps="\n".join(_fn_to_steps(funcs[n], funcs)))
        for n in helper_names
        if n in funcs and "unknown" not in n.lower() and n not in ("未知", "未知状态")
    ]

    tasks: list[TaskSpec] = []
    if task_order:
        for name, fn_name in task_order:
            fn = funcs.get(fn_name)
            steps = "\n".join(_fn_to_steps(fn, funcs)) if fn else f"执行 {fn_name}"
            tasks.append(TaskSpec(name=name, steps=steps))
    elif helpers:
        tasks.append(TaskSpec(
            name=goal or py_path.stem,
            steps="\n".join(f"@{h.name}" for h in helpers if h.name),
        ))
    else:
        dw = funcs.get("do_work")
        tasks.append(TaskSpec(
            name=goal or py_path.stem,
            steps="\n".join(_fn_to_steps(dw, funcs)) if dw else "",
        ))

    return ScriptSpec(
        goal=goal,
        source_dir=str(img_dir) if img_dir else "",
        images=images,
        helpers=helpers,
        tasks=tasks,
        notes="",
    )


def _tasks_from_do_work(fn: ast.AST | None) -> list[tuple[str, str]]:
    if fn is None:
        return []
    out: list[tuple[str, str]] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        name = ""
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        if name != "run_step_chain":
            continue
        if not node.args or len(node.args) < 2:
            continue
        steps_node = node.args[1]
        if not isinstance(steps_node, ast.List):
            continue
        for elt in steps_node.elts:
            if not isinstance(elt, ast.Tuple) or len(elt.elts) < 2:
                continue
            label = _const_str(elt.elts[0]) or ""
            fn_name = ""
            second = elt.elts[1]
            if isinstance(second, ast.Name):
                fn_name = second.id
            if label and fn_name:
                out.append((label, fn_name))
        if out:
            return out
    # 直接 await 中文函数
    for node in ast.walk(fn):
        if not isinstance(node, ast.Await) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        if isinstance(func, ast.Name) and func.id not in _SKIP_FUNCS and not func.id.startswith("_"):
            if any("\u4e00" <= ch <= "\u9fff" for ch in func.id):
                out.append((func.id, func.id))
    return out


def _fn_to_steps(fn: ast.AST, funcs: dict) -> list[str]:
    if fn is None:
        return []
    steps: list[str] = []
    for node in fn.body if hasattr(fn, "body") else []:
        steps.extend(_stmt_to_steps(node, funcs))
        if len(steps) >= 24:
            break
    return steps[:24]


def _stmt_to_steps(node: ast.AST, funcs: dict) -> list[str]:
    if isinstance(node, ast.Expr):
        return _stmt_to_steps(node.value, funcs)
    if isinstance(node, ast.Await):
        return _stmt_to_steps(node.value, funcs)
    if isinstance(node, ast.Assign):
        return _stmt_to_steps(node.value, funcs)
    if isinstance(node, ast.Return) and node.value is not None:
        return _stmt_to_steps(node.value, funcs)
    if isinstance(node, ast.If):
        cond = _cond_to_text(node.test, funcs)
        body = []
        for s in node.body[:6]:
            body.extend(_stmt_to_steps(s, funcs))
        if cond and body:
            return [f"若{cond} → {body[0]}"] + [f"  {b}" for b in body[1:3]]
        if cond:
            return [f"若{cond}"]
        return body[:3]
    if isinstance(node, ast.For):
        inner = []
        for s in node.body[:8]:
            inner.extend(_stmt_to_steps(s, funcs))
        if inner:
            return ["重复："] + [f"  {x}" for x in inner[:6]]
        return []
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id in funcs:
            callee = node.func.id
            if callee in _SKIP_FUNCS or callee.startswith("_"):
                return _fn_to_steps(funcs.get(callee), funcs)[:8]
            if any("\u4e00" <= ch <= "\u9fff" for ch in callee):
                return [f"@{callee}"]
        hit = _browser_call(node)
        if hit:
            line = _browser_step(hit[0], hit[1])
            return [line] if line else []
    return []


def _cond_to_text(test: ast.AST, funcs: dict) -> str:
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        inner = _cond_to_text(test.operand, funcs)
        return f"未{inner}" if inner else ""
    if isinstance(test, ast.Await):
        return _cond_to_text(test.value, funcs)
    if isinstance(test, ast.Call):
        hit = _browser_call(test)
        if hit:
            method, call = hit
            img = _first_path_arg_name(call)
            if img:
                verb = {"click_image": "点到", "match_image": "匹配到", "wait_image": "等到"}.get(method, method)
                return f"{verb} {ensure_image_name(img)}"
        if isinstance(test.func, ast.Name) and test.func.id in funcs:
            return test.func.id
    return ""


def _browser_step(method: str, call: ast.Call) -> str:
    img = _first_path_arg_name(call)
    img_s = ensure_image_name(img) if img else ""
    if method == "click_image":
        extra = []
        th = _kw_num(call, "threshold")
        if th:
            extra.append(f"阈值 {th}")
        line = f"点击 {img_s}" if img_s else "点击图片"
        return line + ("（" + "，".join(extra) + "）" if extra else "")
    if method == "match_image":
        return f"匹配 {img_s}" if img_s else "匹配图片"
    if method == "wait_image":
        return f"等待 {img_s}" if img_s else "等待图片"
    if method == "b_sleep":
        args = []
        for a in call.args[:2]:
            if isinstance(a, ast.Constant):
                args.append(str(a.value))
        if len(args) == 2:
            return f"等待 {args[0]}~{args[1]} 秒"
        if args:
            return f"等待 {args[0]} 秒"
        return "短暂等待"
    if method == "script_log":
        return ""
    if method == "update_frame":
        return ""
    return ""
