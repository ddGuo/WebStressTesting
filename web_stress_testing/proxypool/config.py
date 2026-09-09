# -*- coding: utf-8 -*-
"""代理池配置：默认本机 MySQL（proxy_pool/proxy_pool/123456），可用环境变量覆盖。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class ProxyPoolConfig:
    # ---- 存储（MySQL 默认）----
    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_user: str = "proxy_pool"
    db_password: str = "123456"
    db_name: str = "proxy_pool"
    db_url: Optional[str] = None  # 例: sqlite:///C:/tmp/pool.db（无 MySQL 时的替代）

    # ---- API 服务 ----
    api_host: str = "0.0.0.0"
    api_port: int = 5010

    # ---- 校验 ----
    test_url: str = "http://www.baidu.com"   # 连通性测试地址（可改为内网/目标探测点）
    ip_api: str = ""                          # 出口 IP 探测接口，留空跳过匿名性检测
    timeout: float = 6.0
    validate_concurrency: int = 200
    score_start: int = 0
    score_max: int = 10
    score_min: int = 0
    fail_threshold: int = 3                   # 连续失败次数达到即删除
    ttl_seconds: int = 600                    # 校验有效期：超时未重测视为过期

    # ---- 调度 ----
    check_interval: float = 60.0              # 增量校验周期（秒）
    full_check_interval: float = 3600.0       # 全量深检周期（秒）
    crawl_interval: float = 1800.0            # 爬取周期（秒）
    crawl_use_pool: bool = True              # 抓代理站时用池内有效 IP 转发（防反爬）
    crawl_max_pages: int = 3                 # 每源自动翻页上限
    crawl_concurrency: int = 5               # 源间并发抓取数
    cleanup_interval: float = 300.0           # 清理周期（秒）
    heartbeat_interval: float = 60.0          # 心跳日志周期（秒）

    # ---- 抓取源（可多个；置空列表即禁用爬取）----
    crawler_sources: List[str] = field(default_factory=lambda: [
        "kuaidaili", "ip3366", "ip89", "kxdaili",
        "daili66", "docip", "freevpnnode", "geonode", "goodips", "ihuan",
        "proxifly", "roundproxies", "scdn", "zdaye", "66ip",
    ])

    # ---- 日志 ----
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "ProxyPoolConfig":
        """从环境变量覆盖：PROXYPOOL_DB_URL/PORT/TEST_URL/IP_API/... 等。"""
        cfg = cls(
            db_url=os.environ.get("PROXYPOOL_DB_URL") or None,
            api_host=os.environ.get("PROXYPOOL_API_HOST", cls.api_host),
            api_port=int(os.environ.get("PROXYPOOL_API_PORT", cls.api_port)),
            test_url=os.environ.get("PROXYPOOL_TEST_URL", cls.test_url),
            ip_api=os.environ.get("PROXYPOOL_IP_API", cls.ip_api),
            timeout=float(os.environ.get("PROXYPOOL_TIMEOUT", cls.timeout)),
            validate_concurrency=int(os.environ.get("PROXYPOOL_CONCURRENCY", cls.validate_concurrency)),
            fail_threshold=int(os.environ.get("PROXYPOOL_FAIL_THRESHOLD", cls.fail_threshold)),
            ttl_seconds=int(os.environ.get("PROXYPOOL_TTL", cls.ttl_seconds)),
            check_interval=float(os.environ.get("PROXYPOOL_CHECK_INTERVAL", cls.check_interval)),
            heartbeat_interval=float(os.environ.get("PROXYPOOL_HEARTBEAT", cls.heartbeat_interval)),
            crawl_interval=float(os.environ.get("PROXYPOOL_CRAWL_INTERVAL", cls.crawl_interval)),
            crawl_max_pages=int(os.environ.get("PROXYPOOL_MAX_PAGES", cls.crawl_max_pages)),
            crawl_concurrency=int(os.environ.get("PROXYPOOL_CRAWL_CONCURRENCY", cls.crawl_concurrency)),
            log_level=os.environ.get("PROXYPOOL_LOG_LEVEL", cls.log_level),
        )
        cfg.crawl_use_pool = os.environ.get("PROXYPOOL_CRAWL_USE_POOL", "1").lower() in ("1", "true", "yes", "on")
        sources = os.environ.get("PROXYPOOL_SOURCES")
        if sources is not None:
            cfg.crawler_sources = [s.strip() for s in sources.split(",") if s.strip()]
        return cfg
