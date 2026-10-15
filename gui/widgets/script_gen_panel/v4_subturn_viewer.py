"""Read-only V4 subturn conversation viewer backed by durable JSONL traces."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core.path import PROJECT_ROOT


def find_latest_v4_trace(session_id: str, root: Path | None = None) -> Path | None:
    root = Path(root or (PROJECT_ROOT / "logs" / "runtime"))
    wanted = str(session_id or "")
    for path in sorted(root.glob("*.jsonl"), key=lambda item: item.stat().st_mtime, reverse=True)[:40]:
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[:5]:
                row = json.loads(line)
                if row.get("source") != "collab_v4":
                    continue
                message = json.loads(str(row.get("message") or "{}"))
                if message.get("event") == "trace_started" and (
                    not wanted or str(message.get("session_id") or "") == wanted
                ):
                    return path
        except Exception:
            continue
    return None


def parse_v4_trace(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
            if row.get("source") != "collab_v4":
                continue
            body = json.loads(str(row.get("message") or "{}"))
            if not isinstance(body, dict):
                continue
            body["_ts"] = row.get("ts")
            body["_level"] = row.get("level")
            events.append(body)
        except Exception:
            continue
    return events


def _event_view(event: dict[str, Any]) -> tuple[str, str, str]:
    kind = str(event.get("event") or "event")
    screen = str(event.get("screen") or "")
    subturn = event.get("subturn")
    suffix = f" · 副轮 {subturn}" if subturn is not None else ""
    if screen:
        suffix += f" · {screen}"
    if kind == "form_request":
        content = event.get("payload") or {
            "context_keys": event.get("context_keys"),
            "prompt_chars": event.get("prompt_chars"),
            "repair": event.get("repair"),
        }
        return "runtime", f"Runtime → LLM{suffix}", json.dumps(content, ensure_ascii=False, indent=2)
    if kind == "form_response":
        return "llm", f"LLM → Runtime{suffix}", str(event.get("raw") or "")
    labels = {
        "menu_selected": "菜单选择",
        "query_completed": "本地查询结果",
        "capability_started": "工具调用",
        "capability_completed": "工具返回",
        "temporary_script_started": "临时脚本",
        "temporary_script_completed": "临时脚本结果",
        "invoke_contract_switched": "切换工具契约",
        "form_invalid": "表单校验失败",
        "form_repair_exhausted": "表单重填耗尽",
        "temporary_script_failed": "临时脚本异常",
        "capability_failed": "工具异常",
        "llm_call_failed": "LLM 调用异常",
        "main_turn_failed": "主轮异常",
        "main_turn_completed": "主轮完成",
    }
    return (
        "error" if str(event.get("_level") or "").upper() == "ERROR" else "system",
        f"{labels.get(kind, kind)}{suffix}",
        json.dumps({k: v for k, v in event.items() if not k.startswith("_")}, ensure_ascii=False, indent=2),
    )


class V4SubturnViewer(QDialog):
    def __init__(self, *, session_id: str, log_path: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("V4 副轮观察器")
        self.resize(1050, 760)
        self._session_id = str(session_id or "")
        candidate = Path(log_path) if log_path else None
        self._path = candidate if candidate and candidate.is_file() else find_latest_v4_trace(self._session_id)

        root = QVBoxLayout(self)
        bar = QHBoxLayout()
        self._path_label = QLabel("")
        self._path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        bar.addWidget(self._path_label, 1)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.reload)
        reveal = QPushButton("打开日志目录")
        reveal.clicked.connect(self._reveal)
        bar.addWidget(refresh)
        bar.addWidget(reveal)
        root.addLayout(bar)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._host = QWidget()
        self._messages = QVBoxLayout(self._host)
        self._messages.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._messages.setSpacing(10)
        self._scroll.setWidget(self._host)
        root.addWidget(self._scroll, 1)
        self.reload()

    def reload(self) -> None:
        latest = find_latest_v4_trace(self._session_id)
        if latest is not None:
            self._path = latest
        while self._messages.count():
            item = self._messages.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not self._path or not self._path.is_file():
            self._path_label.setText("当前会话还没有 V4 副轮日志")
            return
        self._path_label.setText(str(self._path))
        for event in parse_v4_trace(self._path):
            if event.get("event") in {"trace_started", "form_valid", "main_turn_started"}:
                continue
            role, title, text = _event_view(event)
            self._messages.addWidget(self._bubble(role, title, text))
        bar = self._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _bubble(self, role: str, title: str, text: str) -> QWidget:
        row = QWidget()
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(4, 0, 4, 0)
        card = QFrame()
        card.setMaximumWidth(850)
        colors = {
            "runtime": ("#eef4ff", "#b8cdf3"),
            "llm": ("#eef9f1", "#b7ddc1"),
            "error": ("#fff0f0", "#e5aaaa"),
            "system": ("#f5f3ef", "#d7d0c5"),
        }
        bg, border = colors.get(role, colors["system"])
        card.setStyleSheet(f"QFrame {{ background:{bg}; border:1px solid {border}; border-radius:8px; }}")
        lay = QVBoxLayout(card)
        head = QLabel(title)
        head.setStyleSheet("font-weight:600; border:none; background:transparent;")
        lay.addWidget(head)
        body = QPlainTextEdit()
        body.setReadOnly(True)
        body.setPlainText(text)
        body.setFont(QFont("Consolas", 9))
        body.setMinimumHeight(90)
        body.setMaximumHeight(330)
        body.setStyleSheet("border:none; background:transparent;")
        lay.addWidget(body)
        if role == "llm":
            row_lay.addStretch(1)
            row_lay.addWidget(card, 4)
        else:
            row_lay.addWidget(card, 4)
            row_lay.addStretch(1)
        return row

    def _reveal(self) -> None:
        target = self._path.parent if self._path else PROJECT_ROOT / "logs" / "runtime"
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
