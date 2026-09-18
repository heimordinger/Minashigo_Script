"""SpecEditor 草稿 mixin（autosave + 具名多版）。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QVBoxLayout,
)

from script_spec.draft_store import (
    DRAFT_DIR,
    AUTOSAVE_PATH,
    NAMED_DRAFT_DIR,
    draft_preview_line,
    migrate_legacy_draft,
    safe_draft_filename,
    suggest_draft_name,
)
from script_spec.model import ScriptSpec


class DraftMixin:
    # ── 草稿（autosave + 具名多版）──

    def _ensure_draft_dirs(self) -> None:
        migrate_legacy_draft()
        DRAFT_DIR.mkdir(parents=True, exist_ok=True)
        NAMED_DRAFT_DIR.mkdir(parents=True, exist_ok=True)

    def _parse_draft_payload(self, raw: dict) -> tuple[ScriptSpec | None, str, str]:
        """返回 (spec|None, saved_at, name)。"""
        if not isinstance(raw, dict):
            return None, "", ""
        data = dict(raw)
        saved_at = str(data.pop("_draft_saved_at", "") or "")
        name = str(data.pop("_draft_name", "") or "")
        spec = ScriptSpec.from_dict(data)
        if spec.is_blank():
            return None, saved_at, name
        return spec, saved_at, name

    def _read_draft_path(self, path: Path) -> tuple[ScriptSpec | None, str, str]:
        if not path.is_file():
            return None, "", ""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None, "", ""
        return self._parse_draft_payload(raw)

    def _write_draft_path(
        self, path: Path, spec: ScriptSpec, *, name: str = ""
    ) -> str:
        self._ensure_draft_dirs()
        path.parent.mkdir(parents=True, exist_ok=True)
        saved_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        data = spec.to_dict()
        data["_draft_saved_at"] = saved_at
        if name:
            data["_draft_name"] = name
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return saved_at

    def _read_autosave(self) -> tuple[ScriptSpec | None, str]:
        self._ensure_draft_dirs()
        spec, saved_at, _ = self._read_draft_path(AUTOSAVE_PATH)
        return spec, saved_at

    def _list_named_drafts(self) -> list[dict]:
        """按时间新→旧。每项: path, name, saved_at, preview, kind."""
        self._ensure_draft_dirs()
        out: list[dict] = []
        for path in NAMED_DRAFT_DIR.glob("*.json"):
            spec, saved_at, name = self._read_draft_path(path)
            if not spec:
                continue
            out.append(
                {
                    "kind": "named",
                    "path": path,
                    "name": name or path.stem,
                    "saved_at": saved_at,
                    "preview": draft_preview_line(spec),
                }
            )
        out.sort(key=lambda d: d.get("saved_at") or "", reverse=True)
        return out

    def _list_all_draft_entries(self) -> list[dict]:
        entries: list[dict] = []
        auto, auto_at = self._read_autosave()
        if auto:
            entries.append(
                {
                    "kind": "autosave",
                    "path": AUTOSAVE_PATH,
                    "name": "自动草稿（最近编辑）",
                    "saved_at": auto_at,
                    "preview": draft_preview_line(auto),
                }
            )
        entries.extend(self._list_named_drafts())
        return entries

    def _named_path_for(self, name: str) -> Path:
        stem = safe_draft_filename(name)
        return NAMED_DRAFT_DIR / f"{stem}.json"

    def _find_named_by_name(self, name: str) -> Path | None:
        want = (name or "").strip()
        if not want:
            return None
        for d in self._list_named_drafts():
            if d["name"] == want:
                return d["path"]
        # 同 stem 文件名
        p = self._named_path_for(want)
        return p if p.is_file() else None

    def _refresh_draft_chip(self, saved_at: str | None = None):
        auto_spec, auto_at = self._read_autosave()
        named = self._list_named_drafts()
        parts: list[str] = []
        tip_lines = [f"草稿目录\n{DRAFT_DIR}", ""]
        if auto_spec and auto_at:
            short = auto_at[11:16] if len(auto_at) >= 16 else auto_at
            parts.append(f"自动 {short}")
            tip_lines.append(f"自动草稿：{auto_at}")
            tip_lines.append(f"  {draft_preview_line(auto_spec)}")
        if named:
            parts.append(f"具名 {len(named)}")
            tip_lines.append(f"具名草稿：{len(named)} 份")
            for d in named[:6]:
                tip_lines.append(f"  · {d['name']} ({d['saved_at'] or '?'})")
            if len(named) > 6:
                tip_lines.append(f"  …另有 {len(named) - 6} 份")
        if parts:
            self._draft_chip.setText(" · ".join(parts))
            self._draft_chip.setProperty("hasDraft", "true")
        else:
            self._draft_chip.setText("无草稿")
            self._draft_chip.setProperty("hasDraft", "false")
        self._draft_chip.setToolTip("\n".join(tip_lines).rstrip())
        self._draft_chip.style().unpolish(self._draft_chip)
        self._draft_chip.style().polish(self._draft_chip)

    def _autosave_draft(self):
        if self._updating or self._draft_loading:
            return
        spec = self.get_spec()
        if spec.is_blank():
            return
        try:
            self._write_draft_path(AUTOSAVE_PATH, spec, name="")
            self._refresh_draft_chip()
        except Exception:
            pass

    def _apply_draft_spec(self, spec: ScriptSpec, saved_at: str = "") -> None:
        self._draft_loading = True
        try:
            self.set_spec(spec)
        finally:
            self._draft_loading = False
        self._refresh_draft_chip(saved_at)
        self._refresh_preview()

    def _restore_draft_on_start(self):
        """启动只恢复自动草稿，不弹具名列表。"""
        spec, saved_at = self._read_autosave()
        if not spec:
            self._refresh_draft_chip("")
            return
        self._apply_draft_spec(spec, saved_at)

    def _pick_draft_entry(
        self,
        *,
        title: str,
        allow_delete: bool,
        entries: list[dict] | None = None,
    ) -> dict | None:
        items = entries if entries is not None else self._list_all_draft_entries()
        if not items:
            QMessageBox.information(self, title, "还没有可恢复的草稿")
            return None

        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.resize(480, 360)
        lay = QVBoxLayout(dlg)
        tip = QLabel(
            "自动草稿会被后续编辑覆盖；具名草稿互不影响。"
            + (" 可选中后删除具名草稿。" if allow_delete else "")
        )
        tip.setWordWrap(True)
        tip.setObjectName("MutedLabel")
        lay.addWidget(tip)

        lst = QListWidget()
        for d in items:
            kind = "自动" if d["kind"] == "autosave" else "具名"
            line = f"[{kind}] {d['name']}"
            if d.get("saved_at"):
                line += f"  ·  {d['saved_at']}"
            if d.get("preview"):
                line += f"\n    {d['preview']}"
            item = QListWidgetItem(line)
            item.setData(Qt.ItemDataRole.UserRole, d)
            lst.addItem(item)
        lst.setCurrentRow(0)
        lay.addWidget(lst, stretch=1)

        buttons = QDialogButtonBox()
        btn_ok = buttons.addButton("恢复", QDialogButtonBox.ButtonRole.AcceptRole)
        btn_del = None
        if allow_delete:
            btn_del = buttons.addButton("删除", QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        lay.addWidget(buttons)

        chosen: dict | None = None

        def _current() -> dict | None:
            it = lst.currentItem()
            return it.data(Qt.ItemDataRole.UserRole) if it else None

        def _on_ok():
            nonlocal chosen
            chosen = _current()
            if chosen:
                dlg.accept()

        def _on_del():
            cur = _current()
            if not cur:
                return
            if cur["kind"] == "autosave":
                QMessageBox.information(dlg, "删除", "自动草稿请用「清除」当前内容后自然覆盖；或直接覆盖保存。")
                return
            ans = QMessageBox.question(
                dlg,
                "删除具名草稿",
                f"删除「{cur['name']}」？\n{cur['path']}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return
            try:
                Path(cur["path"]).unlink(missing_ok=True)
            except Exception as e:
                QMessageBox.warning(dlg, "删除失败", str(e))
                return
            row = lst.currentRow()
            lst.takeItem(row)
            self._refresh_draft_chip()
            if lst.count() == 0:
                dlg.reject()

        btn_ok.clicked.connect(_on_ok)
        if btn_del is not None:
            btn_del.clicked.connect(_on_del)
        buttons.rejected.connect(dlg.reject)
        lst.itemDoubleClicked.connect(lambda *_: _on_ok())

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        return chosen

    def _load_draft_clicked(self):
        entry = self._pick_draft_entry(title="恢复草稿", allow_delete=False)
        if not entry:
            return
        if not self.get_spec().is_blank():
            ans = QMessageBox.question(
                self,
                "恢复草稿",
                f"用「{entry['name']}」覆盖当前内容？\n时间：{entry.get('saved_at') or '未知'}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return
        spec, saved_at, _ = self._read_draft_path(Path(entry["path"]))
        if not spec:
            QMessageBox.warning(self, "恢复草稿", "草稿文件已损坏或为空")
            return
        self._apply_draft_spec(spec, saved_at)

    def _save_draft_clicked(self):
        spec = self.get_spec()
        if spec.is_blank():
            QMessageBox.information(self, "草稿", "当前内容为空，未写入草稿")
            return
        result = self._prompt_save_draft(spec)
        if not result:
            return
        name, path, from_list = result
        if (not from_list) and path.is_file():
            ans = QMessageBox.question(
                self,
                "覆盖草稿",
                f"已有同名草稿「{name}」，覆盖？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return
        try:
            self._write_draft_path(path, spec, name=name)
            self._write_draft_path(AUTOSAVE_PATH, spec, name="")
        except Exception as e:
            QMessageBox.warning(self, "存草稿失败", str(e))
            return
        self._refresh_draft_chip()
        QMessageBox.information(
            self,
            "已存草稿",
            f"具名草稿「{name}」已保存\n{path}\n\n"
            f"加载其它介绍并修改时，只会覆盖「自动草稿」，不会动这份具名草稿。",
        )

    def _prompt_save_draft(
        self, spec: ScriptSpec
    ) -> tuple[str, Path, bool] | None:
        """存草稿对话框：名称 + 已有具名列表（点选即覆盖目标）。

        返回 (name, path, selected_from_list)。取消则 None。
        """
        default = suggest_draft_name(spec)
        named = self._list_named_drafts()

        dlg = QDialog(self)
        dlg.setWindowTitle("存具名草稿")
        dlg.resize(460, 380)
        lay = QVBoxLayout(dlg)

        tip = QLabel(
            "可输入新名称另存，或从下方列表点选已有草稿进行覆盖。"
        )
        tip.setWordWrap(True)
        tip.setObjectName("MutedLabel")
        lay.addWidget(tip)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("草稿名称"))
        name_edit = QLineEdit(default)
        name_edit.setPlaceholderText("新草稿名称…")
        name_row.addWidget(name_edit, stretch=1)
        lay.addLayout(name_row)

        lay.addWidget(QLabel("已有具名草稿（点选覆盖）"))
        lst = QListWidget()
        lst.setMinimumHeight(180)
        none_item = QListWidgetItem("（不覆盖，另存为上方名称）")
        none_item.setData(Qt.ItemDataRole.UserRole, None)
        lst.addItem(none_item)
        for d in named:
            line = d["name"]
            if d.get("saved_at"):
                line += f"  ·  {d['saved_at']}"
            if d.get("preview"):
                line += f"\n    {d['preview']}"
            item = QListWidgetItem(line)
            item.setData(Qt.ItemDataRole.UserRole, d)
            lst.addItem(item)
        lst.setCurrentRow(0)
        lay.addWidget(lst, stretch=1)

        status = QLabel("")
        status.setObjectName("MutedLabel")
        status.setWordWrap(True)
        lay.addWidget(status)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        lay.addWidget(buttons)

        selected_from_list = {"on": False}

        def _refresh_status():
            data = None
            it = lst.currentItem()
            if it is not None:
                data = it.data(Qt.ItemDataRole.UserRole)
            name = name_edit.text().strip() or default
            if data:
                status.setText(f"将覆盖：「{data['name']}」\n{data['path']}")
                selected_from_list["on"] = True
            else:
                existing = self._find_named_by_name(name)
                if existing is not None:
                    status.setText(f"名称已存在，保存将覆盖：\n{existing}")
                else:
                    status.setText(f"将另存为新草稿「{name}」")
                selected_from_list["on"] = False

        def _on_list_changed():
            it = lst.currentItem()
            data = it.data(Qt.ItemDataRole.UserRole) if it else None
            if data:
                # 阻断 textChanged 误清选项时先填名称
                name_edit.blockSignals(True)
                name_edit.setText(data["name"])
                name_edit.blockSignals(False)
            _refresh_status()

        def _on_name_edited(_text: str):
            # 用户改名 → 视为另存/按名称匹配，取消列表覆盖选中感
            cur = lst.currentItem()
            data = cur.data(Qt.ItemDataRole.UserRole) if cur else None
            if data and name_edit.text().strip() != data["name"]:
                lst.blockSignals(True)
                lst.setCurrentRow(0)
                lst.blockSignals(False)
            _refresh_status()

        lst.currentItemChanged.connect(lambda *_: _on_list_changed())
        lst.itemDoubleClicked.connect(lambda *_: dlg.accept())
        name_edit.textChanged.connect(_on_name_edited)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        _refresh_status()

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None

        it = lst.currentItem()
        data = it.data(Qt.ItemDataRole.UserRole) if it else None
        if data:
            return data["name"], Path(data["path"]), True
        name = name_edit.text().strip() or default
        existing = self._find_named_by_name(name)
        path = existing if existing is not None else self._named_path_for(name)
        return name, path, False

    def _manage_drafts_clicked(self):
        entry = self._pick_draft_entry(title="草稿管理", allow_delete=True)
        if not entry:
            self._refresh_draft_chip()
            return
        # 管理页选「恢复」也加载
        if not self.get_spec().is_blank():
            ans = QMessageBox.question(
                self,
                "恢复草稿",
                f"用「{entry['name']}」覆盖当前内容？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return
        spec, saved_at, _ = self._read_draft_path(Path(entry["path"]))
        if not spec:
            QMessageBox.warning(self, "恢复草稿", "草稿文件已损坏或为空")
            return
        self._apply_draft_spec(spec, saved_at)
