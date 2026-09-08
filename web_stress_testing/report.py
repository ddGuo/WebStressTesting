# -*- coding: utf-8 -*-
"""报告输出：JSON / CSV / 配置存档。"""
from __future__ import annotations

import csv
import json
import os
from typing import Any, Dict, List

from web_stress_testing.utils import safe_div


def write_json(path: str, data: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def write_series_csv(path: str, series: List[Dict[str, Any]]) -> None:
    if not series:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write("")
        return
    fields = ["t", "users", "phase", "rps", "avg_ms", "p50_ms", "p90_ms",
              "p95_ms", "p99_ms", "max_ms", "failed", "throughput_bps"]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in series:
            writer.writerow(row)


def write_failures_csv(path: str, samples: List[Dict[str, Any]]) -> None:
    fields = ["timestamp", "method", "endpoint", "status", "exc_type", "message"]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in samples:
            writer.writerow({k: row.get(k, "") for k in fields})


def write_config_json(path: str, config_dict: Dict[str, Any]) -> None:
    write_json(path, config_dict)