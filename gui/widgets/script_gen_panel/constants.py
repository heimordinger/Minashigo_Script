"""Script Generator panel constants."""
from __future__ import annotations

from core.path import SCRIPTS_PATH

# 试运行写入 scripts/_trial/，由 TaskController 按 scripts._trial.* 加载
_TRIAL_REL = "_trial/_gen_trial.py"
_TRIAL_DIR = SCRIPTS_PATH / "_trial"
# 协作草稿统一堆在 scripts/_collab/（文件名即标识，不进正式任务列表）；
# 用户确定要用某个脚本后再自行复制到 scripts/<游戏>/。
_COLLAB_DIR = SCRIPTS_PATH / "_collab"
_OPTIMIZE_PAGE_SIZE = 20
_INTRO_FILENAMES = ("脚本介绍.txt", "脚本解释.txt")
_KEYRING_SERVICE = "Minashigo_ScriptGenerator"
_DEFAULT_PROFILE = "默认"
