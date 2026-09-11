# -*- coding: utf-8 -*-
"""浏览器加载期网络优化：挡广告/追踪、跟踪游戏相关在途请求。"""
from __future__ import annotations

from urllib.parse import urlparse

# 永不拦截（游戏 / DMM / 必要 CDN）
_ALLOW_HOST_SUFFIXES = (
    "deepone-online.com",
    "dmm.co.jp",
    "dmm.com",
    "dmm.net",
    "dmmapis.com",
)

# 从 DO 登录 Network 导出归纳的广告/追踪域名（后缀匹配）
_BLOCK_HOST_SUFFIXES = (
    "ladsp.com",
    "im-apps.net",
    "shinobi.jp",
    "gsspat.jp",
    "bance.jp",
    "bing.com",
    "bing.net",
    "yahoo.co.jp",
    "yimg.jp",
    "healthroundprince.com",
    "genieedmp.com",
    "i-mobile.co.jp",
    "ad-stir.com",
    "fam-8.net",
    "zucks.net",
    "doubleclick.net",
    "googlesyndication.com",
    "googleadservices.com",
    "googletagmanager.com",
    "google-analytics.com",
    "facebook.net",
    "facebook.com",
    "scorecardresearch.com",
    "taboola.com",
    "outbrain.com",
    "criteo.com",
    "adsrvr.org",
    "moatads.com",
    "amazon-adsystem.com",
    "adnxs.com",
    "pubmatic.com",
    "openx.net",
    "rubiconproject.com",
    "casalemedia.com",
    "exelator.com",
    "adsafeprotected.com",
    "2mdn.net",
)

# 纯 host 精确/后缀；gstatic 过宽不挡整站
_BLOCK_HOST_EXACT_SKIP_PREFIX = ("gstatic.com", "googleapis.com", "google.com")


def _host(url: str) -> str:
    try:
        return (urlparse(url or "").hostname or "").lower()
    except Exception:
        return ""


def is_allowed_host(host: str) -> bool:
    h = (host or "").lower()
    if not h:
        return False
    return any(h == s or h.endswith("." + s) for s in _ALLOW_HOST_SUFFIXES)


def is_ad_or_tracker_host(host: str) -> bool:
    h = (host or "").lower()
    if not h or is_allowed_host(h):
        return False
    if any(h == s or h.endswith("." + s) for s in _BLOCK_HOST_EXACT_SKIP_PREFIX):
        return False
    for s in _BLOCK_HOST_SUFFIXES:
        if "." not in s:
            continue
        if h == s or h.endswith("." + s):
            return True
    return False


def should_block_url(url: str) -> bool:
    """自动化浏览器 route 拦截判定。"""
    u = (url or "").strip()
    if not u or u.startswith(("data:", "blob:", "about:", "chrome:", "devtools:")):
        return False
    return is_ad_or_tracker_host(_host(u))


def is_game_network_url(url: str) -> bool:
    """是否计入「游戏加载在途」：DeepOne API / 资源 CDN。"""
    h = _host(url)
    if not h:
        return False
    if "deepone-online.com" not in h:
        return False
    path = ""
    try:
        path = (urlparse(url).path or "").lower()
    except Exception:
        pass
    # 主站 API、CDN、资源清单
    if "/api/" in path or "download" in path or "cdn" in h:
        return True
    # 其它 deepone 子域也算（脚本/入口）
    return True


def url_path_key(url: str) -> str:
    """去 fragment、保留 path+排序后的 query 键，用于影子行合并。"""
    try:
        p = urlparse(url or "")
        return f"{p.scheme}://{p.netloc}{p.path}?{p.query}"
    except Exception:
        return url or ""
