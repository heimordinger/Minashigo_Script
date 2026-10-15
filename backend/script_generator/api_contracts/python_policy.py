"""脚本可用的 Python 能力策略（设计 §4.3）。

原则：
- **Python 语言基础**允许：控制流、函数、类、容器、推导式、异常处理；
- **标准库按白名单**：不是"属于标准库就放行"；
- **项目 API** 只允许当前 runtime profile 契约登记的名字（由 registry 决定，本模块不管）；
- ``eval`` / ``exec`` / ``compile`` / ``__import__`` / ``open`` / 进程 / 网络 / 动态加载
  **默认禁止**，要开放必须新增单独的能力契约。

本模块是纯数据 + 纯判定，供 ``registry.render`` 与契约校验器共用。
"""

from __future__ import annotations

import ast

# ── 允许的标准库模块（顶层模块名或子模块全名）──
#: ⚠️ ``os`` / ``os.path`` 已**移出白名单并加入禁用表**（用户决定）：
#: 它绕过"禁止文件写入/进程"的整类能力（实测 ``module_policy('os.system')`` 曾返回 True）。
#: 需要纯路径处理时改用 ``pathlib`` 的纯路径能力。
ALLOWED_STDLIB_MODULES: frozenset[str] = frozenset(
    {
        "__future__",
        "abc",
        "asyncio",
        "bisect",
        "collections",
        "contextlib",
        "copy",
        "dataclasses",
        "datetime",
        "decimal",
        "enum",
        "fractions",
        "functools",
        "hashlib",
        "heapq",
        "itertools",
        "json",
        "math",
        "numbers",
        "operator",
        "pathlib",
        "random",
        "re",
        "statistics",
        "string",
        "textwrap",
        "time",
        "types",
        "typing",
        "unicodedata",
        "uuid",
    }
)

# ── 允许的项目内部导入（显式登记，禁止按前缀放行）──
ALLOWED_PROJECT_IMPORTS: frozenset[str] = frozenset(
    {
        "backend.automation.user_window",
        "backend.browser.user_browser",
        "backend.script_generator.click_confirm",
        "core.config.config",
        "core.match.match_result",
        "core.path",
    }
)

# ── 明令禁止的模块（即使属于标准库）──
BANNED_MODULES: frozenset[str] = frozenset(
    {
        "atexit",
        "builtins",
        "ctypes",
        "dbm",
        "ftplib",
        "gc",
        "glob",
        "http",
        "importlib",
        "marshal",
        "multiprocessing",
        "os",
        "pickle",
        "platform",
        "pty",
        "requests",
        "shelve",
        "shutil",
        "signal",
        "smtplib",
        "socket",
        "ssl",
        "subprocess",
        "sys",
        "telnetlib",
        "tempfile",
        "threading",
        "tkinter",
        "urllib",
        "webbrowser",
        "xml",
    }
)

# ── 明令禁止的内建/名字（调用即视为越权）──
BANNED_BUILTINS: frozenset[str] = frozenset(
    {
        "__import__",
        "breakpoint",
        "compile",
        "eval",
        "exec",
        "exit",
        "globals",
        "input",
        "locals",
        "open",
        "quit",
    }
)

#: 需要显式能力契约才允许的调用（当前一律禁止，但语义上独立于"危险内建"）。
NEEDS_CAPABILITY_CONTRACT: frozenset[str] = frozenset(
    {
        "browser.cursor_wiggle",  # 见设计 §20.3：未开放前不可用
    }
)


# ── pathlib：只允许纯路径构造与属性读取，禁止一切文件系统副作用/读取/扫描 ──

#: pathlib 的纯路径构造器（可安全实例化；`Path("a") / "b"` 也属纯路径操作）。
PATHLIB_SAFE_NAMES: frozenset[str] = frozenset(
    {
        "Path",
        "PurePath",
        "PurePosixPath",
        "PureWindowsPath",
        "PosixPath",
        "WindowsPath",
    }
)

#: 这些方法名**本身**即高风险（写/删/读/扫描），任何接收者上都拒绝。
#: 之所以按名字就能拒，是因为它们在非路径对象上几乎不出现，误伤面极小。
PATHLIB_SIDE_EFFECT_METHODS: frozenset[str] = frozenset(
    {
        # 写
        "write_text",
        "write_bytes",
        "touch",
        "mkdir",
        "rmdir",
        "unlink",
        "symlink_to",
        "hardlink_to",
        "lchmod",
        # 读 / 扫描
        "read_text",
        "read_bytes",
        "glob",
        "rglob",
        "iterdir",
    }
)

#: 这些方法名**有歧义**（`str.replace`、字典/句柄的 `open` 等都合法），
#: 只有当接收者被解析为 pathlib / Path 时才拒绝。
PATHLIB_BANNED_IF_PATH: frozenset[str] = frozenset(
    {
        "replace",
        "rename",
        "open",
        "remove",
        "chmod",
        "walk",
        "makedirs",
        "removedirs",
    }
)


def pathlib_reason(attr: str) -> str:
    return (
        "pathlib.{}() 会产生文件系统副作用或读取/扫描文件系统；"
        "脚本素材应通过 asset token / materializer 获得，不要自行读写文件（设计 §4.3）".format(attr)
    )


def method_name_policy(name: str) -> tuple[bool, str]:
    """按**方法名**判定高风险文件系统调用。

    「无法确定接收者时宁可拒绝高风险调用，不静默放行」（用户决定）：
    只对 :data:`PATHLIB_SIDE_EFFECT_METHODS` 这种名字本身即高风险的方法生效；
    有歧义的名字（``replace`` 等）交给 :func:`attr_policy` 在接收者可解析时判定，
    避免把 ``"a-b".replace(...)`` 这类常见写法误判成文件操作。
    """
    key = str(name or "").strip()
    if key in PATHLIB_SIDE_EFFECT_METHODS:
        return False, pathlib_reason(key)
    return True, ""


def attr_policy(qualified: str) -> tuple[bool, str]:
    """判定 ``<qualified>.<attr>`` 是否允许。

    ``qualified`` 必须是**已解析别名**的点分名，例如 ``pathlib.Path``、
    ``pathlib.Path.write_text``、``os.system``、``os``。调用方（validator）
    负责把 ``import os as system_os`` / ``from pathlib import Path as P`` 解析到这里。
    """
    key = str(qualified or "").strip()
    if not key:
        return True, ""
    parts = key.split(".")
    root = parts[0]
    if root == "os":
        return False, (
            "os 默认全部禁止（含 os.system/os.popen/os.remove/os.unlink/os.rename/"
            "os.replace/os.mkdir/os.makedirs）；纯路径处理请改用 pathlib（设计 §4.3）"
        )
    owner = parts[-2] if len(parts) >= 2 else ""
    attr = parts[-1]
    is_path_owner = owner in PATHLIB_SAFE_NAMES or root == "pathlib"
    if is_path_owner:
        if attr in PATHLIB_SIDE_EFFECT_METHODS:
            return False, pathlib_reason(attr)
        if attr in PATHLIB_BANNED_IF_PATH:
            return False, pathlib_reason(attr)
    return True, ""


def import_alias_map(tree: ast.AST) -> dict[str, str]:
    """把 ``import ... as ...`` / ``from ... import ... as ...`` 解析成别名表。

    返回 ``本地名 -> 全限定名``：

    - ``import os as system_os`` → ``{"system_os": "os"}``
    - ``import pathlib`` → ``{"pathlib": "pathlib"}``
    - ``from pathlib import Path as P`` → ``{"P": "pathlib.Path"}``

    只做**可静态确定**的映射；``from x import *`` 不产生条目（无法确定）。
    """
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".", 1)[0]
                out[local] = alias.name if alias.asname else alias.name.split(".", 1)[0]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if not base:
                continue  # 相对导入无法确定绝对模块，不猜
            for alias in node.names:
                if alias.name == "*":
                    continue
                local = alias.asname or alias.name
                out[local] = "{}.{}".format(base, alias.name)
    return out


def module_policy(module_name: str) -> tuple[bool, str]:
    """判定 ``import module_name`` 是否允许。

    返回 ``(allowed, reason)``。未登记且不在禁用表里的模块**默认拒绝**
    （白名单语义），原因是设计 §4.3 明确否定了"标准库全部允许"。
    """
    name = str(module_name or "").strip()
    if not name:
        return False, "空模块名"
    root = name.split(".", 1)[0]
    if name in BANNED_MODULES or root in BANNED_MODULES:
        return False, "模块 {!r} 在禁用表（进程/网络/动态加载/文件系统类）".format(name)
    if name in ALLOWED_STDLIB_MODULES or root in ALLOWED_STDLIB_MODULES:
        return True, "已登记的标准库模块"
    if name in ALLOWED_PROJECT_IMPORTS:
        return True, "已登记的项目模块"
    return False, "模块 {!r} 未登记（当前策略为白名单，需先登记能力契约）".format(name)


def name_policy(name: str) -> tuple[bool, str]:
    """判定一个被调用的内建名是否允许。"""
    key = str(name or "").strip()
    if not key:
        return True, ""
    if key in BANNED_BUILTINS:
        return False, "{!r} 被 Python 能力策略禁止（设计 §4.3）".format(key)
    return True, ""


def import_node_module(node: ast.AST) -> str:
    """从 ``ast.Import`` / ``ast.ImportFrom`` 提取模块全名。"""
    if isinstance(node, ast.Import):
        return ",".join(alias.name for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        base = node.module or ""
        if node.level:
            base = "." * node.level + base
        return base
    return ""


def iter_imported_modules(tree: ast.AST) -> list[tuple[str, int]]:
    """列出树里所有导入的模块与行号（``from x import y`` 记 ``x``）。"""
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for part in import_node_module(node).split(","):
                part = part.strip()
                if part:
                    found.append((part, getattr(node, "lineno", 0)))
    return found


def policy_summary() -> dict[str, list[str]]:
    """给人看的策略摘要（渲染/排障用）。"""
    return {
        "allowed_stdlib_modules": sorted(ALLOWED_STDLIB_MODULES),
        "allowed_project_imports": sorted(ALLOWED_PROJECT_IMPORTS),
        "banned_modules": sorted(BANNED_MODULES),
        "banned_builtins": sorted(BANNED_BUILTINS),
        "pathlib_allowed_names": sorted(PATHLIB_SAFE_NAMES),
        "pathlib_side_effect_methods": sorted(PATHLIB_SIDE_EFFECT_METHODS),
        "pathlib_banned_if_path": sorted(PATHLIB_BANNED_IF_PATH),
    }
