# -*- coding: utf-8 -*-
"""AioTest 原生压测脚本示例（可由 WebStressTesting 工具自动生成等价的脚本并运行）。

本文件演示如何使用 AioTest 官方风格编写压测场景：
  1. 定义 HttpUser 子类（方法名以 test_ 开头即成为任务）
  2. 定义恰好一个 LoadUserShape 子类控制负载曲线

注意：顺序执行模式（默认）下所有任务权重必须为 1；
如需按权重分配任务频率，请把 User 设为 ExecutionMode.CONCURRENT。

运行方式（需要先 pip install aiotest）：
  python -m aiotest -f examples/website_pressure.py -H https://example.com
"""
from __future__ import annotations

from aiotest import HttpUser, LoadUserShape


class WebsiteUser(HttpUser):
    """网站访问用户：首页 + 探活接口 + 详情页。"""

    # 目标地址由命令行 -H 传入；这里只做兜底
    host = "https://example.com"
    wait_time = 0  # 请求间不加思考时间，压满

    async def test_homepage(self):
        """首页。"""
        async with self.client.get("/", name="首页") as resp:
            if resp.status >= 400:
                raise AssertionError(f"首页返回 {resp.status}")

    async def test_health(self):
        """健康检查接口。"""
        async with self.client.get("/healthz", name="健康检查") as resp:
            if resp.status >= 400:
                raise AssertionError(f"健康检查返回 {resp.status}")

    async def test_detail(self):
        """详情页。"""
        async with self.client.get("/detail/1", name="详情页") as resp:
            if resp.status >= 500:
                raise AssertionError(f"详情页返回 {resp.status}")


class PressureShape(LoadUserShape):
    """3s 爬坡 -> 5s 平稳 -> 2s 降载（示例用短时长，实际请自行调整）。"""

    def __init__(self):
        super().__init__()
        self.peak_users = 50

    def tick(self):
        t = self.get_run_time()
        if t < 3:
            target = max(1, int(round(self.peak_users * t / 3)))
            return (target, min(50 / 3, float(target)))  # 速率不能超过当前目标用户数
        if t < 8:
            return (self.peak_users, 50 / 3)
        if t < 10:
            target = max(1, int(round(self.peak_users * (10 - t) / 2)))
            return (target, min(50 / 2, float(target)))
        return None