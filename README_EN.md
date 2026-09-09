# ⚡ WebStressTesting — A Modern Website Stress Testing Tool Built on AioTest

> Give it a URL (domain or full link) and it stress-tests the website with high concurrency:
> a real-time terminal dashboard, offline chart reports and machine-readable data in one command.

[**English**](./README_EN.md) | [**中文**](./README.md)

![Version](https://img.shields.io/badge/version-0.2.0-38bdf8.svg)
![Python](https://img.shields.io/badge/python-3.10%2B-34d399.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Based on](https://img.shields.io/badge/based%20on-AioTest-22d3ee.svg)

![Sample HTML report](assets/screenshots/report.png)

---

## Table of Contents

- [Overview](#overview)
- [Highlights](#highlights)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [CLI Reference](#cli-reference)
- [Load Model](#load-model)
- [Request Configuration](#request-configuration)
- [Reports](#reports)
- [Relationship with AioTest](#relationship-with-aiotest)
- [High Concurrency on Windows](#high-concurrency-on-windows)
- [FAQ](#faq)
- [Self Check](#self-check)
- [Contributing](#contributing)
- [License & Disclaimer](#license--disclaimer)

## Overview

**WebStressTesting** is a modern, asyncio-based website stress (load) testing tool.
Provide a domain or URL and it continuously hammers the target with high-concurrency
requests, showing live metrics — RPS, latency percentiles, success rate — on a terminal
dashboard, then produces a shareable HTML chart report plus JSON / CSV data for
record-keeping, comparison and CI integration.

Under the hood it is built on **AioTest** (an asyncio load testing framework): it dynamically
creates `HttpUser` and `LoadUserShape` classes, runs them with AioTest's `LocalRunner`
in-process, and aggregates metrics by subscribing to AioTest's `request_metrics` event.

## Highlights

- **One command for any website**: `WebStressTesting https://example.com -u 100 -d 60`
- **Three-stage load curve**: ramp-up → steady → ramp-down (auto or manual)
- **Real-time terminal dashboard**: RPS, latency percentiles, success rate, status codes, throughput and sparklines
- **Connectivity preflight**: DNS resolution, HTTP status, Server header and response time before the run
- **Flexible requests**: any HTTP method, custom headers, query params, JSON / file body, timeouts, skip SSL verification
- **Precise early stop**: `--max-requests` keeps overshoot within a few requests; `-d 0` = pure request-count mode
- **Offline HTML report**: inline SVG charts (throughput / latency / users / status codes), no CDN needed
- **Machine-readable artifacts**: `report.json` / `series.csv` / `failures.csv` / `config.json`
- **Prometheus endpoint**: `--prometheus-port 8089` exposes AioTest's native `/metrics`
- **High concurrency on Windows**: automatically switches to Proactor (IOCP) beyond 256 users, bypassing the `select()` file-descriptor limit
- **Built-in ProxyPool**: a self-hosted crawl → validate → store (MySQL) → API pipeline that keeps only valid proxies; flag-in supported via `--proxy-api` / `--proxy-file` / `--proxy` with **one user = one IP** (sticky, auto re-bind on failure)

## Installation

```bash
# Option 1: install dependencies and run as a module
pip install -r requirements.txt
python -m web_stress_testing --help

# Option 2: editable install, provides the `WebStressTesting` command
pip install -e .
WebStressTesting --help
```

> Requirements: Python 3.10+. On Windows, **Windows Terminal / PowerShell** is recommended
> for the best terminal rendering. Dependencies are simply `aiotest` and `rich`.

## Quick Start

```bash
# Stress-test example.com: 100 concurrent users, 60s steady phase (auto ramp)
python -m web_stress_testing https://example.com -u 100 -d 60
# or, after install:
WebStressTesting https://example.com -u 100 -d 60

# POST + JSON body + custom headers + query param
WebStressTesting --url https://api.example.com/login -X POST \
    --body '{"user":"bob","pwd":"x"}' --content-type application/json \
    --headers "Authorization: Bearer xxx" --param "v=1" -u 50 -d 30

# 320 users, 120s, explicit 30s ramp-up, stop after 50k requests
WebStressTesting https://example.com -u 320 -d 120 --ramp-up 30 --max-requests 50000

# Self-signed certificate
WebStressTesting https://selfsigned.example.com -u 20 -d 30 --no-verify-ssl

# Domain only — https:// is added automatically
WebStressTesting example.com -u 20 -d 30
```

When the run finishes, report paths are printed:

```
✔ Report saved
  reports\example.com_20260908_121500\report.html
  reports\example.com_20260908_121500\report.json
  ...
```

Open `report.html` for the full chart report (the image above is an example).

## CLI Reference

| Option | Description | Default |
|---|---|---|
| `target` / `--url` | Target URL (domain or full URL; `https://` is auto-added) | required |
| `-u, --users` | Peak concurrent users | 10 |
| `-d, --duration` | Steady phase duration in seconds | 60 |
| `--ramp-up` | Ramp-up seconds | auto |
| `--ramp-down` | Ramp-down seconds | auto |
| `--spawn-rate` | User spawn rate (users/sec) | users / ramp_up |
| `--think-time` | Think time between requests (sec) | 0 |
| `--max-requests` | Stop after this many requests | none |
| `-X, --method` | HTTP method GET/POST/PUT/DELETE... | GET |
| `--path` | Request path (overrides URL path) | URL path |
| `--headers` | Headers: JSON object or `K: V, K2: V2` | none |
| `--param K=V` | Query params (repeatable) | none |
| `--body` | Request body: JSON string or `@file` | none |
| `--content-type` | Content-Type (with `--body`) | none |
| `--timeout` | Per-request timeout (sec) | 30 |
| `--no-verify-ssl` | Skip SSL certificate verification | verify |
| `--preflight` / `--no-preflight` | Connectivity preflight | on |
| `--no-dashboard` | Disable the live dashboard | on |
| `-q, --quiet` | Quiet mode | off |
| `--report-dir` | Report output directory | `./reports/<name>_<timestamp>` |
| `--name` | Test name | target host |
| `--prometheus-port` | AioTest metrics port (0=random) | 0 |
| `--proxy` | Single proxy, e.g. `ip:port` or `http://ip:port` | none |
| `--proxy-file` | Proxy list file (one per line) | none |
| `--proxy-api` | ProxyPool service URL (e.g. `http://127.0.0.1:5010`) | none |
| `--proxy-sticky` / `--no-proxy-sticky` | One proxy IP per user (sticky) | on |
| `--loop-policy` | Windows event loop: `auto`/`selector`/`proactor` | auto |
| `--loglevel` | AioTest log level (written to aiotest.log) | WARNING |

## Load Model

The default run follows three stages:

```
users
peak ┤        ┌───────────────┐
     │       /                 \
     │      /                   \
   0 ┤─────┴─────────────────────┴──▶ time
      ramp-up       steady      ramp-down
```

- **ramp-up**: users grow smoothly from 0 to peak (default ≈15% of total duration, configurable)
- **steady**: keep the peak for `--duration` seconds
- **ramp-down**: users scale back down (default ≈10% of total duration)
- **Pure request-count mode**: `-d 0 --max-requests N` keeps full load until N requests are sent
- **Early exit**: whichever comes first — `--max-requests` or the time budget

## Reports

Each run writes the following files into the report directory:

| File | Contents |
|---|---|
| `report.html` | Chart-based report (self-contained, offline, shareable) |
| `report.json` | Full summary + per-second time series + error samples |
| `series.csv` | One row per second: RPS, latency percentiles, users, failures, throughput |
| `failures.csv` | Error details (status, type, message; up to 2000 rows) |
| `config.json` | Full configuration replay of this run |
| `aiotest.log` | AioTest framework logs |

Metrics:

- **Latency percentiles**: min / avg / p50 / p90 / p95 / p99 / p999 / max of all request
  durations, computed from a 1ms histogram with constant memory (safe for long, high-RPS runs).
- **RPS / Throughput**: requests per second / response bytes per second.
- **Errors**: 4xx / 5xx, connection errors and timeouts all count as failures,
  grouped by (path, status, error type).

## Relationship with AioTest

- WebStressTesting drives AioTest in-process: `RunnerFactory.create("local", ...)` +
  `runner.start()` + `runner.run_until_complete()` — no extra subprocess.
- The virtual users are dynamically generated `HttpUser` subclasses (task method `test_pressure`);
  the load curve is a dynamically generated `LoadUserShape` subclass (`tick()` returns target users & rate).
- Metrics are aggregated by subscribing to AioTest's `request_metrics` event,
  in parallel with AioTest's built-in Prometheus collector.
- Prefer the native AioTest style? See [`examples/website_pressure.py`](examples/website_pressure.py):

```bash
pip install aiotest
python -m aiotest -f examples/website_pressure.py -H https://example.com
```

## High Concurrency on Windows

`select()`-based event loops on Windows are limited by the file-descriptor set size
(≈512 sockets). With 500+ concurrent users you may hit:

```
ValueError: too many file descriptors in select()
```

How WebStressTesting handles it:

- Automatically switches to **Proactor (IOCP)** when users > 256 — no fd limit, thousands of connections supported;
- Override anytime with `--loop-policy selector|proactor`;
- A warning is shown if you force `selector` with high concurrency.

## FAQ

**Q: `too many file descriptors in select()`?**
A: That's the Windows `select()` limit. Use v0.2.0+ — it switches to Proactor automatically
beyond 256 users, or pass `--loop-policy proactor` explicitly.

**Q: Very high P99 / dropping success rate?**
A: Usually a real bottleneck of the target (bandwidth, connections, CPU, rate limiting).
Lower `-u` step by step (500 → 300 → 200) to find the throughput knee point, and compare
the curves in `report.html`.

**Q: Garbled Chinese in the terminal?**
A: Use Windows Terminal / PowerShell (or switch the console codepage to UTF-8).
The tool always outputs UTF-8.

**Q: How are 4xx / 5xx handled?**
A: All count as failures (error rate) and are recorded in `failures.csv`. A 404 does not
necessarily mean the system is broken — interpret it against your business rules.

**Q: Does it retry requests?**
A: No. `max_retries=0` in load-testing mode — each request is sent exactly once so retries
cannot distort the metrics.

## Self Check

```bash
python scripts/self_check.py            # main flow
python scripts/self_check_proxypool.py  # ProxyPool e2e (local proxies + one-user-one-IP)
```

See [`docs/PROXY_POOL.md`](docs/PROXY_POOL.md) for the built-in ProxyPool: service, API, validation, stress-test integration.

## Legacy Self-Check

```bash
python scripts/self_check.py
```

Starts a local mock server, runs a full stress test and validates the report artifacts and
metrics — handy for CI.

## Contributing

Issues and PRs are welcome: feature ideas, bug reports, documentation improvements,
more load shapes (staged, stepped, requests-per-second mode, etc.).

## License & Disclaimer

- Licensed under the [MIT License](./LICENSE).
- **Only run stress tests against systems you are authorized to test.** Before load-testing
  public or production domains, make sure you have permission — high concurrency may trigger
  rate limiting, bans, or impact live services.
