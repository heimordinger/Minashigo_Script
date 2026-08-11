# scripts/converter/__main__.py
"""命令行入口：python -m scripts.converter <script.py> [-o output.json]"""

import json
import sys
from pathlib import Path

from . import convert_script


def main():
    if len(sys.argv) < 2:
        print("用法: python -m scripts.converter <script.py> [-o output.json]")
        sys.exit(1)

    src = sys.argv[1]
    out = None
    if "-o" in sys.argv:
        idx = sys.argv.index("-o")
        if idx + 1 < len(sys.argv):
            out = sys.argv[idx + 1]

    print(f"转换: {src}")
    try:
        graph = convert_script(src)
    except NotImplementedError as e:
        print(f"暂不支持: {e}")
        sys.exit(1)

    if out:
        Path(out).write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写入: {out}")
    else:
        print(json.dumps(graph, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
