"""运行时日志自动备份：边跑边落盘，省得为了排查问题手工复制大段日志。

背景：`Controller.emit_log` 原本只把日志塞进内存 deque（每账号 1000 条）并发 Qt 信号，
**不落盘**。唯一的落盘是任务结束/异常时写最近 40 行到 `screenshots/daily_stop/`。
于是排查长任务时，1000 条之前的日志就永久丢了，用户只能手工复制粘贴。

设计要点：
- **只订阅、不改 emit_log**：主流程零侵入，备份失败也只打印一行，不影响运行；
- 每次「脚本任务开始」写一个新文件，避免把几十次运行混在一起；
- 文件是 JSONL（逐行追加、崩了也不丢已写的部分），另写一份人读的 `.log`；
- 保留最近 N 个运行文件，自动删旧的，不会无限长；
- 落盘放在线程里做（`emit_log` 可能来自任意线程），不阻塞调用方。
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# 默认保留最近多少次运行的日志
DEFAULT_KEEP_RUNS = 12
# 单次运行日志的软上限：超过就只写 .log 的尾部（避免一次跑几天把磁盘写满）
DEFAULT_MAX_LINES = 200_000


def _slug(text: str | None, max_len: int = 24) -> str:
    """把账号名/任务名压成一个安全的文件名片段（保留中文与 ASCII 字母数字）。"""
    raw = (text or "").strip()
    raw = raw.replace("\\", "/")
    # 任务名多半是路径：取最后一段、去掉扩展名，得到 "刷关卡" / "结界勋章"
    raw = raw.rsplit("/", 1)[-1]
    if raw.endswith(".py"):
        raw = raw[:-3]
    s = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", raw, flags=re.UNICODE)
    s = re.sub(r"_+", "_", s).strip("_")
    return s[:max_len] or "account"


class RuntimeLogBackup:
    """把运行时日志按「每次脚本任务一个文件」追加落盘。"""

    def __init__(
        self,
        root: Path | str,
        *,
        keep_runs: int = DEFAULT_KEEP_RUNS,
        max_lines: int = DEFAULT_MAX_LINES,
        enabled: bool = True,
    ) -> None:
        self.root = Path(root)
        self.keep_runs = max(1, int(keep_runs))
        self.max_lines = max(1000, int(max_lines))
        self.enabled = bool(enabled)
        self._lock = threading.Lock()
        self._jsonl: Optional[Path] = None
        self._log: Optional[Path] = None
        self._lines = 0
        self._warned = False

    # ── 对外 ─────────────────────────────────────────────────────────

    @property
    def current_jsonl(self) -> Optional[Path]:
        return self._jsonl

    def begin_run(self, label: str = "", *, account: str = "") -> Optional[Path]:
        """开始一次运行：新开文件，并清理过旧的运行。"""
        if not self.enabled:
            return None
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        # 同一秒内可能连开两次（比如刚停又点开始）：加随机后缀保证一个运行一个文件，
        # 否则会往同一个文件里追加，两次运行的日志就混在一起了。时间戳仍在前，排序不受影响。
        name = f"{ts}_{secrets.token_hex(2)}_{_slug(account or label)}"
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            self._warn(f"创建日志目录失败: {e}")
            return None
        with self._lock:
            self._jsonl = self.root / f"{name}.jsonl"
            self._log = self.root / f"{name}.log"
            self._lines = 0
            try:
                with self._jsonl.open("a", encoding="utf-8") as f:
                    f.write(
                        json.dumps(
                            {
                                "ts": datetime.now().isoformat(timespec="seconds"),
                                "level": "INFO",
                                "account": account,
                                "source": "backup",
                                "message": f"[日志备份] 开始记录运行：{label or account}",
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                # 人读版也立刻建出来：这样一次运行固定是成对的 .jsonl + .log，
                # 轮转时不会出现「只删掉一半」的孤儿文件。
                self._log.touch(exist_ok=True)
            except Exception as e:  # noqa: BLE001
                self._warn(f"写日志备份失败: {e}")
                self._jsonl = None
                return None
            self._prune_locked()
            return self._jsonl

    def record(self, *, account: str = "", level: Any = "", message: str = "", source: Any = "") -> None:
        """记一条日志（由 Controller.emit_log 调用；失败只打印一次，不影响运行）。"""
        if not self.enabled or self._jsonl is None:
            return
        text = str(message or "")
        if not text:
            return
        lvl = str(getattr(level, "value", level) or "")
        src = str(getattr(source, "value", source) or "")
        row = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "level": lvl,
            "account": str(account or ""),
            "source": src,
            "message": text,
        }
        with self._lock:
            if self._lines >= self.max_lines:
                return
            try:
                with self._jsonl.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                with self._log.open("a", encoding="utf-8") as f:
                    f.write(f"{row['ts']} [{lvl or 'INFO'}] {text}\n")
                self._lines += 1
            except Exception as e:  # noqa: BLE001
                self._warn(f"写日志备份失败: {e}")

    def tail(self, lines: int = 200) -> str:
        """读当前运行日志的尾部（人读格式），方便直接贴给 Agent。"""
        if self._log is None or not self._log.is_file():
            return ""
        try:
            with self._log.open("r", encoding="utf-8", errors="replace") as f:
                return "".join(f.readlines()[-max(1, int(lines)):])
        except Exception:
            return ""

    # ── 内部 ─────────────────────────────────────────────────────────

    def _prune_locked(self) -> None:
        """只保留最近 keep_runs 次运行（.jsonl 与 .log 成对删）。"""
        try:
            groups: dict[str, list[Path]] = {}
            for p in self.root.glob("*"):
                if p.suffix not in (".jsonl", ".log"):
                    continue
                groups.setdefault(p.stem, []).append(p)
            ordered = sorted(groups.items(), key=lambda kv: kv[0], reverse=True)
            for _stem, files in ordered[self.keep_runs:]:
                for p in files:
                    try:
                        p.unlink()
                    except Exception:
                        pass
        except Exception:
            pass

    def _warn(self, msg: str) -> None:
        if self._warned:
            return
        self._warned = True
        try:
            print(f"[RuntimeLogBackup] {msg}")
        except Exception:
            pass
