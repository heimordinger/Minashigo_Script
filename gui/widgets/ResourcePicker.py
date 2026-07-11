# gui/widgets/ResourcePicker.py
"""通用资源选择器 —— 脚本选择/目录浏览复用同一套界面。"""

from pathlib import Path

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTreeWidget, QTreeWidgetItem, QTreeView, QFileSystemModel,
    QMenu, QInputDialog, QMessageBox, QApplication, QCheckBox,
)
from PySide6.QtCore import Qt, QDir
from PySide6.QtGui import QKeySequence, QShortcut, QIcon

from core.path import ICON_PATH


class ResourcePickerDialog(QDialog):
    """通用资源选择器。

    两种模式：
      mode="files"   — 列出预定义的路径（脚本选择）
      mode="folders" — 浏览文件系统（目录选择），支持创建/删除/重命名
    """

    def __init__(self, parent=None, mode="files", paths=None, root_path=None,
                 show_recursive=True, file_filter=None, multi_select=False):
        super().__init__(parent)
        if mode == "files":
            title = "选择脚本"
        elif mode == "pick_file":
            title = "选择文件" + ("（可多选）" if multi_select else "")
        else:
            title = "选择保存路径"
        self.setWindowTitle(title)
        self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.setMinimumSize(420, 480)

        self.selected_path: str | None = None
        self.selected_paths: list[str] = []
        self.recursive: bool = True
        self._show_recursive = show_recursive if mode == "folders" else False
        self._file_filter = file_filter
        self._multi_select = multi_select
        self._mode = mode
        self._folder_model = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)

        if mode == "files" and paths is not None:
            self._build_file_tree(layout, paths)
        elif mode in ("folders", "pick_file"):
            self._build_folder_tree(layout, root_path)
        else:
            raise ValueError(f"未知模式: {mode}")

        # 确定/取消按钮（文件模式用共享按钮，目录模式已内建）
        if mode == "files":
            btn_row = QHBoxLayout()
            btn_ok = QPushButton("确定")
            btn_ok.clicked.connect(self._accept)
            btn_cancel = QPushButton("取消")
            btn_cancel.clicked.connect(self.reject)
            btn_row.addStretch()
            btn_row.addWidget(btn_ok)
            btn_row.addWidget(btn_cancel)
            layout.addLayout(btn_row)

    # ═══════════════════════════════════════
    #  文件列表模式（脚本选择）
    # ═══════════════════════════════════════

    def _build_file_tree(self, layout, paths):
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setAnimated(True)
        layout.addWidget(self.tree)
        self.tree.itemDoubleClicked.connect(self._accept)

        root = {"children": {}, "files": []}
        for p in paths:
            parts = p.split("/")
            node = root
            for i, part in enumerate(parts[:-1]):
                if part not in node["children"]:
                    node["children"][part] = {"children": {}, "files": []}
                node = node["children"][part]
            node["files"].append(p)

        def add_items(parent_widget, node):
            for name, sub in sorted(node["children"].items()):
                item = QTreeWidgetItem(parent_widget)
                item.setText(0, f"📁 {name}")
                item.setExpanded(False)
                add_items(item, sub)
            for fp in sorted(node["files"]):
                item = QTreeWidgetItem(parent_widget)
                item.setText(0, f"📄 {fp.split('/')[-1]}")
                item.setData(0, Qt.UserRole, fp)

        add_items(self.tree.invisibleRootItem(), root)

    # ═══════════════════════════════════════
    #  目录浏览模式（文件夹选择）
    # ═══════════════════════════════════════

    def _build_folder_tree(self, layout, root_path=None):
        if root_path is None:
            from core.path import PROJECT_ROOT
            root_path = str(PROJECT_ROOT)

        self._folder_model = QFileSystemModel()
        self._folder_model.setRootPath("")
        filters = QDir.Drives | QDir.AllDirs | QDir.NoDotAndDotDot
        if self._mode == "pick_file":
            filters |= QDir.Files
        self._folder_model.setFilter(filters)

        # 当前路径显示
        self._path_label = QLabel()
        self._path_label.setStyleSheet("color: #888; font-size: 11px; padding: 2px 0;")
        layout.addWidget(self._path_label)

        self.tree = QTreeView()
        self.tree.setModel(self._folder_model)
        self.tree.setRootIndex(self._folder_model.index(root_path))
        self.tree.setHeaderHidden(True)
        self.tree.setAnimated(True)
        self.tree.hideColumn(1)
        self.tree.hideColumn(2)
        self.tree.hideColumn(3)
        if self._multi_select:
            self.tree.setSelectionMode(QTreeView.SelectionMode.ExtendedSelection)
        self.tree.setEditTriggers(QTreeView.EditTrigger.EditKeyPressed)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_context_menu)
        self.tree.selectionModel().currentChanged.connect(self._update_path_label)
        layout.addWidget(self.tree)
        if self._mode == "pick_file":
            self.tree.doubleClicked.connect(self._on_file_double_click)
        else:
            self.tree.doubleClicked.connect(self._accept)
        self._update_path_label()

        # 确定/取消按钮行（新建文件夹放这里）
        btn_row = QHBoxLayout()
        if self._show_recursive:
            self._recursive_cb = QCheckBox("包括子文件夹")
            self._recursive_cb.setChecked(self.recursive)
            self._recursive_cb.setStyleSheet("QCheckBox { font-size: 12px; }")
            self._recursive_cb.toggled.connect(lambda v: setattr(self, 'recursive', v))
            btn_row.addWidget(self._recursive_cb)
        btn_row.addStretch()

        new_btn = QPushButton("新建文件夹")
        new_btn.clicked.connect(self._tree_new_folder)
        btn_row.addWidget(new_btn)
        browse_btn = QPushButton("浏览")
        browse_btn.setToolTip("打开系统目录选择器")
        browse_btn.clicked.connect(self._browse_system)
        btn_row.addWidget(browse_btn)
        self._btn_ok = QPushButton("确定")
        self._btn_ok.clicked.connect(self._accept)
        self._btn_cancel = QPushButton("取消")
        self._btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(self._btn_ok)
        btn_row.addWidget(self._btn_cancel)
        layout.addLayout(btn_row)

    def _on_file_double_click(self):
        idx = self.tree.currentIndex()
        path = self._folder_model.filePath(idx)
        if path and Path(path).is_file():
            self.selected_path = path
            self.accept()

    def _browse_system(self):
        """打开系统文件对话框选择目录，选中后更新树。"""
        from PySide6.QtWidgets import QFileDialog
        path = QFileDialog.getExistingDirectory(self, "选择目录", self._folder_model.rootPath())
        if path:
            self._folder_model.setRootPath(path)
            self.tree.setRootIndex(self._folder_model.index(path))

    def _update_path_label(self):
        """更新顶部路径显示。"""
        idx = self.tree.currentIndex()
        if idx.isValid():
            path = self._folder_model.filePath(idx)
        else:
            from core.path import PROJECT_ROOT
            path = str(PROJECT_ROOT)
        self._path_label.setText(f"📁 {path}")

        # 快捷键
        QShortcut(QKeySequence("F2"), self).activated.connect(
            self._tree_rename
        )
        QShortcut(QKeySequence("Delete"), self).activated.connect(
            self._tree_delete
        )

    def _current_dir(self) -> str:
        """当前选中的目录路径。"""
        idx = self.tree.currentIndex()
        if idx.isValid():
            path = self._folder_model.filePath(idx)
            if Path(path).is_dir():
                return path
            return str(Path(path).parent)
        return self._folder_model.rootPath()

    def _tree_new_folder(self):
        parent = self._current_dir()
        name, ok = QInputDialog.getText(self, "新建文件夹", "文件夹名称:")
        if ok and name:
            path = Path(parent) / name
            path.mkdir(parents=True, exist_ok=True)

    def _tree_delete(self):
        idx = self.tree.currentIndex()
        if not idx.isValid():
            return
        path = self._folder_model.filePath(idx)
        if not Path(path).is_dir():
            return
        if QMessageBox.question(self, "确认删除", f"确定删除 {path} ?") == QMessageBox.Yes:
            self._folder_model.rmdir(idx)

    def _tree_rename(self):
        idx = self.tree.currentIndex()
        if idx.isValid() and idx.column() == 0:
            path = self._folder_model.filePath(idx)
            if path and Path(path).is_dir() and not path.endswith(":\\"):
                self.tree.edit(idx)

    def _tree_context_menu(self, pos):
        idx = self.tree.indexAt(pos)
        menu = QMenu(self.tree)
        menu.addAction("新建文件夹", self._tree_new_folder)
        if idx.isValid():
            menu.addAction("重命名", self._tree_rename)
            menu.addAction("删除", self._tree_delete)
            menu.addSeparator()
            path = self._folder_model.filePath(idx)
            menu.addAction("复制路径", lambda: QApplication.clipboard().setText(path))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    # ═══════════════════════════════════════
    #  确认
    # ═══════════════════════════════════════

    def _accept(self):
        if self._mode == "files":
            item = self.tree.currentItem()
            if item:
                fp = item.data(0, Qt.UserRole)
                if fp:
                    self.selected_path = fp
                    self.accept()
        else:
            if self._multi_select and self._mode == "pick_file":
                idxs = self.tree.selectedIndexes()
                files = []
                for idx in idxs:
                    if idx.column() == 0:  # 每行只取一次
                        path = self._folder_model.filePath(idx)
                        if path and Path(path).is_file():
                            files.append(path)
                if files:
                    self.selected_paths = files
                    self.selected_path = files[0]
                    self.accept()
                return
            idx = self.tree.currentIndex()
            path = self._folder_model.filePath(idx)
            if path:
                if self._mode == "pick_file" and Path(path).is_dir():
                    return
                self.selected_path = path
                self.accept()
