# -*- coding: utf-8 -*-
"""自包含 HTML 报表生成器：KPI 卡片 + 内联 SVG 图表 + 表格（无需外部资源）。

图表全部用 Python 生成内联 SVG，离线可看，避免依赖 CDN。
"""
from __future__ import annotations

import html as _html
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from web_stress_testing.utils import fmt_bytes, fmt_ms, fmt_pct, fmt_rps, safe_div

# ---------------------------------------------------------------------------
# 配色
# ---------------------------------------------------------------------------
C_BG = "#0b1220"
C_CARD = "#111a2e"
C_BORDER = "#1e293b"
C_TEXT = "#e2e8f0"
C_MUTED = "#8ea0bb"
C_AXIS = "#334155"
C_GRID = "#1c2942"
C_ACCENT = "#38bdf8"
C_GREEN = "#34d399"
C_YELLOW = "#fbbf24"
C_RED = "#f87171"
C_CYAN = "#22d3ee"
C_PURPLE = "#a78bfa"
C_ORANGE = "#fb923c"

MONO = '"SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace'


def esc(v: Any) -> str:
    return _html.escape(str(v), quote=True)


# ---------------------------------------------------------------------------
# SVG 图表
# ---------------------------------------------------------------------------

def _nice_ticks(lo: float, hi: float, n: int = 5) -> List[float]:
    if hi <= lo:
        hi = lo + 1
    span = hi - lo
    step_raw = span / max(1, n)
    mag = 10 ** (len(str(int(step_raw))) - 1) if step_raw >= 1 else 10 ** (-len(f"{step_raw:.10f}".split('.')[1].rstrip('0')) or 1)
    # 估算数量级
    import math
    if step_raw == 0:
        return [0.0]
    mag = 10 ** math.floor(math.log10(step_raw))
    for m in (1, 2, 5, 10):
        if step_raw <= m * mag:
            step = m * mag
            break
    else:
        step = 10 * mag
    start = math.floor(lo / step) * step
    ticks = []
    v = start
    while v <= hi + 1e-9 and len(ticks) < 12:
        ticks.append(round(v, 6))
        v += step
    return ticks


def _fmt_axis(v: float) -> str:
    av = abs(v)
    if av >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if av >= 1000:
        return f"{v / 1000:.1f}k"
    if av >= 100:
        return f"{v:.0f}"
    if av >= 10:
        return f"{v:.1f}"
    return f"{v:.2f}"


def svg_line_chart(
    series: List[Tuple[str, List[Tuple[float, float]], str]],
    width: int = 820,
    height: int = 280,
    ylabel: str = "",
    y_max: Optional[float] = None,
) -> str:
    """series: [(名称, [(x, y), ...], 颜色), ...]"""
    pad_l, pad_r, pad_t, pad_b = 52, 14, 20, 30
    iw, ih = width - pad_l - pad_r, height - pad_t - pad_b

    all_pts = [p for _, pts, _ in series for p in pts]
    if not all_pts:
        return ""
    xs = [p[0] for p in all_pts]
    ys = [p[1] for p in all_pts]
    x_lo, x_hi = min(xs), max(xs)
    y_lo = 0.0
    y_hi = y_max if y_max is not None else max(ys)
    if y_hi <= 0:
        y_hi = 1.0
    if x_hi == x_lo:
        x_hi = x_lo + 1

    def X(x: float) -> float:
        return pad_l + (x - x_lo) / (x_hi - x_lo) * iw

    def Y(y: float) -> float:
        return pad_t + (1 - (y - y_lo) / (y_hi - y_lo)) * ih

    y_ticks = _nice_ticks(y_lo, y_hi, 5)
    x_ticks = _nice_ticks(x_lo, x_hi, 6)

    parts: List[str] = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="100%" role="img" aria-label="{esc(ylabel)}">']
    parts.append(f'<rect x="0" y="0" width="{width}" height="{height}" fill="none"/>')

    for ty in y_ticks:
        y = Y(ty)
        parts.append(f'<line x1="{pad_l:.1f}" y1="{y:.1f}" x2="{width - pad_r:.1f}" y2="{y:.1f}" stroke="{C_GRID}" stroke-width="1"/>')
        parts.append(f'<text x="{pad_l - 6:.1f}" y="{y + 4:.1f}" text-anchor="end" font-size="10" fill="{C_MUTED}" font-family="{MONO}">{_fmt_axis(ty)}</text>')
    for tx in x_ticks:
        x = X(tx)
        parts.append(f'<text x="{x:.1f}" y="{height - 8:.1f}" text-anchor="middle" font-size="10" fill="{C_MUTED}" font-family="{MONO}">{_fmt_axis(tx)}</text>')

    # 边框
    parts.append(f'<rect x="{pad_l:.1f}" y="{pad_t:.1f}" width="{iw:.1f}" height="{ih:.1f}" fill="none" stroke="{C_AXIS}" stroke-width="1"/>')
    if ylabel:
        parts.append(f'<text x="12" y="{pad_t + 8:.1f}" font-size="11" fill="{C_MUTED}">{esc(ylabel)}</text>')

    for name, pts, color in series:
        if not pts:
            continue
        pts2 = pts if len(pts) >= 2 else [pts[0], (pts[0][0] + 1, pts[0][1])]
        d = "M" + " L".join(f"{X(x):.1f},{Y(y):.1f}" for x, y in pts2)
        parts.append(
            f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round">'
            f'<title>{esc(name)}</title></path>'
        )
        # 末端点
        lx, ly = pts2[-1]
        parts.append(f'<circle cx="{X(lx):.1f}" cy="{Y(ly):.1f}" r="2.5" fill="{color}"/>')

    # 图例
    lx = pad_l
    for name, _, color in series:
        parts.append(f'<rect x="{lx}" y="6" width="10" height="10" rx="2" fill="{color}"/>')
        parts.append(f'<text x="{lx + 14}" y="15" font-size="11" fill="{C_TEXT}">{esc(name)}</text>')
        lx += 14 + len(name) * 6.5 + 16
    parts.append("</svg>")
    return "".join(parts)


def svg_bar_chart(
    labels: List[str],
    values: List[float],
    colors: List[str],
    width: int = 820,
    height: int = 260,
    ylabel: str = "",
) -> str:
    pad_l, pad_r, pad_t, pad_b = 52, 14, 20, 34
    iw, ih = width - pad_l - pad_r, height - pad_t - pad_b
    if not labels:
        return ""
    y_hi = max(values) if values else 1.0
    if y_hi <= 0:
        y_hi = 1.0
    n = len(labels)
    slot = iw / n
    bar_w = max(6.0, slot * 0.62)
    y_ticks = _nice_ticks(0, y_hi, 5)

    def Y(v: float) -> float:
        return pad_t + (1 - v / y_hi) * ih

    parts: List[str] = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="100%" role="img">']
    for ty in y_ticks:
        y = Y(ty)
        parts.append(f'<line x1="{pad_l:.1f}" y1="{y:.1f}" x2="{width - pad_r:.1f}" y2="{y:.1f}" stroke="{C_GRID}" stroke-width="1"/>')
        parts.append(f'<text x="{pad_l - 6:.1f}" y="{y + 4:.1f}" text-anchor="end" font-size="10" fill="{C_MUTED}" font-family="{MONO}">{_fmt_axis(ty)}</text>')
    parts.append(f'<rect x="{pad_l:.1f}" y="{pad_t:.1f}" width="{iw:.1f}" height="{ih:.1f}" fill="none" stroke="{C_AXIS}" stroke-width="1"/>')
    if ylabel:
        parts.append(f'<text x="12" y="{pad_t + 8:.1f}" font-size="11" fill="{C_MUTED}">{esc(ylabel)}</text>')

    for i, (label, v, color) in enumerate(zip(labels, values, colors)):
        x = pad_l + i * slot + (slot - bar_w) / 2
        y = Y(v)
        h = max(ih - (y - pad_t), 1.0)
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{h:.1f}" rx="3" fill="{color}">'
            f'<title>{esc(label)}: {esc(_fmt_axis(v))}</title></rect>'
        )
        parts.append(f'<text x="{x + bar_w / 2:.1f}" y="{y - 5:.1f}" text-anchor="middle" font-size="10" fill="{C_TEXT}" font-family="{MONO}">{_fmt_axis(v)}</text>')
        parts.append(f'<text x="{x + bar_w / 2:.1f}" y="{height - 14:.1f}" text-anchor="middle" font-size="10" fill="{C_MUTED}">{esc(label)}</text>')
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# HTML 报表
# ---------------------------------------------------------------------------
CARD_TMPL = """
<div class="card">
  <div class="card-label">{label}</div>
  <div class="card-value" style="color:{color}">{value}</div>
  <div class="card-sub">{sub}</div>
</div>
"""


def _status_class_summary(status_codes: Dict[str, int]) -> Dict[str, int]:
    out = {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0, "err": 0}
    for k, v in status_codes.items():
        try:
            code = int(k)
        except ValueError:
            out["err"] += v
            continue
        if 200 <= code < 300:
            out["2xx"] += v
        elif 300 <= code < 400:
            out["3xx"] += v
        elif 400 <= code < 500:
            out["4xx"] += v
        elif 500 <= code < 600:
            out["5xx"] += v
        else:
            out["err"] += v
    return out


def build_html_report(cfg: Dict[str, Any], summary: Dict[str, Any],
                      series: List[Dict[str, Any]], started_at: str,
                      ended_at: str, interrupted: bool = False) -> str:
    lat = summary.get("latency_ms", {})
    sc = summary.get("status_codes", {})
    scs = _status_class_summary(sc)
    total = summary.get("total_requests", 0)
    failed = summary.get("failed", 0)
    ok_count = total - failed
    error_rate = safe_div(failed, total)

    cards = []
    cards.append(CARD_TMPL.format(label="总请求数", value=f"{total:,}", color=C_TEXT,
                                  sub=f"成功 {ok_count:,} · 失败 {failed:,}"))
    cards.append(CARD_TMPL.format(label="吞吐量 RPS", value=fmt_rps(summary.get("rps", 0)), color=C_ACCENT,
                                  sub=f"总耗时 {summary.get('elapsed_seconds', 0):.1f}s"))
    cards.append(CARD_TMPL.format(label="平均延迟", value=fmt_ms(lat.get("avg", 0)), color=C_GREEN,
                                  sub=f"P50 {fmt_ms(lat.get('p50', 0))}"))
    cards.append(CARD_TMPL.format(label="P95 延迟", value=fmt_ms(lat.get("p95", 0)), color=C_YELLOW,
                                  sub=f"P99 {fmt_ms(lat.get('p99', 0))}"))
    cards.append(CARD_TMPL.format(label="最大延迟", value=fmt_ms(lat.get("max", 0)), color=C_ORANGE,
                                  sub=f"最小 {fmt_ms(lat.get('min', 0))}"))
    cards.append(CARD_TMPL.format(label="错误率", value=fmt_pct(error_rate),
                                  color=C_RED if error_rate > 0.01 else C_GREEN,
                                  sub=f"总流量 {fmt_bytes(summary.get('bytes_total', 0))}"))

    # 图表
    xs = [s.get("t", i) for i, s in enumerate(series)]
    rps_series = [(xs[i], s.get("rps", 0)) for i, s in enumerate(series)]
    avg_series = [(xs[i], s.get("avg_ms", 0)) for i, s in enumerate(series)]
    p95_series = [(xs[i], s.get("p95_ms", 0)) for i, s in enumerate(series)]
    p99_series = [(xs[i], s.get("p99_ms", 0)) for i, s in enumerate(series)]
    users_series = [(xs[i], s.get("users", 0)) for i, s in enumerate(series)]

    chart_rps = svg_line_chart([("RPS", rps_series, C_ACCENT)], ylabel="请求/秒")
    chart_lat = svg_line_chart([
        ("avg", avg_series, C_GREEN),
        ("p95", p95_series, C_YELLOW),
        ("p99", p99_series, C_RED),
    ], ylabel="延迟 ms")
    chart_users = svg_line_chart([("活跃用户", users_series, C_CYAN)], ylabel="用户数")

    status_labels = ["2xx", "3xx", "4xx", "5xx", "err"]
    status_colors = [C_GREEN, C_CYAN, C_YELLOW, C_RED, C_PURPLE]
    status_values = [scs.get(l, 0) for l in status_labels]
    chart_status = svg_bar_chart(status_labels, [float(v) for v in status_values],
                                 status_colors, ylabel="请求数")

    # 延迟分位表
    lat_rows = ""
    for key, label in (("min", "最小"), ("p50", "P50"), ("p90", "P90"),
                       ("p95", "P95"), ("p99", "P99"), ("p999", "P999"),
                       ("max", "最大"), ("avg", "平均")):
        lat_rows += f"<tr><td>{label}</td><td>{fmt_ms(lat.get(key, 0))}</td></tr>\n"

    # 状态码表
    sc_rows = ""
    for code in sorted(sc.keys(), key=lambda c: int(c)):
        cnt = sc[code]
        pct = safe_div(cnt, total) * 100
        cls = "ok" if 200 <= int(code) < 400 else "bad"
        sc_rows += f"<tr><td>{esc(code)}</td><td>{cnt:,}</td><td class=\"{cls}\">{pct:.2f}%</td></tr>\n"

    # 错误分类表
    err_rows = ""
    for e in summary.get("error_kinds", []):
        err_rows += (
            f"<tr><td>{esc(e.get('method', ''))}</td><td>{esc(e.get('endpoint', ''))}</td>"
            f"<td>{esc(e.get('status', ''))}</td><td>{esc(e.get('exc_type', ''))}</td>"
            f"<td>{e.get('count', 0):,}</td></tr>\n"
        )

    # 错误样本
    samples = summary.get("error_samples", [])
    sample_rows = ""
    for s in samples[-10:]:
        sample_rows += (
            f"<tr><td>{esc(s.get('timestamp', ''))}</td><td>{esc(s.get('method', ''))}</td>"
            f"<td>{esc(s.get('endpoint', ''))}</td><td>{esc(s.get('status', ''))}</td>"
            f"<td class=\"mono\">{esc(s.get('message', ''))[:120]}</td></tr>\n"
        )
    if not samples:
        sample_rows = '<tr><td colspan="5" class="muted">无错误样本</td></tr>'

    req = cfg.get("request", {})
    load = cfg.get("load", {})
    meta_rows = f"""
      <tr><td>目标</td><td><b>{esc(cfg.get('target', ''))}</b></td></tr>
      <tr><td>请求</td><td>{esc(req.get('method', 'GET'))} {esc(req.get('path', '/'))} · 超时 {req.get('timeout', '')}s · SSL验证 {'开' if req.get('verify_ssl', True) else '关'}</td></tr>
      <tr><td>负载</td><td>并发用户 {load.get('users', '')} · 爬坡 {load.get('ramp_up', '')}s · 平稳 {load.get('steady', '')}s · 降载 {load.get('ramp_down', '')}s · 思考时间 {load.get('think_time', 0)}s</td></tr>
      <tr><td>时间</td><td>{esc(started_at)} → {esc(ended_at)}{' <span class="bad">(被中断)</span>' if interrupted else ''}</td></tr>
    """
    if req.get("headers"):
        meta_rows += f"<tr><td>请求头</td><td class=\"mono\">{esc(req.get('headers'))}</td></tr>\n"
    if req.get("params"):
        meta_rows += f"<tr><td>查询参数</td><td class=\"mono\">{esc(req.get('params'))}</td></tr>\n"
    if req.get("body") is not None:
        meta_rows += f"<tr><td>请求体</td><td class=\"mono\">{esc(str(req.get('body'))[:200])}</td></tr>\n"

    interrupted_html = (
        '<div class="banner bad">⚠ 测试被手动中断，以下为中断前的统计结果</div>'
        if interrupted else ""
    )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>压测报告 · {esc(cfg.get('target', ''))}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: {C_BG}; color: {C_TEXT}; font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; padding: 32px 24px 64px; }}
  .wrap {{ max-width: 980px; margin: 0 auto; }}
  h1 {{ font-size: 22px; font-weight: 700; margin-bottom: 6px; }}
  h2 {{ font-size: 16px; font-weight: 600; margin: 28px 0 12px; color: {C_ACCENT}; }}
  .sub {{ color: {C_MUTED}; font-size: 13px; margin-bottom: 18px; }}
  .banner {{ padding: 10px 14px; border-radius: 8px; margin: 14px 0; font-size: 13px; }}
  .banner.bad {{ background: rgba(248,113,113,.12); color: {C_RED}; border: 1px solid rgba(248,113,113,.35); }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin: 18px 0; }}
  .card {{ background: {C_CARD}; border: 1px solid {C_BORDER}; border-radius: 10px; padding: 14px 16px; }}
  .card-label {{ color: {C_MUTED}; font-size: 12px; margin-bottom: 6px; }}
  .card-value {{ font-size: 22px; font-weight: 700; font-family: {MONO}; }}
  .card-sub {{ color: {C_MUTED}; font-size: 11px; margin-top: 4px; }}
  .chart {{ background: {C_CARD}; border: 1px solid {C_BORDER}; border-radius: 10px; padding: 14px; margin-bottom: 14px; }}
  table {{ width: 100%; border-collapse: collapse; background: {C_CARD}; border: 1px solid {C_BORDER}; border-radius: 10px; overflow: hidden; font-size: 13px; }}
  th, td {{ padding: 9px 14px; text-align: left; border-bottom: 1px solid {C_BORDER}; }}
  th {{ color: {C_MUTED}; font-weight: 500; background: rgba(255,255,255,.02); }}
  td.mono, .mono {{ font-family: {MONO}; font-size: 12px; }}
  .ok {{ color: {C_GREEN}; }}
  .bad {{ color: {C_RED}; }}
  .muted {{ color: {C_MUTED}; }}
  .grid2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }}
  @media (max-width: 760px) {{ .grid2 {{ grid-template-columns: 1fr; }} }}
  footer {{ margin-top: 40px; color: {C_MUTED}; font-size: 12px; text-align: center; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>⚡ 压测报告</h1>
  <div class="sub">{esc(cfg.get('target', ''))} · 由 WebStressTesting 生成（基于 AioTest）</div>
  {interrupted_html}
  <div class="cards">{''.join(cards)}</div>

  <h2>吞吐量</h2>
  <div class="chart">{chart_rps}</div>

  <h2>延迟（ms）</h2>
  <div class="chart">{chart_lat}</div>

  <h2>活跃用户</h2>
  <div class="chart">{chart_users}</div>

  <h2>状态码分布</h2>
  <div class="chart">{chart_status}</div>

  <div class="grid2">
    <div>
      <h2>延迟分位数</h2>
      <table><tbody>{lat_rows}</tbody></table>
    </div>
    <div>
      <h2>状态码明细</h2>
      <table><thead><tr><th>状态码</th><th>次数</th><th>占比</th></tr></thead><tbody>{sc_rows}</tbody></table>
    </div>
  </div>

  <h2>错误分类</h2>
  <table>
    <thead><tr><th>方法</th><th>路径</th><th>状态码</th><th>错误类型</th><th>次数</th></tr></thead>
    <tbody>{err_rows or '<tr><td colspan="5" class="muted">无错误</td></tr>'}</tbody>
  </table>

  <h2>错误样本（最近）</h2>
  <table>
    <thead><tr><th>时间</th><th>方法</th><th>路径</th><th>状态</th><th>信息</th></tr></thead>
    <tbody>{sample_rows}</tbody>
  </table>

  <h2>测试信息</h2>
  <table><tbody>{meta_rows}</tbody></table>

  <footer>WebStressTesting · 基于 AioTest 的现代化压测工具 · 报告生成于 {esc(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}</footer>
</div>
</body>
</html>
"""