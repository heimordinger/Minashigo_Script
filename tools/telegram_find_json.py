#!/usr/bin/env python3
"""
在 Telegram 群组/频道中批量查找 .json 文件。

用法:
  1. pip install telethon
  2. 在 my.telegram.org 创建应用，拿到 api_id / api_hash
  3. 复制 tools/telegram_config.example.json 为 telegram_config.json 并填写
  4. python tools/telegram_find_json.py
  5. python tools/telegram_find_json.py --download   # 下载到 tools/telegram_json_downloads/
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from telethon import TelegramClient
from telethon.tl.types import DocumentAttributeFilename

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "telegram_config.json"
SESSION_PATH = ROOT / "telegram_session"
DEFAULT_DOWNLOAD_DIR = ROOT / "telegram_json_downloads"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        print(f"请先创建配置文件: {CONFIG_PATH}")
        print("可复制 telegram_config.example.json 并改名为 telegram_config.json")
        sys.exit(1)
    with CONFIG_PATH.open(encoding="utf-8") as f:
        cfg = json.load(f)
    for key in ("api_id", "api_hash", "group"):
        if not cfg.get(key):
            print(f"配置缺少字段: {key}")
            sys.exit(1)
    return cfg


def filename_from_message(msg) -> str:
    name = ""
    if msg.file and getattr(msg.file, "name", None):
        name = msg.file.name
    doc = getattr(msg, "document", None)
    if doc:
        for attr in doc.attributes or []:
            if isinstance(attr, DocumentAttributeFilename):
                name = attr.file_name or name
    return name or ""


def is_json_filename(name: str) -> bool:
    return name.lower().endswith(".json")


def build_message_link(chat, msg_id: int) -> str:
    username = getattr(chat, "username", None)
    if username:
        return f"https://t.me/{username}/{msg_id}"
    chat_id = getattr(chat, "id", None)
    if chat_id is not None:
        internal = str(abs(chat_id)).removeprefix("100")
        return f"https://t.me/c/{internal}/{msg_id}"
    return ""


async def collect_json_messages(client: TelegramClient, group: str, keyword: str | None):
    entity = await client.get_entity(group)
    rows = []
    keyword_lower = keyword.lower() if keyword else None

    async for msg in client.iter_messages(entity, limit=None):
        if not msg.file:
            continue
        name = filename_from_message(msg)
        if not is_json_filename(name):
            continue
        if keyword_lower and keyword_lower not in name.lower():
            continue

        size = msg.file.size or 0
        rows.append(
            {
                "date": msg.date.strftime("%Y-%m-%d %H:%M:%S") if msg.date else "",
                "name": name,
                "size_kb": round(size / 1024, 1),
                "msg_id": msg.id,
                "link": build_message_link(entity, msg.id),
                "message": msg,
            }
        )

    rows.sort(key=lambda r: r["date"], reverse=True)
    return rows


def print_rows(rows: list[dict]) -> None:
    if not rows:
        print("未找到 .json 文件。")
        return
    print(f"共找到 {len(rows)} 个 .json 文件:\n")
    for i, row in enumerate(rows, 1):
        print(f"{i:>3}. {row['date']} | {row['size_kb']:>8.1f} KB | {row['name']}")
        if row["link"]:
            print(f"     {row['link']}")


def export_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f, fieldnames=["date", "name", "size_kb", "msg_id", "link"]
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row[k] for k in writer.fieldnames})
    print(f"\n已导出: {path}")


def safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    return name.strip() or "unnamed.json"


async def download_rows(rows: list[dict], download_dir: Path) -> None:
    download_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n开始下载到: {download_dir}")
    for i, row in enumerate(rows, 1):
        msg = row["message"]
        target = download_dir / safe_filename(row["name"])
        if target.exists():
            stem, suffix = target.stem, target.suffix
            target = download_dir / f"{stem}_{row['msg_id']}{suffix}"
        print(f"[{i}/{len(rows)}] {row['name']}")
        await msg.download_media(file=str(target))


async def main_async(args: argparse.Namespace) -> None:
    cfg = load_config()
    client = TelegramClient(
        str(SESSION_PATH),
        int(cfg["api_id"]),
        cfg["api_hash"],
    )

    async with client:
        if not await client.is_user_authorized():
            phone = cfg.get("phone") or input("请输入手机号（含国家码，如 +86138...）: ").strip()
            await client.send_code_request(phone)
            code = input("请输入 Telegram 验证码: ").strip()
            try:
                await client.sign_in(phone=phone, code=code)
            except Exception:
                password = input("该账号开启了二步验证，请输入密码: ").strip()
                await client.sign_in(password=password)

        rows = await collect_json_messages(client, cfg["group"], args.keyword)
        print_rows(rows)

        if args.csv:
            export_csv(rows, Path(args.csv))

        if args.download and rows:
            await download_rows(rows, Path(args.download_dir))


def main() -> None:
    parser = argparse.ArgumentParser(description="在 Telegram 群组中查找 .json 文件")
    parser.add_argument("--keyword", "-k", help="按文件名关键词过滤，如 config")
    parser.add_argument("--csv", help="导出结果到 CSV 文件")
    parser.add_argument("--download", action="store_true", help="下载所有匹配的 .json")
    parser.add_argument(
        "--download-dir",
        default=str(DEFAULT_DOWNLOAD_DIR),
        help=f"下载目录，默认 {DEFAULT_DOWNLOAD_DIR}",
    )
    args = parser.parse_args()

    if args.csv is None and not args.download:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        args.csv = str(ROOT / f"telegram_json_list_{ts}.csv")

    import asyncio

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
