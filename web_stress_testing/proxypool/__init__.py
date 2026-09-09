# -*- coding: utf-8 -*-
"""WebStressTesting 内置代理池（ProxyPool）。

参照 jhao104/proxy_pool 架构：抓取(Crawler) → 校验(Validator) → 存储(MySQL) → API。
只保留有效代理：入库先校验、定时重测打分、失败剔除、过期清理。

- 服务模式：python -m web_stress_testing.proxypool serve
- 压测接入：python -m web_stress_testing <url> --proxy-api http://127.0.0.1:5010
"""
from web_stress_testing.proxypool.config import ProxyPoolConfig
from web_stress_testing.proxypool.storage import create_storage

__all__ = ["ProxyPoolConfig", "create_storage"]
