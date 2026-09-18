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

from gui.widgets.script_gen_panel.constants import _TRIAL_REL
from gui.widgets.script_gen_panel.widgets import FullCodeDialog
from gui.widgets.script_gen_panel.workers import GenerateWorker
from core.path import SCRIPTS_PATH


class GenerateMixin:
    def _on_generate(self):
        api_key = self._api_key.text().strip()
        if not api_key:
            QMessageBox.warning(self, "缺少 API Key", "请先填写 API Key")
            return
        self._gen_ctx = {}  # 新一轮生成，丢弃上一轮上下文

        expl_text = self._explanation.toPlainText().strip()
        if not expl_text:
            QMessageBox.warning(self, "缺少说明", "请填写脚本解释或描述")
            return

        if not self._source_dir:
            QMessageBox.warning(
                self,
                "缺少图片文件夹",
                "请先点「选择图片文件夹」。\n\n"
                "否则生成的脚本会落到默认路径 assets/images/game/script，"
                "运行时会出现「图片不存在」。",
            )
            return
        # 生成前按素材夹递归重扫，确保识图/白名单含子目录图
        try:
            from backend.script_generator.agent import list_source_image_paths
            disk = list_source_image_paths(str(self._source_dir))
            if disk:
                self._image_entries = [{"path": p, "desc": ""} for p in disk]
                self._update_img_label()
        except Exception as e:
            print(f"[ScriptGenerator] 递归扫描素材失败: {e}")
        if not self._image_entries:
            QMessageBox.warning(
                self,
                "文件夹内无图片",
                "所选文件夹里没有可用图片，请换一个包含 .png 等素材的目录。",
            )
            return

        try:
            self._persist_explanation(expl_text)
        except Exception as e:
            print(f"[ScriptGenerator] 生成前保存介绍失败: {e}")

        params = {
            "provider": self._current_provider(),
            "api_key": api_key,
            "model": self._model.currentText().strip(),
            "api_endpoint": self._endpoint.text().strip() or None,
            "explanation_text": expl_text,
            "image_paths": [e["path"] for e in self._image_entries],
            "source_dir": str(self._source_dir) if self._source_dir else "",
            "send_images": self._send_img_cb.isChecked(),
            "compress_images": self._compress_img_cb.isChecked(),
            "max_tokens": int(self._max_tokens.value()),
            "free_mode": bool(self._free_mode_cb.isChecked()),
        }
        if (
            self._send_img_cb.isChecked()
            and (self._image_mode.currentData() or "direct") == "assist"
        ):
            v_key = self._vision_api_key.text().strip()
            v_model = self._vision_model.currentText().strip()
            if not v_key or not v_model:
                QMessageBox.warning(
                    self,
                    "辅助识图未配置",
                    "已选择「辅助识图」，请先在「API 配置」页填写识图提供商 / Key / 模型，并保存。",
                )
                return
            params["vision_assist"] = {
                "provider": self._current_vision_provider(),
                "api_key": v_key,
                "model": v_model,
                "api_endpoint": self._vision_endpoint.text().strip() or None,
                "compress_images": self._compress_img_cb.isChecked(),
                "refresh_vision": self._vision_refresh_cb.isChecked(),
            }
        try:
            self._save_settings()
        except Exception:
            pass

        self._generate_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)
        self._holding_prev_live_code = False
        self._set_agent_busy("生成", "开始生成…", indeterminate=True)
        self._token_label.setText("")
        self._stream_buf = ""
        self._set_live_code("")
        self._view_code_btn.setEnabled(False)
        self._tabs.setCurrentIndex(self.TAB_GEN)
        self._archive.begin("generate")
        self._trajectory.begin_run("开始生成", fresh=True)
        self._last_revise_summary = ""
        self._last_diagnosis_json = ""
        self._chat_session = None
        self._stop_frame_path = ""
        self._last_trial_frame = None

        self._worker = GenerateWorker(params)
        self._worker.finished.connect(self._on_success)
        self._worker.partial.connect(self._on_partial)
        self._worker.status.connect(self._on_status)
        self._worker.artifact.connect(self._on_artifact)
        self._worker.token_info.connect(self._on_token_info)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _on_token_info(self, inp: int, out: int):
        if inp or out:
            self._last_inp_tokens = int(inp or 0)
            self._last_out_tokens = int(out or 0)
            self._token_label.setText(f"↕ {inp} 入 / {out} 出")
            if hasattr(self, "_agent_status") and self._agent_busy:
                # 保持相位文案，仅刷新 token（已在 label）
                pass

    def _on_artifact(self, kind: str, payload: str):
        if kind == "gen_ctx":
            # 生成管线的校验上下文（plan/free/source_dir）：归档与试运行沿用同一套
            try:
                import json as _json

                data = _json.loads(payload or "{}")
                if isinstance(data, dict):
                    self._gen_ctx = data
            except Exception as e:
                print(f"[ScriptGenerator] gen_ctx 解析失败: {e}")
            return
        if kind == "plan":
            # 计划正文并入轨迹「规划」步骤，不再单独占一块面板
            text = (payload or "").strip()
            self._trajectory.update_step(
                "plan",
                "Plan",
                "规划完成",
                status="done",
                body=text,
            )
        elif kind in ("explain_norm_meta", "explain_normalized"):
            text = (payload or "").strip()
            if text and getattr(self, "_archive", None) is not None:
                try:
                    name = (
                        "explain_norm_meta.json"
                        if kind == "explain_norm_meta"
                        else "explain_normalized.txt"
                    )
                    self._archive.write_text(name, text)
                except Exception as e:
                    print(f"[ScriptGenerator] 归档 {kind} 失败: {e}")
            if kind == "explain_norm_meta" and text:
                self._trajectory.update_step(
                    "explain_norm",
                    "Norm",
                    "介绍规范化",
                    status="done",
                    body=text,
                )
        elif kind == "chat_session":
            text = (payload or "").strip()
            if text:
                try:
                    from backend.script_generator.chat_session import loads_session
                    sess = loads_session(text)
                    if sess:
                        self._chat_session = sess
                        n = len(sess.get("messages") or [])
                        self._trajectory.update_step(
                            "chat_session",
                            "Chat",
                            "同会话已保存",
                            status="done",
                            body=f"messages={n}（修订将续写此对话）",
                        )
                except Exception as e:
                    print(f"[ScriptGenerator] chat_session 解析失败: {e}")
                if getattr(self, "_archive", None) is not None:
                    try:
                        self._archive.write_text("chat_session.json", text)
                    except Exception as e:
                        print(f"[ScriptGenerator] 归档 chat_session 失败: {e}")
        elif kind == "stage":
            self._apply_stage_artifact(payload or "")
        elif kind == "diagnosis":
            text = (payload or "").strip()
            if text:
                self._last_diagnosis_json = text
            if text and getattr(self, "_archive", None) is not None:
                try:
                    self._archive.write_text("diagnosis.json", text)
                except Exception as e:
                    print(f"[ScriptGenerator] 归档 diagnosis 失败: {e}")
            if text:
                self._trajectory.update_step(
                    "diagnose",
                    "Think",
                    "试跑诊断",
                    status="done",
                    body=text,
                )

    def _apply_stage_artifact(self, payload: str):
        """解析 stage 载荷: key|status|title|body"""
        parts = (payload or "").split("|", 3)
        if len(parts) < 3:
            return
        key, status, title = parts[0], parts[1], parts[2]
        body = parts[3] if len(parts) > 3 else ""
        prefix = key.split("_", 1)[0]
        kind_map = {
            "plan": "Plan",
            "generate": "Generate",
            "task": "Task",
            "merge": "Merge",
            "validate": "Validate",
            "fix": "Fix",
            "revise": "Revise",
            "review": "Review",
            "optimize": "Optimize",
            "diagnose": "Think",
            "vision_decide": "Think",
            "stop_vision": "Vision",
            "think": "Think",
            "vision": "Vision",
        }
        kind = kind_map.get(prefix, "Info")
        self._trajectory.update_step(
            key, kind, title, status=status, body=body,
        )
        # 顶栏进度
        pct = _STAGE_PROGRESS.get(prefix)
        if pct is not None and status == "running":
            self._set_agent_progress(pct)
        if title:
            self._set_agent_status_text(title)

    def _on_status(self, msg: str):
        if hasattr(self, "_trajectory"):
            self._trajectory.set_status_hint(msg or "")
        if msg:
            self._set_agent_status_text(msg)

    def _on_partial(self, text: str):
        if not hasattr(self, "_stream_buf"):
            self._stream_buf = ""
        if getattr(self, "_holding_prev_live_code", False):
            # 开始真正输出时再替换「上一版」，避免长时间空白
            self._holding_prev_live_code = False
            self._stream_buf = ""
        self._stream_buf += text or ""
        # 流式过程中也可打开查看（看当前缓冲）
        if self._stream_buf.strip():
            self._view_code_btn.setEnabled(True)
            if hasattr(self, "_live_code_hint"):
                n = len(self._stream_buf)
                self._live_code_hint.setText(f"流式写入中 · {n:,} 字符")
        self._schedule_live_code_refresh()

    def _schedule_live_code_refresh(self):
        if not hasattr(self, "_live_code_timer"):
            return
        if not self._live_code_timer.isActive():
            self._live_code_timer.start(90)

    def _flush_live_code(self):
        if not hasattr(self, "_live_code"):
            return
        if getattr(self, "_holding_prev_live_code", False):
            return
        buf = getattr(self, "_stream_buf", "") or ""
        if self._live_code.toPlainText() == buf:
            return
        self._live_code.setPlainText(buf)
        cursor = self._live_code.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self._live_code.setTextCursor(cursor)

    def _set_live_code(self, text: str):
        if not hasattr(self, "_live_code"):
            return
        t = text or ""
        self._holding_prev_live_code = False
        self._live_code.setPlainText(t)
        if hasattr(self, "_live_code_hint"):
            if t.strip():
                self._live_code_hint.setText(f"{len(t):,} 字符")
            else:
                self._live_code_hint.setText("流式输出会显示在这里")

    def _keep_prev_live_code(self, *, phase: str) -> None:
        """修订/优化开始时保留上一版代码，避免右侧变黑像卡住。"""
        prev = (self._generated_code or "").strip()
        self._stream_buf = ""
        if not prev:
            self._holding_prev_live_code = False
            self._set_live_code("")
            self._view_code_btn.setEnabled(False)
            self._save_btn.setEnabled(False)
            self._copy_btn.setEnabled(False)
            return
        self._holding_prev_live_code = True
        # 不调用 _set_live_code，以免清掉 holding 标记
        if hasattr(self, "_live_code") and not (self._live_code.toPlainText() or "").strip():
            self._live_code.setPlainText(self._generated_code)
        if hasattr(self, "_live_code_hint"):
            label = {"修订": "修订中", "优化": "优化中"}.get(phase, "处理中")
            self._live_code_hint.setText(
                f"{label}… · 暂显上一版（{len(prev):,} 字符），写出后替换"
            )
        self._view_code_btn.setEnabled(True)
        self._save_btn.setEnabled(True)
        self._copy_btn.setEnabled(True)

    def _format_elapsed(self, seconds: float) -> str:
        s = max(0, int(seconds))
        if s < 60:
            return f"已 {s} 秒"
        return f"已 {s // 60} 分 {s % 60:02d} 秒"

    def _tick_agent_elapsed(self):
        if not getattr(self, "_agent_busy", False):
            return
        import time
        started = float(getattr(self, "_agent_busy_started", 0) or 0)
        if started <= 0:
            return
        base = (getattr(self, "_agent_status_base", "") or "").strip() or "进行中"
        elapsed = self._format_elapsed(time.monotonic() - started)
        # 省略号循环：生成中. / .. / ...
        n_dots = (int((time.monotonic() - started) * 2) % 3) + 1
        dots = "." * n_dots
        self._render_agent_status(f"{base}{dots} · {elapsed}")
        banner_base = (getattr(self, "_busy_banner_base", "") or "").strip()
        if banner_base and hasattr(self, "_gen_result_banner"):
            self._gen_result_banner.setText(f"{banner_base}{dots}")

    def _render_agent_status(self, text: str):
        if hasattr(self, "_agent_status"):
            t = (text or "").replace("\n", " ").strip()
            if len(t) > 110:
                t = t[:109] + "…"
            self._agent_status.setText(t or "…")

    def _set_busy_banner(self, text: str):
        if not hasattr(self, "_gen_result_banner"):
            return
        base = (text or "").rstrip(".…· ")
        self._busy_banner_base = base
        self._gen_result_banner.setText(base + "…")
        self._gen_result_banner.setProperty("ok", False)
        self._gen_result_banner.setProperty("busy", True)
        self._gen_result_banner.setProperty("warn", False)
        self._gen_result_banner.style().unpolish(self._gen_result_banner)
        self._gen_result_banner.style().polish(self._gen_result_banner)
        self._gen_result_banner.show()

    def _set_warn_banner(self, text: str):
        if not hasattr(self, "_gen_result_banner"):
            return
        self._busy_banner_base = ""
        self._gen_result_banner.setText(text)
        self._gen_result_banner.setProperty("ok", False)
        self._gen_result_banner.setProperty("busy", False)
        self._gen_result_banner.setProperty("warn", True)
        self._gen_result_banner.style().unpolish(self._gen_result_banner)
        self._gen_result_banner.style().polish(self._gen_result_banner)
        self._gen_result_banner.show()

    def _set_ok_banner(self, text: str):
        if not hasattr(self, "_gen_result_banner"):
            return
        self._busy_banner_base = ""
        self._gen_result_banner.setText(text)
        self._gen_result_banner.setProperty("ok", True)
        self._gen_result_banner.setProperty("busy", False)
        self._gen_result_banner.setProperty("warn", False)
        self._gen_result_banner.style().unpolish(self._gen_result_banner)
        self._gen_result_banner.style().polish(self._gen_result_banner)
        self._gen_result_banner.show()

    def _set_agent_busy(self, phase: str, status: str, *, indeterminate: bool = True):
        import time

        self._agent_busy = True
        self._agent_busy_started = time.monotonic()
        if hasattr(self, "_agent_phase"):
            self._agent_phase.setText(phase or "Agent")
        # 状态正文不加「并非卡住」；省略号由计时器动画补上
        clean = (status or "").strip().rstrip(".…")
        self._set_agent_status_text(clean or f"{phase}中")
        if hasattr(self, "_agent_dots"):
            self._agent_dots.start()
        elif hasattr(self, "_agent_dot") and hasattr(self._agent_dot, "setProperty"):
            self._agent_dot.setProperty("busy", True)
            self._agent_dot.setProperty("success", False)
            self._agent_dot.style().unpolish(self._agent_dot)
            self._agent_dot.style().polish(self._agent_dot)
        busy_tips = {
            "生成": "生成中",
            "修订": "修订中 · 辅助工具处理中，右侧暂显上一版",
            "优化": "优化中 · 分析改写中，右侧暂显上一版",
        }
        self._set_busy_banner(busy_tips.get(phase, f"{phase}中"))
        if hasattr(self, "_to_trial_btn"):
            self._to_trial_btn.setProperty("ready", False)
            self._to_trial_btn.setText("去试运行 →")
            self._to_trial_btn.style().unpolish(self._to_trial_btn)
            self._to_trial_btn.style().polish(self._to_trial_btn)
        if hasattr(self, "_progress"):
            self._progress.show()
            if indeterminate:
                self._progress.setRange(0, 0)
            else:
                self._progress.setRange(0, 100)
                self._progress.setValue(5)
        if hasattr(self, "_agent_elapsed_timer"):
            self._agent_elapsed_timer.start()
            self._tick_agent_elapsed()
        for btn in (getattr(self, "_cancel_btn", None), getattr(self, "_strip_cancel_btn", None)):
            if btn is not None:
                btn.setEnabled(True)

    def _set_agent_idle(self, status: str = "就绪", *, success: bool = False):
        self._agent_busy = False
        self._busy_banner_base = ""
        if hasattr(self, "_agent_elapsed_timer"):
            self._agent_elapsed_timer.stop()
        if hasattr(self, "_agent_phase"):
            self._agent_phase.setText("Agent")
        self._agent_status_base = ""
        self._set_agent_status_text(status)
        if hasattr(self, "_agent_dots"):
            self._agent_dots.stop()
        elif hasattr(self, "_agent_dot") and hasattr(self._agent_dot, "setProperty"):
            self._agent_dot.setProperty("busy", False)
            self._agent_dot.setProperty("success", bool(success))
            self._agent_dot.style().unpolish(self._agent_dot)
            self._agent_dot.style().polish(self._agent_dot)
        if hasattr(self, "_progress"):
            self._progress.hide()
            self._progress.setRange(0, 0)
        # 失败/取消时收起「进行中」蓝条；成功由 _set_ok_banner 覆盖
        if not success and hasattr(self, "_gen_result_banner"):
            busy = self._gen_result_banner.property("busy")
            if busy in (True, "true"):
                self._gen_result_banner.hide()
                self._gen_result_banner.clear()
                self._gen_result_banner.setProperty("busy", False)
        for btn in (getattr(self, "_cancel_btn", None), getattr(self, "_strip_cancel_btn", None)):
            if btn is not None:
                btn.setEnabled(False)

    def _set_agent_status_text(self, text: str):
        t = (text or "").replace("\n", " ").strip()
        if getattr(self, "_agent_busy", False):
            self._agent_status_base = t
            import time
            started = float(getattr(self, "_agent_busy_started", 0) or 0)
            if started > 0:
                elapsed = self._format_elapsed(time.monotonic() - started)
                self._render_agent_status(f"{t} · {elapsed}" if t else elapsed)
                return
        self._render_agent_status(t or "…")

    def _set_agent_progress(self, pct: int):
        if not hasattr(self, "_progress"):
            return
        self._progress.show()
        if self._progress.maximum() == 0:
            self._progress.setRange(0, 100)
        self._progress.setValue(max(0, min(100, int(pct))))

    def _open_session_archive(self):
        d = None
        if getattr(self, "_archive", None) is not None:
            d = self._archive.session_dir
        if d is None or not Path(d).is_dir():
            # 打开 sessions 根目录
            try:
                from backend.script_generator.session_archive import SESSIONS_ROOT
                root = SESSIONS_ROOT
                root.mkdir(parents=True, exist_ok=True)
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(root)))
                QMessageBox.information(
                    self,
                    "会话归档",
                    "当前还没有本次会话目录。\n已打开归档根目录，生成/试跑后会写入子文件夹。",
                )
            except Exception as e:
                QMessageBox.warning(self, "无法打开归档", str(e))
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(d)))

    def _refresh_trial_chip_styles(self):
        chips = getattr(self, "_trial_chips", None) or {}
        for chip in chips.values():
            chip.style().unpolish(chip)
            chip.style().polish(chip)

    def _set_trial_chip(self, key: str, stage: str):
        chips = getattr(self, "_trial_chips", None) or {}
        chip = chips.get(key)
        if chip is None:
            return
        chip.setProperty("stage", stage)
        self._refresh_trial_chip_styles()

    def _update_trial_frame_preview(self):
        lbl = getattr(self, "_trial_frame_lbl", None)
        if lbl is None:
            return
        frame = self._last_trial_frame
        if frame is None:
            lbl.setPixmap(QPixmap())
            lbl.setText("试跑结束或停止后\n显示画面缩略图")
            return
        try:
            import numpy as np
            from PySide6.QtGui import QImage

            arr = np.asarray(frame)
            if arr.ndim != 3 or arr.shape[2] < 3:
                lbl.setText("画面格式无法预览")
                return
            # BGR → RGB
            rgb = arr[:, :, :3][:, :, ::-1].copy()
            h, w, _ = rgb.shape
            qimg = QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888)
            pix = QPixmap.fromImage(qimg.copy())
            scaled = pix.scaled(
                lbl.width() or 200,
                lbl.height() or 160,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            lbl.setText("")
            lbl.setPixmap(scaled)
            self._set_trial_chip("frame", "done")
        except Exception as e:
            lbl.setText(f"预览失败\n{e}")

    def _current_code_text(self) -> str:
        code = (self._generated_code or "").strip()
        if code:
            return self._generated_code
        return getattr(self, "_stream_buf", "") or ""

    def _view_full_code(self):
        code = self._current_code_text()
        if not code.strip():
            QMessageBox.information(self, "暂无代码", "还没有可查看的脚本，请先生成。")
            return
        dlg = FullCodeDialog(self, code=code)
        dlg.exec()

    def _apply_edited_code(self, code: str):
        """从完整代码弹窗写回当前生成结果，并同步试运行文件。"""
        text = code if code.endswith("\n") else (code + "\n" if code else "")
        self._generated_code = text
        self._stream_buf = text
        self._set_live_code(text)
        self._view_code_btn.setEnabled(bool(text.strip()))
        self._save_btn.setEnabled(bool(text.strip()))
        self._copy_btn.setEnabled(bool(text.strip()))
        try:
            if text.strip():
                self._sync_trial_code()
        except Exception as e:
            print(f"[ScriptGenerator] 写回试运行文件失败: {e}")

    def _flash_taskbar(self, _reason: str = ""):
        """完成后提醒：任务栏图标闪烁 / 底部高亮，直到用户点回窗口（不抢焦点）。"""
        win = self.window()
        if win is None:
            return
        app = QApplication.instance()
        if app is not None:
            try:
                app.alert(win, 0)
            except Exception:
                pass
        # Windows 任务栏底部色条（窗口不在前台时持续闪到用户点回来）
        try:
            import ctypes
            from ctypes import wintypes

            class FLASHWINFO(ctypes.Structure):
                _fields_ = (
                    ("cbSize", wintypes.UINT),
                    ("hwnd", wintypes.HWND),
                    ("dwFlags", wintypes.DWORD),
                    ("uCount", wintypes.UINT),
                    ("dwTimeout", wintypes.DWORD),
                )

            FLASHW_TRAY = 0x00000002
            FLASHW_TIMERNOFG = 0x0000000C
            info = FLASHWINFO()
            info.cbSize = ctypes.sizeof(FLASHWINFO)
            info.hwnd = int(win.winId())
            info.dwFlags = FLASHW_TRAY | FLASHW_TIMERNOFG
            info.uCount = 0
            info.dwTimeout = 0
            ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
        except Exception:
            pass

    def _reset_trial_surface_after_new_code(self) -> None:
        """重新生成/写入新代码后，清掉上一轮试跑的红条/日志，避免误以为仍失败。"""
        try:
            self._trial_log_lines.clear()
            if hasattr(self, "_trial_log"):
                self._trial_log.clear()
        except Exception:
            pass
        if getattr(self, "_trial_hud", None) is not None:
            try:
                self._trial_hud.reset("")
            except Exception:
                pass
        for key in ("write", "run", "frame", "feas"):
            try:
                self._set_trial_chip(key, "idle")
            except Exception:
                pass
        # 上一轮自动填的「修复本地校验」在本次已通过时清掉，避免误导再修订
        if not self._trial_blocked and hasattr(self, "_feedback"):
            fb = self._feedback.toPlainText().strip()
            if fb.startswith("修复本地校验错误"):
                self._feedback.clear()
                self._set_feedback_stale(False)
        if self._trial_blocked:
            self._append_trial_log(
                f"[脚本检查] 未通过：{self._trial_block_reason or '见上方提示'}"
            )
        else:
            self._append_trial_log("[脚本检查] 通过 · 可点「试运行」")

    def _on_success(self, code: str):
        self._generated_code = code
        self._note_script_origin(code or "", collab=False)
        self._stream_buf = code or ""
        self._set_live_code(code or "")
        self._view_code_btn.setEnabled(True)
        self._save_btn.setEnabled(True)
        self._copy_btn.setEnabled(True)
        self._revise_btn.setEnabled(True)
        self._confirm_btn.setEnabled(True)
        self._generate_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)

        n_chars = len(code or "")
        tok = ""
        if hasattr(self, "_token_label"):
            tok = (self._token_label.text() or "").strip()
        summary_bits = [f"脚本约 {n_chars:,} 字符"]
        if tok and "入" in tok:
            summary_bits.append(tok.replace("↑ ", "").strip())
        src = ""
        if getattr(self, "_source_dir", None):
            src = Path(self._source_dir).name
        if src:
            summary_bits.append(f"素材「{src}」")
        summary = " · ".join(summary_bits)

        self._sync_trial_code()
        self._reset_trial_surface_after_new_code()
        blocked = bool(self._trial_blocked)
        reason = (self._trial_block_reason or "脚本检查未通过").strip()
        if blocked:
            summary += f"\n本地校验未过，暂不可试运行：{reason}"
            self._set_agent_idle(f"已生成 · 校验未过不可试运行", success=False)
            if hasattr(self, "_live_code_hint"):
                self._live_code_hint.setText(
                    f"已生成 · {n_chars:,} 字符 · 校验未过，请修订或改代码"
                )
            self._set_warn_banner(
                f"生成完成但不可试运行　　{reason}　　可点「根据反馈修订」"
            )
            if hasattr(self, "_to_trial_btn"):
                self._to_trial_btn.setProperty("ready", False)
                self._to_trial_btn.style().unpolish(self._to_trial_btn)
                self._to_trial_btn.style().polish(self._to_trial_btn)
                self._to_trial_btn.setText("去试运行 →（先修校验）")
            # 自动带上校验错误，方便一键修订
            if hasattr(self, "_feedback") and not self._feedback.toPlainText().strip():
                errs = []
                # reason is first error; pull from last validate via re-run is heavy — use reason
                self._feedback.setPlainText(f"修复本地校验错误：{reason}")
            self._trajectory.succeed_run("生成完成（校验未过）", summary=summary)
        else:
            summary += "\n结构校验已通过。建议：保存到文件 → 去试运行验证匹配与流程。"
            self._set_agent_idle("✓ 脚本已就绪 · 可去试运行", success=True)
            if hasattr(self, "_live_code_hint"):
                self._live_code_hint.setText(f"已生成 · {n_chars:,} 字符 · 可保存或试运行")
            self._set_ok_banner(
                f"✓ 生成成功　　{summary_bits[0]}"
                + "　　下一步：点右下角「去试运行 →」"
            )
            if hasattr(self, "_to_trial_btn"):
                self._to_trial_btn.setProperty("ready", True)
                self._to_trial_btn.style().unpolish(self._to_trial_btn)
                self._to_trial_btn.style().polish(self._to_trial_btn)
                self._to_trial_btn.setText("去试运行 → 验证脚本")
            self._trajectory.succeed_run("生成成功", summary=summary)

        self._update_trial_availability()
        self._flash_taskbar("脚本已生成" if not blocked else "脚本已生成但校验未过")
        self._archive_generate_done(code)
        if getattr(self, "_vision_refresh_cb", None) and self._vision_refresh_cb.isChecked():
            self._vision_refresh_cb.setChecked(False)

    def _on_error(self, msg: str):
        translated = self._translate_error(msg)
        self._generate_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        reason = (translated.split("\n", 1)[0] or "生成失败").strip()
        self._set_agent_idle(f"生成失败 · {reason}")
        self._restore_code_preview_after_agent_fail()
        self._update_trial_availability()
        self._trajectory.fail_run(reason)
        # 失败也落盘：便于复盘半截/膨胀代码（此前只归档成功结果）
        try:
            self._archive_generate_failed(translated)
        except Exception as e:
            print(f"[ScriptGenerator] 归档生成失败信息出错: {e}")
        QMessageBox.critical(self, "生成失败", translated)

    def _archive_generate_failed(self, error_msg: str):
        code = (getattr(self, "_stream_buf", "") or self._generated_code or "").strip()
        if self._archive.session_dir is None:
            self._archive.begin("generate", reuse=False)
        if code:
            self._archive.write_text("code_failed.py", code if code.endswith("\n") else code + "\n")
            self._set_live_code(code)
            self._view_code_btn.setEnabled(True)
            self._save_btn.setEnabled(True)
            self._copy_btn.setEnabled(True)
            # 允许用户拿失败稿去试运行页手工修订
            self._generated_code = code if code.endswith("\n") else code + "\n"
        self._archive.write_text("generate_error.txt", error_msg or "")
        self._archive.write_json("trajectory.json", self._archive_trajectory())
        expl = ""
        if hasattr(self, "_explanation"):
            expl = self._explanation.toPlainText()
        if expl.strip():
            self._archive.write_text("explanation.txt", expl)
        self._archive.merge_meta({
            **self._archive_meta(),
            "last_event": "generate_failed",
            "failed_code_chars": len(code),
        })
        self._archive.append_event(
            "generate_failed",
            error=(error_msg or "")[:800],
            code_chars=len(code),
        )
        d = self._archive.session_dir
        if d:
            self._trajectory.update_step(
                "archive",
                "Info",
                "已归档失败稿",
                status="done",
                body=str(d),
            )

    def _archive_meta(self) -> dict:
        return {
            "provider": self._current_provider(),
            "model": self._model.currentText().strip(),
            "source_dir": str(self._source_dir or ""),
            "expl_path": str(self._expl_path or ""),
            "trial_account": self._trial_account_name or "",
            "tokens_in": self._last_inp_tokens,
            "tokens_out": self._last_out_tokens,
        }

    def _archive_trajectory(self) -> list[dict]:
        if hasattr(self, "_trajectory"):
            return self._trajectory.export_snapshot()
        return []

    def _try_cache_trial_frame(self):
        name = self._trial_account_name
        if not name or self._facade is None:
            return
        try:
            ctrl = self._facade.controller
            ub = ctrl._browser_instances.get(name)
            if ub is not None and getattr(ub, "_browser", None) is not None:
                frame = getattr(ub._browser, "_frame", None)
                if frame is not None:
                    self._last_trial_frame = frame.copy()
                    return
        except Exception:
            pass

    def _persist_stop_frame(self, *, name: str = "screenshot_stop.png") -> str:
        """把缓存的停帧写成 PNG，供修订诊断识图。返回路径或空串。"""
        if self._last_trial_frame is None:
            return self._stop_frame_path or ""
        try:
            if self._archive.session_dir is None:
                self._archive.begin("trial", reuse=False)
            p = self._archive.save_bgr_frame(name, self._last_trial_frame)
            if p is not None:
                self._stop_frame_path = str(p)
                return self._stop_frame_path
        except Exception as e:
            print(f"[ScriptGenerator] 持久化停帧失败: {e}")
        # 无归档时落到临时文件
        try:
            import tempfile
            import cv2
            import numpy as np

            fd, tmp = tempfile.mkstemp(prefix="ms_stop_", suffix=".png")
            import os
            os.close(fd)
            arr = np.asarray(self._last_trial_frame)
            if cv2.imencode(".png", arr)[0]:
                cv2.imwrite(tmp, arr)
                self._stop_frame_path = tmp
                return tmp
        except Exception as e:
            print(f"[ScriptGenerator] 临时停帧失败: {e}")
        return self._stop_frame_path or ""

    def _cache_stop_frame_now(self, *, also_capture: bool = True) -> str:
        """停止瞬间：优先拷当前帧，可选再请求一次截图，并落盘。"""
        self._try_cache_trial_frame()
        if also_capture and self._facade and self._trial_account_name:
            try:
                acc = self._selected_account()
                if acc is None or acc.get("name") != self._trial_account_name:
                    acc = {"name": self._trial_account_name}
                self._facade.controller.capture_screenshot(acc)
            except Exception:
                pass
        path = self._persist_stop_frame()
        if path:
            self._append_trial_log(f"[试运行] 已缓存停帧: {path}")
        return path

    def _on_screenshot_ready(self, account_name: str, frame):
        # 协作「用当前窗口」抓帧
        if getattr(self, "_collab_capture_pending", False):
            want = getattr(self, "_collab_capture_account", "") or ""
            if not want or account_name == want:
                self._collab_capture_pending = False
                self._collab_capture_account = ""
                try:
                    self._deliver_collab_window_frame(account_name, frame)
                except Exception as e:
                    print(f"[ScriptGenerator] 协作抓窗失败: {e}")
                    collab = getattr(self, "_collab", None)
                    if collab is not None:
                        collab.chat.add_bubble("agent", f"抓取窗口失败：{e}")
                return

        if account_name != self._trial_account_name:
            return
        try:
            self._last_trial_frame = frame.copy() if hasattr(frame, "copy") else frame
        except Exception:
            self._last_trial_frame = frame
        # 试跑中也刷缩略图（弱实时）；结束时再定格
        try:
            self._update_trial_frame_preview()
        except Exception:
            pass
        # 试跑结束/停止后异步截图到达时，补写停帧文件
        if not self._trial_running:
            try:
                self._persist_stop_frame()
            except Exception:
                pass

    def _deliver_collab_window_frame(self, account_name: str, frame) -> None:
        """把绑定账号客户区帧交给协作页。"""
        import cv2
        import numpy as np
        from datetime import datetime
        from core.path import PROJECT_ROOT

        arr = np.asarray(frame)
        if arr.ndim == 2:
            bgr = cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
        elif arr.shape[2] == 4:
            bgr = cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)
        else:
            bgr = arr
        out_dir = PROJECT_ROOT / "screenshots" / "collab_snips"
        out_dir.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(account_name))
        path = out_dir / f"window_{safe}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        ok, buf = cv2.imencode(".png", bgr)
        if not ok:
            raise RuntimeError("编码截图失败")
        path.write_bytes(buf.tobytes())
        collab = getattr(self, "_collab", None)
        if collab is not None and hasattr(collab, "_on_bound_window_frame"):
            collab._on_bound_window_frame(path, account_name)
        else:
            print(f"[ScriptGenerator] 协作抓窗已保存: {path}")

    def _archive_generate_done(self, code: str):
        try:
            from backend.script_generator.agent import (
                apply_codegen_patches,
                validate_script_local,
            )

            expl = self._explanation.toPlainText()
            ctx = getattr(self, "_gen_ctx", {}) or {}
            plan = ctx.get("plan_struct") or None
            free = ctx.get("free_mode")
            if not isinstance(free, bool):
                free = bool(self._free_mode_cb.isChecked())
            src = str(ctx.get("source_dir") or self._source_dir or "")
            # 与管线同参数再跑一次确定性补丁：legacy/异常路径漏补的在此兜住，
            # 归档错误 = 补丁后的真实残差，不再出现「管线过、UI 拦」。
            patched, notes = apply_codegen_patches(
                code,
                source_dir=src,
                plan=plan,
                explanation=expl,
                free_mode=free,
            )
            if patched.strip() and self._code_compiles(patched):
                if patched != code:
                    self._append_trial_log(
                        f"[归档] 生成后本地补全 {len(notes)} 项（含: "
                        + "; ".join(notes[:3]) + "）"
                    )
                code = patched
                self._generated_code = patched
                self._stream_buf = patched
            errs = validate_script_local(
                code,
                plan=plan,
                explanation=expl,
                source_dir=src,
                free_mode=free,
            )
            if errs:
                self._trial_blocked = True
                self._trial_block_reason = errs[0]
                self._append_trial_log(
                    f"[归档] 仍有 {len(errs)} 项校验残差（可点「重修订」带上错误修复）"
                )
                if not self._feedback.toPlainText().strip():
                    self._feedback.setPlainText(
                        "修复本地校验错误：\n"
                        + "\n".join(f"- {e}" for e in errs[:8])
                    )
            d = self._archive.snapshot_generate(
                explanation=expl,
                code=code,
                trajectory=self._archive_trajectory(),
                meta=self._archive_meta(),
                validation_errors=errs or None,
            )
            self._append_trial_log(f"[归档] 生成材料: {d}")
        except Exception as e:
            print(f"[ScriptGenerator] 归档生成失败: {e}")

    def _archive_trial_end(self, status: str):
        try:
            if self._archive.session_dir is None:
                self._archive.begin("trial", reuse=False)
            self._try_cache_trial_frame()
            if not self._stop_frame_path:
                self._persist_stop_frame(name="screenshot_trial_end.png")
            acc = self._selected_account()
            if acc and self._facade:
                try:
                    self._facade.controller.capture_screenshot(acc)
                except Exception:
                    pass
            d = self._archive.snapshot_trial_end(
                status=status,
                trial_log="\n".join(self._trial_log_lines),
                code=self._generated_code or "",
                feedback_draft=self._feedback.toPlainText(),
                meta=self._archive_meta(),
                frame=self._last_trial_frame,
            )
            self._append_trial_log(f"[归档] 试运行材料: {d}")
        except Exception as e:
            print(f"[ScriptGenerator] 归档试跑失败: {e}")

    def _archive_revise_start(self, feedback: str):
        try:
            if self._archive.session_dir is None:
                self._archive.begin("revise")
            self._try_cache_trial_frame()
            d = self._archive.snapshot_revise_start(
                feedback=feedback,
                code_before=self._generated_code or "",
                explanation=self._explanation.toPlainText(),
                trial_log="\n".join(self._trial_log_lines[-200:]),
                trajectory=self._archive_trajectory(),
                meta=self._archive_meta(),
                frame=self._last_trial_frame,
            )
            self._append_trial_log(f"[归档] 修订前材料: {d}")
        except Exception as e:
            print(f"[ScriptGenerator] 归档修订前失败: {e}")

    def _archive_revise_done(self, code: str, summary: str, writeback: list[str] | None = None):
        try:
            from backend.script_generator.agent import validate_script_local
            expl = self._explanation.toPlainText()
            errs = validate_script_local(
                code,
                explanation=expl,
                source_dir=str(self._source_dir or ""),
                free_mode=bool(self._free_mode_cb.isChecked()),
            )
            d = self._archive.snapshot_revise_done(
                code_after=code,
                summary=summary,
                trajectory=self._archive_trajectory(),
                meta=self._archive_meta(),
                validation_errors=errs or None,
                writeback_bullets=writeback,
            )
            self._append_trial_log(f"[归档] 修订后材料: {d}")
        except Exception as e:
            print(f"[ScriptGenerator] 归档修订后失败: {e}")
