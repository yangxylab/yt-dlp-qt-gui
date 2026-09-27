"""Bilibili 弹幕下载模块

B站弹幕获取链路：
    1. 通过 cid（B 站分 P / 分集的唯一弹幕 ID）请求 XML
       - endpoint: https://api.bilibili.com/x/v1/dm/list.so?oid={cid}
       - 返回 gzip/deflate 压缩的 XML <i><d p="...">内容</d></i>
       - curl_cffi 自动解压 Content-Encoding，直接读 r.text 就是 UTF-8 XML
    2. 调用方可以自己传 cid，也可以传 B 站 URL 让模块先解出 cid

本模块只关心"给定 cid → 把弹幕 XML 写到目标目录"这件事，
不依赖 PySide / 下载队列，独立可测。
"""

from __future__ import annotations

import os
import re
from typing import Optional
from urllib.parse import urlparse

from curl_cffi import requests

BILIBILI_API_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36 Edg/128.0.0.0"
    ),
    "Referer": "https://www.bilibili.com/",
}


def is_bilibili_url(url: str) -> bool:
    """判断一个 URL 是不是 B 站（包括 bilibili.com / bilibili.cn）"""
    try:
        host = urlparse(url).netloc.lower()
        return "bilibili" in host
    except Exception:
        return False


def extract_bvid_ep_id(url: str) -> tuple[Optional[str], Optional[str]]:
    """从 B 站 URL 里提取 bvid 或 ep_id（bangumi 分集的播放 ID）

    支持的形态：
        https://www.bilibili.com/video/BV1HEf2YWEvs          → (BV1HEf2YWEvs, None)
        https://www.bilibili.com/video/BV1HEf2YWEvs?p=2      → (BV1HEf2YWEvs, None)
        https://www.bilibili.com/bangumi/play/ep780621       → (None, ep780621)
        https://www.bilibili.com/bangumi/media/md21095734    → (None, None) 没有直接 id
        https://www.bilibili.com/list/...                    → (None, None)
    """
    m_bv = re.search(r"(BV[0-9A-Za-z]{10})", url)
    if m_bv:
        return m_bv.group(1), None

    m_ep = re.search(r"(ep\d+)", url, re.IGNORECASE)
    if m_ep:
        return None, m_ep.group(1).lower()

    return None, None


def resolve_cid(bvid: Optional[str], ep_id: Optional[str], page: int = 1,
                cookies: Optional[dict] = None) -> Optional[int]:
    """调 B 站 API 把 bvid / ep_id + page → cid

    失败（API 挂、返回非 0 code、URL 拿不到任何 id）返回 None。
    """
    if not bvid and not ep_id:
        return None

    kwargs = {"headers": BILIBILI_API_HEADERS, "impersonate": "edge101"}
    if cookies:
        kwargs["cookies"] = cookies

    try:
        if bvid:
            r = requests.get(
                f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
                **kwargs,
            )
            data = r.json()
            if data.get("code") != 0:
                return None
            pages = data.get("data", {}).get("pages", [])
            idx = max(0, min(page - 1, len(pages) - 1))
            return pages[idx].get("cid")

        if ep_id:
            # bangumi season API
            r = requests.get(
                "https://api.bilibili.com/pgc/view/web/season",
                params={"ep_id": int(ep_id[2:])},
                **kwargs,
            )
            data = r.json()
            if data.get("code") != 0:
                return None
            ep = data.get("result", {}).get("ep")
            if ep:
                return ep.get("cid")
            # 某些场景下 ep 列表在 result.episodes 里
            episodes = data.get("result", {}).get("episodes", [])
            if episodes:
                return episodes[0].get("cid")
    except Exception:
        return None

    return None


def download_danmaku(cid: int, out_path: str,
                     cookies: Optional[dict] = None) -> Optional[str]:
    """下载 B 站弹幕 XML 到指定路径。

    注意：curl_cffi 会根据 Content-Encoding 自动解压 gzip/deflate，
    直接读 r.text 就是 UTF-8 XML 文本（bilibili 永远声明 encoding="UTF-8"）。
    """
    if not cid:
        return None

    # 规范化 out_path：确保扩展名是 .xml，同时保留 .danmaku.xml 这种 player 友好形式
    if not out_path.endswith(".xml"):
        out_path = out_path + ".danmaku.xml"

    try:
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    except Exception:
        return None

    kwargs = {"headers": BILIBILI_API_HEADERS, "impersonate": "edge101"}
    if cookies:
        kwargs["cookies"] = cookies

    try:
        r = requests.get(
            f"https://api.bilibili.com/x/v1/dm/list.so?oid={cid}",
            **kwargs,
        )
        if r.status_code != 200:
            return None

        # curl_cffi 自动解压 Content-Encoding（gzip / deflate），r.text 就是 UTF-8 XML
        xml_text = r.text
        if not xml_text.lstrip().startswith("<?xml") and not xml_text.lstrip().startswith("<i"):
            # 不是 XML → 可能 API 被风控，直接放弃
            return None

        with open(out_path, "w", encoding="utf-8") as f:
            f.write(xml_text)

        return out_path
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 高层便利函数：URL + 页面序号 → cid → 下载
# ---------------------------------------------------------------------------

def download_danmaku_from_url(url: str, out_dir: str,
                              filename_base: Optional[str] = None,
                              page: int = 1,
                              cookies: Optional[dict] = None) -> Optional[str]:
    """完整链路：URL → cid → 下载弹幕。返回写入文件路径（失败 None）。"""
    if not is_bilibili_url(url):
        return None

    bvid, ep_id = extract_bvid_ep_id(url)
    cid = resolve_cid(bvid, ep_id, page=page, cookies=cookies)
    if not cid:
        return None

    base = filename_base or f"bilibili_{cid}"
    out = os.path.join(out_dir, base + ".danmaku.xml")
    return download_danmaku(cid, out, cookies=cookies)
