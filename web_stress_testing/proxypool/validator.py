# -*- coding: utf-8 -*-
"""异步并发校验器：验证代理连通性/延迟/出口 IP，并维护"只保留有效"的打分规则。"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Dict, List, Optional

import aiohttp

from web_stress_testing.proxypool.config import ProxyPoolConfig


def _extract_ip(text: str) -> str:
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            origin = data.get("origin") or data.get("ip")
            if origin:
                return str(origin).split(",")[0].strip()
    except Exception:
        pass
    m = re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", text)
    return m.group(1) if m else ""


def proxy_url(proxy: Dict[str, Any]) -> str:
    proto = proxy.get("protocol") or "http"
    if proto not in ("http", "https"):
        proto = "http"
    return f"{proto}://{proxy['ip']}:{proxy['port']}"


async def validate_one(cfg: ProxyPoolConfig, proxy: Dict[str, Any]) -> Dict[str, Any]:
    """校验单个代理，返回 {ok, latency_ms, exit_ip, error}。"""
    url = proxy_url(proxy)
    t0 = time.perf_counter()
    ok, latency, exit_ip, error = False, 0, None, ""
    if proxy.get("protocol") in ("socks5", "socks4"):
        return {"ok": False, "latency_ms": 0, "exit_ip": None,
                "error": "socks 协议暂不支持，需要 aiohttp_socks"}
    try:
        timeout = aiohttp.ClientTimeout(total=cfg.timeout)
        async with aiohttp.ClientSession(timeout=timeout, connector=None) as s:
            async with s.get(cfg.test_url, proxy=url, ssl=False) as r:
                await r.read()
                status = r.status
            latency = int((time.perf_counter() - t0) * 1000)
            if status < 400:
                ok = True
            else:
                error = f"HTTP {status}"
            if ok and cfg.ip_api:
                try:
                    async with s.get(cfg.ip_api, proxy=url, ssl=False) as r2:
                        exit_ip = _extract_ip(await r2.text())
                except Exception:
                    pass
    except Exception as e:
        error = f"{type(e).__name__}: {str(e)[:120]}"
        latency = int((time.perf_counter() - t0) * 1000)
    return {"ok": ok, "latency_ms": latency, "exit_ip": exit_ip, "error": error}


async def validate_batch(cfg: ProxyPoolConfig, proxies: List[Dict[str, Any]],
                         log=None) -> List[Dict[str, Any]]:
    """并发校验一批代理，返回与输入等长的结果列表。"""
    sem = asyncio.Semaphore(max(1, cfg.validate_concurrency))

    async def one(p: Dict[str, Any]) -> Dict[str, Any]:
        async with sem:
            return await validate_one(cfg, p)

    results = await asyncio.gather(*[one(p) for p in proxies], return_exceptions=True)
    out = []
    for p, r in zip(proxies, results):
        if isinstance(r, Exception):
            out.append({"ok": False, "latency_ms": 0, "exit_ip": None, "error": f"{type(r).__name__}"})
        else:
            out.append(r)
        out[-1]["proxy"] = p
    return out


async def validate_and_apply(cfg: ProxyPoolConfig, storage, proxies: List[Dict[str, Any]],
                             log=None, source_hint: str = "") -> Dict[str, Any]:
    """校验一批代理并立即写回存储（只保留有效），返回统计。"""
    if not proxies:
        return {"total": 0, "ok": 0, "fail": 0, "deleted": 0}
    results = await validate_batch(cfg, proxies, log)
    stats = {"total": len(results), "ok": 0, "fail": 0, "deleted": 0}
    apply = []

    def _apply():
        for res in results:
            p = res["proxy"]
            if res["ok"]:
                stats["ok"] += 1
                status = storage.update_result(p["ip"], int(p["port"]), True, res["latency_ms"])
                storage.add(p["ip"], int(p["port"]), p.get("protocol", "http"), p.get("source", source_hint or "manual"))
            else:
                stats["fail"] += 1
                status = storage.update_result(p["ip"], int(p["port"]), False, res["latency_ms"])
            if status == "deleted":
                stats["deleted"] += 1

    import asyncio
    await asyncio.to_thread(_apply)
    return stats
