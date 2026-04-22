# backend/browser/mixins/multi_step.py
from typing import Optional, Callable


class MultiStepMixin:
    """多点组合操作混入类"""

    async def multi_step(
            self,
            steps: list,
            abort_check: Optional[Callable] = None,
    ) -> bool:
        for i, (method_name, args, kwargs) in enumerate(steps):
            if abort_check and abort_check():
                print(f"{self.account['name']}: 操作序列在第 {i + 1} 步被中止")
                return False

            method = getattr(self, method_name, None)
            if not method:
                print(f"{self.account['name']}: 未知方法 {method_name}")
                return False

            try:
                result = await method(*args, **kwargs)
                if result is False:
                    print(f"{self.account['name']}: 第 {i + 1} 步失败")
                    return False
            except Exception as e:
                print(f"{self.account['name']}: 第 {i + 1} 步异常: {e}")
                return False

        print(f"{self.account['name']}: 多步操作完成")
        return True