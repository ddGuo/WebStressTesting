# -*- coding: utf-8 -*-
"""压测端代理消费器：粘性分配（一个用户一个 IP）、故障换绑、使用统计。"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import aiohttp

from web_stress_testing.config import TestConfig


def normalize_proxy(text: str) -> str:
    """把 'ip:port' / 'http://ip:port' 规范化为 'http://ip:port'；非法返回 ''。"""
    text = (text or "").strip()
    if not text or text.startswith(("#", "//")):
        return ""
    if "://" in text:
        u = urlsplit(text)
        if u.hostname and u.port:
            return f"{u.scheme}://{u.hostname}:{u.port}"
        return ""
    if ":" in text:
        ip, _, port = text.rpartition(":")
        if ip and port.isdigit():
            return f"http://{ip}:{port}"
    return ""


def read_file_lines(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return [normalize_proxy(line) for line in f if normalize_proxy(line)]


async def fetch_from_api(api_url: str, count: int = 100) -> List[str]:
    """从池服务拉取有效代理（兼容 /get_all 与 /get?count=N）。"""
    base = api_url.rstrip("/")
    endpoints = [f"{base}/get_all", f"{base}/get?count={count}"]
    timeout = aiohttp.ClientTimeout(total=10)
    for ep in endpoints:
        try:
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(ep) as r:
                    data = await r.json()
            rows = data.get("data") or []
            out = []
            for row in rows:
                protocol = row.get("protocol") or "http"
                if protocol in ("socks4", "socks5"):
                    continue
                out.append(f"{protocol}://{row['ip']}:{row['port']}")
            if out:
                return out
        except Exception:
            continue
    return []


class ProxyAllocator:
    """压测进程内的代理分配器：粘性 = 每个用户创建时固定一个代理。"""

    def __init__(self, proxies: List[str], source: str, sticky: bool = True,
                 api_url: str = "", fail_threshold: int = 2):
        self._pool: List[str] = [p for p in proxies if p]
        self._source = source
        self._sticky = sticky
        self._api_url = api_url
        self._fail_threshold = max(1, int(fail_threshold))
        self._cursor = 0
        self._failed: set = set()
        self._fail_counts: Dict[str, int] = {}
        self._direct_fallbacks = 0
        self._refined_out = 0
        self._last_refill = 0.0
        self._stats: Dict[str, Dict[str, Any]] = {}

    @property
    def source(self) -> str:
        return self._source

    @property
    def size(self) -> int:
        return len(self._pool)

    @classmethod
    async def create(cls, config: TestConfig) -> "Optional[ProxyAllocator]":
        """按配置创建分配器；未配置任何代理来源时返回 None。"""
        proxies: List[str] = []
        source = ""
        api_url = ""
        if config.proxy:
            p = normalize_proxy(config.proxy)
            if p:
                proxies = [p]
            source = "single"
        elif config.proxy_file:
            lines = await asyncio.to_thread(read_file_lines, config.proxy_file)
            proxies = lines
            source = f"file:{config.proxy_file}"
        elif config.proxy_api:
            api_url = config.proxy_api.rstrip("/")
            proxies = await fetch_from_api(api_url, count=max(config.load.users, 1) * 2)
            source = f"api:{api_url}"
        else:
            return None
        return cls(proxies, source, sticky=config.proxy_sticky, api_url=api_url,
                  fail_threshold=config.proxy_fail_threshold)

    # ------------------------------------------------------------------
    def acquire_sync(self) -> Optional[str]:
        alive = [p for p in self._pool if p not in self._failed]
        if not alive:
            return None
        p = alive[self._cursor % len(alive)]
        self._cursor += 1
        return p

    async def acquire(self) -> Optional[str]:
        p = self.acquire_sync()
        if p is None and self._api_url:
            await self._refill()
            p = self.acquire_sync()
        if p is None:
            self._direct_fallbacks += 1
        return p

    async def _refill(self) -> None:
        now = time.monotonic()
        if now - self._last_refill < 30:
            return
        self._last_refill = now
        new = await fetch_from_api(self._api_url, count=200)
        if new:
            merged = list(dict.fromkeys(self._pool + new))
            self._pool = merged
            self._failed = {f for f in self._failed if f in self._pool}

    async def refine(self, check_url: str, timeout: float = 3.0,
                     concurrency: int = 80) -> int:
        """按目标地址预检：只保留对该目标可达的代理，返回剔除数量。"""
        alive = [p for p in self._pool if p not in self._failed]
        if not alive:
            return 0
        sem = asyncio.Semaphore(max(1, concurrency))
        timeout_ = aiohttp.ClientTimeout(total=max(1.0, timeout))

        async def ok(p: str) -> bool:
            async with sem:
                try:
                    async with aiohttp.ClientSession(timeout=timeout_) as s:
                        async with s.get(check_url, proxy=p, ssl=False) as r:
                            await r.read()
                            return r.status < 400
                except Exception:
                    return False

        results = await asyncio.gather(*[ok(p) for p in alive])
        dropped = {p for p, good in zip(alive, results) if not good}
        if dropped:
            self._pool = [p for p in self._pool if p not in dropped]
            self._failed = {f for f in self._failed if f in self._pool}
        self._refined_out += len(dropped)  # 预检剔除单独统计，不计入运行期 ok/fail
        return len(dropped)

    def mark_failed(self, proxy: str) -> None:
        """失败累计达阈值才本地剔除（避免一次抖动误杀）。"""
        if not proxy:
            return
        n = self._fail_counts.get(proxy, 0) + 1
        self._fail_counts[proxy] = n
        if n >= self._fail_threshold:
            self._failed.add(proxy)

    def record(self, proxy: str, ok: bool, ms: float) -> None:
        if proxy:
            self._record(proxy, ok, ms)

    def _record(self, proxy: str, ok: bool, ms: float) -> None:
        s = self._stats.setdefault(proxy, {"ok": 0, "fail": 0, "latency_sum": 0.0, "n": 0})
        s["ok" if ok else "fail"] += 1
        if ok:
            s["latency_sum"] += ms
            s["n"] += 1
            self._fail_counts[proxy] = 0

    def stats(self) -> Dict[str, Any]:
        total_ok = sum(s["ok"] for s in self._stats.values())
        total_fail = sum(s["fail"] for s in self._stats.values())
        rows = []
        for p in sorted(self._pool):
            s = self._stats.get(p, {"ok": 0, "fail": 0, "latency_sum": 0.0, "n": 0})
            rows.append({
                "proxy": p,
                "ok": s["ok"],
                "fail": s["fail"],
                "avg_ms": round(s["latency_sum"] / s["n"], 1) if s["n"] else 0.0,
                "failed_out": p in self._failed,
            })
        return {
            "enabled": True,
            "source": self._source,
            "sticky": self._sticky,
            "pool_size": self.size,
            "fail_threshold": self._fail_threshold,
            "direct_fallbacks": self._direct_fallbacks,
            "refined_out": self._refined_out,
            "total_ok": total_ok,
            "total_fail": total_fail,
            "proxies": rows,
        }
