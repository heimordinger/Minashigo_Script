# -*- coding: utf-8 -*-
"""独立启动入口；实现见 gui.widgets.NetworkInspector。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gui.widgets.NetworkInspector import main

if __name__ == "__main__":
    main()
