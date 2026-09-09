# -*- coding: utf-8 -*-
"""代理池服务入口。

用法:
  python -m web_stress_testing.proxypool serve                 # 常驻服务（调度+API）
  python -m web_stress_testing.proxypool import proxies.txt    # 导入并校验代理文件
  python -m web_stress_testing.proxypool status                # 查看池状态
  python -m web_stress_testing.proxypool add --ip 1.2.3.4 --port 8080

环境变量覆盖：PROXYPOOL_DB_URL（sqlite:///path）、PROXYPOOL_API_PORT、PROXYPOOL_TEST_URL 等。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from aiohttp import web

from web_stress_testing.proxypool.api import build_app
from web_stress_testing.proxypool.config import ProxyPoolConfig
from web_stress_testing.proxypool.scheduler import PoolScheduler
from web_stress_testing.proxypool.storage import create_storage
from web_stress_testing.proxypool.validator import validate_and_apply


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)8s | %(name)s | %(message)s",
        force=True,
    )


def _parse_proxy_line(line: str):
    """解析 'ip:port' 或 'http://ip:port'；非法行返回 None。"""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.lower().startswith(("http://", "https://", "socks4://", "socks5://")):
        from urllib.parse import urlsplit
        u = urlsplit(line)
        if u.hostname and u.port:
            return {"ip": u.hostname, "port": u.port,
                    "protocol": u.scheme, "source": "file"}
        return None
    if ":" in line:
        ip, _, port = line.rpartition(":")
        if ip and port.isdigit():
            return {"ip": ip, "port": int(port), "protocol": "http", "source": "file"}
    return None


async def cmd_import(cfg: ProxyPoolConfig, path: str, validate: bool = True) -> int:
    storage = create_storage(cfg.db_url)
    storage.init()
    rows = []
    for line in Path(path).read_text(encoding="utf-8", errors="ignore").splitlines():
        p = _parse_proxy_line(line)
        if p:
            rows.append(p)
    print(f"文件解析: {len(rows)} 个代理")
    added = 0
    for p in rows:
        if await asyncio.to_thread(storage.add, p["ip"], p["port"], p["protocol"], p["source"]):
            added += 1
    print(f"新增入库: {added}（其余为已存在）")
    if validate:
        stats = await validate_and_apply(cfg, storage, rows, source_hint="file")
        print(f"校验: 共 {stats['total']}，通过 {stats['ok']}，失败 {stats['fail']}，失效删除 {stats['deleted']}")
        if stats["fail"]:
            from web_stress_testing.proxypool.validator import validate_batch
            results = await validate_batch(cfg, rows[:3])
            for res in results:
                p_ = res["proxy"]
                print(f"  失败样例 {p_['ip']}:{p_['port']} -> {res['error'] or 'unknown'}")
        valid = await asyncio.to_thread(storage.count, True)
        print(f"当前有效代理: {valid}")
    storage.close()
    return 0


async def cmd_status(cfg: ProxyPoolConfig) -> int:
    storage = create_storage(cfg.db_url)
    storage.init()
    total = await asyncio.to_thread(storage.count, False)
    valid = await asyncio.to_thread(storage.count, True)
    print(f"总记录 {total}，有效 {valid}，无效 {total - valid}")
    rows = await asyncio.to_thread(storage.get_all)
    for r in rows[:20]:
        print(f"  {r['protocol']}://{r['ip']}:{r['port']}  score={r['score']} "
              f"latency={r['latency_ms']}ms ok={r['success_count']} fail={r['fail_count']}")
    if len(rows) > 20:
        print(f"  ... 其余 {len(rows) - 20} 条省略")
    storage.close()
    return 0


async def cmd_add(cfg: ProxyPoolConfig, ip: str, port: int, protocol: str) -> int:
    from web_stress_testing.proxypool.validator import validate_one
    storage = create_storage(cfg.db_url)
    storage.init()
    res = await validate_one(cfg, {"ip": ip, "port": port, "protocol": protocol})
    print(f"校验: {'通过' if res['ok'] else '失败'} ({res['error'] or str(res['latency_ms']) + 'ms'})")
    if res["ok"]:
        await asyncio.to_thread(storage.add, ip, port, protocol, "cli")
        await asyncio.to_thread(storage.update_result, ip, port, True, res["latency_ms"])
        print(f"已入库 {protocol}://{ip}:{port}")
    storage.close()
    return 0 if res["ok"] else 1


async def cmd_serve(cfg: ProxyPoolConfig) -> int:
    storage = create_storage(cfg.db_url)
    storage.init()
    scheduler = PoolScheduler(cfg, storage)
    app = build_app(cfg, storage, scheduler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, cfg.api_host, cfg.api_port)
    await site.start()
    print(f"ProxyPool 服务已启动: http://{cfg.api_host}:{cfg.api_port} "
          f"(存储={'sqlite:' + cfg.db_url if cfg.db_url else 'mysql'})", flush=True)
    await scheduler.start()
    print("调度器已启动: 抓取/校验/清理 循环运行中", flush=True)

    # 启动即活动：立刻清理一次，并异步执行一轮爬取（API 不阻塞）
    await scheduler.cleanup_pass()

    async def boot_crawl():
        await asyncio.sleep(2)
        stats = await scheduler.crawl_pass()
        dead = [s["source"] for s in scheduler.crawler.source_status() if not s["alive"]]
        print(f"[启动爬取] 本轮新增候选 {stats} 个；无效源（冷却）: {', '.join(dead) if dead else '无'}", flush=True)

    asyncio.create_task(boot_crawl())

    # 周期心跳：让运行状态可见
    async def heartbeat():
        while True:
            try:
                await asyncio.sleep(cfg.heartbeat_interval)
                total = await asyncio.to_thread(storage.count, False)
                valid = await asyncio.to_thread(storage.count, True)
                dead = [s["source"] for s in scheduler.crawler.source_status() if not s["alive"]]
                print(f"[心跳] 代理池 总={total} 有效={valid} 失效/冷却源={len(dead)} "
                      f"{('(' + ', '.join(dead) + ')') if dead else ''}", flush=True)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.getLogger("proxypool").warning("心跳异常: %s", e)

    hb = asyncio.create_task(heartbeat())
    total = await asyncio.to_thread(storage.count, False)
    valid = await asyncio.to_thread(storage.count, True)
    print(f"当前代理池: 总记录 {total}，有效 {valid}", flush=True)
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        hb.cancel()
        await scheduler.stop()
        await runner.cleanup()
        storage.close()
        print("ProxyPool 服务已停止")
    return 0


def main(argv=None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="proxypool", description="WebStressTesting 内置代理池")
    parser.add_argument("--db-url", default=None, help="存储连接串（默认本机 MySQL；sqlite:///path 可替代）")
    parser.add_argument("--port", type=int, default=None, help="API 端口（默认 5010）")
    parser.add_argument("--test-url", default=None, help="校验连通性目标地址")
    parser.add_argument("--loglevel", default=None, help="日志级别")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", help="常驻服务（调度+API）")
    p_import = sub.add_parser("import", help="导入代理文件并校验")
    p_import.add_argument("file", help="代理文件，每行 ip:port 或 protocol://ip:port")
    sub.add_parser("status", help="查看池状态")
    p_add = sub.add_parser("add", help="添加并校验单个代理")
    p_add.add_argument("--ip", required=True)
    p_add.add_argument("--port", type=int, required=True)
    p_add.add_argument("--protocol", default="http", choices=["http", "https"])

    args = parser.parse_args(argv)
    cfg = ProxyPoolConfig.from_env()
    if args.db_url:
        cfg.db_url = args.db_url
    if args.port:
        cfg.api_port = args.port
    if args.test_url:
        cfg.test_url = args.test_url
    if args.loglevel:
        cfg.log_level = args.loglevel
    setup_logging(cfg.log_level)

    if args.command == "serve":
        return asyncio.run(cmd_serve(cfg))
    if args.command == "import":
        return asyncio.run(cmd_import(cfg, args.file))
    if args.command == "status":
        return asyncio.run(cmd_status(cfg))
    if args.command == "add":
        return asyncio.run(cmd_add(cfg, args.ip, args.port, args.protocol))
    return 2


if __name__ == "__main__":
    sys.exit(main())
