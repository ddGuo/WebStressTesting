# -*- coding: utf-8 -*-
"""免费代理源爬虫（覆盖 jhao104/proxy_pool fetcher/sources 全部免费源）。

- 内置源：kuaidaili / ip3366 / ip89 / kxdaili / daili66 / docip / freevpnnode /
          geonode / goodips / ihuan / proxifly / roundproxies / scdn / zdaye / 66ip
- 解析规则参考 jhao104/proxy_pool（fetcher/sources，MIT）改写为 aiohttp/正则实现
- 无效源自动过滤：连续失败（网络异常或解析为空）达到阈值后进入冷却期，
  冷却期内不再抓取；通过 source_status() 可在日志/API 中查看各源健康状态。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Callable, Dict, List, Tuple

import aiohttp

log = logging.getLogger("proxypool")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

PROXY_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?:\s*:\s*|\s+)(\d{2,5})(?!\d)")
IP_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def parse_proxies_from_text(text: str) -> List[Tuple[str, int]]:
    """从任意文本中提取 ip:port 列表（兼容 host:port / host port）。"""
    out = []
    if not text:
        return out
    for m in PROXY_RE.finditer(text):
        try:
            out.append((m.group(1), int(m.group(2))))
        except ValueError:
            continue
    return out


# ---------------------------------------------------------------------------
# 抓取小工具
# ---------------------------------------------------------------------------
async def _get_text(session: aiohttp.ClientSession, url: str, timeout: float = 12.0,
                    ssl: bool = False) -> str:
    async with session.get(url, ssl=ssl, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
        return await r.text(errors="ignore")


async def _get_json(session: aiohttp.ClientSession, url: str, timeout: float = 12.0,
                    ssl: bool = False):
    text = await _get_text(session, url, timeout, ssl)
    return json.loads(text)


def _valid(ip: str, port) -> bool:
    return bool(ip and IP_RE.match(ip or "") and str(port).isdigit() and 1 <= int(port) <= 65535)


def _rows(ip_port_pairs: List[Tuple[str, int]], source: str) -> List[Dict[str, Any]]:
    out = []
    for ip, port in ip_port_pairs:
        if _valid(ip, port):
            out.append({"ip": ip, "port": int(port), "protocol": "http", "source": f"crawler:{source}"})
    return out


# ---------------------------------------------------------------------------
# 各免费源实现（async fetch(session) -> List[Dict]）
# ---------------------------------------------------------------------------
async def _daili66(session):
    """66代理（api.66daili.com JSON）"""
    r = await _get_json(session, "http://api.66daili.com/?format=json", timeout=10)
    pairs = [(e.get("ip"), e.get("port")) for e in (r.get("data") or [])]
    return _rows([(ip, port) for ip, port in pairs], "daili66")


async def _docip(session):
    """稻壳代理（docip.net JSON）"""
    r = await _get_json(session, "https://www.docip.net/data/free.json", timeout=10)
    pairs = []
    for e in (r.get("data") or []):
        ip = e.get("ip")
        port = e.get("port")
        if ip and not str(ip).isdigit():  # docip 的 data 可能是 "ip:port" 字符串
            ip, _, port = str(ip).partition(":")
        pairs.append((ip, port))
    return _rows([(ip, port) for ip, port in pairs], "docip")


async def _freevpnnode(session):
    """FreeVPNNode（HTML 表格 + 文本兜底）"""
    text = await _get_text(session, "https://cn.freevpnnode.com/free-proxy/", timeout=8, ssl=False)
    pairs = []
    for tr in re.findall(r"<tr>(.*?)</tr>", text, re.S):
        cells = [c.strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        cells = [re.sub(r"<[^>]+>", "", c) for c in cells]
        if len(cells) >= 2 and _valid(cells[0], cells[1]):
            pairs.append((cells[0], int(cells[1])))
    pairs += parse_proxies_from_text(text)
    return _rows(pairs, "freevpnnode")


async def _geonode(session):
    """Geonode（proxylist.geonode.com JSON）"""
    url = ("https://proxylist.geonode.com/api/proxy-list?"
           "filterLastChecked=10&page=1&limit=100&sort_by=lastChecked&sort_type=desc")
    r = await _get_json(session, url, timeout=8, ssl=False)
    pairs = [(e.get("ip"), e.get("port")) for e in (r.get("data") or [])]
    return _rows([(ip, port) for ip, port in pairs], "geonode")


async def _goodips(session):
    """谷德代理（goodips.com HTML）"""
    text = await _get_text(session, "https://www.goodips.com/", timeout=8, ssl=False)
    pairs = []
    for item in re.findall(r"<div class=['\"]table-list['\"].*?</div>", text, re.S):
        ip = "".join(re.findall(r"<li[^>]*>([\d.]+)</li>", item))[:2]
        nums = re.findall(r"<li[^>]*>([^<]+)</li>", item)
        if len(nums) >= 2 and _valid(nums[0].strip(), nums[1].strip()):
            pairs.append((nums[0].strip(), int(nums[1].strip())))
    return _rows(pairs, "goodips")


async def _ihuan(session):
    """小幻代理（ip.ihuan.me，需要先取 cookie）"""
    await _get_text(session, "https://ip.ihuan.me/", timeout=8, ssl=False)  # 首次请求种 cookie
    text = await _get_text(session, "https://ip.ihuan.me/", timeout=8, ssl=False)
    pairs = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", text, re.S):
        cells = [c.strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        merge = lambda c: re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", c))
        if len(cells) >= 2 and _valid(merge(cells[0]), merge(cells[1])):
            pairs.append((merge(cells[0]), int(merge(cells[1]))))
    return _rows(pairs, "ihuan")


async def _ip3366(session):
    """云代理（ip3366.net HTML 正则）"""
    pairs = []
    for page in ("1", "2"):
        url = f"http://www.ip3366.net/free/?stype={page}"
        text = await _get_text(session, url, timeout=10)
        pairs += [(m.group(1), int(m.group(2))) for m in
                  re.finditer(r"<td>(\d{1,3}(?:\.\d{1,3}){3})</td>\s*<td>(\d+)</td>", text)]
    return _rows(pairs, "ip3366")


async def _ip89(session):
    """89免费代理（89ip.cn HTML 正则）"""
    text = await _get_text(session, "https://www.89ip.cn/index_1.html", timeout=10, ssl=False)
    pairs = [(m.group(1), int(m.group(2))) for m in
             re.finditer(r"<td[^>]*>\s*(\d{1,3}(?:\.\d{1,3}){3})\s*</td>\s*<td[^>]*>\s*(\d+)\s*</td>", text)]
    return _rows(pairs, "ip89")


async def _kuaidaili(session):
    """快代理（kuaidaili.com，页间 sleep 1s）"""
    pairs = []
    for pattern in ("https://www.kuaidaili.com/free/inha/1/",
                    "https://www.kuaidaili.com/free/intr/1/"):
        text = await _get_text(session, pattern, timeout=10, ssl=False)
        for m in re.finditer(r'<td data-title="IP">([\d.]+)</td>\s*<td data-title="PORT">(\d+)</td>', text):
            pairs.append((m.group(1), int(m.group(2))))
        await asyncio.sleep(1)
    return _rows(pairs, "kuaidaili")


async def _kxdaili(session):
    """开心代理（kxdaili.com HTML 表格）"""
    pairs = []
    for url in ("http://www.kxdaili.com/dailiip.html",
                "http://www.kxdaili.com/dailiip/2/1.html"):
        text = await _get_text(session, url, timeout=10)
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", text, re.S):
            if re.search(r"class=['\"]active['\"]", tr):
                cells = [c.strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
                if len(cells) >= 2 and _valid(cells[0], cells[1]):
                    pairs.append((cells[0], int(cells[1])))
    return _rows(pairs, "kxdaili")


async def _proxifly(session):
    """Proxifly（jsdelivr JSON，仅 CN + http）"""
    url = "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/all/data.json"
    data = await _get_json(session, url, timeout=12, ssl=False)
    pairs = []
    for e in data or []:
        if e.get("protocol") != "http":
            continue
        proxy = e.get("proxy") or ""
        ip, _, port = proxy.partition(":")
        if _valid(ip, port):
            pairs.append((ip, int(port)))
    return _rows(pairs, "proxifly")


async def _roundproxies(session):
    """Roundproxies（JSON API）"""
    url = ("https://roundproxies.com/api/get-free-proxies/"
           "?limit=50&page=1&sort_by=lastChecked&sort_type=desc")
    r = await _get_json(session, url, timeout=10, ssl=False)
    pairs = [(e.get("ip"), e.get("port")) for e in (r.get("data") or [])]
    return _rows([(ip, port) for ip, port in pairs], "roundproxies")


async def _scdn(session):
    """SCDN（JSON：table_html / data 兜底，再兜底文本）"""
    url = "https://proxy.scdn.io/get_proxies.php?protocol=&country=&per_page=100&page=1"
    text = await _get_text(session, url, timeout=8, ssl=False)
    pairs = []
    try:
        data = json.loads(text)
        table_html = data.get("table_html") if isinstance(data, dict) else ""
        if table_html:
            for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, re.S):
                cells = [c.strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
                if len(cells) >= 2 and _valid(cells[0], cells[1]):
                    pairs.append((cells[0], int(cells[1])))
        if isinstance(data, dict):
            for item in data.get("data") or []:
                if _valid(item.get("ip"), item.get("port")):
                    pairs.append((item["ip"], int(item["port"])))
    except Exception:
        pass
    pairs += parse_proxies_from_text(text)
    return _rows(pairs, "scdn")


async def _zdaye(session):
    """站大爷（zdaye.com：首页最新帖 → 详情页，最多 2 页，页间 sleep 5s）"""
    pairs = []
    home = await _get_text(session, "https://www.zdaye.com/free/", timeout=10, ssl=False)
    hrefs = re.findall(r"<h3[^>]*class=['\"]thread_title['\"][^>]*>.*?<a[^>]+href=['\"]([^'\"]+)['\"]", home, re.S)
    if not hrefs:
        return _rows(pairs, "zdaye")
    target = hrefs[0] if hrefs[0].startswith("http") else "https://www.zdaye.com/" + hrefs[0].lstrip("/")
    for page in range(2):
        text = await _get_text(session, target, timeout=10, ssl=False)
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", text, re.S)
        for tr in rows:
            cells = [c.strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
            if len(cells) >= 2 and _valid(cells[0], cells[1]):
                pairs.append((cells[0], int(cells[1])))
        nxt = re.findall(r"<a[^>]+title=['\"]?下一页['\"]?[^>]+href=['\"]([^'\"]+)['\"]", text)
        if not nxt or page == 1:
            break
        target = "https://www.zdaye.com/" + nxt[0].lstrip("/")
        await asyncio.sleep(5)
    return _rows(pairs, "zdaye")


async def _ip66(session):
    """66ip（旧 · www.66ip.cn 文本行）"""
    text = await _get_text(session, "http://www.66ip.cn/mo.php?tqsl=200", timeout=10, ssl=False)
    return _rows([(ip, int(port)) for ip, port in parse_proxies_from_text(text)], "66ip")


# ---------------------------------------------------------------------------
# 源注册表
# ---------------------------------------------------------------------------
SOURCES: Dict[str, Callable[[aiohttp.ClientSession], List[Dict[str, Any]]]] = {
    "kuaidaili": _kuaidaili,
    "ip3366": _ip3366,
    "ip89": _ip89,
    "kxdaili": _kxdaili,
    "daili66": _daili66,
    "docip": _docip,
    "freevpnnode": _freevpnnode,
    "geonode": _geonode,
    "goodips": _goodips,
    "ihuan": _ihuan,
    "proxifly": _proxifly,
    "roundproxies": _roundproxies,
    "scdn": _scdn,
    "zdaye": _zdaye,
    "66ip": _ip66,
}

SOURCE_HOMEPAGE: Dict[str, str] = {
    "kuaidaili": "https://www.kuaidaili.com", "ip3366": "http://www.ip3366.net/",
    "ip89": "https://www.89ip.cn/", "kxdaili": "http://www.kxdaili.com/dailiip.html",
    "daili66": "https://www.66daili.com", "docip": "https://www.docip.net/",
    "freevpnnode": "https://cn.freevpnnode.com", "geonode": "https://geonode.com/",
    "goodips": "https://www.goodips.com/", "ihuan": "https://ip.ihuan.me/",
    "proxifly": "https://proxifly.dev/", "roundproxies": "https://roundproxies.com/free-proxy-list",
    "scdn": "https://proxy.scdn.io/", "zdaye": "https://www.zdaye.com/dayProxy.html",
    "66ip": "http://www.66ip.cn/",
}


class ProxyCrawler:
    """抓取器：逐源抓取 + 解析 + 去重 + 无效源自动过滤（连续失败冷却禁用）。"""

    # 连续失败（网络异常或 0 结果）达到该次数 → 冷却禁用
    FAIL_THRESHOLD = 2
    # 冷却时长（秒）
    COOLDOWN_SECONDS = 3600

    def __init__(self, proxy_for_crawl: str = ""):
        self.proxy_for_crawl = proxy_for_crawl
        # name -> {"fails": 连续失败次数, "disabled_until": 冷却截止时间戳,
        #          "found": 最近一次抓取数量, "last_error": 最近错误}
        self._health: Dict[str, Dict[str, Any]] = {
            name: {"fails": 0, "disabled_until": 0.0, "found": 0, "last_error": ""}
            for name in SOURCES
        }

    # ------------------------------------------------------------------
    def _is_disabled(self, name: str, now: float) -> bool:
        return self._health[name]["disabled_until"] > now

    def source_status(self) -> List[Dict[str, Any]]:
        """返回各源健康状态（供日志/报告使用）。"""
        now = time.time()
        out = []
        for name in sorted(SOURCES):
            h = self._health[name]
            disabled = h["disabled_until"] > now
            out.append({
                "source": name,
                "homepage": SOURCE_HOMEPAGE.get(name, ""),
                "alive": not disabled,
                "disabling": (h["fails"] >= self.FAIL_THRESHOLD) and not disabled,
                "consecutive_fails": h["fails"],
                "disabled_until": h["disabled_until"] or None,
                "last_found": h["found"],
                "last_error": h["last_error"][:120] if h["last_error"] else "",
            })
        return out

    # ------------------------------------------------------------------
    async def crawl(self, names: List[str]) -> List[Dict[str, Any]]:
        """抓取指定源，返回去重后的代理字典列表。"""
        found_list: List[Dict[str, Any]] = []
        seen = set()
        headers = {"User-Agent": UA, "Accept": "text/html,application/json,*/*;q=0.9"}
        async with aiohttp.ClientSession(headers=headers) as session:
            for name in names:
                fn = SOURCES.get(name)
                if fn is None:
                    log.warning("未知代理源: %s（已跳过）", name)
                    continue
                now = time.time()
                if self._is_disabled(name, now):
                    log.info("代理源 %s 处于冷却禁用中（%s）", name,
                             time.strftime("%H:%M:%S", time.localtime(self._health[name]["disabled_until"])))
                    continue
                try:
                    proxies = await fn(session)
                    self._health[name]["found"] = len(proxies)
                    self._health[name]["last_error"] = ""
                    if len(proxies) == 0:
                        self._mark_fail(name, "抓取 0 条（站点无效/页面变化）")
                        continue
                    self._health[name]["fails"] = 0
                    log.info("抓取 %s：获得 %d 个候选代理", name, len(proxies))
                    for p in proxies:
                        key = (p["ip"], p["port"])
                        if key not in seen:
                            seen.add(key)
                            found_list.append(p)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._mark_fail(name, f"{type(e).__name__}: {str(e)[:100]}")
                    continue
        return found_list

    def _mark_fail(self, name: str, reason: str) -> None:
        h = self._health[name]
        h["fails"] += 1
        h["last_error"] = reason
        if h["fails"] >= self.FAIL_THRESHOLD:
            h["disabled_until"] = time.time() + self.COOLDOWN_SECONDS
            log.warning("代理源 %s 连续 %d 次无效，已自动过滤（冷却 %d 秒）：%s",
                        name, h["fails"], self.COOLDOWN_SECONDS, reason)
        else:
            log.info("代理源 %s 抓取失败（第 %d/%d 次）：%s",
                     name, h["fails"], self.FAIL_THRESHOLD, reason)