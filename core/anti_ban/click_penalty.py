# core/anti_ban/click_penalty.py
import time


class ClickPenalty:
    def __init__(
            self,
            ubrowser,
            base_delay=0.3,
            factor=1.8,
            max_delay=6.0,
            decay_time=8.0,
    ):
        self.ubrowser = ubrowser
        self.records = {}
        self.base_delay = base_delay
        self.factor = factor
        self.max_delay = max_delay
        self.decay_time = decay_time

    async def before(self, key: str, mode="normal", max_delay=None, start_count=0):
        """
        key: 惩罚对应的动作/元素标识
        mode: 'spam_ok' 不触发惩罚
        max_delay: 单次惩罚上限
        start_count: 惩罚起步值
        """
        if mode == "spam_ok":
            return

        now = time.time()
        info = self.records.get(key)
        max_delay = self.max_delay if max_delay is None else max_delay

        if info:
            count, last = info
            if now - last < self.decay_time:
                effective_count = max(count, start_count)

                if self.base_delay * (self.factor ** effective_count) > max_delay:
                    delay = start_count
                else:
                    delay = min(self.base_delay * (self.factor ** effective_count), max_delay)

                await self.ubrowser.b_sleep(delay)
                self.records[key] = (count + 1, now)
            else:
                self.records[key] = (start_count + 1, now)
        else:
            self.records[key] = (start_count + 1, now)
