# -*- coding: utf-8 -*-
import os, pathlib, sys, time, subprocess, json, shutil
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

root = pathlib.Path(r"D:\workbuddy_p\WebStressTesting")
py = r"C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
mock = pathlib.Path(os.environ["TEMP"]) / "wst_selfcheck_mock.py"
rep = pathlib.Path(os.environ["TEMP"]) / "wst_report_check"

mock.write_text('''
import asyncio, random
from aiohttp import web

async def root(request):
    await asyncio.sleep(random.uniform(0.005, 0.02))
    return web.Response(text="hello", status=200)

async def slow(request):
    await asyncio.sleep(0.15)
    return web.Response(text="slow", status=200)

async def err(request):
    return web.Response(text="err", status=500)

async def main():
    app = web.Application()
    app.router.add_get("/", root)
    app.router.add_get("/slow", slow)
    app.router.add_get("/err", err)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 18088)
    await site.start()
    print("MOCK_READY", flush=True)
    await asyncio.Event().wait()

asyncio.run(main())
''', encoding="utf-8")

proc = subprocess.Popen([py, str(mock)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace')
try:
    time.sleep(2)
    if proc.poll() is not None:
        out, err = proc.communicate()
        print("mock failed:", out, err)
        sys.exit(1)
    print("--- mock server started ---")

    if rep.exists():
        shutil.rmtree(rep)
    res = subprocess.run(
        [py, "-m", "web_stress_testing", "http://127.0.0.1:18088", "-u", "20", "-d", "8",
         "--ramp-up", "2", "--ramp-down", "1", "--no-dashboard",
         "--report-dir", str(rep), "--loglevel", "WARNING"],
        cwd=str(root), capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120)
    print("wst stdout:")
    print(res.stdout)
    if res.stderr:
        print("wst stderr (tail):")
        print(res.stderr[-2000:])
    if res.returncode != 0:
        print("WST FAILED rc=", res.returncode)
        sys.exit(1)

    for f in ("report.html", "report.json", "series.csv", "failures.csv", "config.json", "aiotest.log"):
        p = rep / f
        if not p.exists():
            print("missing report file:", f)
            sys.exit(1)

    data = json.loads((rep / "report.json").read_text(encoding="utf-8"))
    s = data["summary"]
    print(f"requests = {s['total_requests']}, failed = {s['failed']}, rps = {s['rps']:.1f}, series_rows = {len(data['series'])}")
    if s["total_requests"] < 50:
        print("LOW REQUEST COUNT")
        sys.exit(1)
    print("SELF CHECK PASSED")
finally:
    if proc.poll() is None:
        proc.kill()