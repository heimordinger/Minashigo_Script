import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from backend.browser.user_browser import UserBrowser
from core.path import IMG_PATH

IMG_DIR = IMG_PATH / "game" / "script"


@dataclass
class Config:
    img_dir: Path = IMG_DIR
    threshold: float = 0.9
    nav_threshold: float = 0.8
    icon_threshold: float = 0.85
    use_polling_cache: bool = True
    total_timeout: float = 600.0


CFG = Config()


def _img(name: str) -> Path:
    return CFG.img_dir / (name if name.endswith(".png") else name + ".png")


StateName = Optional[str]
GUARDS = []


def register_guard(img_path, pianyi=(0, 0), desc=""):
    GUARDS.append((img_path, pianyi, desc))


async def check_guards(browser) -> bool:
    for img_path, pianyi, desc in GUARDS:
        if await browser.click_image(img_path, pianyi=pianyi, threshold=CFG.threshold):
            await browser.b_sleep(0.3, 0.8)
            return True
    return False


async def unknown_state(browser) -> StateName:
    cs = {"主界面": _img("rank")}
    rs = await asyncio.gather(*[
        browser.match_image(p, threshold=CFG.nav_threshold) for p in cs.values()
    ])
    for name, hit in zip(cs.keys(), rs):
        if hit:
            return name
    await browser.b_sleep(1.5, 2.5)
    return None


async def home_state(browser) -> StateName:
    browser.script_log("[home]")
    if await browser.match_image(_img("rank"), threshold=CFG.nav_threshold):
        return "__exit__"
    return "未知"


STATES = {"未知": unknown_state, "主界面": home_state}
STATE_TIMEOUT = {"未知": 180, "主界面": 30}


async def do_work(browser: UserBrowser):
    if CFG.use_polling_cache:
        browser.use_polling_temp_cache = True
    total_start = asyncio.get_event_loop().time()
    state_name = await unknown_state(browser) or "未知"
    se_time = asyncio.get_event_loop().time()
    while True:
        try:
            if asyncio.get_event_loop().time() - total_start > CFG.total_timeout:
                break
            browser.note_state(state_name)
            await browser.update_frame()
            if await check_guards(browser):
                continue
            now = asyncio.get_event_loop().time()
            if now - se_time > STATE_TIMEOUT.get(state_name, 30):
                state_name = await unknown_state(browser) or "未知"
                se_time = now
                continue
            handler = STATES.get(state_name)
            if handler is None:
                state_name = "未知"
                se_time = now
                continue
            nxt = await handler(browser)
            if nxt == "__exit__":
                break
            if nxt and nxt != state_name:
                state_name = nxt
                se_time = now
            await browser.b_sleep(0.05, 0.15)
        except (TimeoutError, RuntimeError) as e:
            browser.script_log(f"[ERROR] {type(e).__name__}: {e}")
            state_name = "未知"
            se_time = asyncio.get_event_loop().time()
