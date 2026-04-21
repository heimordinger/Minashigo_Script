from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel
from PySide6.QtCore import Qt
import core.app_info as app
from core.config.config import config


class AboutWidget(QWidget):
    def __init__(self):
        super().__init__()

        layout = QVBoxLayout(self)

        title = QLabel()
        title.setStyleSheet("font-size:18px;font-weight:bold;")

        info = QLabel()
        info.setTextFormat(Qt.RichText)
        info.setOpenExternalLinks(True)

        about_html = app.ABOUT.replace("\n", "<br>")

        links_html = (
            f"项目地址👉 <a href='{app.LINKS['GitHub']}' "
            f"style='color:#4FC3F7; text-decoration:none;'>GitHub</a>"
        )

        info.setText(f"""
        <b>版本：</b>{app.VERSION}<br>
        <b>作者：</b>{config.author}<br><br>
        {about_html}<br><br>
        {links_html}
        """)

        layout.addWidget(title)
        layout.addWidget(info)