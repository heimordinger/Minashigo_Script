from __future__ import annotations

import asyncio
import re
from pathlib import Path

from PySide6.QtGui import (
    QFont, QPixmap, QKeySequence, QShortcut, QTextCursor, QTextDocument, QDesktopServices,
)
from PySide6.QtCore import QThread, Signal, Qt, QTimer, QUrl
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QFormLayout,
    QLineEdit, QComboBox, QPushButton, QCheckBox, QTextEdit,
    QLabel, QFileDialog, QSpinBox, QTabWidget, QSplitter,
    QMessageBox, QGroupBox, QProgressBar, QStyle, QFrame,
    QApplication, QDialog, QScrollArea, QDialogButtonBox, QProgressDialog,
    QInputDialog, QListWidget, QListWidgetItem, QSizePolicy,
)

from gui.widgets.GenTrajectory import GenTrajectory
from gui.widgets.TrialHud import TrialHud
from core.path import IMG_PATH, SCRIPTS_PATH

def _reload_generator_modules():
    """生成/测连前热重载，改 agent/graph 后无需重启整个应用。"""
    try:
        from backend.script_generator.reload import reload_script_generator
        reload_script_generator()
    except Exception as e:
        print(f"[ScriptGenerator] 热重载失败（将使用已加载模块）: {e}")

class ConnectionTestWorker(QThread):
    finished = Signal(dict)

    def __init__(self, params: dict):
        super().__init__()
        self.params = params

    def run(self):
        _reload_generator_modules()
        from backend.script_generator.agent import test_connection as _test
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(_test(**self.params))
            self.finished.emit(result)
        except Exception as e:
            self.finished.emit({
                "ok": False,
                "latency_ms": 0,
                "reply": "",
                "error": str(e),
                "input_tokens": 0,
                "output_tokens": 0,
            })
        finally:
            loop.close()

class GenerateWorker(QThread):
    finished = Signal(str)
    partial = Signal(str)  # 流式输出的片段
    status = Signal(str)  # LangGraph 阶段提示
    artifact = Signal(str, str)  # kind, payload（如 plan）
    token_info = Signal(int, int)  # 输入tokens, 输出tokens
    error = Signal(str)

    def __init__(self, params: dict):
        super().__init__()
        self.params = params

    def run(self):
        _reload_generator_modules()
        from backend.script_generator.agent import generate_script as _generate
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.params["on_partial"] = lambda text: self.partial.emit(text)
            self.params["on_status"] = lambda msg: self.status.emit(msg)
            self.params["on_artifact"] = lambda kind, payload: self.artifact.emit(kind, payload)
            code, inp, out = loop.run_until_complete(_generate(**self.params))
            self.token_info.emit(inp, out)
            self.finished.emit(code)
        except Exception as e:
            self.error.emit(str(e))
        finally:
            loop.close()

class ReviseWorker(QThread):
    finished = Signal(str, str, object)  # code, change_summary, meta
    partial = Signal(str)
    status = Signal(str)
    artifact = Signal(str, str)
    token_info = Signal(int, int)
    error = Signal(str)

    def __init__(self, params: dict):
        super().__init__()
        self.params = params

    def run(self):
        _reload_generator_modules()
        from backend.script_generator.agent import revise_script as _revise
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.params["on_partial"] = lambda text: self.partial.emit(text)
            self.params["on_status"] = lambda msg: self.status.emit(msg)
            self.params["on_artifact"] = lambda kind, payload: self.artifact.emit(kind, payload)
            code, summary, inp, out, meta = loop.run_until_complete(_revise(**self.params))
            self.token_info.emit(inp, out)
            self.finished.emit(code, summary or "", meta or {})
        except Exception as e:
            self.error.emit(str(e))
        finally:
            loop.close()

class OptimizeWorker(QThread):
    finished = Signal(str, str, object)  # code, change_summary, meta
    partial = Signal(str)
    status = Signal(str)
    artifact = Signal(str, str)
    token_info = Signal(int, int)
    error = Signal(str)

    def __init__(self, params: dict):
        super().__init__()
        self.params = params

    def run(self):
        _reload_generator_modules()
        from backend.script_generator.optimize import optimize_script as _optimize
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.params["on_partial"] = lambda text: self.partial.emit(text)
            self.params["on_status"] = lambda msg: self.status.emit(msg)
            self.params["on_artifact"] = lambda kind, payload: self.artifact.emit(kind, payload)
            code, summary, inp, out, meta = loop.run_until_complete(_optimize(**self.params))
            self.token_info.emit(inp, out)
            self.finished.emit(code, summary or "", meta or {})
        except Exception as e:
            self.error.emit(str(e))
        finally:
            loop.close()


class CollabLlmWorker(QThread):
    """协作页一轮 LLM（含可选 tools / JSON 协议）。"""

    finished = Signal(str, object, object)  # visible, meta, session_dict
    status = Signal(str)
    partial = Signal(str)  # 流式增量（delta）
    error = Signal(str)
    token_info = Signal(int, int)

    def __init__(self, params: dict):
        super().__init__()
        self.params = params

    def run(self):
        # 协作对话不热重载：每轮 reload 会明显拖慢首 token
        from backend.script_generator.collaborator.llm_turn import run_collab_llm_turn
        from backend.script_generator.collaborator.session import CollabSession
        from backend.script_generator.collaborator.skills.registry import default_registry

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            session = CollabSession.from_dict(dict(self.params.get("session") or {}))
            registry = default_registry()

            def _status(msg: str) -> None:
                self.status.emit(str(msg or ""))

            def _partial(piece: str) -> None:
                if piece:
                    self.partial.emit(str(piece))

            visible, meta, inp, out = loop.run_until_complete(
                run_collab_llm_turn(
                    provider=str(self.params.get("provider") or ""),
                    api_key=str(self.params.get("api_key") or ""),
                    model=str(self.params.get("model") or ""),
                    api_endpoint=self.params.get("api_endpoint"),
                    max_tokens=self.params.get("max_tokens"),
                    registry=registry,
                    session=session,
                    vision_assist=self.params.get("vision_assist"),
                    permissions=self.params.get("permissions"),
                    on_status=_status,
                    on_partial=_partial,
                    resume_permission=str(self.params.get("resume_permission") or ""),
                )
            )
            self.token_info.emit(int(inp or 0), int(out or 0))
            self.finished.emit(visible, meta or {}, session.to_dict())
        except Exception as e:
            self.error.emit(str(e))
        finally:
            loop.close()
