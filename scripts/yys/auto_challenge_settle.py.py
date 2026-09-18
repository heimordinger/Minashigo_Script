# -*- coding: utf-8 -*-
"""自动挑战-结算循环脚本（新超时版）
步骤：等挑战3s -> 点挑战 -> 等结算11s -> 点结算 -> 停1s
"""
import time

from automation import Assistant  # 运行时提供的助手基类


CHALLENGE_TIMEOUT = 3.0   # 等「挑战」出现
SETTLE_TIMEOUT = 11.0     # 等「结算」出现
TAIL_SLEEP = 1.0          # 收尾停顿


class Script(Assistant):
    def run(self):
        # 1. 等「挑战」按钮出现
        self.wait("asset_3b7b8c42", timeout=CHALLENGE_TIMEOUT)

        # 2. 点击「挑战」
        self.click("asset_3b7b8c42")

        # 3. 等「结算」界面出现
        self.wait("asset_ae313466", timeout=SETTLE_TIMEOUT)

        # 4. 点击「结算」
        self.click("asset_ae313466")

        # 5. 收尾停顿
        time.sleep(TAIL_SLEEP)