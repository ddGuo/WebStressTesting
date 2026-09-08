# -*- coding: utf-8 -*-
"""实时终端仪表盘（基于 rich，纯文本方案，兼容普通终端）。"""
from __future__ import annotations

from typing import Any, Dict, Optional

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from web_stress_testing.utils import fmt_bytes, fmt_pct, fmt_rps, sparkline

PHASE_NAMES = {
    "ramp_up": "爬坡 ramp-up",
    "steady": "平稳 steady",
    "ramp_down": "降载 ramp-down",
    "done": "结束 done",
}

STATUS_CLASS = {
    "2xx": "green",
    "3xx": "cyan",
    "4xx": "yellow",
    "5xx": "red",
    "err": "magenta",
}


class Dashboard:
    """rich Live 实时面板：无终端时自动降级为无输出。"""

    def __init__(self, console: Optional[Console] = None, enabled: bool = True):
        self.console = console or Console()
        self.enabled = enabled and self.console.is_terminal
        self.live: Optional[Live] = None
        self._cache: Dict[str, Any] = {}

    def start(self) -> None:
        if not self.enabled:
            return
        self.live = Live(
            console=self.console,
            refresh_per_second=4,
            vertical_overflow="visible",
        )
        self.live.start()

    def update(self, *, target: str, elapsed: float, total_seconds: float,
               phase: str, users: int, max_users: int, cpu: float,
               snap: Dict[str, Any], agg) -> None:
        if not self.enabled or self.live is None:
            return
        self.live.update(self._build_panel(
            target, elapsed, total_seconds, phase, users, max_users, cpu, snap, agg))

    def _build_panel(self, target, elapsed, total_seconds, phase, users, max_users,
                     cpu, snap, agg):
        title = Text()
        title.append(" WebStressTesting 压测 ", style="bold white on blue")
        title.append(f" {target}")
        title.append(f"  [{PHASE_NAMES.get(phase, phase)}]")

        status = agg.status_counts
        total = agg.total
        failed = agg.failed
        ok_rate = (total - failed) / total if total else 1.0

        # 状态码条
        codes = status
        c2 = sum(v for k, v in codes.items() if 200 <= int(k) < 300)
        c3 = sum(v for k, v in codes.items() if 300 <= int(k) < 400)
        c4 = sum(v for k, v in codes.items() if 400 <= int(k) < 500)
        c5 = sum(v for k, v in codes.items() if 500 <= int(k) < 600)
        cerr = sum(v for k, v in codes.items() if int(k) == 0)

        parts = []
        for label, val, color in (("2xx", c2, "green"), ("3xx", c3, "cyan"),
                                  ("4xx", c4, "yellow"), ("5xx", c5, "red"),
                                  ("err", cerr, "magenta")):
            if val:
                pct = val / total if total else 0
                parts.append((label, f"{val} ({pct*100:.1f}%)", color))

        status_parts = []
        for label, text_val, color in parts:
            status_parts.append(f"[{color}]{label} {text_val}[/]")

        rps_series = [s["rps"] for s in agg.series[-40:]]
        lat_series = [s["p99_ms"] for s in agg.series[-40:]]

        ratio = min(elapsed / total_seconds, 1.0) if total_seconds > 0 else 1.0
        bar_w = 22
        filled = int(ratio * bar_w)
        progress = "█" * filled + "░" * (bar_w - filled)

        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column(justify="left", no_wrap=True)
        table.add_column(justify="right", no_wrap=True)

        kpis = [
            ("RPS", f"[bold cyan]{fmt_rps(snap['rps'])}[/]"),
            ("平均延迟", f"{snap['avg_ms']:.1f} ms"),
            ("P95", f"{snap['p95_ms']:.1f} ms"),
            ("P99", f"{snap['p99_ms']:.1f} ms"),
            ("吞吐", f"{fmt_bytes(snap['throughput_bps'])}/s"),
            ("成功率", f"[{'green' if ok_rate > 0.95 else 'red'}]{fmt_pct(ok_rate)}[/]"),
        ]
        for name, val in kpis:
            table.add_row(name, val)

        info = Text()
        info.append(f"运行 {elapsed:.0f}s / {total_seconds:.0f}s  {progress}  ")
        info.append(f"用户 {users}/{max_users}  ", style="cyan")
        info.append(f"CPU {cpu:.0f}%  ", style="dim")
        info.append(f"请求 {total}  ", style="bold")
        info.append(f"失败 {failed}", style="red" if failed else "dim")

        lines = Group(
            info,
            table,
            Text.assemble(("RPS  ", "dim"), (sparkline(rps_series), "cyan"), ("   P99  ", "dim"), (sparkline(lat_series), "yellow")),
            Text.assemble(("状态码  ", "dim"), ("   ".join(status_parts), "") if status_parts else Text("暂无", style="dim")),
        )
        return Panel(lines, title=title, border_style="blue", padding=(1, 1))

    def stop(self) -> None:
        if self.live is not None:
            self.live.stop()
            self.live = None