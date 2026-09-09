# -*- coding: utf-8 -*-
"""免费代理源爬虫（覆盖 jhao104/proxy_pool fetcher/sources 全部免费源）。

特性：
- 自动翻页：各源自动获取后续页（kuaidaili/ip89/geonode/roundproxies/scdn/zdaye 等）
- 并发抓取：源与源之间并发，页与页之间并发（受信号量限制）
- 自代理爬取：抓代理站时优先使用代理池内的有效 IP 转发请求，降低被反爬/封本机 IP 的风险；
  池为空或 PROXYPOOL_CRAWL_USE_POOL=0 时自动退化为直连
- 无效源自动过滤：连续失败（网络异常/0 条）达到阈值后冷却禁用，到期自动恢复

解析规则参考 jhao104/proxy_pool（fetcher/sources，MIT）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

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


def _valid(ip: str, port) -> bool:
    return bool(ip and IP_RE.match(ip or "") and str(port).isdigit() and 1 <= int(port) <= 65535)


def _rows(pairs: List[Tuple[str, int]], source: str) -> List[Dict[str, Any]]:
    return [{"ip": ip, "port": int(port), "protocol": "http", "source": f"crawler:{source}"}
            for ip, port in pairs if _valid(ip, port)]


class ProxyCrawler:
    """抓取器：并发多源 + 自动翻页 + 自代理 + 无效源自动过滤。"""

    FAIL_THRESHOLD = 2        # 连续失败（网络异常或 0 结果）达到该次数 → 冷却禁用
    COOLDOWN_SECONDS = 3600   # 冷却时长（秒）
    PROXY_FAIL_THRESHOLD = 2  # 供爬取使用的池内代理连续失败次数 → 暂时弃用
    PROXY_BAN_SECONDS = 600   # 差代理弃用时长（秒）

    def __init__(self,
                 proxy_provider: Optional[Callable[[], Awaitable[List[str]]]] = None,
                 max_pages: int = 3,
                 source_concurrency: int = 5,
                 page_concurrency: int = 2,
                 use_pool: bool = True,
                 proxy_attempts: int = 2):
        """
        proxy_provider: 异步回调，返回可用代理 URL 列表（如 ["http://ip:port"]）；
                        返回空/None 表示直连（池空时自动退化）。
        max_pages:       每个滚动分页源最多翻几页。
        """
        self.proxy_provider = proxy_provider if use_pool else None
        self.max_pages = max(1, int(max_pages))
        self.source_concurrency = max(1, int(source_concurrency))
        self.page_concurrency = max(1, int(page_concurrency))
        self.proxy_attempts = max(0, int(proxy_attempts))
        self._proxies: List[str] = []
        self._pindex = 0
        self._proxy_fails: Dict[str, int] = {}
        self._bad_proxies: Dict[str, float] = {}
        self._health: Dict[str, Dict[str, Any]] = {
            name: {"fails": 0, "disabled_until": 0.0, "found": 0, "last_error": ""}
            for name in SOURCES
        }

    # ------------------------------------------------------------------
    # 代理轮换：做代理站抓取时，用池内有效 IP 转发，防本机 IP 被反爬
    # ------------------------------------------------------------------
    def _is_bad_proxy(self, proxy: str, now: float) -> bool:
        return self._bad_proxies.get(proxy, 0.0) > now

    async def _pick_proxy(self) -> Optional[str]:
        now = time.time()
        if self.proxy_provider is not None and (
                not self._proxies or all(self._is_bad_proxy(p, now) for p in self._proxies)):
            try:
                self._proxies = [p for p in (await self.proxy_provider()) if p]
            except Exception as e:
                log.warning("获取池内代理失败，本轮直连: %s", e)
                self._proxies = []
        alive = [p for p in self._proxies if not self._is_bad_proxy(p, now)]
        if not alive:
            return None  # 直连
        p = alive[self._pindex % len(alive)]
        self._pindex += 1
        return p

    def _note_proxy_fail(self, proxy: str, error: Exception) -> None:
        fails = self._proxy_fails.get(proxy, 0) + 1
        self._proxy_fails[proxy] = fails
        if fails >= self.PROXY_FAIL_THRESHOLD:
            self._bad_proxies[proxy] = time.time() + self.PROXY_BAN_SECONDS
            log.info("爬取代理 %s 连续失败 %d 次，暂时弃用 %d 秒（%s）",
                     proxy, fails, self.PROXY_BAN_SECONDS, str(error)[:60])

    # ------------------------------------------------------------------
    # 抓取工具
    # ------------------------------------------------------------------
    async def _get_text(self, session: aiohttp.ClientSession, url: str, timeout: float = 10.0,
                        ssl: bool = False) -> str:
        """逐请求多代理尝试 + 直连兜底：不让一个坏代理毁掉整个源。"""
        candidates = []
        seen = set()
        for _ in range(self.proxy_attempts):
            p = await self._pick_proxy()
            key = p or "\x00direct"
            if key not in seen:
                seen.add(key)
                candidates.append(p)
        if None not in candidates:
            candidates.append(None)  # 直连兜底
        last_exc: Optional[Exception] = None
        for proxy in candidates:
            if proxy is not None and self._is_bad_proxy(proxy, time.time()):
                continue
            try:
                async with session.get(url, proxy=proxy, ssl=ssl,
                                       timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                    if r.status >= 400:
                        raise aiohttp.ClientResponseError(
                            r.request_info, r.history,
                            status=r.status, message=f"HTTP {r.status}")
                    text = await r.text(errors="ignore")
                    if not text.strip():
                        raise RuntimeError("empty response body")
                    return text
            except asyncio.CancelledError:
                raise
            except Exception as e:
                last_exc = e
                if proxy is not None:
                    self._note_proxy_fail(proxy, e)
        assert last_exc is not None
        raise last_exc

    async def _get_json(self, session: aiohttp.ClientSession, url: str, timeout: float = 10.0,
                        ssl: bool = False):
        return json.loads(await self._get_text(session, url, timeout, ssl))

    async def _fetch_pages(self, session: aiohttp.ClientSession, urls: List[str],
                           parser: Callable[[str], List[Tuple[str, int]]],
                           timeout: float = 10.0, ssl: bool = False,
                           page_sleep: float = 0.0) -> List[Tuple[str, int]]:
        """抓取多页：page_sleep>0 时顺序（站点要求节流），否则页间并发。"""
        urls = [u for u in urls if u]
        if not urls:
            return []
        if page_sleep > 0:
            pairs: List[Tuple[str, int]] = []
            for u in urls:
                try:
                    pairs += parser(await self._get_text(session, u, timeout, ssl))
                    await asyncio.sleep(page_sleep)
                except Exception:
                    continue
            return pairs
        sem = asyncio.Semaphore(self.page_concurrency)

        async def one(u: str):
            async with sem:
                return parser(await self._get_text(session, u, timeout, ssl))

        results = await asyncio.gather(*[one(u) for u in urls], return_exceptions=True)
        pairs = []
        for r in results:
            if isinstance(r, Exception):
                continue
            pairs += r
        return pairs

    # ------------------------------------------------------------------
    # 各源实现（返回 (ip, port) 列表）
    # ------------------------------------------------------------------
    async def _daili66(self, session):
        r = await self._get_json(session, "http://api.66daili.com/?format=json", timeout=10)
        return [(e.get("ip"), e.get("port")) for e in (r.get("data") or [])]

    async def _docip(self, session):
        r = await self._get_json(session, "https://www.docip.net/data/free.json", timeout=10)
        pairs = []
        for e in (r.get("data") or []):
            ip, port = e.get("ip"), e.get("port")
            if ip and not str(ip).isdigit():
                ip, _, port = str(ip).partition(":")
            pairs.append((ip, port))
        return pairs

    async def _freevpnnode(self, session):
        urls = ["https://cn.freevpnnode.com/free-proxy/"]
        for i in range(2, self.max_pages + 1):
            urls.append(f"https://cn.freevpnnode.com/free-proxy/{i}/")

        def parser(text):
            pairs = []
            for tr in re.findall(r"<tr>(.*?)</tr>", text, re.S):
                cells = [re.sub(r"<[^>]+>", "", c.strip())
                         for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
                if len(cells) >= 2 and _valid(cells[0], cells[1]):
                    pairs.append((cells[0], int(cells[1])))
            pairs += parse_proxies_from_text(text)
            return pairs
        return await self._fetch_pages(session, urls, parser, timeout=8)

    async def _geonode(self, session):
        urls = [
            "https://proxylist.geonode.com/api/proxy-list?filterLastChecked=10&page="
            f"{i}&limit=100&sort_by=lastChecked&sort_type=desc"
            for i in range(1, self.max_pages + 1)
        ]

        async def one(u):
            r = await self._get_json(session, u, timeout=8)
            return [(e.get("ip"), e.get("port")) for e in (r.get("data") or [])]
        results = await asyncio.gather(*[one(u) for u in urls], return_exceptions=True)
        pairs = [p for r in results if not isinstance(r, Exception) for p in r]
        return pairs

    async def _goodips(self, session):
        text = await self._get_text(session, "https://www.goodips.com/", timeout=8)
        pairs = []
        for item in re.findall(r"<div class=['\"]table-list['\"].*?</div>", text, re.S):
            nums = [n.strip() for n in re.findall(r"<li[^>]*>([^<]+)</li>", item)]
            if len(nums) >= 2 and _valid(nums[0], nums[1]):
                pairs.append((nums[0], int(nums[1])))
        return pairs

    async def _ihuan(self, session):
        await self._get_text(session, "https://ip.ihuan.me/", timeout=8)  # 取 cookie
        text = await self._get_text(session, "https://ip.ihuan.me/", timeout=8)
        pairs = []
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", text, re.S):
            cells = [c.strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
            merge = lambda c: re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", c))
            if len(cells) >= 2 and _valid(merge(cells[0]), merge(cells[1])):
                pairs.append((merge(cells[0]), int(merge(cells[1]))))
        return pairs

    async def _ip3366(self, session):
        urls = [f"http://www.ip3366.net/free/?stype={s}" for s in (1, 2)]

        def parser(text):
            return [(m.group(1), int(m.group(2))) for m in
                    re.finditer(r"<td>(\d{1,3}(?:\.\d{1,3}){3})</td>\s*<td>(\d+)</td>", text)]
        return await self._fetch_pages(session, urls, parser, timeout=10)

    async def _ip89(self, session):
        urls = [f"https://www.89ip.cn/index_{i}.html" for i in range(1, self.max_pages + 1)]

        def parser(text):
            return [(m.group(1), int(m.group(2))) for m in
                    re.finditer(r"<td[^>]*>\s*(\d{1,3}(?:\.\d{1,3}){3})\s*</td>\s*<td[^>]*>\s*(\d+)\s*</td>", text)]
        return await self._fetch_pages(session, urls, parser, timeout=10)

    async def _kuaidaili(self, session):
        urls = []
        for i in range(1, self.max_pages + 1):
            urls += [f"https://www.kuaidaili.com/free/inha/{i}/",
                     f"https://www.kuaidaili.com/free/intr/{i}/"]

        def parser(text):
            return [(m.group(1), int(m.group(2))) for m in
                    re.finditer(r"<td data-title=\"IP\">([\d.]+)</td>\s*<td data-title=\"PORT\">(\d+)</td>", text)]
        return await self._fetch_pages(session, urls, parser, timeout=10, page_sleep=1)

    async def _kxdaili(self, session):
        urls = ["http://www.kxdaili.com/dailiip.html",
                "http://www.kxdaili.com/dailiip/2/1.html"]

        def parser(text):
            pairs = []
            for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", text, re.S):
                if re.search(r"class=['\"]active['\"]", tr):
                    cells = [c.strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
                    if len(cells) >= 2 and _valid(cells[0], cells[1]):
                        pairs.append((cells[0], int(cells[1])))
            return pairs
        return await self._fetch_pages(session, urls, parser, timeout=10)

    async def _proxifly(self, session):
        url = "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/all/data.json"
        data = await self._get_json(session, url, timeout=12)
        pairs = []
        for e in data or []:
            if e.get("protocol") != "http":
                continue
            ip, _, port = (e.get("proxy") or "").partition(":")
            if _valid(ip, port):
                pairs.append((ip, int(port)))
        return pairs

    async def _roundproxies(self, session):
        urls = [
            "https://roundproxies.com/api/get-free-proxies/?limit=50&page="
            f"{i}&sort_by=lastChecked&sort_type=desc"
            for i in range(1, self.max_pages + 1)
        ]

        async def one(u):
            r = await self._get_json(session, u, timeout=10)
            return [(e.get("ip"), e.get("port")) for e in (r.get("data") or [])]
        results = await asyncio.gather(*[one(u) for u in urls], return_exceptions=True)
        return [p for r in results if not isinstance(r, Exception) for p in r]

    async def _scdn(self, session):
        urls = [
            f"https://proxy.scdn.io/get_proxies.php?protocol=&country=&per_page=100&page={i}"
            for i in range(1, self.max_pages + 1)
        ]

        async def one(u):
            text = await self._get_text(session, u, timeout=8)
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
            return pairs
        results = await asyncio.gather(*[one(u) for u in urls], return_exceptions=True)
        return [p for r in results if not isinstance(r, Exception) for p in r]

    async def _zdaye(self, session):
        pairs: List[Tuple[str, int]] = []
        home = await self._get_text(session, "https://www.zdaye.com/free/", timeout=10)
        hrefs = re.findall(r"<h3[^>]*class=['\"]thread_title['\"][^>]*>.*?<a[^>]+href=['\"]([^'\"]+)['\"]", home, re.S)
        if not hrefs:
            return pairs
        target = hrefs[0] if hrefs[0].startswith("http") else "https://www.zdaye.com/" + hrefs[0].lstrip("/")
        for _ in range(self.max_pages):
            text = await self._get_text(session, target, timeout=10)
            for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", text, re.S):
                cells = [c.strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
                if len(cells) >= 2 and _valid(cells[0], cells[1]):
                    pairs.append((cells[0], int(cells[1])))
            nxt = re.findall(r"<a[^>]+title=['\"]?下一页['\"]?[^>]+href=['\"]([^'\"]+)['\"]", text)
            if not nxt:
                break
            target = "https://www.zdaye.com/" + nxt[0].lstrip("/")
            await asyncio.sleep(5)
        return pairs

    async def _ip66(self, session):
        text = await self._get_text(session, "http://www.66ip.cn/mo.php?tqsl=200", timeout=10)
        return parse_proxies_from_text(text)

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    async def _crawl_source(self, session: aiohttp.ClientSession, name: str) -> List[Dict[str, Any]]:
        fn = SOURCES.get(name)
        if fn is None:
            log.warning("未知代理源: %s（已跳过）", name)
            return []
        now = time.time()
        if self._health[name]["disabled_until"] > now:
            log.info("代理源 %s 处于冷却禁用中（%s）", name,
                     time.strftime("%H:%M:%S", time.localtime(self._health[name]["disabled_until"])))
            return []
        try:
            pairs = await fn(self, session)
            rows = _rows(pairs, name)
            self._health[name]["found"] = len(rows)
            self._health[name]["last_error"] = ""
            if not rows:
                self._mark_fail(name, "抓取 0 条（站点无效/页面变化）")
                return []
            self._health[name]["fails"] = 0
            log.info("抓取 %s：获得 %d 个候选代理", name, len(rows))
            return rows
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._mark_fail(name, f"{type(e).__name__}: {str(e)[:100]}")
            return []

    async def crawl(self, names: List[str]) -> List[Dict[str, Any]]:
        sem = asyncio.Semaphore(self.source_concurrency)
        headers = {"User-Agent": UA, "Accept": "text/html,application/json,*/*;q=0.9"}

        async def one(name: str) -> List[Dict[str, Any]]:
            async with sem:
                return await self._crawl_source(session, name)

        async with aiohttp.ClientSession(headers=headers) as session:
            results = await asyncio.gather(*[one(n) for n in names], return_exceptions=True)

        seen = set()
        found: List[Dict[str, Any]] = []
        for r in results:
            if isinstance(r, Exception):
                continue
            for p in r:
                key = (p["ip"], p["port"])
                if key not in seen:
                    seen.add(key)
                    found.append(p)
        return found

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

    def source_status(self) -> List[Dict[str, Any]]:
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


# ---------------------------------------------------------------------------
# 源注册表
# ---------------------------------------------------------------------------
SOURCES: Dict[str, Callable[[ProxyCrawler, aiohttp.ClientSession], Any]] = {
    "kuaidaili": ProxyCrawler._kuaidaili,
    "ip3366": ProxyCrawler._ip3366,
    "ip89": ProxyCrawler._ip89,
    "kxdaili": ProxyCrawler._kxdaili,
    "daili66": ProxyCrawler._daili66,
    "docip": ProxyCrawler._docip,
    "freevpnnode": ProxyCrawler._freevpnnode,
    "geonode": ProxyCrawler._geonode,
    "goodips": ProxyCrawler._goodips,
    "ihuan": ProxyCrawler._ihuan,
    "proxifly": ProxyCrawler._proxifly,
    "roundproxies": ProxyCrawler._roundproxies,
    "scdn": ProxyCrawler._scdn,
    "zdaye": ProxyCrawler._zdaye,
    "66ip": ProxyCrawler._ip66,
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