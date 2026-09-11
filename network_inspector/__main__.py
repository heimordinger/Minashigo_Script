"""可直接运行: python -m network_inspector"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def main():
    from gui.widgets.NetworkInspector import main as _main

    _main(facade=None)


if __name__ == "__main__":
    main()
