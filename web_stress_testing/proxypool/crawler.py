# -*- coding: utf-8 -*-
"""免费代理源爬虫（可插拔，默认开启 kuaidaili/ip3366/66ip；抓取结果需经校验入库）。"""
from __future__ import annotations

import re
from typing import Callable, Dict, List, Tuple

import aiohttp

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


async def _fetch(url: str, timeout: float = 12.0, proxy: str = "") -> str:
    headers = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
               "Accept-Language": "zh-CN,zh;q=0.9"}
    timeout_ = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(timeout=timeout_, headers=headers) as s:
        async with s.get(url, proxy=proxy or None, ssl=False) as r:
            return await r.text(errors="ignore")


def _parse_kuaidaili(html: str) -> List[Tuple[str, int]]:
    out = []
    pat = r'<td data-title="IP">([\d.]+)</td>\s*<td data-title="PORT">(\d+)</td>'
    for m in re.finditer(pat, html):
        out.append((m.group(1), int(m.group(2))))
    return out


def _parse_ip3366(html: str) -> List[Tuple[str, int]]:
    out = []
    pat = r'<td>(\d{1,3}(?:\.\d{1,3}){3})</td>\s*<td>(\d+)</td>'
    for m in re.finditer(pat, html):
        out.append((m.group(1), int(m.group(2))))
    return out


def _parse_ip66(html: str) -> List[Tuple[str, int]]:
    out = []
    pat = r'(\d{1,3}(?:\.\d{1,3}){3}):(\d+)'
    for m in re.finditer(pat, html):
        out.append((m.group(1), int(m.group(2))))
    return out[:200]


# 可插拔源定义：名称 -> (URL 列表, 解析函数)
SOURCES: Dict[str, Tuple[List[str], Callable[[str], List[Tuple[str, int]]]]] = {
    "kuaidaili": (
        [f"https://www.kuaidaili.com/free/inha/{i}/" for i in range(1, 3)],
        _parse_kuaidaili,
    ),
    "ip3366": (
        ["http://www.ip3366.net/free/?stype=1", "http://www.ip3366.net/free/?stype=2"],
        _parse_ip3366,
    ),
    "66ip": (
        ["http://www.66ip.cn/mo.php?tqsl=200", "http://www.66ip.cn/areaindex_1/1.html"],
        _parse_ip66,
    ),
}


class ProxyCrawler:
    """免费源抓取器：逐源抓取+解析+去重，单个源失败不影响其他源。"""

    def __init__(self, proxy_for_crawl: str = ""):
        self.proxy_for_crawl = proxy_for_crawl  # 抓取代理站本身时使用的代理（可选）

    async def crawl(self, names: List[str]) -> List[Dict]:
        found: Dict[Tuple[str, int], None] = {}
        results = []
        for name in names:
            spec = SOURCES.get(name)
            if not spec:
                continue
            urls, parser = spec
            for url in urls:
                try:
                    html = await _fetch(url, proxy=self.proxy_for_crawl)
                    for ip, port in parser(html):
                        if (ip, port) not in found:
                            found[(ip, port)] = None
                            results.append({"ip": ip, "port": port, "protocol": "http", "source": f"crawler:{name}"})
                except Exception:
                    continue
        return results