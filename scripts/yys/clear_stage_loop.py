# -*- coding: utf-8 -*-
# minashigo-collab-linear
from pathlib import Path

from backend.automation.user_window import UserWindow

IMG_DIR = Path('F:\\Minashigo_script\\screenshots\\collab_snips')


async def do_work(browser: UserWindow):
    while True:
        ok = await browser.wait_image('F:\\Minashigo_script\\screenshots\\collab_snips\\snip_20260917_155046.png', timeout=30)
        if not ok:
            continue
        ok = await browser.wait_image('F:\\Minashigo_script\\screenshots\\collab_snips\\snip_20260917_155046.png', timeout=30)
        if not ok:
            continue
        ok = await browser.wait_image('F:\\Minashigo_script\\screenshots\\collab_snips\\snip_20260917_175220.png', timeout=30)
        if not ok:
            continue
        ok = await browser.wait_image('F:\\Minashigo_script\\screenshots\\collab_snips\\snip_20260917_175220.png', timeout=30)
        if not ok:
            continue
        ok = await browser.wait_image('F:\\Minashigo_script\\screenshots\\collab_snips\\snip_20260918_132447.png', timeout=30)
        if not ok:
            continue
        ok = await browser.wait_image('F:\\Minashigo_script\\screenshots\\collab_snips\\snip_20260918_132447.png', timeout=30)
        if not ok:
            continue
        await browser.b_sleep(0.3)