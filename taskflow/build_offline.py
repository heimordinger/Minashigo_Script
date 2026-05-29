"""
build_offline.py —— 构建离线版 index.html

把 index.html 中所有 <script type="module" src="..."> 涉及的文件
打包到一个非模块 <script> 标签里（包裹在 async IIFE 中），
使得可以直接用 file:/// 在浏览器中打开，无需 HTTP 服务。

离线时除了 WebSocket 不会自动连接（因为 Python 后端没启动），
其余所有 UI 功能（节点编辑、保存/加载、canvas 操作等）均正常。
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INDEX_HTML = ROOT / "index.html"
OUTPUT = ROOT / "index_offline.html"

# ── 所有模块文件的加载顺序（拓扑排序，从底向上） ──────────────────
FILE_ORDER = [
    # Phase 1: 零依赖模块
    "js/state.js",
    "core/account-manager.js",
    "JS&PyMessage/message.js",
    "JS&PyMessage/wsManager.js",
    "core/node-reporter.js",
    "core/input-dialog.js",
    "core/workflow-controller.js",

    # Phase 2: 依赖 phase 1
    "core/action-node.js",
    "js/layout.js",
    "js/drag.js",
    "core/node-panel.js",
    "core/runtime-panel.js",
    "core/control-panel.js",
    "core/taskflow-backend.js",
    "core/account-api.js",

    # Phase 3: 高层 UI 模块（含循环依赖，同一 IIFE 里解决）
    "js/tab.js",
    "core/loader.js",
    "js/main.js",

    # Phase 4: 节点定义（纯副作用，注册 LiteGraph 节点类型）
    "nodes/flow/start.js",
    "nodes/flow/delay.js",
    "nodes/flow/label.js",
    "nodes/flow/goto.js",
    "nodes/flow/multi_input.js",
    "nodes/flow/end.js",
    "nodes/flow/wait_image.js",
    "nodes/flow/sleep.js",
    "nodes/flow/screenshot.js",
    "nodes/action/click.js",
    "nodes/action/url_goto.js",
    "nodes/action/url.js",
    "nodes/action/click_image.js",
    "nodes/action/click_text.js",
    "nodes/action/click_until_gone.js",
    "nodes/action/dmm_login.js",
    "nodes/action/match_image.js",
    "nodes/mnsg/scene_detect.js",
    "nodes/test/test_error.js",
]


def _replace_import_block(m: re.Match) -> str:
    """处理 import { X as Y } → 生成 var Y = X; 在相同位置"""
    body = m.group(1)
    aliases = []
    for part in body.split(","):
        part = part.strip()
        if " as " in part:
            raw_name, alias = [x.strip() for x in part.split(" as ", 1)]
            aliases.append(f"var {alias} = {raw_name};")
    return ("\n".join(aliases) + "\n") if aliases else ""


def strip_imports(source: str) -> str:
    """移除 import 语句，处理别名、side-effect import。"""
    # 1. import { ... } from "..."
    source = re.sub(
        r'import\s*\{([^}]*)\}\s*from\s*["\'][^"\']*["\']\s*;?\n?',
        _replace_import_block,
        source,
    )
    # 2. import X from "..."
    source = re.sub(
        r'import\s+\w+(?:\s*,\s*\{[^}]*\})?\s*from\s*["\'][^"\']*["\']\s*;?\n?',
        '',
        source,
    )
    # 3. import "..."  (side-effect import)
    source = re.sub(
        r'import\s*["\'][^"\']*["\']\s*;?\n?',
        '',
        source,
    )

    # ── 移除 export 关键字（保留声明本身） ──────────────────
    # export function / export async function / export class  (含 export default)
    source = re.sub(
        r'\bexport\s+(default\s+)?(?=(async\s+)?(function|class)\b)',
        '',
        source,
    )
    # export const / export let / export var
    source = re.sub(r'\bexport\s+(?=(const|let|var)\b)', '', source)
    # 独立的 export { … };
    source = re.sub(r'^export\s+\{[^}]*\}\s*;?\s*$', '', source, flags=re.MULTILINE)

    return source


def strip_dynamic_imports(source: str, file_rel: str) -> str:
    """处理动态 import() 调用。"""
    # loader.js: 节点文件的动态 import → 整行移除（内部含有 Date.now() 嵌套括号）
    if "loader.js" in file_rel:
        source = re.sub(r'^\s*await\s+import\(.*\)\s*;?\s*$', '', source, flags=re.MULTILINE)
    # control-panel.js: 懒加载 tab.js → 移除（tab 已在 bundle 中）
    source = source.replace(
        'const { createTab } = await import("../js/tab.js");',
        "// (离线模式: createTab 已在 bundle 中)"
    )
    return source


def transform_file(file_rel: str) -> str:
    """读取一个 JS 文件，转译为非模块代码。"""
    path = ROOT / file_rel
    if not path.exists():
        print(f"  [!] 文件不存在: {file_rel}")
        return f"// MISSING: {file_rel}\n"

    source = path.read_text(encoding="utf-8")
    source = strip_imports(source)
    source = strip_dynamic_imports(source, file_rel)

    # 在文件首尾加注释方便调试
    header = f"\n// ── {file_rel} ──\n"
    return header + source + "\n"


def build() -> None:
    print("=" * 60)
    print("  TaskFlow 离线版构建")
    print("=" * 60)

    # ── 收集所有模块代码 ────────────────────────────────────────
    fragments = []
    fragments.append(
        "// ===== TaskFlow 离线 Bundle (由 build_offline.py 生成) =====\n"
        "// 默认 WebSocket 端口（离线时不用，Python 后端未启动则连接自动失败）\n"
        'var WS_PORT = 8011;\n'
    )

    for rel in FILE_ORDER:
        print(f"  打包: {rel}")
        fragments.append(transform_file(rel))

    # ── 把全部代码包入一个 async IIFE ────────────────────────────
    # 原因：main.js 中有顶层 await，必须放在 async function 内
    # 同时 function 声明在 IIFE 内仍然是 hoisted 的，不影响模块间的引用
    bundle_js = (
        "// @ts-nocheck\n"
        "(async () => {\n"
        + "".join(fragments) +
        "})();\n"
    )

    # ── 读取 index.html，替换 script 标签 ───────────────────────
    html = INDEX_HTML.read_text(encoding="utf-8")

    # 去掉所有 <script type="module" src="..."> 标签
    html = re.sub(
        r'<script\s+type="module"\s+src="[^"]*\.js\?[^"]*"[^>]*>\s*</script>\s*\n?',
        "",
        html,
    )

    # 在 </body> 前插入 bundle script（放在 litegraph_dev.js 之后）
    bundle_tag = f'<script>\n{bundle_js}\n</script>\n</body>'
    html = html.replace("</body>", bundle_tag)

    # ── 写入 ─────────────────────────────────────────────────────
    OUTPUT.write_text(html, encoding="utf-8")
    size_kb = len(bundle_js) / 1024
    print(f"\n[OK] 构建完成: {OUTPUT}")
    print(f"     Bundle 大小: {size_kb:.1f} KB")
    print(f"\n     现在可以直接用浏览器打开: {OUTPUT}")
    print(f"     (如要完整功能，请通过 run_taskflow.py 启动后端)")


if __name__ == "__main__":
    build()
