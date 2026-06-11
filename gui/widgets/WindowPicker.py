"""
窗口选择器 —— 列出所有可见窗口，预览截图，确认后返回 Win32Target。
"""

from __future__ import annotations

import sys
from pathlib import Path

_project_root = Path(__file__).resolve().parents[2]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QLabel, QLineEdit, QSplitter, QWidget, QMessageBox,
    QAbstractItemView, QFrame, QApplication, QProgressBar,
    QSpinBox,
)

# cv2 / numpy / Win32Target 在方法内按需惰性导入（加快启动）


class WindowPickerDialog(QDialog):
    """窗口选择对话框。

    用法::

        dialog = WindowPickerDialog(parent=self)
        if dialog.exec():
            target = dialog.selected_target
            # target 就是选中的 Win32Target
    """

    COL_TITLE = 0
    COL_CLASS = 1
    COL_SIZE = 2
    COL_HWND = 3  # 隐藏列

    def __init__(self, parent=None):
        super().__init__(parent)
        self._targets: list[Win32Target] = []
        self.selected_target: Optional[Win32Target] = None
        self._has_loaded = False
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(300)
        self._preview_timer.timeout.connect(self._do_preview)

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_timer.timeout.connect(self._apply_filter)

        self._scan_result = None

        # 主线程定时器：轮询后台线程的扫描结果（QTimer 必须在主线程创建）
        self._scan_checker = QTimer(self)
        self._scan_checker.setInterval(100)
        self._scan_checker.timeout.connect(self._check_scan_result)

        self._setup_ui()
        self._show_empty_state()
        self.resize(900, 620)
        self.setWindowTitle("选择目标窗口")

    # ── 界面搭建 ─────────────────────────────────────────────

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # 搜索栏
        search_layout = QHBoxLayout()
        search_label = QLabel("搜索:")
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("输入窗口标题关键词…")
        self._search_input.textChanged.connect(self._on_search)
        search_layout.addWidget(search_label)
        search_layout.addWidget(self._search_input, 1)
        layout.addLayout(search_layout)

        # 分割线：上=列表，下=预览
        splitter = QSplitter(Qt.Vertical)

        # ── 上：表格 ──
        self._table = QTableWidget()
        self._table.setColumnCount(4)
        self._table.setHorizontalHeaderLabels(["窗口标题", "类名", "客户区", "HWND"])
        self._table.setColumnHidden(self.COL_HWND, True)  # 隐藏 HWND 列
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.itemSelectionChanged.connect(self._on_selection_changed)
        self._table.setSortingEnabled(True)

        hdr = self._table.horizontalHeader()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(self.COL_TITLE, QHeaderView.Stretch)
        hdr.setSectionResizeMode(self.COL_CLASS, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(self.COL_SIZE, QHeaderView.ResizeToContents)

        splitter.addWidget(self._table)

        # ── 下：预览区 ──
        preview_widget = QWidget()
        preview_layout = QVBoxLayout(preview_widget)
        preview_layout.setContentsMargins(0, 4, 0, 0)

        preview_header = QHBoxLayout()
        self._preview_label = QLabel("选中窗口后在此预览截图")
        self._preview_label.setStyleSheet("color: #888;")
        self._preview_info = QLabel("")
        self._preview_info.setStyleSheet("color: #555; font-size: 11px;")
        preview_header.addWidget(self._preview_label, 1)
        preview_header.addWidget(self._preview_info)
        preview_layout.addLayout(preview_header)

        self._preview_image = QLabel()
        self._preview_image.setAlignment(Qt.AlignCenter)
        self._preview_image.setMinimumHeight(250)
        self._preview_image.setFrameShape(QFrame.StyledPanel)
        self._preview_image.setStyleSheet("background: #1e1e1e; border: 1px solid #444;")
        preview_layout.addWidget(self._preview_image, 1)

        # ── 点击测试栏 ──
        click_bar = QHBoxLayout()
        click_bar.setContentsMargins(0, 4, 0, 0)

        click_bar.addWidget(QLabel("点击测试:"))

        self._click_x = QSpinBox()
        self._click_x.setRange(0, 99999)
        self._click_x.setPrefix("X=")
        self._click_x.setFixedWidth(110)
        click_bar.addWidget(self._click_x)

        self._click_y = QSpinBox()
        self._click_y.setRange(0, 99999)
        self._click_y.setPrefix("Y=")
        self._click_y.setFixedWidth(110)
        click_bar.addWidget(self._click_y)

        self._click_test_btn = QPushButton("点击")
        self._click_test_btn.setFixedWidth(60)
        self._click_test_btn.setEnabled(False)
        self._click_test_btn.clicked.connect(self._do_click_test)
        click_bar.addWidget(self._click_test_btn)

        self._template_btn = QPushButton("截取模板")
        self._template_btn.setFixedWidth(80)
        self._template_btn.setEnabled(False)
        self._template_btn.setToolTip("将当前坐标附近区域保存为匹配模板")
        self._template_btn.clicked.connect(self._save_template)
        click_bar.addWidget(self._template_btn)

        self._match_btn = QPushButton("匹配点击")
        self._match_btn.setFixedWidth(80)
        self._match_btn.setEnabled(False)
        self._match_btn.setToolTip("用模板精确匹配定位并后台点击")
        self._match_btn.clicked.connect(self._do_match_click)
        click_bar.addWidget(self._match_btn)

        self._click_result = QLabel("")
        self._click_result.setStyleSheet("color: #888; font-size: 11px;")
        click_bar.addWidget(self._click_result, 1)

        self._preview_image.mousePressEvent = lambda e: self._on_preview_click(e)

        preview_layout.addLayout(click_bar)

        splitter.addWidget(preview_widget)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        layout.addWidget(splitter, 1)

        # ── 扫描进度条（默认隐藏） ──
        self._loading_bar = QProgressBar()
        self._loading_bar.setRange(0, 0)  # 不确定模式（持续动画）
        self._loading_bar.setFixedHeight(20)
        self._loading_bar.setTextVisible(True)
        self._loading_bar.setFormat("正在扫描窗口…")
        self._loading_bar.setVisible(False)
        self._loading_bar.setStyleSheet("""
            QProgressBar {
                border: 1px solid #555;
                border-radius: 4px;
                background: #2d2d2d;
                text-align: center;
                color: #ccc;
                font-size: 12px;
            }
            QProgressBar::chunk {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:0,
                    stop:0 #0077b6, stop:1 #00b4d8
                );
                border-radius: 3px;
            }
        """)
        layout.addWidget(self._loading_bar)

        # ── 底部按钮 ──
        btn_layout = QHBoxLayout()
        self._count_label = QLabel("")
        self._count_label.setStyleSheet("color: #888;")
        btn_layout.addWidget(self._count_label, 1)

        self._scan_btn = QPushButton("获取窗口")
        self._scan_btn.clicked.connect(self._load_windows)
        self._scan_btn.setStyleSheet("font-weight: bold;")
        btn_layout.addWidget(self._scan_btn)

        self._preview_btn = QPushButton("刷新预览")
        self._preview_btn.clicked.connect(self._do_preview)
        self._preview_btn.setEnabled(False)
        btn_layout.addWidget(self._preview_btn)

        btn_layout.addSpacing(20)

        self._ok_btn = QPushButton("确认选择")
        self._ok_btn.setDefault(True)
        self._ok_btn.clicked.connect(self._on_confirm)
        btn_layout.addWidget(self._ok_btn)

        self._cancel_btn = QPushButton("取消")
        self._cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self._cancel_btn)

        layout.addLayout(btn_layout)

    # ── 空状态 ──────────────────────────────────────────────

    def _show_empty_state(self):
        """显示未扫描状态。"""
        self._search_input.setEnabled(False)
        self._table.setRowCount(0)
        self._count_label.setText("点击「获取窗口」扫描所有可见窗口")
        self._ok_btn.setEnabled(False)
        self._click_test_btn.setEnabled(False)
        self._template_btn.setEnabled(False)
        self._match_btn.setEnabled(False)
        self._preview_label.setText("点击「获取窗口」扫描后再选择")

    # ── 扫描窗口 ─────────────────────────────────────────────

    def _load_windows(self):
        """后台线程扫描窗口，不阻塞界面。"""
        self._scan_btn.setEnabled(False)
        self._scan_btn.setText("扫描中…")
        self._preview_btn.setEnabled(False)
        self._search_input.setEnabled(False)
        self._count_label.setText("正在扫描窗口…")
        self._table.setRowCount(0)
        self._loading_bar.setVisible(True)  # 启动动画
        self._scan_result = None
        self._scan_checker.start()  # 开始轮询后台线程结果

        import threading
        t = threading.Thread(target=self._scan_task, daemon=True)
        t.start()

    def _scan_task(self):
        """在后台线程中枚举窗口。"""
        import threading
        import time
        try:
            t0 = time.perf_counter()
            print(f"[scan] 开始枚举窗口 (thread={threading.current_thread().name})")

            from backend.automation.win32_target import Win32Target

            targets = [
                t for t in Win32Target.all_visible()
                if t.title.strip()
                and t.client_rect["width"] > 10 and t.client_rect["height"] > 10
            ]
            targets.sort(key=lambda w: w.title.lower())
            t1 = time.perf_counter()
            print(f"[scan] 完成: {len(targets)} 个窗口, 耗时 {t1-t0:.3f}s")

            # 把结果给主线程（_scan_checker 会取走）
            self._scan_result = targets
        except Exception as e:
            import traceback
            print(f"[scan] 异常: {e}")
            traceback.print_exc()
            self._scan_result = []  # 空列表让主线程知道扫描结束（但无结果）

    def _check_scan_result(self):
        """主线程轮询：取走后台线程的扫描结果。"""
        if self._scan_result is not None:
            targets = self._scan_result
            self._scan_result = None
            self._scan_checker.stop()
            self._on_scan_done(targets)

    def _on_scan_done(self, targets):
        """扫描完成，主线程更新界面。"""
        self._targets = targets
        self._has_loaded = True
        self._scan_btn.setEnabled(True)
        self._scan_btn.setText("重新获取")
        self._preview_btn.setEnabled(True)
        self._search_input.setEnabled(True)
        self._loading_bar.setVisible(False)  # 停止动画
        self._apply_filter()

    def _apply_filter(self):
        """初始填充表格（仅一次：扫描完成后调用）。"""
        self._table.setSortingEnabled(False)
        self._table.setRowCount(len(self._targets))

        for row, target in enumerate(self._targets):
            cr = target.client_rect
            title_item = QTableWidgetItem(target.title or "(无标题)")
            title_item.setToolTip(target.title)
            self._table.setItem(row, self.COL_TITLE, title_item)
            self._table.setItem(row, self.COL_CLASS, QTableWidgetItem(target.class_name))
            size_text = f"{cr['width']}×{cr['height']}" if cr['width'] > 0 else "(最小化)"
            self._table.setItem(row, self.COL_SIZE, QTableWidgetItem(size_text))
            self._table.setItem(row, self.COL_HWND, QTableWidgetItem(str(target.hwnd)))

        self._table.setSortingEnabled(True)
        # 初始无搜索词，显示全部
        self._apply_search()

    def _apply_search(self):
        """搜索过滤：只隐藏/显示行，不改表格结构。"""
        keyword = self._search_input.text().strip().lower()
        total_rows = self._table.rowCount()
        matched = 0
        for row in range(total_rows):
            title_item = self._table.item(row, self.COL_TITLE)
            class_item = self._table.item(row, self.COL_CLASS)
            if not title_item or not class_item:
                self._table.setRowHidden(row, True)
                continue
            if not keyword:
                self._table.setRowHidden(row, False)
                matched += 1
            else:
                match = (keyword in title_item.text().lower()
                         or keyword in class_item.text().lower())
                self._table.setRowHidden(row, not match)
                if match:
                    matched += 1
        self._count_label.setText(f"共 {matched} 个窗口")

    def _on_search(self, _text):
        self._search_timer.start()  # 防抖 200ms，停止输入后才刷新

    # ── 选中 / 预览 ─────────────────────────────────────────

    def _on_selection_changed(self):
        target = self._get_selected_target()
        if target:
            self._ok_btn.setEnabled(True)
            self._click_test_btn.setEnabled(True)
            self._template_btn.setEnabled(True)
            self._preview_info.setText(f"{target.client_rect['width']}×{target.client_rect['height']}")
            self._preview_label.setText(f"预览: {target.title}")
            self._preview_timer.start()  # 防抖 300ms
        else:
            self._ok_btn.setEnabled(False)
            self._click_test_btn.setEnabled(False)
            self._template_btn.setEnabled(False)
            self._match_btn.setEnabled(False)
            self._preview_label.setText("选中窗口后在此预览截图")
            self._preview_info.setText("")
            self._preview_image.clear()

    def _do_preview(self):
        target = self._get_selected_target()
        if target is None:
            return

        try:
            # auto 模式：PrintWindow 优先，失败则 BitBlt 从屏幕复制
            frame = target.screenshot(client_only=True, method="auto")
            self._show_frame(frame)
        except Exception as e:
            self._preview_image.setText(f"截图失败: {e}")
            self._preview_image.setStyleSheet(
                "background: #1e1e1e; border: 1px solid #444; color: #c44; padding: 20px;"
            )

    def _show_frame(self, frame):
        """将 OpenCV BGR 矩阵显示到 QLabel 上。"""
        import cv2

        h, w = frame.shape[:2]
        if h == 0 or w == 0:
            self._preview_image.setText("窗口内容为空")
            return

        # 记录原始尺寸、缩放比、显示尺寸（用于鼠标点击坐标换算）
        self._frame_w = w
        self._frame_h = h
        self._current_frame = frame  # 保存截图，供截取模板用

        # BGR → RGB
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        # 缩放到预览区
        label_w = self._preview_image.width() or 600
        label_h = self._preview_image.height() or 250
        scale = min(label_w / w, label_h / h, 1.0)
        self._frame_scale = scale
        if scale < 1.0:
            new_w, new_h = int(w * scale), int(h * scale)
            rgb = cv2.resize(rgb, (new_w, new_h), interpolation=cv2.INTER_AREA)
        else:
            new_w, new_h = w, h
        self._frame_display_w = new_w
        self._frame_display_h = new_h

        # 更新坐标输入框的范围
        self._click_x.setRange(0, w)
        self._click_y.setRange(0, h)

        # 转 QPixmap
        h2, w2 = rgb.shape[:2]
        qimg = QImage(rgb.data, w2, h2, w2 * 3, QImage.Format_RGB888)
        pixmap = QPixmap.fromImage(qimg)

        self._preview_image.setPixmap(pixmap)
        self._preview_image.setStyleSheet("background: #1e1e1e; border: 1px solid #444;")

    # ── 点击测试 ────────────────────────────────────────────

    def _on_preview_click(self, event):
        """鼠标点击预览图 → 换算为截图像素坐标 → 填入 X/Y。

        标签左上角到图像左上角可能有居中偏移，在点击时实时计算。
        """
        if not hasattr(self, '_frame_display_w'):
            return

        lx = event.position().x()
        ly = event.position().y()
        dw = self._frame_display_w
        dh = self._frame_display_h

        # 当前标签的居中偏移（实时计算，不受布局变化影响）
        offx = max((self._preview_image.width() - dw) // 2, 0)
        offy = max((self._preview_image.height() - dh) // 2, 0)

        # 相对图像的像素
        ix = lx - offx
        iy = ly - offy

        # 点在图像范围之外的忽略
        if ix < 0 or iy < 0 or ix > dw or iy > dh:
            return

        # 还原到原始截图尺寸
        s = self._frame_scale
        ox = int(ix / s) if s > 0 else int(ix)
        oy = int(iy / s) if s > 0 else int(iy)
        ox = min(ox, self._frame_w)
        oy = min(oy, self._frame_h)

        self._click_x.setValue(ox)
        self._click_y.setValue(oy)

    def _do_click_test(self):
        """发送后台 PostMessage 点击并输出调试信息。"""
        target = self._get_selected_target()
        if target is None:
            return
        x = self._click_x.value()  # 截图物理像素
        y = self._click_y.value()
        try:
            target.click(x, y)
            self._click_result.setText(f"✓ 已发送 ({x}, {y})")
            self._click_result.setStyleSheet("color: #4caf50; font-size: 11px;")
            self._preview_timer.start()
        except Exception as e:
            self._click_result.setText(f"✗ 失败: {e}")
            self._click_result.setStyleSheet("color: #e63946; font-size: 11px;")

    def _save_template(self):
        """从当前截图截取模板（以当前 X/Y 为中心的 100×100 区域）。"""
        frame = getattr(self, '_current_frame', None)
        if frame is None:
            return
        x = self._click_x.value()
        y = self._click_y.value()
        h, w = frame.shape[:2]

        # 以 (x,y) 为中心裁 100×100
        half = 50
        x1 = max(x - half, 0)
        x2 = min(x + half, w)
        y1 = max(y - half, 0)
        y2 = min(y + half, h)
        template = frame[y1:y2, x1:x2]

        out = Path("click_template.png")
        import cv2
        cv2.imwrite(str(out), template)
        self._match_btn.setEnabled(True)
        self._click_result.setText(f"✓ 模板已保存 ({out.name}) {x2-x1}×{y2-y1}")
        self._click_result.setStyleSheet("color: #4caf50; font-size: 11px;")
        print(f"[template] 已保存 {out.name} 位置=({x1},{y1})~({x2},{y2})")

    def _do_match_click(self):
        """用 Matcher 精确找模板 → 自动点击。"""
        frame = getattr(self, '_current_frame', None)
        if frame is None:
            return
        target = self._get_selected_target()
        if target is None:
            return

        from backend.matcher.matcher import matcher
        import cv2
        template_path = Path("click_template.png")
        if not template_path.exists():
            self._click_result.setText("✗ 请先截取模板")
            self._click_result.setStyleSheet("color: #e63946; font-size: 11px;")
            return

        result = matcher.match(
            target=frame, template=str(template_path),
            threshold=0.7, match_select="best",
        )
        if result and result.score > 0.7:
            mx, my = result.x, result.y
            self._click_x.setValue(mx)
            self._click_y.setValue(my)
            print(f"[match] 找到模板 ({mx},{my}) 置信度={result.score:.3f}")
            # 自动点击
            target.click(mx, my)
            self._click_result.setText(f"✓ 匹配点击 ({mx},{my}) conf={result.score:.3f}")
            self._click_result.setStyleSheet("color: #4caf50; font-size: 11px;")
            self._preview_timer.start()
        else:
            self._click_result.setText(f"✗ 未匹配到 (最高={result.score:.3f})" if result else "✗ 匹配失败")
            self._click_result.setStyleSheet("color: #e63946; font-size: 11px;")

    # ── 确认 ────────────────────────────────────────────────

    def _get_selected_target(self) -> Optional[Win32Target]:
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            return None
        row = rows[0].row()
        hwnd_item = self._table.item(row, self.COL_HWND)
        if hwnd_item is None:
            return None
        hwnd = int(hwnd_item.text())
        for t in self._targets:
            if t.hwnd == hwnd:
                return t
        return None

    def _on_confirm(self):
        target = self._get_selected_target()
        if target is None:
            QMessageBox.warning(self, "提示", "请先选择一个窗口")
            return
        self.selected_target = target
        self.accept()

    # ── 外部调用接口 ─────────────────────────────────────────

    @staticmethod
    def pick(parent=None) -> Optional[Win32Target]:
        """弹出窗口选择器，返回选中的 Win32Target（取消返回 None）。

        用法::

            target = WindowPickerDialog.pick()
            if target:
                frame = target.screenshot()
        """
        dlg = WindowPickerDialog(parent)
        if dlg.exec():
            return dlg.selected_target
        return None


if __name__ == "__main__":

    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)

    target = WindowPickerDialog.pick()
    if target:
        print(f"\n选中窗口: [{target.title}]")
        print(f"  hwnd:      {target.hwnd}")
        print(f"  类名:      {target.class_name}")
        print(f"  PID:       {target.pid}")
        print(f"  窗口位置:  {target.rect}")
        print(f"  客户区:    {target.client_rect['width']}×{target.client_rect['height']}")
    else:
        print("用户取消了选择")

