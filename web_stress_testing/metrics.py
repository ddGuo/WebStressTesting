# -*- coding: utf-8 -*-
"""指标聚合器。

订阅 AioTest 的 request_metrics 事件，在进程内聚合：
- 延迟直方图（1ms 桶，内存恒定）-> 各类分位数
- 每秒时间序列 -> 仪表盘 / CSV / 报表
- 错误样本与状态码分布
"""
from __future__ import annotations

import math
import time
from collections import Counter, deque
from typing import Any, Dict, List

from web_stress_testing.utils import safe_div


class LatencyHistogram:
    """1ms 粒度延迟直方图：内存 O(max_bucket)，支持任意分位数查询。"""

    def __init__(self, max_bucket_ms: int = 60_000):
        self.max_bucket_ms = max_bucket_ms
        self.buckets = [0] * (max_bucket_ms + 1)
        self.count = 0
        self.total_ms = 0.0
        self.min_ms = math.inf
        self.max_ms = 0.0

    def add(self, ms: float) -> None:
        idx = int(round(ms))
        idx = 0 if idx < 0 else (self.max_bucket_ms if idx > self.max_bucket_ms else idx)
        self.buckets[idx] += 1
        self.count += 1
        self.total_ms += ms
        if ms < self.min_ms:
            self.min_ms = ms
        if ms > self.max_ms:
            self.max_ms = ms

    def percentile(self, pct: float) -> float:
        if self.count == 0:
            return 0.0
        target = math.ceil(self.count * pct)
        acc = 0
        for idx, n in enumerate(self.buckets):
            acc += n
            if acc >= target:
                return float(idx)
        return float(self.max_bucket_ms)

    def avg(self) -> float:
        return safe_div(self.total_ms, self.count)


class MetricsAggregator:
    """进程内指标聚合器（仅事件循环内访问，无需加锁）。"""

    def __init__(self, max_error_samples: int = 2000):
        self.hist = LatencyHistogram()
        self.total = 0
        self.failed = 0
        self.bytes_total = 0
        self.status_counts: Counter = Counter()
        self.error_kinds: Counter = Counter()
        self.error_samples: deque = deque(maxlen=max_error_samples)

        self.started_at = time.monotonic()
        self.series: List[Dict[str, Any]] = []

        # 每秒窗口
        self._win_start = time.monotonic()
        self._win_hist = LatencyHistogram()
        self._win_sum_ms = 0.0
        self._win_bytes = 0
        self._win_failed = 0

    # ------------------------------------------------------------------
    # AioTest 事件回调
    # ------------------------------------------------------------------
    async def on_request_metrics(self, **kwargs: Any) -> None:
        m = kwargs.get("metrics")
        if m is None:
            return
        self._record(m)

    def _record(self, m: Any) -> None:
        ms = m.duration * 1000.0
        status = m.status_code or 0
        err = bool(m.error) or status >= 400

        self.hist.add(ms)
        self._win_hist.add(ms)
        self._win_sum_ms += ms
        self.total += 1
        self.bytes_total += m.response_size or 0
        self._win_bytes += m.response_size or 0
        self.status_counts[status] += 1

        if err:
            self.failed += 1
            self._win_failed += 1
            exc_type = (m.error or {}).get("exc_type", "HTTPError") if m.error else "HTTPError"
            key = (m.method, m.endpoint, status, exc_type)
            self.error_kinds[key] += 1
            if m.error and len(self.error_samples) < self.error_samples.maxlen:
                msg = str(m.error.get("message", ""))
                self.error_samples.append({
                    "timestamp": round(m.timestamp, 3),
                    "method": m.method,
                    "endpoint": m.endpoint,
                    "status": status,
                    "exc_type": exc_type,
                    "message": msg[:300],
                })

    # ------------------------------------------------------------------
    # 每秒快照
    # ------------------------------------------------------------------
    def snapshot(self, users: int, phase: str = "") -> Dict[str, Any]:
        now = time.monotonic()
        dt = now - self._win_start
        n = self._win_hist.count
        rps = safe_div(n, dt)
        row = {
            "t": round(now - self.started_at, 3),
            "users": int(users),
            "phase": phase,
            "rps": round(rps, 1),
            "avg_ms": round(safe_div(self._win_sum_ms, n), 2),
            "p50_ms": round(self._win_hist.percentile(0.50), 2),
            "p90_ms": round(self._win_hist.percentile(0.90), 2),
            "p95_ms": round(self._win_hist.percentile(0.95), 2),
            "p99_ms": round(self._win_hist.percentile(0.99), 2),
            "max_ms": round(self._win_hist.max_ms, 2) if n else 0.0,
            "failed": self._win_failed,
            "throughput_bps": round(safe_div(self._win_bytes, dt), 1),
        }
        self.series.append(row)
        # 重置窗口
        self._win_start = now
        self._win_hist = LatencyHistogram()
        self._win_sum_ms = 0.0
        self._win_bytes = 0
        self._win_failed = 0
        return row

    # ------------------------------------------------------------------
    # 汇总
    # ------------------------------------------------------------------
    def summary(self) -> Dict[str, Any]:
        elapsed = time.monotonic() - self.started_at
        h = self.hist
        errors_by_status = Counter()
        for (_method, _endpoint, status, _exc_type), cnt in self.error_kinds.items():
            errors_by_status[status] += cnt
        return {
            "total_requests": self.total,
            "succeeded": self.total - self.failed,
            "failed": self.failed,
            "error_rate": safe_div(self.failed, self.total),
            "elapsed_seconds": round(elapsed, 3),
            "rps": safe_div(self.total, elapsed),
            "throughput_bps": safe_div(self.bytes_total, elapsed),
            "bytes_total": self.bytes_total,
            "latency_ms": {
                "min": round(h.min_ms, 2) if h.count else 0.0,
                "avg": round(h.avg(), 2),
                "p50": round(h.percentile(0.50), 2),
                "p90": round(h.percentile(0.90), 2),
                "p95": round(h.percentile(0.95), 2),
                "p99": round(h.percentile(0.99), 2),
                "p999": round(h.percentile(0.999), 2),
                "max": round(h.max_ms, 2) if h.count else 0.0,
            },
            "status_codes": {str(k): v for k, v in sorted(self.status_counts.items())},
            "error_kinds": [
                {
                    "method": method,
                    "endpoint": endpoint,
                    "status": status,
                    "exc_type": exc_type,
                    "count": cnt,
                }
                for (method, endpoint, status, exc_type), cnt in self.error_kinds.most_common(20)
            ],
            "errors_by_status": {str(k): v for k, v in sorted(errors_by_status.items())},
        }