# -*- coding: utf-8 -*-
"""通用工具：格式化、迷你 sparkline 等。"""
from __future__ import annotations

from typing import Iterable, Optional, Sequence

SPARK_CHARS = "▁▂▃▄▅▆▇█"


def sparkline(values: Sequence[float], width: int = 36,
              lo: Optional[float] = None, hi: Optional[float] = None) -> str:
    """把一组数值渲染成字符条形 sparkline（无依赖）。"""
    if not values:
        return ""
    lo = lo if lo is not None else min(values)
    hi = hi if hi is not None else max(values)
    rng = (hi - lo) or 1.0
    seg = list(values)[-width:]
    out = []
    for v in seg:
        idx = int((v - lo) / rng * (len(SPARK_CHARS) - 1) + 0.5)
        idx = max(0, min(len(SPARK_CHARS) - 1, idx))
        out.append(SPARK_CHARS[idx])
    return "".join(out)


def fmt_ms(ms: float) -> str:
    if ms >= 1000:
        return f"{ms / 1000:.2f}s"
    return f"{ms:.1f}ms"


def fmt_rps(rps: float) -> str:
    if rps >= 1000:
        return f"{rps / 1000:.2f}k"
    return f"{rps:.1f}"


def fmt_bytes(n: float) -> str:
    if n <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    v = float(n)
    while v >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    return f"{v:.1f} {units[i]}"


def fmt_pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def safe_div(a: float, b: float, default: float = 0.0) -> float:
    return a / b if b else default