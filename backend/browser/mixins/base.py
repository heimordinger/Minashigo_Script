# backend/browser/mixins/base.py
from core.logging.events import LogSource, LogLevel


class BaseMixin:
    """基础方法混入类"""

    def _log(self, msg, level=LogLevel.INFO):
        if self.controller:
            self.controller.emit_log(
                account=self.account["name"],
                message=msg,
                level=level,
                source=LogSource.BROWSER
            )

    def script_log(self, msg):
        self._log(msg=msg)