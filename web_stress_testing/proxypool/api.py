# -*- coding: utf-8 -*-
"""代理池 HTTP API（aiohttp，端口默认 5010），接口与 jhao104/proxy_pool 兼容：
/get /get_all /count /get_status /pop /delete /add /healthz
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict

from aiohttp import web

from web_stress_testing.proxypool.config import ProxyPoolConfig
from web_stress_testing.proxypool.storage import BaseStorage
from web_stress_testing.proxypool.validator import validate_one


def _row_dict(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ip": row["ip"],
        "port": int(row["port"]),
        "protocol": row.get("protocol") or "http",
        "source": row.get("source") or "manual",
        "score": int(row.get("score") or 0),
        "latency_ms": int(row.get("latency_ms") or 0),
    }


def _json(data, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False))


def build_app(cfg: ProxyPoolConfig, storage: BaseStorage, scheduler=None) -> web.Application:
    app = web.Application()

    async def _get(request: web.Request) -> web.Response:
        try:
            count = int(request.query.get("count", 1))
        except ValueError:
            count = 1
        count = max(1, min(count, 500))
        rows = await asyncio.to_thread(storage.get_random, count)
        return _json({"count": len(rows), "data": [_row_dict(r) for r in rows]})

    async def _get_all(request: web.Request) -> web.Response:
        rows = await asyncio.to_thread(storage.get_all)
        return _json({"count": len(rows), "data": [_row_dict(r) for r in rows]})

    async def _count(request: web.Request) -> web.Response:
        n = await asyncio.to_thread(storage.count, True)
        return _json({"count": n})

    async def _get_status(request: web.Request) -> web.Response:
        total = await asyncio.to_thread(storage.count, False)
        valid = await asyncio.to_thread(storage.count, True)
        return _json({"total": total, "valid": valid, "invalid": max(total - valid, 0),
                      "running": bool(scheduler is not None)})

    async def _pop(request: web.Request) -> web.Response:
        rows = await asyncio.to_thread(storage.get_random, 1)
        if not rows:
            return _json({"code": 1, "msg": "代理池为空"}, status=200)
        r = rows[0]
        await asyncio.to_thread(storage.delete, r["ip"], int(r["port"]))
        return _json({"code": 0, "data": _row_dict(r)})

    async def _delete(request: web.Request) -> web.Response:
        ip = request.query.get("ip")
        try:
            port = int(request.query.get("port", 0))
        except ValueError:
            port = 0
        if not ip or not port:
            return _json({"code": 1, "msg": "缺少 ip/port"}, status=400)
        ok = await asyncio.to_thread(storage.delete, ip, port)
        return _json({"code": 0 if ok else 1, "msg": "delete success" if ok else "not found"})

    async def _add(request: web.Request) -> web.Response:
        ip = request.query.get("ip")
        try:
            port = int(request.query.get("port", 0))
        except ValueError:
            port = 0
        protocol = request.query.get("protocol", "http")
        if not ip or not port:
            return _json({"code": 1, "msg": "缺少 ip/port"}, status=400)
        if protocol not in ("http", "https"):
            return _json({"code": 1, "msg": "protocol 仅支持 http/https（socks 需 aiohttp_socks）"}, status=400)
        res = await validate_one(cfg, {"ip": ip, "port": port, "protocol": protocol})
        if not res["ok"]:
            return _json({"code": 1, "msg": f"校验失败: {res['error']}", "data": None}, status=200)
        added = await asyncio.to_thread(storage.add, ip, port, protocol, "api")
        await asyncio.to_thread(storage.update_result, ip, port, True, res["latency_ms"])
        return _json({"code": 0, "msg": "add success", "data": _row_dict(
            {"ip": ip, "port": port, "protocol": protocol, "source": "api", "score": 1,
             "latency_ms": res["latency_ms"]})})

    async def _healthz(request: web.Request) -> web.Response:
        return _json({"status": "ok"})

    app.router.add_get("/get", _get)
    app.router.add_get("/get_all", _get_all)
    app.router.add_get("/count", _count)
    app.router.add_get("/get_status", _get_status)
    app.router.add_get("/pop", _pop)
    app.router.add_get("/delete", _delete)
    app.router.add_get("/add", _add)
    app.router.add_get("/healthz", _healthz)
    return app
