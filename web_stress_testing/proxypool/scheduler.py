# -*- coding: utf-8 -*-
"""调度器：抓取、校验、清理三个后台循环，保证池内只保留有效代理。"""
from __future__ import annotations

import asyncio
import logging
from typing import List

from web_stress_testing.proxypool.config import ProxyPoolConfig
from web_stress_testing.proxypool.crawler import ProxyCrawler
from web_stress_testing.proxypool.storage import BaseStorage
from web_stress_testing.proxypool.validator import validate_and_apply

log = logging.getLogger("proxypool")


class PoolScheduler:
    def __init__(self, cfg: ProxyPoolConfig, storage: BaseStorage):
        self.cfg = cfg
        self.storage = storage
        self.crawler = ProxyCrawler(
            proxy_provider=self._pool_proxy_provider,
            max_pages=cfg.crawl_max_pages,
            source_concurrency=cfg.crawl_concurrency,
            use_pool=cfg.crawl_use_pool,
            proxy_attempts=cfg.crawl_proxy_attempts,
        )
        self._tasks: List[asyncio.Task] = []
        self._scan_round = 0

    async def _pool_proxy_provider(self) -> List[str]:
        """供爬虫使用的池内有效代理（抓代理站时转发请求，防反爬）。"""
        if not self.cfg.crawl_use_pool:
            return []
        rows = await asyncio.to_thread(self.storage.get_random, 50)
        return [f"{r.get('protocol', 'http')}://{r['ip']}:{r['port']}" for r in rows]

    async def start(self) -> None:
        # 启动时先做一次"全量深检"：把池里待校验/过期代理全部过一遍
        await self.validate_pass(limit=10000)
        self._tasks = [
            asyncio.create_task(self._crawl_loop(), name="proxypool_crawl"),
            asyncio.create_task(self._validate_loop(), name="proxypool_validate"),
            asyncio.create_task(self._cleanup_loop(), name="proxypool_cleanup"),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    # ------------------------------------------------------------------
    async def crawl_pass(self) -> int:
        if not self.cfg.crawler_sources:
            return 0
        proxies = await self.crawler.crawl(self.cfg.crawler_sources)
        dead = [s["source"] for s in self.crawler.source_status() if not s["alive"]]
        if dead:
            log.info("无效源已自动过滤（冷却中）: %s", ", ".join(dead))
        if not proxies:
            log.warning("本轮抓取未获取到新代理（全部来源为空/被过滤）")
            return 0
        # 先入库（去重），再对新增项立即校验
        added = 0
        for p in proxies:
            if await asyncio.to_thread(self.storage.add, p["ip"], p["port"], p["protocol"], p["source"]):
                added += 1
        stats = await validate_and_apply(self.cfg, self.storage, proxies, log)
        log.info("抓取: 候选 %d，新增 %d，校验通过 %d，失效删除 %d",
                 len(proxies), added, stats["ok"], stats["deleted"])
        return added

    async def validate_pass(self, limit: int = 500) -> Dict:
        due = await asyncio.to_thread(self.storage.due_for_check, limit)
        if not due:
            total = await asyncio.to_thread(self.storage.count, True)
            log.info("校验: 暂无待检代理（池内均新鲜，有效 %d 个）", total)
            return {"total": 0, "ok": 0, "fail": 0, "deleted": 0}
        stats = await validate_and_apply(self.cfg, self.storage, due, log)
        log.info("校验: 本批 %d，通过 %d，失败 %d，删除 %d", stats["total"], stats["ok"],
                 stats["fail"], stats["deleted"])
        return stats

    async def cleanup_pass(self) -> int:
        n = await asyncio.to_thread(self.storage.cleanup)
        if n:
            log.info("清理: 移除失效/超龄代理 %d 个", n)
        else:
            log.info("清理: 未发现需要清理的记录")
        return n

    # ------------------------------------------------------------------
    async def _crawl_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.cfg.crawl_interval)
                await self.crawl_pass()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("抓取循环异常: %s", e)
                await asyncio.sleep(min(self.cfg.crawl_interval, 30))

    async def _validate_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.cfg.check_interval)
                self._scan_round += 1
                limit = 100000 if (self._scan_round % max(1, int(self.cfg.full_check_interval / max(1, self.cfg.check_interval))) == 0) else 500
                await self.validate_pass(limit=limit)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("校验循环异常: %s", e)
                await asyncio.sleep(min(self.cfg.check_interval, 30))

    async def _cleanup_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.cfg.cleanup_interval)
                await self.cleanup_pass()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("清理循环异常: %s", e)
                await asyncio.sleep(min(self.cfg.cleanup_interval, 30))
