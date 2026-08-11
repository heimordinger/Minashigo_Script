"""隔离预览：脚本说明编辑器。

用法:
  python/python311/Scripts/python.exe -m script_spec
  或: python/python311/Scripts/python.exe scripts/测试/说明编辑器预览.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from script_spec.editor import main

if __name__ == "__main__":
    main()
