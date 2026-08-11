"""
Script Generator Agent
======================
调用 LLM API，根据用户提供的脚本解释和图片，自动生成自动化脚本。
支持单次调用或 LangGraph 多步编排（plan → generate → validate → fix）。
"""

from __future__ import annotations

import ast
import base64
import json
import re
from pathlib import Path
from typing import Optional


# ═══════════════════════════════════════════════════════════════
# 配置 — 从 config.json 加载，支持热更新
# ═══════════════════════════════════════════════════════════════

_CONFIG_PATH = Path(__file__).parent / "config.json"


def _load_config() -> dict:
    """加载配置 JSON"""
    return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))


def build_img_dir_line(source_dir: str = "") -> str:
    """根据所选图片目录生成 IMG_DIR 赋值行。"""
    if not (source_dir or "").strip():
        return "IMG_DIR = IMG_PATH / 'game' / 'script'"
    try:
        from core.path import IMG_PATH
        rel = Path(source_dir).resolve().relative_to(Path(IMG_PATH).resolve())
        parts = [f"'{p}'" for p in rel.parts]
        return "IMG_DIR = IMG_PATH / " + " / ".join(parts)
    except (ValueError, Exception):
        return f'IMG_DIR = Path(r"{source_dir}")'


def enforce_img_dir(code: str, source_dir: str = "") -> str:
    """强制把生成代码中的 IMG_DIR 改成所选目录（不依赖模型自觉）。"""
    if not (source_dir or "").strip() or not (code or "").strip():
        return code
    line = build_img_dir_line(source_dir)
    if re.search(r"^IMG_DIR\s*=", code, flags=re.MULTILINE):
        return re.sub(r"^IMG_DIR\s*=\s*.+$", line, code, count=1, flags=re.MULTILINE)
    # 没有 IMG_DIR 时插到 IMG_PATH import 之后
    m = re.search(r"^(from\s+core\.path\s+import\s+IMG_PATH\s*)$", code, flags=re.MULTILINE)
    if m:
        pos = m.end()
        return code[:pos] + "\n\n" + line + code[pos:]
    return line + "\n\n" + code


def _build_system_prompt(source_dir: str = "") -> str:
    """从 config.json 动态构建 system prompt"""
    cfg = _load_config()
    defaults = cfg.get("defaults", {})
    th = defaults.get("threshold", 0.9)
    nav = defaults.get("nav_threshold", 0.8)
    icon_th = defaults.get("icon_threshold", 0.85)

    scripts = cfg.get("available_scripts", [])
    script_lines = "\n".join(
        f'- **{s["module"]}**: `{s["name"]}` — {s["desc"]}'
        for s in scripts
    )
    scripts_block = f"以下脚本已存在，生成新脚本时应 import 使用：\n{script_lines}\n\n用法示例（注意用 scripts. 前缀）：\n"
    scripts_block += "```python\n"
    for s in scripts[:3]:
        name = s["name"].split("(")[0]
        scripts_block += f"from {s['module']} import {name}\n"
    scripts_block += "```"

    rules = cfg.get("rules", [])
    rules_block = "\n".join(f"{i+1}. {r}" for i, r in enumerate(rules))

    img_dir_line = build_img_dir_line(source_dir)
    if source_dir:
        src_line = (
            f"图片文件夹路径: {source_dir}\n"
            f"MUST set exactly this line (do NOT use game/script placeholder):\n{img_dir_line}"
        )
    else:
        src_line = "（未指定图片文件夹 — 请要求用户先选择）"

    template = cfg.get("system_prompt_template", "")
    prompt = template.replace("$THRESHOLD", str(th))
    prompt = prompt.replace("$NAV_THRESHOLD", str(nav))
    prompt = prompt.replace("$ICON_THRESHOLD", str(icon_th))
    prompt = prompt.replace("$AVAILABLE_SCRIPTS", scripts_block)
    prompt = prompt.replace("$SOURCE_DIR", src_line)
    prompt = prompt.replace("$IMG_DIR_LINE", img_dir_line)
    prompt = prompt.replace("$RULES", rules_block)

    return prompt


def _image_b64(image_path: Path, compress: bool = False, max_size: int = 800) -> tuple[str, str]:
    """读取图片为 base64，返回 (base64_data, media_type)。"""
    import cv2
    img = cv2.imread(str(image_path))
    if img is None:
        raise ValueError(f"无法读取图片: {image_path}")
    if compress:
        h, w = img.shape[:2]
        if max(h, w) > max_size:
            scale = max_size / max(h, w)
            new_w, new_h = int(w * scale), int(h * scale)
            img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
    _, buf = cv2.imencode(".png", img)
    data = base64.b64encode(bytes(buf)).decode("utf-8")
    ext = image_path.suffix.lower().lstrip(".")
    media_type = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(ext, "image/png")
    return data, media_type


def _provider_supports_images(provider: str) -> bool:
    """DeepSeek 等文本模型不支持多模态图片输入。"""
    return provider not in ("deepseek",)


def _build_messages(
    explanation_text: str,
    image_paths: list[Path],
    provider: str = "claude",
    send_images: bool = True,
    compress_images: bool = False,
) -> list[dict]:
    """构建消息列表，根据 provider 选择图片格式。"""
    content = [{"type": "text", "text": explanation_text}]
    can_send = bool(send_images and image_paths and _provider_supports_images(provider))
    if send_images and image_paths and not _provider_supports_images(provider):
        # 仍把文件名列表塞进文本，避免模型完全不知道有哪些图
        names = "\n".join(f"- {Path(p).name}" for p in image_paths)
        content.append({
            "type": "text",
            "text": (
                f"\n\n（当前提供商不支持看图，已跳过图片二进制。"
                f"请仅根据文件名编写 _img() 引用）\n参考图片文件名：\n{names}"
            ),
        })
    elif can_send:
        content.append({"type": "text", "text": f"\n\n参考图片共 {len(image_paths)} 张，文件名对应脚本中的图片名："})
        for img_path in image_paths:
            try:
                b64data, media_type = _image_b64(img_path, compress=compress_images)
                if provider == "claude":
                    encoded = {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64data}}
                elif provider == "google":
                    encoded = {"inline_data": {"mime_type": media_type, "data": b64data}}
                else:  # openai / groq
                    encoded = {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64data}"}}
                content.append(encoded)
                content.append({"type": "text", "text": f"  → {img_path.name}"})
            except Exception as e:
                content.append({"type": "text", "text": f"[图片加载失败: {img_path.name} - {e}]"})
    return [{"role": "user", "content": content}]


def _flatten_openai_messages(messages: list[dict]) -> list[dict]:
    """把仅含 text 的 content 列表压成字符串，兼容 DeepSeek 等实现。"""
    out: list[dict] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            texts: list[str] = []
            has_non_text = False
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    texts.append(str(part.get("text") or ""))
                elif isinstance(part, dict) and part.get("type") in ("image_url", "image"):
                    has_non_text = True
                elif isinstance(part, str):
                    texts.append(part)
                else:
                    has_non_text = True
            if has_non_text:
                out.append(msg)
            else:
                out.append({**msg, "content": "\n".join(texts)})
        else:
            out.append(msg)
    return out


def _deepseek_extra_body(_model: str = "") -> dict:
    """
    DeepSeek V4 默认开启 thinking：思考 token 会占满 max_tokens，
    导致 finish_reason=length 且 content 为空。脚本生成关闭 thinking。
    """
    return {"thinking": {"type": "disabled"}}


def strip_code_fences(raw: str) -> str:
    """去掉 markdown 代码块标记。"""
    raw = (raw or "").strip()
    if raw.startswith("```python"):
        raw = raw[len("```python"):].strip()
    if raw.startswith("```"):
        raw = raw[len("```"):].strip()
    if raw.endswith("```"):
        raw = raw[:-len("```")].strip()
    return raw


_CN_PUNCT_RE = re.compile(r"[，。！？；：、“”‘’（）【】《》、]")

# Script Generator 官方 API（与 config system_prompt 一致）
ALLOWED_BROWSER_METHODS = frozenset({
    "match_image",
    "match_image_multi",
    "click_image",
    "wait_image",
    "b_sleep",
    "update_frame",
    "script_log",
    "note_state",
    "note_progress",
})

_BUILTINS = (
    set(__builtins__.keys()) if isinstance(__builtins__, dict) else set(dir(__builtins__))
)
_BUILTINS |= {
    "True", "False", "None", "Ellipsis", "NotImplemented",
    "asyncio", "Optional", "Path", "Union", "List", "Dict", "Tuple", "Any",
    "print", "range", "len", "min", "max", "sum", "enumerate", "zip", "list", "dict",
    "str", "int", "float", "bool", "type", "isinstance", "hasattr", "getattr",
    "Exception", "TimeoutError", "RuntimeError", "NameError", "ImportError",
    "ValueError", "TypeError", "StopIteration", "BaseException",
}


def _collect_imported_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for n in tree.body:
        if isinstance(n, ast.Import):
            for a in n.names:
                names.add(a.asname or a.name.split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                if a.name == "*":
                    continue
                names.add(a.asname or a.name)
    return names


def _collect_function_locals(fn: ast.AST) -> set[str]:
    names: set[str] = set()
    if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for a in fn.args.args + fn.args.kwonlyargs:
            names.add(a.arg)
        if fn.args.vararg:
            names.add(fn.args.vararg.arg)
        if fn.args.kwarg:
            names.add(fn.args.kwarg.arg)
    for node in ast.walk(fn):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not fn:
            names.add(node.name)
        elif isinstance(node, ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
                elif isinstance(t, (ast.Tuple, ast.List)):
                    for elt in t.elts:
                        if isinstance(elt, ast.Name):
                            names.add(elt.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.For):
            def _bind(t):
                if isinstance(t, ast.Name):
                    names.add(t.id)
                elif isinstance(t, (ast.Tuple, ast.List)):
                    for elt in t.elts:
                        _bind(elt)
            _bind(node.target)
        elif isinstance(node, ast.withitem) and node.optional_vars and isinstance(node.optional_vars, ast.Name):
            names.add(node.optional_vars.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _find_undefined_calls(tree: ast.AST) -> list[str]:
    """模块级可见名字 + 函数局部；报告未定义的 Name 调用。"""
    top = _collect_assigned_names(tree) | _collect_imported_names(tree)
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            top.add(n.name)

    issues: list[str] = []
    for n in tree.body:
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        local = top | _collect_function_locals(n)
        for node in ast.walk(n):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id not in local and func.id not in _BUILTINS:
                issues.append(f"{func.id}@{getattr(node, 'lineno', 0)}")
    # 去重保序
    seen = set()
    out = []
    for i in issues:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _unknown_state_returns_unknown_on_match(tree: ast.AST) -> list[str]:
    """
    检测 unknown_state 类函数：在 if <name> 分支里 return '未知'，
    且该 name 来自 match 结果循环（启发式：for ... in zip / if r: return 未知）。
    """
    errors: list[str] = []
    for fn in tree.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        lname = fn.name.lower()
        if "unknown" not in lname and fn.name not in ("未知", "未知状态"):
            # 也检查中文名
            if "未知" not in fn.name:
                continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.If):
                continue
            # if r: / if result: / if n:
            test = node.test
            if not isinstance(test, ast.Name):
                continue
            for stmt in node.body:
                ret_val = None
                if isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Constant):
                    ret_val = stmt.value.value
                if ret_val in ("未知", "\u672a\u77e5"):
                    errors.append(
                        f"{fn.name}@{getattr(stmt, 'lineno', 0)}: "
                        f"匹配成功分支禁止 return '未知'，应返回 scene_map 对应业务状态名"
                    )
    return errors


def _validate_task_state_keys(tree: ast.AST, plan: Optional[dict]) -> list[str]:
    if not plan:
        return []
    tasks = plan.get("tasks") or []
    if not tasks:
        return []
    errors: list[str] = []
    # 收集所有 *_STATES 字典
    state_dicts: list[tuple[str, set[str]]] = []
    assigned = _collect_assigned_names(tree)
    for name in assigned:
        if name == "STATES" or name.endswith("_STATES"):
            d = _find_module_dict_assign(tree, name)
            if d:
                state_dicts.append((name, _dict_literal_keys(d)))
    if not state_dicts:
        return ["计划含 tasks，但代码中未找到 TASK*_STATES / STATES 字典"]

    for task in tasks:
        tname = str(task.get("name") or "")
        required = [str(s) for s in (task.get("states") or []) if str(s).strip()]
        if not required:
            continue
        # 找名字最接近的 dict：包含任务名片段，或任意包含全部 required
        best = None
        for dname, keys in state_dicts:
            if tname and any(part and part in dname for part in re.split(r"\W+", tname) if len(part) >= 2):
                best = (dname, keys)
                break
        if best is None:
            # fallback: 任一 dict 包含 required 的大部分
            for dname, keys in state_dicts:
                if sum(1 for r in required if r in keys) >= max(1, len(required) - 1):
                    best = (dname, keys)
                    break
        if best is None:
            # 用第一个 *_STATES 非 STATES
            for dname, keys in state_dicts:
                if dname != "STATES":
                    best = (dname, keys)
                    break
        if best is None:
            best = state_dicts[0]
        dname, keys = best
        missing = [r for r in required if r not in keys]
        if missing:
            errors.append(
                f"任务「{tname}」的状态表 {dname} 缺少键: {', '.join(missing)}"
            )
    return errors


def _validate_reuse_imports(tree: ast.AST, plan: Optional[dict], code: str) -> list[str]:
    errors: list[str] = []
    imported = _collect_imported_names(tree)
    for u in (plan or {}).get("reuse") or []:
        name_field = str(u.get("name") or "")
        m = re.match(r"([A-Za-z_][\w]*)", name_field.strip())
        sym = m.group(1) if m else ""
        if not sym:
            continue
        if sym in imported:
            continue
        errors.append(
            f"计划 reuse 要求使用 {sym}，但代码中未 from/import 该符号"
        )
    return errors


def _collect_assigned_names(tree: ast.AST) -> set[str]:
    assigned: set[str] = set()
    for n in getattr(tree, "body", []):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    assigned.add(t.id)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            assigned.add(n.target.id)
    return assigned


def _dict_literal_keys(node: ast.AST) -> set[str]:
    keys: set[str] = set()
    if not isinstance(node, ast.Dict):
        return keys
    for k in node.keys:
        if isinstance(k, ast.Constant) and isinstance(k.value, str):
            keys.add(k.value)
    return keys


def _find_module_dict_assign(tree: ast.AST, name: str) -> Optional[ast.Dict]:
    for n in tree.body:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id == name and isinstance(n.value, ast.Dict):
                    return n.value
        elif isinstance(n, ast.AnnAssign):
            if isinstance(n.target, ast.Name) and n.target.id == name and isinstance(n.value, ast.Dict):
                return n.value
    return None


def _iter_browser_method_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            if func.value.id == "browser":
                yield func.attr, getattr(node, "lineno", 0)


def _collect_img_names(tree: ast.AST) -> list[str]:
    """收集 _img('xxx') / _img(\"xxx\") 中的图片名。"""
    names: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "_img" and node.args:
            arg0 = node.args[0]
            if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
                names.append(arg0.value)
    return names


def _find_do_work(tree: ast.AST) -> Optional[ast.AsyncFunctionDef]:
    for n in tree.body:
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "do_work":
            return n
    return None


def validate_generated_code(
    code: str,
    plan: Optional[dict] = None,
    source_dir: str = "",
    image_paths: Optional[list] = None,
) -> list[str]:
    """本地校验生成代码，返回错误列表（空表示通过）。"""
    errors: list[str] = []
    if not (code or "").strip():
        return ["生成结果为空"]

    try:
        compile(code, "<generated>", "exec")
    except SyntaxError as e:
        errors.append(f"语法错误: {e.msg}（第 {e.lineno} 行）")
        return errors

    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        errors.append(f"AST 解析失败: {e.msg}（第 {e.lineno} 行）")
        return errors

    top_names = {
        n.name
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    assigned = _collect_assigned_names(tree)
    kind = (plan or {}).get("kind") if plan else None

    if "do_work" not in top_names:
        errors.append("缺少 async def do_work(...)")
    else:
        dw = _find_do_work(tree)
        if dw is not None:
            if not dw.args.args:
                errors.append("do_work 缺少参数，需要 (browser: UserBrowser) 或 (win: UserWindow)")
            else:
                ann = dw.args.args[0].annotation
                if ann is None:
                    errors.append(
                        "do_work 第一个参数缺少类型标注，请写 "
                        "(browser: UserBrowser) 或 (win: UserWindow)"
                    )
                else:
                    ann_src = ast.unparse(ann) if hasattr(ast, "unparse") else ""
                    if not any(t in ann_src for t in ("UserBrowser", "UserWindow", "Browser")):
                        errors.append(
                            f"do_work 参数类型必须为 UserBrowser / UserWindow，当前为: {ann_src or '?'}"
                        )

    # IMG_DIR 路径校验
    if "IMG_DIR" not in assigned:
        errors.append("缺少 IMG_DIR 赋值")
    src = (source_dir or "").strip()
    if src:
        expected = build_img_dir_line(src)
        # 禁止占位目录
        if re.search(r"IMG_DIR\s*=\s*IMG_PATH\s*/\s*['\"]game['\"]\s*/\s*['\"]script['\"]", code):
            errors.append(
                f"IMG_DIR 仍是占位路径 game/script，应为所选目录：{expected}"
            )
        elif expected.replace(" ", "") not in code.replace(" ", ""):
            # 宽松：去掉空格后比较是否包含关键相对路径片段
            try:
                from core.path import IMG_PATH
                rel = Path(src).resolve().relative_to(Path(IMG_PATH).resolve())
                for part in rel.parts:
                    if f"'{part}'" not in code and f'"{part}"' not in code:
                        errors.append(
                            f"IMG_DIR 未包含所选目录片段 '{part}'，期望类似：{expected}"
                        )
                        break
            except Exception:
                if str(Path(src)) not in code and src not in code:
                    errors.append(f"IMG_DIR 未指向所选图片目录：{src}")

        # _img() 引用的文件是否真实存在
        img_root = Path(src)
        missing: list[str] = []
        for name in _collect_img_names(tree):
            fname = name if name.lower().endswith(".png") else f"{name}.png"
            if not (img_root / fname).is_file():
                missing.append(fname)
        if missing:
            # 去重保序
            seen = set()
            uniq = []
            for m in missing:
                if m not in seen:
                    seen.add(m)
                    uniq.append(m)
            preview = ", ".join(uniq[:8])
            more = f" 等 {len(uniq)} 个" if len(uniq) > 8 else ""
            errors.append(f"图片文件不存在于所选目录：{preview}{more}")
    elif image_paths:
        errors.append("未指定图片文件夹（source_dir），无法校验 IMG_DIR / 图片路径")

    has_states = "STATES" in assigned
    has_task_states = any(name.endswith("_STATES") for name in assigned)
    has_run_task = "run_task" in top_names
    has_timeout = "STATE_TIMEOUT" in assigned or any(
        name.endswith("_TIMEOUT") for name in assigned
    )

    if kind == "multi_task":
        if not has_run_task:
            errors.append("multi_task 计划要求定义 run_task(...)")
        if not has_task_states and not has_states:
            errors.append("multi_task 计划要求 TASK*_STATES（或 STATES）")
    elif kind == "utility":
        # utility 允许只有 helpers；仍建议有 do_work 入口（上面已查）
        pass
    else:
        # single_fsm / unknown
        if not has_states and not (has_task_states and has_run_task):
            errors.append("缺少 STATES 字典（或 TASK*_STATES + run_task）")

    if not has_timeout and (has_states or has_task_states):
        errors.append("缺少 STATE_TIMEOUT（或 TASK*_TIMEOUT）")

    # 未知 状态键
    unknown_keys = {"未知", "\u672a\u77e5"}
    found_unknown = False
    for dict_name in list(assigned):
        if dict_name == "STATES" or dict_name.endswith("_STATES"):
            d = _find_module_dict_assign(tree, dict_name)
            if d and (_dict_literal_keys(d) & unknown_keys):
                found_unknown = True
                break
    if (has_states or has_task_states) and not found_unknown:
        errors.append("STATES / TASK*_STATES 中缺少「未知」恢复状态")

    # browser.xxx 白名单
    illegal: list[str] = []
    for method, lineno in _iter_browser_method_calls(tree):
        if method not in ALLOWED_BROWSER_METHODS:
            illegal.append(f"{method}@{lineno}")
    if illegal:
        # 去重保留顺序
        seen = set()
        uniq = []
        for item in illegal:
            if item not in seen:
                seen.add(item)
                uniq.append(item)
        allowed = ", ".join(sorted(ALLOWED_BROWSER_METHODS))
        errors.append(
            f"非法 browser 方法: {', '.join(uniq[:8])}；仅允许: {allowed}"
        )

    # 未定义函数调用（如 handle_battle_result 未 import）
    undef = _find_undefined_calls(tree)
    if undef:
        errors.append(
            "存在未定义/未导入的调用: "
            + ", ".join(undef[:8])
            + "；请补全 import 或删除调用"
        )

    # unknown_state 误路由
    for msg in _unknown_state_returns_unknown_on_match(tree):
        errors.append(msg)

    # multi_task 状态表键
    for msg in _validate_task_state_keys(tree, plan):
        errors.append(msg)

    # reuse import
    for msg in _validate_reuse_imports(tree, plan, code):
        errors.append(msg)

    for line_no, line in enumerate(code.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _CN_PUNCT_RE.search(line):
            without_str = re.sub(
                r'("""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'|"[^"\\]*(?:\\.[^"\\]*)*"|\'[^\'\\]*(?:\\.[^\'\\]*)*\')',
                "",
                line,
            )
            without_str = re.sub(r"#.*$", "", without_str)
            if _CN_PUNCT_RE.search(without_str):
                errors.append(f"第 {line_no} 行代码区含中文标点（非字符串/注释）")
                break

    return errors


def resolve_max_tokens(max_tokens: Optional[int] = None) -> int:
    """解析 max_tokens：优先入参，否则读 config.json defaults。"""
    if max_tokens is not None:
        try:
            n = int(max_tokens)
            if n > 0:
                return n
        except (TypeError, ValueError):
            pass
    try:
        return int(_load_config().get("defaults", {}).get("max_tokens", 16384))
    except Exception:
        return 16384


async def call_llm(
    *,
    provider: str,
    api_key: str,
    model: str,
    api_endpoint: Optional[str],
    messages: list[dict],
    system_prompt: str = "",
    on_partial=None,
    max_tokens: Optional[int] = None,
) -> tuple[str, int, int]:
    """统一 LLM 调用入口，返回 (text, input_tokens, output_tokens)。"""
    mt = resolve_max_tokens(max_tokens)
    if provider == "claude":
        return await _call_claude(
            api_key, model, api_endpoint, messages, system_prompt,
            on_partial=on_partial, max_tokens=mt,
        )
    if provider in ("openai", "deepseek", "groq"):
        return await _call_openai(
            api_key, model, api_endpoint, messages, system_prompt,
            on_partial=on_partial, provider=provider, max_tokens=mt,
        )
    if provider == "google":
        text = await _call_gemini(api_key, model, api_endpoint, messages, system_prompt, on_partial=on_partial)
        return text, 0, 0
    raise ValueError(f"不支持的 provider: {provider}")


async def test_connection(
    *,
    provider: str,
    api_key: str,
    model: str,
    api_endpoint: Optional[str] = None,
    max_tokens: Optional[int] = None,
) -> dict:
    """
    发送一条极短消息验证 API 连通性。
    返回 {ok, latency_ms, reply, error, input_tokens, output_tokens}。
    """
    import time

    if not (api_key or "").strip():
        return {
            "ok": False,
            "latency_ms": 0,
            "reply": "",
            "error": "API Key 为空",
            "input_tokens": 0,
            "output_tokens": 0,
        }
    if not (model or "").strip():
        return {
            "ok": False,
            "latency_ms": 0,
            "reply": "",
            "error": "模型名为空",
            "input_tokens": 0,
            "output_tokens": 0,
        }

    messages = [{
        "role": "user",
        "content": [{"type": "text", "text": "Reply with exactly: OK"}],
    }]
    system_prompt = "You are a connectivity probe. Reply with exactly OK."

    t0 = time.perf_counter()
    try:
        text, inp, out = await call_llm(
            provider=provider,
            api_key=api_key.strip(),
            model=model.strip(),
            api_endpoint=api_endpoint,
            messages=messages,
            system_prompt=system_prompt,
            max_tokens=max_tokens if max_tokens is not None else 256,
        )
        latency_ms = int((time.perf_counter() - t0) * 1000)
        reply = (text or "").strip()
        if not reply:
            return {
                "ok": False,
                "latency_ms": latency_ms,
                "reply": "",
                "error": "API 返回空内容",
                "input_tokens": inp,
                "output_tokens": out,
            }
        return {
            "ok": True,
            "latency_ms": latency_ms,
            "reply": reply[:200],
            "error": "",
            "input_tokens": inp,
            "output_tokens": out,
        }
    except Exception as e:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return {
            "ok": False,
            "latency_ms": latency_ms,
            "reply": "",
            "error": str(e),
            "input_tokens": 0,
            "output_tokens": 0,
        }


async def _generate_script_legacy(
    *,
    provider: str,
    api_key: str,
    model: str,
    api_endpoint: Optional[str],
    explanation_text: str,
    image_paths: list[Path],
    source_dir: str = "",
    send_images: bool = True,
    compress_images: bool = False,
    on_partial=None,
    max_tokens: Optional[int] = None,
) -> tuple[str, int, int]:
    prompt = _build_system_prompt(source_dir=source_dir)
    messages = _build_messages(
        explanation_text,
        image_paths,
        provider=provider,
        send_images=send_images,
        compress_images=compress_images,
    )
    raw, inp_tok, out_tok = await call_llm(
        provider=provider,
        api_key=api_key,
        model=model,
        api_endpoint=api_endpoint,
        messages=messages,
        system_prompt=prompt,
        on_partial=on_partial,
        max_tokens=max_tokens,
    )
    code = enforce_img_dir(strip_code_fences(raw), source_dir)
    errors = validate_generated_code(
        code, source_dir=source_dir, image_paths=image_paths,
    )
    if errors:
        raise RuntimeError(f"生成的脚本校验失败: {errors[0]}，请点「生成脚本」重试")
    return code, inp_tok, out_tok


async def generate_script(
    *,
    provider: str,
    api_key: str,
    model: str,
    api_endpoint: Optional[str],
    explanation_text: str,
    image_paths: list[Path],
    source_dir: str = "",
    send_images: bool = True,
    compress_images: bool = False,
    on_partial=None,
    on_status=None,
    on_artifact=None,
    max_tokens: Optional[int] = None,
) -> tuple[str, int, int]:
    """
    调用 LLM 生成脚本。返回 (代码, 输入tokens, 输出tokens)。

    默认走 LangGraph 多步编排；config.defaults.use_langgraph=false 或未安装
    langgraph 时回退到单次调用。
    """
    cfg = _load_config()
    defaults = cfg.get("defaults", {})
    use_graph = bool(defaults.get("use_langgraph", True))
    mt = resolve_max_tokens(max_tokens if max_tokens is not None else defaults.get("max_tokens"))

    if use_graph:
        try:
            from backend.script_generator.graph import run_script_gen_graph
        except ImportError as e:
            import sys
            use_graph = False
            detail = f"当前解释器: {sys.executable}；原因: {e}"
            print(f"[ScriptGenerator] LangGraph 导入失败: {detail}")
            if on_status:
                on_status(f"LangGraph 不可用，回退单次生成…（{detail}）")

    if use_graph:
        return await run_script_gen_graph(
            provider=provider,
            api_key=api_key,
            model=model,
            api_endpoint=api_endpoint,
            explanation_text=explanation_text,
            image_paths=image_paths,
            source_dir=source_dir,
            send_images=send_images,
            compress_images=compress_images,
            enable_plan=bool(defaults.get("enable_plan", True)),
            max_fix_retries=int(defaults.get("max_fix_retries", 2)),
            max_tokens=mt,
            on_partial=on_partial,
            on_status=on_status,
            on_artifact=on_artifact,
        )

    return await _generate_script_legacy(
        provider=provider,
        api_key=api_key,
        model=model,
        api_endpoint=api_endpoint,
        explanation_text=explanation_text,
        image_paths=image_paths,
        source_dir=source_dir,
        send_images=send_images,
        compress_images=compress_images,
        on_partial=on_partial,
        max_tokens=mt,
    )


_CFG = _load_config()
_DEFAULTS = _CFG.get("defaults", {})
API_TIMEOUT = _DEFAULTS.get("api_timeout", 300)


async def _call_claude(
    api_key: str,
    model: str,
    api_endpoint: Optional[str],
    messages: list[dict],
    system_prompt: str = "",
    on_partial=None,
    max_tokens: Optional[int] = None,
) -> tuple[str, int, int]:
    """调用 Anthropic Claude API，返回 (代码, 输入tokens, 输出tokens)"""
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("需要安装 anthropic 包: pip install anthropic")

    mt = resolve_max_tokens(max_tokens)
    client_kwargs = {"api_key": api_key, "timeout": API_TIMEOUT}
    if api_endpoint:
        client_kwargs["base_url"] = api_endpoint

    client = anthropic.AsyncAnthropic(**client_kwargs)

    if on_partial:
        text = ""
        async with client.messages.stream(
            model=model, max_tokens=mt, system=system_prompt, messages=messages,
        ) as stream:
            async for chunk in stream.text_stream:
                text += chunk
                on_partial(chunk)
        final = await stream.get_final_message()
        usage = final.usage
        if not text:
            raise RuntimeError("Claude 返回了空内容，请重试")
        return text, usage.input_tokens, usage.output_tokens
    else:
        response = await client.messages.create(
            model=model, max_tokens=mt, system=system_prompt, messages=messages,
        )
        text = response.content[0].text
        if not text:
            raise RuntimeError("Claude 返回了空内容，请重试")
        usage = response.usage
        return text, usage.input_tokens, usage.output_tokens


async def _call_openai(
    api_key: str,
    model: str,
    api_endpoint: Optional[str],
    messages: list[dict],
    system_prompt: str = "",
    on_partial=None,
    provider: str = "openai",
    max_tokens: Optional[int] = None,
) -> tuple[str, int, int]:
    """调用 OpenAI 兼容 API，支持流式输出"""
    try:
        from openai import AsyncOpenAI
    except ImportError:
        raise RuntimeError("需要安装 openai 包: pip install openai")

    from httpx import Timeout as HttpxTimeout
    mt = resolve_max_tokens(max_tokens)
    client_kwargs = {"api_key": api_key, "timeout": HttpxTimeout(API_TIMEOUT)}
    if api_endpoint:
        client_kwargs["base_url"] = api_endpoint

    client = AsyncOpenAI(**client_kwargs)
    flat_messages = _flatten_openai_messages(messages)
    system_msg = [{"role": "system", "content": system_prompt}]
    create_kwargs: dict = {
        "model": model,
        "max_tokens": mt,
        "messages": system_msg + flat_messages,
    }
    # DeepSeek V4 默认 thinking=on，长任务会把额度烧在 reasoning 上，content 为空
    if provider == "deepseek" or "deepseek" in (model or "").lower():
        create_kwargs["extra_body"] = _deepseek_extra_body(model)

    if on_partial:
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        inp_tok = out_tok = 0
        finish_reason = ""
        stream = await client.chat.completions.create(
            **create_kwargs,
            stream=True,
            stream_options={"include_usage": True},
        )
        async for chunk in stream:
            if chunk.usage:
                inp_tok = chunk.usage.prompt_tokens or inp_tok
                out_tok = chunk.usage.completion_tokens or out_tok
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            if choice.finish_reason:
                finish_reason = choice.finish_reason
            delta = choice.delta
            if not delta:
                continue
            piece = delta.content or ""
            if piece:
                text_parts.append(piece)
                on_partial(piece)
            reasoning = getattr(delta, "reasoning_content", None) or ""
            if reasoning:
                reasoning_parts.append(reasoning)
        text = "".join(text_parts)
        if not text:
            raise RuntimeError(
                _empty_completion_error(
                    provider=provider,
                    model=model,
                    finish_reason=finish_reason,
                    out_tok=out_tok,
                    max_tokens=mt,
                    had_reasoning=bool(reasoning_parts),
                )
            )
        return text, inp_tok, out_tok

    response = await client.chat.completions.create(**create_kwargs)
    message = response.choices[0].message
    text = message.content or ""
    finish_reason = response.choices[0].finish_reason or ""
    reasoning = getattr(message, "reasoning_content", None) or ""
    usage = response.usage
    inp = usage.prompt_tokens if usage else 0
    out = usage.completion_tokens if usage else 0
    if not text:
        raise RuntimeError(
            _empty_completion_error(
                provider=provider,
                model=model,
                finish_reason=finish_reason,
                out_tok=out,
                max_tokens=mt,
                had_reasoning=bool(reasoning),
            )
        )
    return text, inp, out


def _empty_completion_error(
    *,
    provider: str,
    model: str,
    finish_reason: str,
    out_tok: int,
    max_tokens: int,
    had_reasoning: bool,
) -> str:
    """生成更可操作的空内容错误信息。"""
    bits = [
        "API 返回了空内容",
        f"provider={provider}",
        f"model={model}",
        f"finish_reason={finish_reason or 'unknown'}",
        f"completion_tokens={out_tok}/{max_tokens}",
    ]
    if had_reasoning:
        bits.append("had_reasoning=1")
    if (finish_reason == "length" or (out_tok and out_tok >= max_tokens * 0.95)) and had_reasoning:
        bits.append(
            "HINT:DeepSeek思考模式占满了输出额度，正文为空。"
            "已尝试自动关闭 thinking；请重试，或换 deepseek-chat / 提高 max_tokens"
        )
    elif finish_reason == "length":
        bits.append("HINT:输出被截断（token 用尽），请提高 max_tokens 或缩短提示词")
    elif provider == "deepseek":
        bits.append("HINT:DeepSeek不支持看图，请确认未依赖图片输入；可重试或换模型")
    return "；".join(bits)


async def _call_gemini(
    api_key: str,
    model: str,
    api_endpoint: Optional[str],
    messages: list[dict],
    system_prompt: str = "",
    on_partial=None,
) -> str:
    """调用 Google Gemini API"""
    try:
        import google.generativeai as genai
    except ImportError:
        raise RuntimeError("需要安装 google-generativeai 包: pip install google-generativeai")

    genai.configure(api_key=api_key)
    gemini_model = genai.GenerativeModel(model_name=model, system_instruction=system_prompt)

    user_text = ""
    for msg in messages:
        for part in msg["content"]:
            if part.get("type") == "text":
                user_text += part["text"] + "\n"

    response = await gemini_model.generate_content_async(user_text)
    return response.text
