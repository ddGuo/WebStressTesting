# -*- coding: utf-8 -*-
"""负载生成器：根据 CLI 参数动态构造 AioTest 的 User 类与 LoadUserShape。

这是与 AioTest 框架衔接的核心：
- 动态生成 HttpUser 子类（job 方法 test_pressure -> 自动被框架收集）
- 动态生成 LoadUserShape 子类（tick 控制 爬坡/平稳/降载 三个阶段）
"""
from __future__ import annotations

import asyncio
import time as _time
from typing import Callable, Optional

from web_stress_testing.config import TestConfig


def build_user_class(config: TestConfig, base_url: str, request_counter=None,
                         proxy_allocator=None):
    """根据请求配置动态生成 HttpUser 子类。proxy_allocator 提供"一个用户一个IP"。"""
    from aiotest import HttpUser

    request = config.request
    method = request.method.upper()
    headers = dict(request.headers or {})
    params = dict(request.params or {})
    body = request.body
    content_type = request.content_type
    path = request.path
    timeout = request.timeout
    verify_ssl = request.verify_ssl
    max_retries = request.max_retries
    think_time = config.load.think_time
    max_requests = config.load.max_requests

    async def test_pressure(self):
        """每个用户循环执行的压测请求。"""
        # 达到 --max-requests 后不再发新请求，让 shape 尽快收尾（避免超发）
        if max_requests is not None and request_counter is not None and request_counter() >= max_requests:
            await asyncio.sleep(0.02)
            return
        kwargs: dict = {}
        if headers:
            kwargs["headers"] = dict(headers)
        if params:
            kwargs["params"] = dict(params)
        if body is not None:
            if isinstance(body, dict) and method not in ("GET", "HEAD"):
                kwargs["json"] = body
            else:
                kwargs["data"] = body
            if content_type:
                hdrs = kwargs.setdefault("headers", {})
                hdrs.setdefault("Content-Type", content_type)

        proxy = getattr(self, "_proxy", None)
        if proxy:
            kwargs["proxy"] = proxy
        t0 = _time.perf_counter()
        try:
            async with self.client.request(method, path, name=path, **kwargs) as resp:
                # 4xx / 5xx 视为失败：抛 AssertionError 让 AioTest 记录错误指标
                if resp.status >= 400:
                    raise AssertionError(f"HTTP {resp.status}")
        except AssertionError:
            if self._allocator is not None:
                self._allocator.record(proxy, False, (_time.perf_counter() - t0) * 1000)
            raise
        except Exception:
            ms = (_time.perf_counter() - t0) * 1000
            if self._allocator is not None:
                self._allocator.record(proxy, False, ms)
                self._allocator.mark_failed(proxy)
                # 代理可疑 -> 下次换一个健康代理
                self._proxy = await self._allocator.acquire()
            raise
        if self._allocator is not None:
            self._allocator.record(proxy, True, (_time.perf_counter() - t0) * 1000)

    def __init__(self):
        HttpUser.__init__(
            self,
            host=base_url,
            wait_time=think_time,
            default_headers=headers or None,
            timeout=timeout,
            max_retries=max_retries,
            verify_ssl=verify_ssl,
        )
        self._allocator = proxy_allocator
        self._proxy = proxy_allocator.acquire_sync() if proxy_allocator is not None else None

    user_cls = type(
        "GeneratedPressureUser",
        (HttpUser,),
        {
            "host": base_url,
            "wait_time": think_time,
            "weight": 1,
            "__init__": __init__,
            "test_pressure": test_pressure,
        },
    )
    return user_cls


class PressureShape:
    """加载形状：ramp-up -> steady -> ramp-down 三段式压测。

    由 AioTest 的 LoadShapeManager 逐秒 tick：
    返回 (目标用户数, 启停速率) 时应用负载；返回 None 时结束测试。
    """

    def __init__(
        self,
        users: int,
        ramp_up: float,
        steady: float,
        ramp_down: float,
        spawn_rate: Optional[float] = None,
        max_requests: Optional[int] = None,
        request_counter: Optional[Callable[[], int]] = None,
    ):
        self.users = int(users)
        self.ramp_up = max(0.0, float(ramp_up))
        self.steady = max(0.0, float(steady))
        self.ramp_down = max(0.0, float(ramp_down))
        self.spawn_rate = spawn_rate
        self.max_requests = max_requests
        self.request_counter = request_counter or (lambda: 0)

    def total_seconds(self) -> float:
        return self.ramp_up + self.steady + self.ramp_down

    def phase(self, t: Optional[float] = None) -> str:
        if t is None:
            t = 0.0
        if t < self.ramp_up:
            return "ramp_up"
        if t < self.ramp_up + self.steady:
            return "steady"
        if t < self.ramp_up + self.steady + self.ramp_down:
            return "ramp_down"
        return "done"

    def _spawn_rate(self) -> float:
        if self.spawn_rate is not None:
            return float(self.spawn_rate)
        if self.ramp_up > 0:
            return self.users / self.ramp_up
        return float(self.users)

    def tick(self, run_time: float) -> Optional[tuple]:
        """根据当前运行时长返回 (目标用户数, 启停速率)，或 None 结束。"""
        if self.max_requests is not None and self.request_counter() >= self.max_requests:
            return None

        t = run_time
        total = self.ramp_up + self.steady + self.ramp_down
        if total <= 0:
            # 纯请求数模式（-d 0）：保持满负载直到达到 --max-requests
            rate = min(max(self._spawn_rate(), 0.5), float(self.users))
            return (self.users, rate)
        if t >= total:
            return None

        users = float(self.users)
        if self.ramp_up > 0 and t < self.ramp_up:
            target = int(round(users * t / self.ramp_up))
            target = max(1, min(target, self.users))
            rate = self._spawn_rate()
        elif t < self.ramp_up + self.steady:
            target = self.users
            rate = self._spawn_rate()
        else:
            if self.ramp_down <= 0:
                return None
            frac = (t - self.ramp_up - self.steady) / self.ramp_down
            target = max(1, int(round(users * (1.0 - frac))))
            rate = users / self.ramp_down

        # AioTest 校验要求: 0 < rate <= target
        rate = min(max(rate, 0.5), float(target))
        return (target, rate)


def build_shape(config: TestConfig, request_counter: Optional[Callable[[], int]] = None):
    """构造真正的 LoadUserShape 子类实例。"""
    from aiotest import LoadUserShape

    load = config.load
    shape = PressureShape(
        users=load.users,
        ramp_up=load.ramp_up,
        steady=load.steady,
        ramp_down=load.ramp_down,
        spawn_rate=load.spawn_rate,
        max_requests=load.max_requests,
        request_counter=request_counter,
    )
    shape_cls = type(
        "GeneratedPressureShape",
        (LoadUserShape,),
        {
            "__init__": lambda self: LoadUserShape.__init__(self),
            "tick": lambda self: shape.tick(self.get_run_time()),
            "phase": lambda self, t=None: shape.phase(t if t is not None else self.get_run_time()),
            "total_seconds": lambda self: shape.total_seconds(),
        },
    )
    return shape_cls()