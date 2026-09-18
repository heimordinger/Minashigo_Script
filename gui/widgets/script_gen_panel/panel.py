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

from gui.widgets.script_gen_panel.constants import (
    _DEFAULT_PROFILE, _INTRO_FILENAMES, _KEYRING_SERVICE,
    _OPTIMIZE_PAGE_SIZE, _TRIAL_DIR, _TRIAL_REL,
)
from gui.widgets.script_gen_panel.mixin_assets import AssetsMixin
from gui.widgets.script_gen_panel.mixin_collab import CollabMixin
from gui.widgets.script_gen_panel.mixin_generate import GenerateMixin
from gui.widgets.script_gen_panel.mixin_runtime import RuntimeMixin
from gui.widgets.script_gen_panel.mixin_settings import SettingsMixin
from gui.widgets.script_gen_panel.widgets import (
    FullCodeDialog, ImagePreviewDialog, _BounceDots, _NoWheelComboBox, _NoWheelSpinBox,
    _PreviewCell, _show_scroll_message,
)
from gui.widgets.script_gen_panel.workers import (
    ConnectionTestWorker, GenerateWorker, OptimizeWorker, ReviseWorker,
)


class ScriptGenerator(
    CollabMixin, SettingsMixin, AssetsMixin, GenerateMixin, RuntimeMixin, QWidget
):
    """用户配置 API、上传脚本解释和图片、生成自动化脚本。"""

    # Tab 索引（0=协作工作台；其后为经典向导）
    TAB_COLLAB = 0
    TAB_API = 1
    TAB_INPUT = 2
    TAB_GEN = 3
    TAB_TRIAL = 4
    TAB_OPTIMIZE = 5

    def __init__(self):
        super().__init__()
        self.setObjectName("ScriptGenPanel")
        self._image_entries: list[dict] = []
        self._source_dir: Path | None = None
        self._expl_path: Path | None = None
        self._expl_loading = False
        self._expl_save_timer = QTimer(self)
        self._expl_save_timer.setSingleShot(True)
        self._expl_save_timer.timeout.connect(self._autosave_explanation)
        self._generated_code: str = ""
        self._stream_buf: str = ""
        self._gen_ctx: dict = {}  # 生成管线校验上下文（plan/free/source_dir）
        self._explanation_text: str = ""
        self._worker: GenerateWorker | None = None
        self._revise_worker: ReviseWorker | None = None
        self._optimize_worker: OptimizeWorker | None = None
        self._optimize_code: str = ""
        self._optimize_script_path: Path | None = None
        self._optimize_script_files: list[Path] = []
        self._optimize_page = 0
        self._optimize_folder = ""
        self._optimize_log_lines: list[str] = []
        self._pre_optimize_trial_log: str = ""
        self._pre_optimize_record_dir: str = ""
        self._awaiting_reliability_retrial: bool = False
        self._facade = None
        self._trial_running = False
        self._trial_account_name: str = ""
        self._collab_capture_pending = False
        self._collab_capture_account: str = ""
        self._trial_log_lines: list[str] = []
        self._last_trial_frame = None
        self._stop_frame_path: str = ""
        self._last_revise_summary: str = ""
        self._last_revise_error: str = ""
        self._last_diagnosis_json: str = ""
        self._chat_session: dict | None = None
        self._last_inp_tokens = 0
        self._last_out_tokens = 0
        self._trial_blocked = False
        self._trial_block_reason = ""
        self._script_origin = ""
        self._busy_banner_base = ""
        self._agent_busy = False
        self._agent_status_base = ""
        self._agent_busy_started = 0.0
        self._holding_prev_live_code = False
        self._agent_elapsed_timer = QTimer(self)
        self._agent_elapsed_timer.setInterval(1000)
        self._agent_elapsed_timer.timeout.connect(self._tick_agent_elapsed)
        from backend.script_generator.session_archive import SessionArchive
        self._archive = SessionArchive()
        self._settings_path = Path.home() / ".minashigo" / "script_gen_config.json"
        self._settings_path.parent.mkdir(parents=True, exist_ok=True)
        self._build_ui()
        self._update_expl_path_label()

    def set_facade(self, facade):
        """注入主程序 Facade，用于账号列表与试运行。"""
        if self._facade is facade:
            self._refresh_accounts()
            self._update_trial_availability()
            return
        if self._facade is not None:
            try:
                self._facade.controller.log_signal.disconnect(self._on_trial_log)
            except Exception:
                pass
            try:
                self._facade.controller.state_event.disconnect(self._on_trial_state)
            except Exception:
                pass
        self._facade = facade
        if facade is not None:
            facade.controller.log_signal.connect(
                self._on_trial_log, type=Qt.QueuedConnection
            )
            facade.controller.state_event.connect(
                self._on_trial_state, type=Qt.QueuedConnection
            )
            try:
                facade.controller.screenshot_ready.connect(
                    self._on_screenshot_ready, type=Qt.QueuedConnection
                )
            except Exception:
                pass
        self._refresh_accounts()
        self._update_trial_availability()

    @staticmethod
    def _wrap_scroll(inner: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(inner)
        return scroll

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 8, 12, 10)
        outer.setSpacing(6)

        outer.addWidget(self._build_agent_strip())

        self._tabs = QTabWidget()
        self._tabs.setObjectName("ScriptGenTabs")
        outer.addWidget(self._tabs, 1)

        # 协作工作台（布局预览）置顶；原 1～5 为经典模式
        self._tabs.addTab(self._build_page_collab(), "协作")
        self._tabs.addTab(self._wrap_scroll(self._build_page_api()), "1. API 配置")
        self._tabs.addTab(self._wrap_scroll(self._build_page_input()), "2. 描述与素材")
        self._tabs.addTab(self._build_page_generate(), "3. 生成")
        self._tabs.addTab(self._build_page_trial(), "4. 试运行")
        self._tabs.addTab(self._wrap_scroll(self._build_page_optimize()), "5. 脚本优化")
        self._tabs.currentChanged.connect(self._on_tab_changed)
        self._tabs.setCurrentIndex(0)

        # 提供商列表要在控件建完后初始化（先屏蔽信号，避免 addItems 触发保存冲掉已有配置）
        self._providers_config = self._load_providers_config()
        self._fill_provider_combo(self._provider)
        self._provider.currentIndexChanged.connect(self._on_provider_changed)
        self._refresh_models()
        self._fill_provider_combo(self._vision_provider, vision_only=True)
        self._vision_provider.currentIndexChanged.connect(self._on_vision_provider_changed)
        self._refresh_vision_models()
        self._load_settings()
        self._apply_provider_hint()
        self._update_trial_availability()
        self._set_agent_idle("就绪 · 协作页可预览布局；经典生成见右侧 Tab")

    def _build_agent_strip(self) -> QWidget:
        """跨 Tab 常驻：当前阶段 / 进度 / token / 取消 / 归档。"""
        strip = QFrame()
        strip.setObjectName("AgentRunStrip")
        strip.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        lay = QHBoxLayout(strip)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(10)

        self._agent_dots = _BounceDots()
        self._agent_dot = self._agent_dots  # 兼容旧属性名（busy/success 走 start/stop）
        lay.addWidget(self._agent_dots)

        self._agent_phase = QLabel("Agent")
        pf = QFont()
        pf.setBold(True)
        pf.setPointSize(11)
        self._agent_phase.setFont(pf)
        self._agent_phase.setObjectName("AgentRunPhase")
        lay.addWidget(self._agent_phase)

        self._agent_status = QLabel("就绪")
        self._agent_status.setObjectName("MutedLabel")
        self._agent_status.setWordWrap(False)
        lay.addWidget(self._agent_status, 1)

        self._progress = QProgressBar()
        self._progress.setObjectName("ScriptGenProgress")
        self._progress.setRange(0, 0)
        self._progress.setTextVisible(False)
        self._progress.setFixedHeight(6)
        self._progress.setMinimumWidth(100)
        self._progress.setMaximumWidth(180)
        self._progress.hide()
        lay.addWidget(self._progress)

        self._token_label = QLabel("")
        self._token_label.setObjectName("MutedLabel")
        lay.addWidget(self._token_label)

        # 全局账号绑定（协作截窗 / 试跑共用）
        acc_lbl = QLabel("账号")
        acc_lbl.setObjectName("MutedLabel")
        lay.addWidget(acc_lbl)
        self._global_account_combo = _NoWheelComboBox()
        self._global_account_combo.setMinimumWidth(160)
        self._global_account_combo.setMaximumWidth(260)
        self._global_account_combo.setToolTip(
            "全局绑定：已启动浏览器或已绑窗口的账号。\n"
            "协作页「用当前窗口」与试运行共用此选择。"
        )
        self._global_account_combo.currentIndexChanged.connect(
            self._on_global_account_changed
        )
        lay.addWidget(self._global_account_combo)
        self._global_refresh_acc_btn = QPushButton("刷新")
        self._global_refresh_acc_btn.setObjectName("GhostButton")
        self._global_refresh_acc_btn.setFixedHeight(26)
        self._global_refresh_acc_btn.setToolTip("刷新已启动账号列表")
        self._global_refresh_acc_btn.clicked.connect(self._refresh_accounts)
        lay.addWidget(self._global_refresh_acc_btn)

        self._strip_cancel_btn = QPushButton("取消")
        self._strip_cancel_btn.setObjectName("GhostButton")
        self._strip_cancel_btn.setEnabled(False)
        self._strip_cancel_btn.setFixedHeight(26)
        self._strip_cancel_btn.clicked.connect(self._cancel_generate)
        lay.addWidget(self._strip_cancel_btn)

        self._archive_btn = QPushButton("打开归档")
        self._archive_btn.setObjectName("GhostButton")
        self._archive_btn.setToolTip("打开本次会话材料目录（代码 / 轨迹 / 日志）")
        self._archive_btn.setFixedHeight(26)
        self._archive_btn.clicked.connect(self._open_session_archive)
        lay.addWidget(self._archive_btn)

        self._goto_traj_btn = QPushButton("轨迹")
        self._goto_traj_btn.setObjectName("GhostButton")
        self._goto_traj_btn.setToolTip("跳到生成页查看 Agent 轨迹与实时代码")
        self._goto_traj_btn.setFixedHeight(26)
        self._goto_traj_btn.clicked.connect(lambda: self._tabs.setCurrentIndex(self.TAB_GEN))
        lay.addWidget(self._goto_traj_btn)

        self._agent_busy = False
        return strip

    def _build_page_api(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(8)

        # ===== 多套配置方案 =====
        profile_group = QGroupBox("配置方案")
        g_prof = QVBoxLayout(profile_group)
        _pl = QLabel(
            "一套方案 = 主模型 + 辅助识图（含各自 Key）。"
            "「默认」是空草稿：填了但没点保存时，也会写进「默认」。"
            "正式组合请用「另存为…」（如 DeepSeek+千问）。"
        )
        _pl.setObjectName("MutedLabel")
        _pl.setWordWrap(True)
        g_prof.addWidget(_pl)
        prow = QHBoxLayout()
        self._profile_combo = _NoWheelComboBox()
        self._profile_combo.setMinimumWidth(180)
        self._profile_combo.setToolTip(
            "当前启用的 API 配置方案。\n"
            "再次点选同一方案可丢弃未保存修改并恢复已保存内容。"
        )
        # activated：即使选同一项也会触发，便于「重选当前方案 → 恢复已保存」
        self._profile_combo.activated.connect(self._on_profile_activated)
        prow.addWidget(QLabel("当前方案:"), 0)
        prow.addWidget(self._profile_combo, 1)
        self._profile_save_btn = QPushButton("保存到当前")
        self._profile_save_btn.setToolTip("把下方填写的主模型/辅助识图保存进当前方案")
        self._profile_save_btn.clicked.connect(self._save_settings)
        prow.addWidget(self._profile_save_btn)
        self._profile_save_as_btn = QPushButton("另存为…")
        self._profile_save_as_btn.setToolTip("复制当前填写内容为新方案并切换过去")
        self._profile_save_as_btn.clicked.connect(self._save_profile_as)
        prow.addWidget(self._profile_save_as_btn)
        self._profile_del_btn = QPushButton("删除")
        self._profile_del_btn.setToolTip("删除当前方案（至少保留一套）")
        self._profile_del_btn.clicked.connect(self._delete_current_profile)
        prow.addWidget(self._profile_del_btn)
        g_prof.addLayout(prow)
        layout.addWidget(profile_group)
        self._profile_switching = False

        api_group = QGroupBox("API 设置（主模型）")
        g_api = QVBoxLayout(api_group)
        _al = QLabel(
            "在此填写主生成/修订用的 AI 账号。填好后建议先点「连接测试」，"
            "通过后再写脚本描述并生成代码。"
        )
        _al.setObjectName("MutedLabel")
        _al.setWordWrap(True)
        g_api.addWidget(_al)
        f = QFormLayout()
        g_api.addLayout(f)

        self._endpoint = QLineEdit()
        self._endpoint.setPlaceholderText("留空则用该提供商官方地址；中转站请填写")
        self._endpoint.setToolTip(
            "API 服务器地址。切换提供商时会填入官方默认地址。\n"
            "使用中转站 / 代理时再改成你的地址（通常以 https:// 开头）。"
        )
        f.addRow("自定义端点:", self._endpoint)

        self._provider = _NoWheelComboBox()
        self._provider.setMaxVisibleItems(24)
        self._provider.setToolTip(
            "选择 AI 服务商。必须与 API Key 来源一致。\n"
            "国内常见：DeepSeek / 通义千问 / Kimi / 智谱 / 豆包；\n"
            "聚合：OpenRouter、硅基流动；本地：Ollama、LM Studio。"
        )
        f.addRow("提供商:", self._provider)

        self._api_key = QLineEdit()
        self._api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_key.setPlaceholderText("在官网申请的密钥，粘贴到这里")
        self._api_key.setToolTip(
            "在对应提供商官网申请的访问密钥（一串以 sk- 等开头的字符）。\n"
            "相当于登录密码，请勿泄露。按方案分别保存在系统凭据里。"
        )
        f.addRow("API Key:", self._api_key)

        self._model = _NoWheelComboBox()
        self._model.setEditable(True)
        self._model.setToolTip(
            "具体使用哪一个 AI 模型。可从列表选择，也可手动输入模型名。\n"
            "不同提供商的模型名不能混用。"
        )
        f.addRow("模型:", self._model)

        self._max_tokens = _NoWheelSpinBox()
        self._max_tokens.setRange(0, 128000)
        self._max_tokens.setSingleStep(1024)
        self._max_tokens.setSpecialValueText("无上限")
        self._max_tokens.setValue(self._default_max_tokens())
        self._max_tokens.setToolTip(
            "单次生成允许的最大输出长度（max_tokens）。\n"
            "设为 0（显示「无上限」）表示不限制输出长度。\n"
            "脚本较长时可调高（如 16384～32768）；\n"
            "DeepSeek 若正文被截断为空，也可适当调高后重试。\n"
            "数值越大通常越慢、费用越高。"
        )
        f.addRow("最大输出 tokens:", self._max_tokens)

        self._provider_hint = QLabel()
        self._provider_hint.setStyleSheet("color:#e8a000;font-size:11px;")
        self._provider_hint.setVisible(False)
        self._provider_hint.setWordWrap(True)
        f.addRow(self._provider_hint)

        btn_row = QHBoxLayout()
        self._test_btn = QPushButton("连接测试")
        self._test_btn.setToolTip("用当前提供商 / 模型 / Key 发送一条短消息验证连通性")
        self._test_btn.clicked.connect(self._on_test_connection)
        btn_row.addWidget(self._test_btn)
        btn_row.addStretch()
        next_btn = QPushButton("下一步：描述与素材 →")
        next_btn.setObjectName("PrimaryButton")
        next_btn.clicked.connect(lambda: self._tabs.setCurrentIndex(self.TAB_INPUT))
        btn_row.addWidget(next_btn)
        g_api.addLayout(btn_row)

        self._test_status = QLabel("")
        self._test_status.setWordWrap(True)
        self._test_status.setStyleSheet("color:#888;font-size:11px;")
        g_api.addWidget(self._test_status)
        self._test_worker: ConnectionTestWorker | None = None

        layout.addWidget(api_group)

        # ===== 辅助识图 API =====
        vision_group = QGroupBox("辅助识图（可选，随方案一起切换）")
        g_vis = QVBoxLayout(vision_group)
        _vl = QLabel(
            "主模型若不支持看图，可在此单独配置识图模型。"
            "「经典」生成选「辅助识图」时先识图再写代码；"
            "「协作」在主模型看不了图且右侧有画面时，会自动用此处模型描述截图再交给主模型。"
            "另外：主模型（如 DeepSeek）无修订 tools 时，若此处填了千问等 Key，"
            "会自动用「qwen3.5-flash」代查函数/日志，再交给主模型写补丁。"
        )
        _vl.setObjectName("MutedLabel")
        _vl.setWordWrap(True)
        g_vis.addWidget(_vl)
        vf = QFormLayout()
        g_vis.addLayout(vf)

        self._vision_endpoint = QLineEdit()
        self._vision_endpoint.setPlaceholderText("留空则用该提供商官方地址；中转站请填写")
        self._vision_endpoint.setToolTip("识图模型的自定义 API 端点。切换提供商时会填入官方默认地址。")
        vf.addRow("自定义端点:", self._vision_endpoint)

        self._vision_provider = _NoWheelComboBox()
        self._vision_provider.setMaxVisibleItems(24)
        self._vision_provider.setToolTip(
            "识图用的提供商，请选支持图片输入的模型（Claude / GPT-4o / Qwen-VL / GLM-4V 等）。"
        )
        vf.addRow("提供商:", self._vision_provider)

        self._vision_api_key = QLineEdit()
        self._vision_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._vision_api_key.setPlaceholderText("识图专用 Key；可与主模型相同")
        self._vision_api_key.setToolTip("识图模型的 API Key，可与上方主模型共用同一把。")
        vf.addRow("API Key:", self._vision_api_key)

        self._vision_model = _NoWheelComboBox()
        self._vision_model.setEditable(True)
        self._vision_model.setToolTip(
            "必须选能看图的模型，例如 qwen-vl-max、qwen-vl-plus、gpt-4o、claude。"
            "不要填 qwen3.5-flash / deepseek 等纯文本（会「完成」但声称看不到图）。"
        )
        vf.addRow("模型:", self._vision_model)

        vis_btn_row = QHBoxLayout()
        self._vision_test_btn = QPushButton("识图连接测试")
        self._vision_test_btn.setToolTip("用当前识图提供商 / 模型 / Key 做连通性测试（纯文本探测）")
        self._vision_test_btn.clicked.connect(self._on_test_vision_connection)
        vis_btn_row.addWidget(self._vision_test_btn)
        vis_btn_row.addStretch()
        g_vis.addLayout(vis_btn_row)

        self._vision_test_status = QLabel("")
        self._vision_test_status.setWordWrap(True)
        self._vision_test_status.setStyleSheet("color:#888;font-size:11px;")
        g_vis.addWidget(self._vision_test_status)
        self._vision_test_worker: ConnectionTestWorker | None = None

        layout.addWidget(vision_group)
        layout.addStretch()
        return page

    def _build_page_input(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(8)

        # ===== 脚本描述 =====
        desc_group = QGroupBox("脚本描述")
        dl = QVBoxLayout(desc_group)
        _dl = QLabel("描述脚本的功能和流程逻辑，AI 将据此生成代码")
        _dl.setObjectName("MutedLabel")
        dl.addWidget(_dl)

        h = QHBoxLayout()
        self._load_expl_btn = QPushButton("加载介绍 txt")
        self._load_expl_btn.clicked.connect(self._load_explanation)
        h.addWidget(self._load_expl_btn)
        self._save_expl_btn = QPushButton("保存到文件")
        self._save_expl_btn.setToolTip("把当前描述（含试运行反馈）写回绑定的介绍 txt")
        self._save_expl_btn.clicked.connect(self._save_explanation_clicked)
        h.addWidget(self._save_expl_btn)
        h.addStretch()
        dl.addLayout(h)

        self._expl_path_label = QLabel("未绑定文件：修改只留在本窗口，关闭即丢失")
        self._expl_path_label.setObjectName("MutedLabel")
        self._expl_path_label.setWordWrap(True)
        dl.addWidget(self._expl_path_label)

        self._explanation = QTextEdit()
        self._explanation.setPlaceholderText("粘贴脚本解释内容，或点击上方加载 .txt 文件")
        self._explanation.setMinimumHeight(140)
        self._explanation.textChanged.connect(self._on_explanation_edited)
        dl.addWidget(self._explanation, 1)

        layout.addWidget(desc_group, 1)

        # ===== 图片管理 =====
        img_group = QGroupBox("参考图片")
        il = QVBoxLayout(img_group)
        _il = QLabel("选择图片所在文件夹，生成脚本时自动引用此路径")
        _il.setObjectName("MutedLabel")
        il.addWidget(_il)

        btn_row = QHBoxLayout()
        self._add_folder_btn = QPushButton("选择图片文件夹")
        self._add_folder_btn.clicked.connect(self._add_folder)
        btn_row.addWidget(self._add_folder_btn)

        self._preview_all_btn = QPushButton("预览全部")
        self._preview_all_btn.clicked.connect(self._preview_all)
        btn_row.addWidget(self._preview_all_btn)

        self._clear_img_btn = QPushButton("清空")
        self._clear_img_btn.clicked.connect(self._clear_images)
        btn_row.addWidget(self._clear_img_btn)
        btn_row.addStretch()
        il.addLayout(btn_row)

        self._img_label = QLabel("未选择图片文件夹")
        self._img_label.setObjectName("MutedLabel")
        il.addWidget(self._img_label)

        opt_row = QHBoxLayout()
        self._send_img_cb = QCheckBox("发送图片给 AI")
        self._send_img_cb.setChecked(True)
        self._send_img_cb.toggled.connect(self._on_send_img_toggled)
        opt_row.addWidget(self._send_img_cb)
        self._compress_img_cb = QCheckBox("压缩图片（省 token）")
        self._compress_img_cb.setChecked(False)
        self._compress_img_cb.setEnabled(True)
        opt_row.addWidget(self._compress_img_cb)
        self._free_mode_cb = QCheckBox("自由模式（少约束）")
        self._free_mode_cb.setToolTip(
            "生成端放宽：关闭 Rules / plan / IR；仍注入结构范式 few-shot。\n"
            "生成时不校验素材文件是否存在；写入试运行文件时自动脚本检查（含素材）。\n"
            "成品结构校验加严；失败可自动修复（默认最多 3 轮）。\n"
            "生成/修订仍会运行结构类本地 patch（不注入业务控制流）。\n"
            "勾选会写入 config.json defaults.codegen_free_mode。"
        )
        self._free_mode_cb.setChecked(self._default_codegen_free_mode())
        self._free_mode_cb.toggled.connect(self._on_free_mode_toggled)
        opt_row.addWidget(self._free_mode_cb)
        opt_row.addStretch()
        il.addLayout(opt_row)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("识图方式:"))
        self._image_mode = _NoWheelComboBox()
        self._image_mode.addItem("主模型直接发图", "direct")
        self._image_mode.addItem("辅助识图（先描述再生成）", "assist")
        self._image_mode.setToolTip(
            "主模型直接发图：把图片发给上方主模型（需主模型支持识图）。\n"
            "辅助识图：先用 API 页配置的识图模型描述图片，再把文字交给主模型写代码。"
        )
        mode_row.addWidget(self._image_mode, 1)
        il.addLayout(mode_row)

        vision_refresh_row = QHBoxLayout()
        self._vision_refresh_cb = QCheckBox("本次重新识图（忽略缓存）")
        self._vision_refresh_cb.setToolTip(
            "勾选后，本次生成会重新调用识图 API，不使用 .vision_cache 与识图目录.txt。\n"
            "脚本介绍仍会作为上下文发给识图模型；完成后会覆盖缓存与识图目录。\n"
            "仅对「辅助识图」模式生效。"
        )
        self._vision_refresh_cb.setEnabled(False)
        vision_refresh_row.addWidget(self._vision_refresh_cb)
        self._clear_vision_cache_btn = QPushButton("清除识图缓存")
        self._clear_vision_cache_btn.setToolTip(
            "删除当前图片文件夹下的 .vision_cache 与识图目录.txt，不影响脚本介绍。"
        )
        self._clear_vision_cache_btn.clicked.connect(self._on_clear_vision_cache)
        vision_refresh_row.addWidget(self._clear_vision_cache_btn)
        vision_refresh_row.addStretch()
        il.addLayout(vision_refresh_row)
        self._image_mode.currentIndexChanged.connect(self._on_image_mode_changed)

        layout.addWidget(img_group)

        # ===== 输出设置 =====
        out_group = QGroupBox("输出（确认完成时的正式保存位置）")
        og = QVBoxLayout(out_group)
        _ol = QLabel("试运行用临时文件；此处仅用于最终「确认完成并保存」")
        _ol.setObjectName("MutedLabel")
        og.addWidget(_ol)
        ol = QHBoxLayout()
        ol.addWidget(QLabel("目录:"))
        self._output_dir = QLineEdit(str(SCRIPTS_PATH))
        self._output_dir.setReadOnly(True)
        ol.addWidget(self._output_dir, 1)
        self._output_dir_btn = QPushButton("选择目录")
        self._output_dir_btn.clicked.connect(self._browse_output)
        ol.addWidget(self._output_dir_btn)
        ol.addWidget(QLabel("文件名:"))
        self._script_name = QLineEdit()
        self._script_name.setPlaceholderText("my_script.py")
        self._script_name.setFixedWidth(200)
        ol.addWidget(self._script_name)
        og.addLayout(ol)
        layout.addWidget(out_group)

        nav = QHBoxLayout()
        back_btn = QPushButton("← 上一步")
        back_btn.clicked.connect(lambda: self._tabs.setCurrentIndex(self.TAB_API))
        nav.addWidget(back_btn)
        nav.addStretch()
        next_btn = QPushButton("下一步：生成 →")
        next_btn.setObjectName("PrimaryButton")
        next_btn.clicked.connect(lambda: self._tabs.setCurrentIndex(self.TAB_GEN))
        nav.addWidget(next_btn)
        layout.addLayout(nav)
        return page

    def _build_page_generate(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 4, 0)
        layout.setSpacing(8)

        gen_row = QHBoxLayout()
        self._generate_btn = QPushButton("生成脚本")
        self._generate_btn.setObjectName("PrimaryButton")
        self._generate_btn.clicked.connect(self._on_generate)
        gen_row.addWidget(self._generate_btn)

        self._cancel_btn = QPushButton("取消")
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.clicked.connect(self._cancel_generate)
        gen_row.addWidget(self._cancel_btn)

        self._rerevise_btn = QPushButton("重修订")
        self._rerevise_btn.setToolTip(
            "用试运行页的反馈对当前代码再次修订（不重新生成）。\n"
            "硬校验未通过时可用此按钮补修；无反馈时会自动带上校验错误说明。"
        )
        self._rerevise_btn.setEnabled(False)
        self._rerevise_btn.clicked.connect(self._on_rerevise)
        gen_row.addWidget(self._rerevise_btn)
        gen_row.addStretch(1)
        layout.addLayout(gen_row)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setObjectName("AgentWorkspaceSplit")
        split.setChildrenCollapsible(False)
        split.setHandleWidth(6)

        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(4)
        self._gen_result_banner = QLabel("")
        self._gen_result_banner.setObjectName("GenResultBanner")
        self._gen_result_banner.setWordWrap(True)
        self._gen_result_banner.hide()
        left_lay.addWidget(self._gen_result_banner)
        self._trajectory = GenTrajectory()
        left_lay.addWidget(self._trajectory, 1)
        split.addWidget(left)

        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.setSpacing(4)
        code_head = QHBoxLayout()
        code_title = QLabel("实时代码")
        ctf = QFont()
        ctf.setBold(True)
        code_title.setFont(ctf)
        code_head.addWidget(code_title)
        self._live_code_hint = QLabel("流式输出会显示在这里")
        self._live_code_hint.setObjectName("MutedLabel")
        code_head.addWidget(self._live_code_hint, 1)
        right_lay.addLayout(code_head)

        self._live_code = QTextEdit()
        self._live_code.setObjectName("AgentLiveCode")
        self._live_code.setReadOnly(True)
        self._live_code.setFont(QFont("Consolas", 9))
        self._live_code.setPlaceholderText(
            "生成时这里会实时滚动代码。\n"
            "修订 / 优化等待阶段会暂显上一版，开始写代码后会替换为新内容。"
        )
        self._live_code.setMinimumWidth(280)
        right_lay.addWidget(self._live_code, 1)
        split.addWidget(right)

        split.setStretchFactor(0, 45)
        split.setStretchFactor(1, 55)
        split.setSizes([360, 440])
        layout.addWidget(split, 1)

        self._live_code_timer = QTimer(self)
        self._live_code_timer.setSingleShot(True)
        self._live_code_timer.timeout.connect(self._flush_live_code)

        act_row = QHBoxLayout()
        self._view_code_btn = QPushButton("查看完整代码")
        self._view_code_btn.setToolTip(
            "查看完整脚本（默认锁定只读，可解锁编辑；Ctrl+F 搜索）"
        )
        self._view_code_btn.setEnabled(False)
        self._view_code_btn.clicked.connect(self._view_full_code)
        act_row.addWidget(self._view_code_btn)

        self._save_btn = QPushButton("保存到文件")
        self._save_btn.clicked.connect(self._save_script)
        self._save_btn.setEnabled(False)
        act_row.addWidget(self._save_btn)

        self._copy_btn = QPushButton("复制代码")
        self._copy_btn.clicked.connect(self._copy_code)
        self._copy_btn.setEnabled(False)
        act_row.addWidget(self._copy_btn)
        act_row.addStretch()
        to_trial = QPushButton("去试运行 →")
        to_trial.setObjectName("PrimaryButton")
        to_trial.setToolTip("生成已完成时可点此进入试运行验证脚本")
        to_trial.clicked.connect(lambda: self._tabs.setCurrentIndex(self.TAB_TRIAL))
        self._to_trial_btn = to_trial
        act_row.addWidget(to_trial)
        layout.addLayout(act_row)
        return page

    def _build_page_trial(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 4, 0)
        layout.setSpacing(8)

        tip = QLabel(
            "从已启动账号里选一个试跑。生成后请对齐 _img 标识与素材文件名；"
            "写入试运行文件时会自动做脚本检查（结构 + 素材）。"
        )
        tip.setWordWrap(True)
        tip.setObjectName("MutedLabel")
        layout.addWidget(tip)

        self._trial_hint = QLabel("")
        self._trial_hint.setWordWrap(True)
        self._trial_hint.setObjectName("MutedLabel")
        self._trial_hint.setStyleSheet("color:#b8891a;")
        layout.addWidget(self._trial_hint)

        # 试跑阶段芯片
        chip_row = QHBoxLayout()
        chip_row.setSpacing(6)
        self._trial_chips: dict[str, QLabel] = {}
        for key, label in (
            ("write", "写入"),
            ("run", "运行"),
            ("frame", "停帧"),
            ("feas", "可行性"),
        ):
            chip = QLabel(label)
            chip.setObjectName("TrialStageChip")
            chip.setProperty("stage", "idle")
            chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
            chip.setFixedHeight(22)
            chip.setMinimumWidth(52)
            self._trial_chips[key] = chip
            chip_row.addWidget(chip)
        chip_row.addStretch(1)
        layout.addLayout(chip_row)
        self._refresh_trial_chip_styles()

        acc_row = QHBoxLayout()
        acc_row.addWidget(QLabel("账号:"))
        self._account_combo = _NoWheelComboBox()
        self._account_combo.setMinimumWidth(180)
        self._account_combo.setToolTip("与顶栏全局账号同步；仅列出已启动浏览器或已绑定窗口的账号")
        self._account_combo.currentIndexChanged.connect(self._on_trial_account_changed)
        acc_row.addWidget(self._account_combo, 1)
        self._refresh_acc_btn = QPushButton("刷新账号")
        self._refresh_acc_btn.clicked.connect(self._refresh_accounts)
        acc_row.addWidget(self._refresh_acc_btn)
        self._trial_btn = QPushButton("试运行")
        self._trial_btn.setObjectName("PrimaryButton")
        self._trial_btn.setEnabled(False)
        self._trial_btn.clicked.connect(self._on_trial_run)
        acc_row.addWidget(self._trial_btn)
        self._stop_trial_btn = QPushButton("停止")
        self._stop_trial_btn.setObjectName("DangerButton")
        self._stop_trial_btn.setEnabled(False)
        self._stop_trial_btn.clicked.connect(self._on_stop_trial)
        acc_row.addWidget(self._stop_trial_btn)
        layout.addLayout(acc_row)

        # 试运行 Live HUD：状态点 / 当前状态 / 最近动作 / 计时 / L1 结论
        self._trial_hud = TrialHud()
        layout.addWidget(self._trial_hud)

        mid = QSplitter(Qt.Orientation.Horizontal)
        mid.setChildrenCollapsible(False)
        mid.setHandleWidth(6)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(6)

        log_panel = QWidget()
        log_lay = QVBoxLayout(log_panel)
        log_lay.setContentsMargins(0, 0, 0, 0)
        log_lay.setSpacing(4)
        log_lay.addWidget(QLabel("试运行日志:"))
        self._trial_log = QTextEdit()
        self._trial_log.setReadOnly(True)
        self._trial_log.setFont(QFont("Consolas", 9))
        self._trial_log.setPlaceholderText("试运行时的脚本日志会出现在这里…")
        self._trial_log.setMinimumHeight(80)
        log_lay.addWidget(self._trial_log, 1)

        fb_panel = QWidget()
        fb_lay = QVBoxLayout(fb_panel)
        fb_lay.setContentsMargins(0, 0, 0, 0)
        fb_lay.setSpacing(4)
        fb_head = QHBoxLayout()
        self._feedback_title = QLabel("反馈（试运行问题描述）:")
        fb_head.addWidget(self._feedback_title)
        self._feedback_stale_hint = QLabel("上次反馈 · 可改写或清空后再修订")
        self._feedback_stale_hint.setObjectName("MutedLabel")
        self._feedback_stale_hint.setStyleSheet("color:#b8891a;")
        self._feedback_stale_hint.hide()
        fb_head.addWidget(self._feedback_stale_hint, 1)
        fb_lay.addLayout(fb_head)
        self._feedback = QTextEdit()
        self._feedback.setObjectName("ScriptGenFeedback")
        self._feedback.setAcceptRichText(False)
        self._feedback.setPlaceholderText(
            "脚本来自协作时，这里的文字会直接发到协作对话。\n"
            "经典生成仍会把本框反馈 + 下方试运行日志（最近约 200 行）一并发给 AI。\n"
            "建议一条一行、写清现象与期望，例如：\n"
            "1. 卡在主界面，日志里一直 match home，但从不点出击\n"
            "2. 点了竞技场入口却进了爬塔，入口点错了\n"
            "3. 识别到 logo 后不要立刻退出，应先领完奖励\n"
            "4. 某某状态超时太短，请改成 60 秒并补 script_log\n"
            "可写：卡在哪一屏 / 点了什么 / 实际去哪 / 日志关键词 / 期望行为"
        )
        self._feedback.setMinimumHeight(60)
        self._feedback.textChanged.connect(self._on_feedback_edited)
        fb_lay.addWidget(self._feedback, 1)
        self._feedback_stale = False
        self._set_feedback_stale(False)

        splitter.addWidget(log_panel)
        splitter.addWidget(fb_panel)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([240, 160])
        mid.addWidget(splitter)

        frame_panel = QWidget()
        frame_panel.setMinimumWidth(160)
        frame_panel.setMaximumWidth(260)
        fl = QVBoxLayout(frame_panel)
        fl.setContentsMargins(4, 0, 0, 0)
        fl.setSpacing(4)
        fl.addWidget(QLabel("Agent 所见（停帧）"))
        self._trial_frame_lbl = QLabel("试跑结束或停止后\n显示画面缩略图")
        self._trial_frame_lbl.setObjectName("TrialFramePreview")
        self._trial_frame_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._trial_frame_lbl.setMinimumHeight(120)
        self._trial_frame_lbl.setWordWrap(True)
        self._trial_frame_lbl.setScaledContents(False)
        fl.addWidget(self._trial_frame_lbl, 1)
        mid.addWidget(frame_panel)
        mid.setStretchFactor(0, 4)
        mid.setStretchFactor(1, 1)
        mid.setSizes([520, 180])
        layout.addWidget(mid, 1)

        rev_row = QHBoxLayout()
        back_btn = QPushButton("← 回生成页")
        back_btn.clicked.connect(lambda: self._tabs.setCurrentIndex(self.TAB_GEN))
        rev_row.addWidget(back_btn)
        rev_row.addStretch()
        self._revise_btn = QPushButton("根据反馈修订")
        self._revise_btn.setObjectName("PrimaryButton")
        self._revise_btn.setEnabled(False)
        self._revise_btn.clicked.connect(self._on_revise)
        rev_row.addWidget(self._revise_btn)
        self._confirm_btn = QPushButton("确认完成并保存")
        self._confirm_btn.setObjectName("PrimaryButton")
        self._confirm_btn.setEnabled(False)
        self._confirm_btn.clicked.connect(self._on_confirm_done)
        rev_row.addWidget(self._confirm_btn)
        to_opt = QPushButton("去脚本优化 →")
        to_opt.clicked.connect(lambda: self._tabs.setCurrentIndex(self.TAB_OPTIMIZE))
        rev_row.addWidget(to_opt)
        layout.addLayout(rev_row)
        return page

    def _build_page_optimize(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 4, 0)
        layout.setSpacing(6)

        tip = QLabel(
            "选脚本 → 写优化方向/反馈 → 开始优化。"
            "账号与试跑均为可选；改完后再去「试运行」验证。"
            "试运行会自动开伪录制；确认保存时再去掉。"
        )
        tip.setWordWrap(True)
        tip.setObjectName("MutedLabel")
        tip.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        layout.addWidget(tip)

        self._optimize_hint = QLabel("")
        self._optimize_hint.setWordWrap(True)
        self._optimize_hint.setObjectName("MutedLabel")
        self._optimize_hint.setStyleSheet("color:#b8891a;")
        self._optimize_hint.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
        )
        layout.addWidget(self._optimize_hint)

        browse = QGroupBox("脚本库（分页）")
        browse.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        bl = QVBoxLayout(browse)
        bl.setSpacing(4)
        filt = QHBoxLayout()
        filt.addWidget(QLabel("目录:"))
        self._optimize_folder_combo = _NoWheelComboBox()
        self._optimize_folder_combo.setMinimumWidth(140)
        self._optimize_folder_combo.currentIndexChanged.connect(self._on_optimize_folder_changed)
        filt.addWidget(self._optimize_folder_combo)
        filt.addWidget(QLabel("搜索:"))
        self._optimize_search = QLineEdit()
        self._optimize_search.setPlaceholderText("文件名或路径关键词…")
        self._optimize_search.returnPressed.connect(self._refresh_optimize_script_list)
        filt.addWidget(self._optimize_search, 1)
        refresh_scripts = QPushButton("刷新列表")
        refresh_scripts.clicked.connect(self._refresh_optimize_script_list)
        filt.addWidget(refresh_scripts)
        bl.addLayout(filt)

        self._optimize_script_list = QListWidget()
        self._optimize_script_list.setMinimumHeight(48)
        self._optimize_script_list.setMaximumHeight(120)
        self._optimize_script_list.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self._optimize_script_list.itemClicked.connect(self._on_optimize_script_activated)
        bl.addWidget(self._optimize_script_list, 0)

        pager = QHBoxLayout()
        self._optimize_prev_btn = QPushButton("上一页")
        self._optimize_prev_btn.clicked.connect(lambda: self._optimize_change_page(-1))
        pager.addWidget(self._optimize_prev_btn)
        self._optimize_page_label = QLabel("第 1/1 页")
        self._optimize_page_label.setObjectName("MutedLabel")
        pager.addWidget(self._optimize_page_label)
        self._optimize_next_btn = QPushButton("下一页")
        self._optimize_next_btn.clicked.connect(lambda: self._optimize_change_page(1))
        pager.addWidget(self._optimize_next_btn)
        pager.addStretch()
        load_btn = QPushButton("加载选中")
        load_btn.clicked.connect(self._on_optimize_load_selected)
        pager.addWidget(load_btn)
        pick_btn = QPushButton("浏览…")
        pick_btn.clicked.connect(self._on_optimize_pick_file)
        pager.addWidget(pick_btn)
        bl.addLayout(pager)
        layout.addWidget(browse)

        loaded_row = QHBoxLayout()
        loaded_row.addWidget(QLabel("当前脚本:"))
        self._optimize_path_label = QLabel("（未加载）")
        self._optimize_path_label.setObjectName("MutedLabel")
        self._optimize_path_label.setWordWrap(False)
        self._optimize_path_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        loaded_row.addWidget(self._optimize_path_label, 1)
        self._optimize_save_btn = QPushButton("保存到文件")
        self._optimize_save_btn.setEnabled(False)
        self._optimize_save_btn.clicked.connect(self._on_optimize_save)
        loaded_row.addWidget(self._optimize_save_btn)
        layout.addLayout(loaded_row)

        # 反馈 + 日志可拖拽分配高度，避免固定最小高度把窗口顶死
        split = QSplitter(Qt.Orientation.Vertical)
        split.setChildrenCollapsible(True)

        fb_box = QGroupBox("优化方向 / 反馈（可选）")
        fb_lay = QVBoxLayout(fb_box)
        fb_lay.setContentsMargins(6, 6, 6, 6)
        self._optimize_feedback = QTextEdit()
        self._optimize_feedback.setAcceptRichText(False)
        self._optimize_feedback.setPlaceholderText(
            "写希望怎么改，例如：登录等待过长、某状态匹配太勤、减少黑屏空转匹配…\n"
            "留空则按伪录制/通用性能规则优化。"
        )
        self._optimize_feedback.setMinimumHeight(40)
        self._optimize_feedback.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        fb_lay.addWidget(self._optimize_feedback)
        split.addWidget(fb_box)

        log_box = QGroupBox("优化日志")
        log_lay = QVBoxLayout(log_box)
        log_lay.setContentsMargins(6, 6, 6, 6)
        self._optimize_log = QTextEdit()
        self._optimize_log.setReadOnly(True)
        self._optimize_log.setFont(QFont("Consolas", 9))
        self._optimize_log.setPlaceholderText("优化日志与边界评估…")
        self._optimize_log.setMinimumHeight(40)
        self._optimize_log.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        log_lay.addWidget(self._optimize_log)
        split.addWidget(log_box)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        split.setSizes([120, 100])
        layout.addWidget(split, 1)

        act = QHBoxLayout()
        view_btn = QPushButton("查看代码")
        view_btn.clicked.connect(self._view_optimize_code)
        act.addWidget(view_btn)
        to_trial = QPushButton("去试运行")
        to_trial.setToolTip("可选：优化后再选账号试跑验证")
        to_trial.clicked.connect(self._on_optimize_go_trial)
        act.addWidget(to_trial)
        act.addStretch()
        self._optimize_btn = QPushButton("开始优化")
        self._optimize_btn.setObjectName("PrimaryButton")
        self._optimize_btn.setEnabled(False)
        self._optimize_btn.setToolTip(
            "按上方反馈与（若有）伪录制做 AI 修订。\n"
            "无需账号；试跑可在改完后单独进行。"
        )
        self._optimize_btn.clicked.connect(self._on_optimize)
        act.addWidget(self._optimize_btn)
        layout.addLayout(act)

        self._init_optimize_folder_combo()
        return page

    def _cancel_generate(self):
        if self._worker and self._worker.isRunning():
            self._worker.terminate()
            self._worker.wait(2000)
        if self._revise_worker and self._revise_worker.isRunning():
            self._revise_worker.terminate()
            self._revise_worker.wait(2000)
        if self._optimize_worker and self._optimize_worker.isRunning():
            self._optimize_worker.terminate()
            self._optimize_worker.wait(2000)
        self._generate_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        self._set_agent_idle("已取消")
        self._update_trial_availability()
        if hasattr(self, "_trajectory"):
            self._trajectory.mark_cancelled()

    @staticmethod
    def _translate_error(msg: str) -> str:
        """把 API / 网络异常翻成用户可读的中文说明。"""
        import re

        raw = (msg or "").strip() or "未知错误"
        low = raw.lower()

        def pack(title: str, advice: str) -> str:
            return (
                f"{title}\n\n"
                f"建议：\n{advice}\n\n"
                f"—— 原始错误（给排查用）——\n{raw}"
            )

        # 本地/结构校验失败（必须最先判：错误文本里常含 TASK_task1_TIMEOUT 等标识符，
        # 会被下面的 timeout 子串匹配误判成「网络超时」）
        if (
            "生成的脚本校验失败" in raw
            or "未通过本地检查" in raw
            or "校验失败（已重试" in raw
            or "仍校验失败" in raw
        ):
            return pack(
                "生成的脚本未通过本地检查",
                "1. 到「4. 试运行」点「重修订」（生成页也有此按钮），会自动带上错误说明\n"
                "2. 也可直接再点「生成脚本」重试一次\n"
                "3. 若同类错误反复出现，请简化脚本描述或补充素材/介绍",
            )

        # 余额 / 额度不足（须在通用 403 之前，DeepSeek 常回 402 + Insufficient Balance）
        if (
            "insufficient balance" in low
            or "insufficient_quota" in low
            or "insufficient credits" in low
            or "exceeded your current quota" in low
            or "billing" in low and ("hard limit" in low or "not active" in low)
            or "余额不足" in raw
            or "额度不足" in raw
            or "欠费" in raw
            or re.search(r"\b402\b", raw)
            or ("error code: 402" in low)
        ):
            return pack(
                "账号余额不足，无法调用该模型",
                "1. 到当前提供商官网充值（例如 DeepSeek 开放平台）\n"
                "2. 或在「API 配置」换一个仍有余额的方案 / 提供商后重试\n"
                "3. 充值到账后可直接点「重修订」或「生成脚本」，不必重新写介绍",
            )

        # API Key 无效 / 缺失（含 DeepSeek 400 + Please pass a valid API key）
        if (
            ("valid api key" in low)
            or ("incorrect api key" in low)
            or ("invalid api key" in low)
            or ("invalid_api_key" in low)
            or ("authentication" in low and ("401" in raw or "invalid" in low))
            or ("api key" in low and ("invalid" in low or "empty" in low or "missing" in low))
            or ("invalid argument" in low and "api" in low and "key" in low)
        ):
            return pack(
                "API Key 无效或未正确填写",
                "1. 检查 API Key 是否完整复制（前后不要有空格或换行）\n"
                "2. 确认 Key 属于当前选择的「提供商」（例如 DeepSeek 的 Key 不能填到 OpenAI）\n"
                "3. 到对应官网重新创建 Key，粘贴后点「保存到当前」，再点「连接测试」",
            )

        if "401" in raw or "unauthorized" in low:
            return pack(
                "身份验证失败（未授权）",
                "1. API Key 可能填错、过期或被撤销\n"
                "2. 重新填写正确的 Key 并保存后重试",
            )

        if "403" in raw or "permission" in low or "forbidden" in low:
            return pack(
                "没有权限使用该模型或接口",
                "1. 确认账号已开通该模型\n"
                "2. 换一个列表中的模型再试\n"
                "3. 检查是否需要充值或完成实名认证",
            )

        # 模型名错误
        if "400" in raw and "model" in low and "api key" not in low:
            m = re.search(r"passed (\S+)", raw)
            model_name = m.group(1) if m else "当前模型"
            return pack(
                f"模型「{model_name}」当前不可用",
                "1. 从下拉列表换一个该提供商支持的模型\n"
                "2. 或确认手动填写的模型名是否拼写正确",
            )

        if "429" in raw or "rate limit" in low or "too many requests" in low:
            return pack(
                "请求过于频繁，已被限流",
                "请稍等一会儿再试；若持续出现，可降低调用频率或升级套餐。",
            )

        if re.search(r"(?<![A-Za-z0-9_])timeout(?![A-Za-z0-9_])|\btimed out\b", low):
            return pack(
                "请求超时",
                "1. 检查本机网络 / 代理是否正常\n"
                "2. 若使用了自定义端点，确认地址可访问\n"
                "3. 稍后重试",
            )

        if any(x in low for x in ("connection", "connecterror", "namenor", "getaddrinfo", "network")):
            return pack(
                "无法连接到 AI 服务器",
                "1. 检查网络是否畅通\n"
                "2. 如需代理，请先配置系统或终端代理\n"
                "3. 自定义端点请确认填写正确（含 https://）",
            )

        if "ssl" in low or "certificate" in low:
            return pack(
                "安全连接（证书）校验失败",
                "多为网络中间设备或代理导致。可检查代理设置，或换网络后再试。",
            )

        if "token" in low and ("exceed" in low or "limit" in low or "too long" in low or "context" in low):
            return pack(
                "内容过长，超出模型限制",
                "1. 减少参考图片数量，或勾选「压缩图片」\n"
                "2. 缩短脚本描述文字后再生成",
            )

        if "500" in raw or "502" in raw or "503" in raw or "overloaded" in low:
            return pack(
                "AI 服务暂时不可用",
                "这是服务端问题，请稍后再试；也可换一个模型或提供商。",
            )

        if "空内容" in raw or "empty content" in low:
            advice = (
                "1. 请再点一次「生成脚本」重试\n"
                "2. 若使用 DeepSeek V4（如 deepseek-v4-flash）：默认会先「思考」再写代码，"
                "思考可能占满输出额度导致正文为空。程序已自动关闭思考模式，请重试\n"
                "3. 图片较多时可勾选「压缩图片」，或暂时取消「发送图片给 AI」只保留文件名\n"
                "4. 仍失败可换 deepseek-chat，或换 Claude / GPT"
            )
            if "思考模式" in raw or "had_reasoning" in raw or "finish_reason=length" in raw:
                advice = (
                    "这通常不是额度不足，而是模型把输出额度用在了「思考过程」上，正文还没写完就结束了。\n\n"
                    "1. 直接再生成一次（程序已对 DeepSeek 关闭 thinking）\n"
                    "2. 图片较多时可勾选「压缩图片」，或减少发送的参考图\n"
                    "3. 或改用 deepseek-chat / 其他提供商"
                )
            return pack("AI 返回了空结果", advice)

        if "校验失败" in raw or "语法错误" in raw or "修订后仍校验失败" in raw:
            return pack(
                "生成的脚本未通过本地检查",
                "若已有代码：优先到「4. 试运行」点「重修订」（生成页也有此按钮）；"
                "硬校验失败时会自动带上错误说明。\n"
                "也可再点「生成脚本」从零重试；若仍失败请简化脚本描述。",
            )

        # 未识别：给通用中文壳，仍附原始错误
        return pack(
            "连接或调用失败",
            "1. 用「连接测试」确认 Key / 模型 / 网络是否正常\n"
            "2. 对照下方原始错误排查（常见是 Key 填错或模型名不对）\n"
            "3. 仍无法解决时，可把原始错误发给开发者协助查看",
        )

    def _save_script(self):
        # 正式落盘前去掉试运行伪录制（确认保存 / 生成页保存共用）
        code = self._strip_pseudo_from_working_code()
        # 保存前语法闸门：剥离/手改后若语法坏了，绝不落盘（历史事故：存进去才发现 SyntaxError）
        if not self._code_compiles(code):
            try:
                compile(code, "<save>", "exec")
            except SyntaxError as e:
                QMessageBox.critical(
                    self,
                    "保存被阻止：语法错误",
                    f"当前代码第 {e.lineno} 行语法错误：{e.msg}\n\n"
                    "请在「查看完整代码」里修正，或点「重修订」后再保存。",
                )
            return
        name = self._script_name.text().strip()
        if not name:
            name = "my_script.py"
        if not name.endswith(".py"):
            name += ".py"

        out_dir = Path(self._output_dir.text().strip())
        out_dir.mkdir(parents=True, exist_ok=True)

        # 自动处理重名
        base = out_dir / name
        stem = name.rsplit(".py", 1)[0]
        counter = 1
        out_path = base
        while out_path.exists():
            out_path = out_dir / f"{stem} ({counter}).py"
            counter += 1

        try:
            out_path.write_text(code, encoding="utf-8")
            QMessageBox.information(self, "保存成功", f"已保存到:\n{out_path}")
        except Exception as e:
            QMessageBox.critical(self, "保存失败", str(e))

    def _copy_code(self):
        QApplication.clipboard().setText(self._generated_code)
        self._copy_btn.setText("已复制")
        QTimer.singleShot(2000, lambda: self._copy_btn.setText("复制代码"))
