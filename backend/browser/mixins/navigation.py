# backend/browser/mixins/navigation.py
class NavigationMixin:
    """页面导航相关方法混入类"""

    async def goto(self, url: str):
        if not url.startswith("http") and "file://" not in url:
            url = "https://" + url

        await self.page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        print(f"{self.account['name']}: 跳转至 {url}")