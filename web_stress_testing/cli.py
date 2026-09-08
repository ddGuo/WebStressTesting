# -*- coding: utf-8 -*-
"""WebStressTesting 命令行入口：解析参数 -> 预检 -> AioTest 本地运行 -> 实时仪表盘 -> 报告。"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import socket
import sys
import time
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.table import Table
from rich.text import Text

from web_stress_testing import __version__
from web_stress_testing.config import (
    ConfigError,
    LoadSpec,
    RequestSpec,
    TestConfig,
    default_timings,
    normalize_target,
    parse_body,
    parse_headers,
    parse_kv_pairs,
    split_target,
    validate_load,
)
from web_stress_testing.dashboard import Dashboard
from web_stress_testing.htmlreport import build_html_report
from web_stress_testing.loadgen import build_shape, build_user_class
from web_stress_testing.metrics import MetricsAggregator
from web_stress_testing.report import write_config_json, write_failures_csv, write_json, write_series_csv
from web_stress_testing.utils import fmt_bytes, fmt_ms, fmt_pct, fmt_rps, safe_div

console = Console()


def _configure_encoding() -> None:
    """统一 stdout/stderr 为 UTF-8，避免 Windows 控制台中文乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 命令行解析
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="WebStressTesting",
        description="⚡ WebStressTesting - 基于 AioTest 的现代化网站压测工具",
        epilog="示例:\n"
               "  WebStressTesting https://example.com -u 100 -d 60\n"
               "  WebStressTesting --url https://example.com/api -X POST --body '{\"a\":1}' -u 50 -d 30\n"
               "  WebStressTesting https://example.com -u 200 -d 120 --ramp-up 30 --max-requests 50000",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("target", nargs="?", default=None,
                        help="目标网址（域名或完整 URL，自动补 https://）")
    parser.add_argument("--url", default=None, help="目标网址（与位置参数二选一）")

    g_load = parser.add_argument_group("负载参数")
    g_load.add_argument("-u", "--users", type=int, default=10,
                        help="并发用户数（峰值）(默认: 10)")
    g_load.add_argument("-d", "--duration", type=float, default=60.0,
                        help="平稳期持续时间，秒 (默认: 60)")
    g_load.add_argument("--ramp-up", type=float, default=None,
                        help="爬坡时间，秒（默认按总时长自动计算）")
    g_load.add_argument("--ramp-down", type=float, default=None,
                        help="降载时间，秒（默认按总时长自动计算，可为 0）")
    g_load.add_argument("--spawn-rate", type=float, default=None,
                        help="用户启动速率，个/秒（默认 users/ramp_up）")
    g_load.add_argument("--think-time", type=float, default=0.0,
                        help="每个请求之间的思考时间，秒 (默认: 0)")
    g_load.add_argument("--max-requests", type=int, default=None,
                        help="达到该请求数后提前结束")

    g_req = parser.add_argument_group("请求参数")
    g_req.add_argument("-X", "--method", default="GET",
                       help="HTTP 方法 GET/POST/PUT/DELETE... (默认: GET)")
    g_req.add_argument("--path", default=None,
                       help="请求路径（覆盖 URL 中的路径），如 /api/v1/health")
    g_req.add_argument("--headers", default=None,
                       help="请求头：JSON 对象 或 'K: V, K2: V2' 列表")
    g_req.add_argument("--param", action="append", default=None, metavar="K=V",
                       help="查询参数，可重复传入（GET 时作为 URL 参数）")
    g_req.add_argument("--body", default=None,
                       help="请求体：JSON 字符串，或 @文件路径（POST/PUT 等）")
    g_req.add_argument("--content-type", default=None, help="Content-Type（配合 --body）")
    g_req.add_argument("--timeout", type=float, default=30.0,
                       help="请求超时，秒 (默认: 30)")
    g_req.add_argument("--no-verify-ssl", action="store_true",
                       help="跳过 SSL 证书校验（自签名证书时使用）")

    g_run = parser.add_argument_group("运行参数")
    g_run.add_argument("--preflight", dest="preflight", action="store_true", default=True,
                       help="压测前进行连通性预检（默认开启）")
    g_run.add_argument("--no-preflight", dest="preflight", action="store_false",
                       help="跳过连通性预检")
    g_run.add_argument("--no-dashboard", action="store_true",
                       help="关闭实时仪表盘")
    g_run.add_argument("-q", "--quiet", action="store_true",
                       help="安静模式：只输出报告路径")
    g_run.add_argument("--report-dir", default=None,
                       help="报告输出目录（默认 ./reports/<name>_<时间戳>）")
    g_run.add_argument("--name", default=None, help="本次测试名称（用于报告命名）")
    g_run.add_argument("--prometheus-port", type=int, default=0,
                       help="AioTest Prometheus 指标端口；0=随机不冲突 (默认: 0)")
    g_run.add_argument("--loop-policy", default="auto",
                       choices=["auto", "selector", "proactor"],
                       help="Windows 事件循环策略：auto=按并发自动选择；selector=经典 select（≤512 连接）；proactor=IOCP 大规模并发")
    g_run.add_argument("--loglevel", default="WARNING",
                       choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                       help="AioTest 日志级别（写入 aiotest.log）(默认: WARNING)")
    g_run.add_argument("-V", "--version", action="version",
                       version=f"WebStressTesting {__version__} (基于 aiotest)")
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# 配置组装
# ---------------------------------------------------------------------------

def build_config(args: argparse.Namespace) -> TestConfig:
    raw = args.url or args.target
    if not raw:
        raise ConfigError("请提供目标网址，例如: WebStressTesting https://example.com")
    target = normalize_target(raw)
    base_url, url_path = split_target(target)

    # 请求体与 GET 参数冲突处理
    body = parse_body(args.body)
    method = args.method.upper()
    params = parse_kv_pairs(args.param)
    if body is not None and isinstance(body, dict) and method in ("GET", "HEAD"):
        console.print("[yellow]提示:[/] GET 请求不支持请求体，已合并到查询参数")
        params.update(body)
        body = None

    path = args.path if args.path is not None else url_path
    if path and not path.startswith("/"):
        path = "/" + path

    request = RequestSpec(
        method=method,
        path=path,
        headers=parse_headers(args.headers) if args.headers else None,
        params=params or None,
        body=body,
        content_type=args.content_type,
        timeout=args.timeout,
        verify_ssl=not args.no_verify_ssl,
        max_retries=0,
    )

    # 负载与默认爬坡/降载
    ramp_up, ramp_down = default_timings(args.duration)
    load = LoadSpec(
        users=args.users,
        ramp_up=args.ramp_up if args.ramp_up is not None else ramp_up,
        steady=args.duration,
        ramp_down=args.ramp_down if args.ramp_down is not None else ramp_down,
        think_time=args.think_time,
        spawn_rate=args.spawn_rate,
        max_requests=args.max_requests,
    )
    validate_load(load)

    # 报告目录
    host = re.sub(r"[^\w.-]", "_", (base_url.split("://")[-1] or "target").split("/")[0])
    run_name = args.name or host
    run_name = re.sub(r"[^\w.-]", "_", run_name) or "loadtest"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if args.report_dir:
        report_dir = os.path.abspath(args.report_dir)
    else:
        report_dir = os.path.abspath(os.path.join("reports", f"{run_name}_{ts}"))

    return TestConfig(
        target=target,
        request=request,
        load=load,
        report_dir=report_dir,
        name=run_name,
        dashboard=not args.no_dashboard,
        preflight=args.preflight,
        quiet=args.quiet,
        loglevel=args.loglevel,
        prometheus_port=args.prometheus_port,
        args_text=" ".join(sys.argv[1:]),
    )


# ---------------------------------------------------------------------------
# 预检
# ---------------------------------------------------------------------------

async def preflight(config: TestConfig) -> bool:
    """对目标发起一次探测请求，展示 DNS/状态/耗时等基础信息。"""
    import aiohttp

    parts = config.target.split("://", 1)[1].split("/")[0]
    host = parts.split(":")[0]
    port = None
    try:
        port = int(parts.split(":")[1]) if ":" in parts else (443 if config.target.startswith("https") else 80)
    except (ValueError, IndexError):
        pass

    ips: List[str] = []
    try:
        loop = asyncio.get_event_loop()
        infos = await loop.getaddrinfo(host, port or 0, type=socket.SOCK_STREAM)
        for info in infos:
            ip = info[4][0]
            if ip not in ips:
                ips.append(ip)
    except Exception as e:
        ips = [f"解析失败: {e}"]

    t0 = time.perf_counter()
    status, server_hdr, ct, size = 0, "-", "-", 0
    timeout = aiohttp.ClientTimeout(total=min(max(config.request.timeout, 2), 15))
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                config.target,
                headers={"User-Agent": f"WebStressTesting/{__version__}"},
                ssl=config.request.verify_ssl,
            ) as resp:
                data = await resp.read()
                status = resp.status
                server_hdr = resp.headers.get("Server", "-")
                ct = resp.headers.get("Content-Type", "-")
                size = len(data)
        cost_ms = (time.perf_counter() - t0) * 1000
    except Exception as e:
        cost_ms = (time.perf_counter() - t0) * 1000
        table = Table(box=None, show_header=False, pad_edge=False)
        table.add_column(style="bold", width=10)
        table.add_column()
        table.add_row("目标", config.target)
        table.add_row("解析IP", ", ".join(ips))
        table.add_row("结果", f"[red]预检失败 ({cost_ms:.0f}ms): {e}[/red]")
        console.print(Text(), "\n", table, "\n")
        console.print("[red]✘ 无法访问目标，压测已取消。[/] 可用 [cyan]--no-preflight[/] 强制运行。")
        return False

    table = Table(box=None, show_header=False, pad_edge=False)
    table.add_column(style="bold", width=10)
    table.add_column()
    table.add_row("目标", config.target)
    table.add_row("解析IP", ", ".join(ips[:6]) + (" ..." if len(ips) > 6 else ""))
    table.add_row("状态", f"[{'green' if status < 400 else 'red'}]{status}[/]")
    table.add_row("Server", server_hdr)
    table.add_row("内容类型", ct)
    table.add_row("响应大小", f"{fmt_bytes(size)}")
    table.add_row("耗时", f"{cost_ms:.1f} ms")
    console.print(Text("┌─ 预检 Preflight ───────────────────────────────────", style="bold blue"))
    console.print(table)
    console.print(Text("└────────────────────────────────────────────────────", style="bold blue"))
    if status >= 400:
        console.print(f"[yellow]注意:[/] 目标返回 {status}，仍将继续压测（4xx/5xx 会计为失败）")
    return True


# ---------------------------------------------------------------------------
# AioTest 日志接管
# ---------------------------------------------------------------------------

def redirect_aiotest_logging(report_dir: str, level: str) -> None:
    """去掉 AioTest 的控制台日志，改为写入报告目录下的 aiotest.log。"""
    from aiotest.logger import logger as aiotest_logger

    # 若在报告目录内产生了默认 logs/ 目录则清理
    legacy = os.path.join(report_dir, "logs")
    if os.path.isdir(legacy):
        shutil.rmtree(legacy, ignore_errors=True)

    aiotest_logger.setLevel(getattr(logging, level.upper(), logging.WARNING))
    for h in list(aiotest_logger.handlers):
        aiotest_logger.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    fh = logging.FileHandler(os.path.join(report_dir, "aiotest.log"), encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)8s | %(message)s"))
    aiotest_logger.addHandler(fh)


# ---------------------------------------------------------------------------
# 实时快照循环
# ---------------------------------------------------------------------------

async def tick_loop(stop_evt: asyncio.Event, config: TestConfig, aggregator: MetricsAggregator,
                    runner: Any, shape: Any, dashboard: Dashboard) -> None:
    while not stop_evt.is_set():
        t = shape.get_run_time()
        phase = shape.phase(t)
        users = runner.active_user_count
        snap = aggregator.snapshot(users=users, phase=phase)
        if dashboard.enabled:
            dashboard.update(
                target=config.target,
                elapsed=t,
                total_seconds=shape.total_seconds(),
                phase=phase,
                users=users,
                max_users=config.load.users,
                cpu=runner.cpu_usage,
                snap=snap,
                agg=aggregator,
            )
        try:
            await asyncio.wait_for(stop_evt.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass


# ---------------------------------------------------------------------------
# 最终统计表
# ---------------------------------------------------------------------------

def print_summary(config: TestConfig, summary: Dict[str, Any],
                  started_at: str, ended_at: str, interrupted: bool) -> None:
    lat = summary.get("latency_ms", {})
    table = Table(title="压测结果", show_header=False, box=None, pad_edge=False, title_style="bold cyan")
    table.add_column(style="bold", width=12)
    table.add_column()
    table.add_row("目标", config.target)
    table.add_row("请求", f"{config.request.method} {config.request.path}")
    table.add_row("时间", f"{started_at} → {ended_at}{'  [red](被中断)[/]' if interrupted else ''}")
    table.add_row("时长", f"{summary.get('elapsed_seconds', 0):.1f} s")
    table.add_row("总请求", f"{summary.get('total_requests', 0):,}")
    table.add_row("成功/失败", f"{summary.get('succeeded', 0):,} / [red]{summary.get('failed', 0):,}[/]")
    table.add_row("成功率", fmt_pct(1 - summary.get("error_rate", 0)))
    table.add_row("RPS", f"{fmt_rps(summary.get('rps', 0))} req/s")
    table.add_row("吞吐", f"{fmt_bytes(summary.get('throughput_bps', 0))}/s")
    table.add_row(
        "延迟(ms)",
        "min {min} · avg {avg} · p50 {p50} · p90 {p90} · p95 {p95} · p99 {p99} · p999 {p999} · max {max}".format(
            **{k: f"{v:.1f}" if isinstance(v, float) else v for k, v in lat.items()},
        ),
    )
    sc = summary.get("status_codes", {})
    sc_txt = ", ".join(f"{k}: {v:,}" for k, v in list(sc.items())[:12])
    if sc_txt:
        table.add_row("状态码", sc_txt)
    errk = summary.get("error_kinds", [])
    if errk:
        top = errk[0]
        table.add_row("主要错误", f"{top.get('exc_type', '')} @ {top.get('endpoint', '')} × {top.get('count', 0)}")
    console.print()
    console.print(table)
    if interrupted:
        console.print("[yellow]注意: 本次运行被中断，结果仅供参考。[/]")


# ---------------------------------------------------------------------------
# 主运行
# ---------------------------------------------------------------------------

async def run(config: TestConfig) -> int:
    if not config.quiet:
        console.print(f"[bold cyan]⚡ WebStressTesting {__version__}[/] 基于 [bold]AioTest[/] 压测开始")
        console.print(f"  [dim]目标[/] {config.target}")
        console.print(f"  [dim]负载[/] {config.load.users} 用户 · 爬坡 {config.load.ramp_up:.0f}s · "
                      f"平稳 {config.load.steady:.0f}s · 降载 {config.load.ramp_down:.0f}s"
                      + (f" · 上限 {config.load.max_requests:,} 请求" if config.load.max_requests else ""))
        console.print()

    # 预检（在任何目录/文件产生之前，失败则直接返回）
    if config.preflight:
        ok = await preflight(config)
        if not ok:
            return 1

    # 报告目录 & chdir（让 aiotest 的 logs/ 落在报告目录内）
    os.makedirs(config.report_dir, exist_ok=True)
    os.chdir(config.report_dir)

    # ---- 首次导入 aiotest（必须在 chdir 之后，避免污染工作目录）----
    from aiotest import configure_connector
    from aiotest.clients import close_all_connections
    from aiotest.events import request_metrics
    from aiotest.runner_factory import RunnerFactory

    redirect_aiotest_logging(config.report_dir, config.loglevel)

    # 高并发下放大连接池
    users = max(config.load.users, 1)
    try:
        configure_connector(
            limit=max(1000, users * 2),
            limit_per_host=max(users, 100),
            verify_ssl=config.request.verify_ssl,
        )
    except Exception:
        pass

    base_url, _ = split_target(config.target)
    aggregator = MetricsAggregator()
    await request_metrics.add_handler(aggregator.on_request_metrics)

    user_cls = build_user_class(config, base_url, request_counter=lambda: aggregator.total)
    shape = build_shape(config, request_counter=lambda: aggregator.total)

    opts = SimpleNamespace(
        host=base_url,
        prometheus_port=config.prometheus_port,
        metrics_collection_interval=5.0,
        metrics_batch_size=500,
        metrics_flush_interval=1.0,
        metrics_buffer_size=20000,
        loglevel=config.loglevel,
    )

    runner = await RunnerFactory.create("local", [user_cls], shape, opts)

    dashboard = Dashboard(enabled=config.dashboard and not config.quiet)
    stop_evt = asyncio.Event()
    tick_task = asyncio.create_task(
        tick_loop(stop_evt, config, aggregator, runner, shape, dashboard))

    started_wall = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    t_start = time.monotonic()
    interrupted = False
    try:
        dashboard.start()
        await runner.start()
        await runner.run_until_complete()
    except (KeyboardInterrupt, asyncio.CancelledError):
        interrupted = True
        if not config.quiet:
            console.print("\n[yellow]收到中断信号，正在优雅收尾...[/]")
    finally:
        stop_evt.set()
        try:
            await asyncio.wait_for(tick_task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            tick_task.cancel()
            try:
                await tick_task
            except (asyncio.CancelledError, Exception):
                pass
        dashboard.stop()
        try:
            await runner.quit()
        except Exception as e:
            console.print(f"[red]收尾异常: {e}[/]")
        # 补一帧最终快照
        try:
            aggregator.snapshot(users=runner.active_user_count, phase="done" if not interrupted else "interrupted")
        except Exception:
            pass

    await close_all_connections()

    summary = aggregator.summary()
    elapsed_wall = time.monotonic() - t_start
    ended_wall = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    summary["elapsed_seconds"] = round(elapsed_wall, 3)

    # ---------- 输出报告 ----------
    report_json: Dict[str, Any] = {
        "meta": {
            "tool": f"WebStressTesting {__version__}",
            "framework": "aiotest",
            "target": config.target,
            "started_at": started_wall,
            "ended_at": ended_wall,
            "interrupted": interrupted,
            "config": config.to_dict(),
        },
        "summary": summary,
        "series": aggregator.series,
    }
    write_json(os.path.join(config.report_dir, "report.json"), report_json)
    write_series_csv(os.path.join(config.report_dir, "series.csv"), aggregator.series)
    write_failures_csv(os.path.join(config.report_dir, "failures.csv"), list(aggregator.error_samples))
    write_config_json(os.path.join(config.report_dir, "config.json"), config.to_dict())

    html = build_html_report(
        cfg=config.to_dict(),
        summary=summary,
        series=aggregator.series,
        started_at=started_wall,
        ended_at=ended_wall,
        interrupted=interrupted,
    )
    html_path = os.path.join(config.report_dir, "report.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    if not config.quiet:
        print_summary(config, summary, started_wall, ended_wall, interrupted)
    console.print()
    console.print("[bold green]✔ 报告已保存[/]")
    for fname in ("report.html", "report.json", "series.csv", "failures.csv"):
        console.print(f"  [cyan]{os.path.join(config.report_dir, fname)}[/]")
    return 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    _configure_encoding()
    args = parse_args(argv)
    try:
        config = build_config(args)
    except ConfigError as e:
        console.print(f"[red]配置错误:[/] {e}")
        return 2
    except Exception as e:
        console.print(f"[red]解析参数失败:[/] {e}")
        return 2

    # AioTest 在 Windows 上默认推荐 Selector 事件循环；但 Selector 基于 select()，
    # 受 FD_SETSIZE 限制（约 512 个 socket），500 并发会报
    # "too many file descriptors in select()"。
    # 因此并发较高时自动切换为 Proactor（IOCP，无 fd 上限，可支撑数千连接）。
    if sys.platform == "win32":
        policy = getattr(args, "loop_policy", "auto") or "auto"
        if policy == "proactor" or (policy == "auto" and config.load.users > 256):
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
            if not args.quiet and policy == "auto":
                console.print("[yellow]提示:[/] 高并发（>256 用户）已自动切换为 Proactor 事件循环（IOCP），"
                              "避免 select() 文件描述符上限；可用 --loop-policy 覆盖")
        else:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            if policy == "selector" and config.load.users > 256 and not args.quiet:
                console.print("[yellow]警告:[/] Selector 事件循环受 select() 文件描述符上限影响，"
                              f"{config.load.users} 用户可能触发 'too many file descriptors in select()'，"
                              "建议改用 --loop-policy proactor")

    try:
        return asyncio.run(run(config))
    except KeyboardInterrupt:
        console.print("\n[yellow]已取消。[/]")
        return 130
    except Exception as e:
        console.print(f"[red]运行失败:[/] {type(e).__name__}: {e}")
        if args.loglevel == "DEBUG":
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())