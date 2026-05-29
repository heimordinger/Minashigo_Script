# backend/browser/mixins/navigation.py
import asyncio

class NavigationMixin:
    """页面导航相关方法混入类"""

    async def goto(self, url: str, retries: int = 3):
        if not url.startswith("http") and "file://" not in url:
            url = "https://" + url

        for attempt in range(retries):
            try:
                await self.page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                break
            except Exception as e:
                if attempt == retries - 1:
                    raise
                print(f"{self.account['name']}: 导航被中断 ({e})，第 {attempt+1} 次重试")
                await asyncio.sleep(1.0)

        print(f"{self.account['name']}: 跳转至 {url}")