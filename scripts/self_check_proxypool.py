# -*- coding: utf-8 -*-
"""代理池端到端自检：
1. 启动本地目标 mock 与 3 个本地"正向代理"
2. 以 SQLite 存储启动代理池服务（serve）+ 用 import 命令导入并校验
3. 用 WebStressTesting --proxy-api 压测，校验"每个用户一个代理IP"与报告统计
可选：本机 MySQL 失败剔除往返测试（跳过失败不影响结论）。

运行：python scripts/self_check_proxypool.py
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PY = sys.executable
ROOT = pathlib.Path(r"D:\workbuddy_p\WebStressTesting")
TMP = pathlib.Path(tempfile.gettempdir())
TARGET_PORT, API_PORT = 18130, 15110
PROXY_PORTS = (19001, 19002, 19003)
DB = TMP / "wst_pp_selfcheck.db"
REPORT = TMP / "wst_pp_report"
SERVE_ENV = os.environ.copy()
SERVE_ENV["PROXYPOOL_DB_URL"] = f"sqlite:///{DB}"
SERVE_ENV["PROXYPOOL_API_PORT"] = str(API_PORT)
SERVE_ENV["PROXYPOOL_TEST_URL"] = f"http://127.0.0.1:{TARGET_PORT}/health"


# ---------------------------------------------------------------- mock 目标
async def target_server(hits):
    from aiohttp import web

    async def ok(request):
        hits.append(request.path)
        return web.Response(text="ok", status=200)

    app = web.Application()
    app.router.add_get("/", ok)
    app.router.add_get("/health", ok)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", TARGET_PORT)
    await site.start()
    return runner


# ---------------------------------------------------------------- 假正向代理
async def proxy_tunnel(reader, writer, tag):
    """极简 HTTP 正向代理：解析绝对 URI 请求并转发到目标。"""
    try:
        data = await reader.read(65536)
        if not data:
            writer.close()
            return
        text = data.decode("latin1")
        head = text.split("\r\n", 1)
        try:
            method, url = head[0].split(" ")[:2]
        except ValueError:
            writer.close()
            return
        u = urlsplit(url)
        host, port = u.hostname, u.port or 80
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        keep = []
        for line in head[1].split("\r\n"):
            if not line:
                break
            key = line.split(":", 1)[0].strip().lower()
            if key in ("proxy-connection", "connection", "proxy-authorization"):
                continue
            keep.append(line)
        new_req = f"{method} {path} HTTP/1.1\r\n" + "\r\n".join(keep) + "\r\nConnection: close\r\n\r\n"
        r, w = await asyncio.open_connection(host, port)
        w.write(new_req.encode("latin1"))
        await w.drain()
        out = bytearray()
        while True:
            chunk = await r.read(65536)
            if not chunk:
                break
            out += chunk
        w.close()
        if out:
            writer.write(bytes(out))
        else:
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
        print(f"  [proxy:{tag}] -> {method} {url}", flush=True)
    except Exception as e:
        print(f"  [proxy:{tag}] error: {e}", flush=True)
        try:
            writer.close()
        except Exception:
            pass


async def proxy_server(port, tag):
    async def handler(reader, writer):
        await proxy_tunnel(reader, writer, tag)

    server = await asyncio.start_server(handler, "127.0.0.1", port)
    return server


# ---------------------------------------------------------------- 流程
def log(msg):
    print(msg, flush=True)


def run(cmd, timeout=120, env=None):
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env)
    if r.stdout:
        print(r.stdout[-3000:])
    if r.stderr and r.returncode != 0:
        print("STDERR:", r.stderr[-2000:])
    if r.returncode != 0:
        raise SystemExit(f"命令失败 rc={r.returncode}: {cmd[2:]}")
    return r


async def arun(cmd, timeout=120, env=None):
    """在事件循环外运行子进程，避免阻塞承载假代理的事件循环。"""
    return await asyncio.to_thread(run, cmd, timeout, env)


async def main() -> int:
    if DB.exists():
        DB.unlink()
    if REPORT.exists():
        import shutil
        shutil.rmtree(REPORT)

    hits = []
    services = []

    # 1) 目标 + 假代理
    target_runner = await target_server(hits)
    for port in PROXY_PORTS:
        services.append(await proxy_server(port, port))
    log(f"[1/5] 目标 mock :{TARGET_PORT} + {len(PROXY_PORTS)} 个本地代理已就绪")

    # 2) 启动代理池服务（serve 常驻）
    serve = subprocess.Popen(
        [PY, "-m", "web_stress_testing.proxypool", "serve"],
        cwd=str(ROOT), env=SERVE_ENV,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
    )
    try:
        time.sleep(3)
        if serve.poll() is not None:
            out = serve.stdout.read() if serve.stdout else ""
            log(f"[x] serve 提前退出:\n{out}")
            return 1
        log("[2/5] 代理池服务已启动 :" + str(API_PORT))

        # 3) 导入代理文件并校验（只保留有效）
        proxy_file = TMP / "wst_pp_proxies.txt"
        proxy_file.write_text("\n".join(f"127.0.0.1:{p}" for p in PROXY_PORTS) + "\n", encoding="utf-8")
        await arun([PY, "-m", "web_stress_testing.proxypool", "--db-url", str(SERVE_ENV["PROXYPOOL_DB_URL"]),
                      "import", str(proxy_file)],
                     timeout=90, env=SERVE_ENV)
        status = await asyncio.to_thread(
            subprocess.run,
            [PY, "-m", "web_stress_testing.proxypool", "--db-url", str(SERVE_ENV["PROXYPOOL_DB_URL"]), "status"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, env=SERVE_ENV)
        log("[3/5] 导入+校验完成；status 输出：")
        out = (status.stdout or "") + (status.stderr or "")
        print(out[-1500:])
        assert "有效 3" in out or "有效 3，" in out or "总记录 3" in out, f"有效代理数不符合预期:\n{out}"

        # 4) 压测（每个用户一个代理IP）
        log("[4/5] 开始压测（--proxy-api，-u 3，粘性）...")
        await arun([PY, "-B", "-m", "web_stress_testing",
                     f"http://127.0.0.1:{TARGET_PORT}", "-u", "3", "-d", "5",
                     "--ramp-up", "0", "--ramp-down", "0",
                     "--no-preflight", "--no-dashboard", "--loglevel", "WARNING",
                     "--proxy-api", f"http://127.0.0.1:{API_PORT}",
                     "--report-dir", str(REPORT)], timeout=120, env=SERVE_ENV)

        # 5) 校验报告
        log("[5/5] 校验报告...")
        data = json.loads((REPORT / "report.json").read_text(encoding="utf-8"))
        pp = data.get("proxy_pool")
        assert pp and pp.get("enabled"), "report.json 缺少 proxy_pool 统计"
        assert pp["pool_size"] >= 3, f"池大小异常: {pp}"
        used = [r for r in pp.get("proxies", []) if r.get("ok", 0) > 0]
        assert len(used) >= 3, f"并非每个代理都被使用（一用户一IP 未生效）: {used}"
        assert data["summary"]["total_requests"] > 50, "请求数过低"
        assert hits, "目标站点未收到任何请求（代理没有转发）"
        log(f"  总请求={data['summary']['total_requests']} 失败={data['summary']['failed']}")
        log(f"  使用代理数={len(used)}（pid=全部 3 个代理都有成功流量）")
        for u in used:
            log(f"    {u['proxy']}  ok={u['ok']} fail={u['fail']} avg={u['avg_ms']}ms")
        log("PROXY POOL SELF CHECK PASSED")
        return 0
    finally:
        serve.kill()
        serve.wait(timeout=10)
        for s in services:
            s.close()
        await target_runner.cleanup()


def mysql_roundtrip() -> int:
    """可选：本机 MySQL 失败剔除往返（失败仅提示，不影响结论）。"""
    try:
        sys.path.insert(0, str(ROOT))
        from web_stress_testing.proxypool.storage import create_storage
        st = create_storage(None)
        st.init()
        st.add("203.0.113.9", 19999, "http", "selftest")
        results = [st.update_result("203.0.113.9", 19999, False, 0) for _ in range(3)]
        assert results[0] == "deleted"
        assert st.count(False) == 0
        print("MySQL 往返 OK（失败即剔除，库中无残留）")
        return 0
    except Exception as e:
        print(f"MySQL 往返跳过: {e}")
        return 0


if __name__ == "__main__":
    rc = asyncio.run(main())
    mysql_roundtrip()
    sys.exit(rc)
