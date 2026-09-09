# -*- coding: utf-8 -*-
"""压测配置：目标解析与参数校验。"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

from web_stress_testing.utils import clamp


class ConfigError(Exception):
    """配置错误。"""


# ---------------------------------------------------------------------------
# URL 规范化
# ---------------------------------------------------------------------------

def normalize_target(url: str) -> str:
    """把用户输入规范化为完整 URL（自动补 https://）。"""
    url = (url or "").strip()
    if not url:
        raise ConfigError("缺少目标网址，请使用 --url https://example.com 或直接传入目标")
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ConfigError(f"不支持的协议: {parts.scheme!r}（仅支持 http/https）")
    if not parts.hostname:
        raise ConfigError(f"无法从 URL 中解析出域名: {url!r}")
    path = parts.path or "/"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def split_target(url: str) -> Tuple[str, str]:
    """返回 (base_url, path)。"""
    parts = urlsplit(url)
    base = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    path = urlunsplit(("", "", parts.path or "/", parts.query, ""))
    return base, path


# ---------------------------------------------------------------------------
# 配置数据
# ---------------------------------------------------------------------------

@dataclass
class RequestSpec:
    method: str = "GET"
    path: str = "/"
    headers: Optional[Dict[str, str]] = None
    params: Optional[Dict[str, str]] = None
    body: Any = None
    content_type: Optional[str] = None
    timeout: float = 30.0
    verify_ssl: bool = True
    max_retries: int = 0


@dataclass
class LoadSpec:
    users: int = 10
    ramp_up: float = 10.0
    steady: float = 60.0
    ramp_down: float = 5.0
    think_time: float = 0.0
    spawn_rate: Optional[float] = None
    max_requests: Optional[int] = None


@dataclass
class TestConfig:
    target: str
    request: RequestSpec
    load: LoadSpec
    report_dir: str
    name: str = "loadtest"
    dashboard: bool = True
    preflight: bool = True
    quiet: bool = False
    loglevel: str = "WARNING"
    prometheus_port: int = 0
    proxy: str = ""
    proxy_file: str = ""
    proxy_api: str = ""
    proxy_sticky: bool = True
    proxy_check_target: bool = True   # 压测前按目标地址预检代理（只保留可达）
    proxy_check_timeout: float = 3.0  # 预检单代理超时（秒）
    proxy_fail_threshold: int = 2     # 压测中某代理失败达到该次数才被本地剔除
    args_text: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target": self.target,
            "request": {
                "method": self.request.method,
                "path": self.request.path,
                "headers": self.request.headers,
                "params": self.request.params,
                "body": self.request.body if not isinstance(self.request.body, bytes) else "<bytes>",
                "content_type": self.request.content_type,
                "timeout": self.request.timeout,
                "verify_ssl": self.request.verify_ssl,
            },
            "load": {
                "users": self.load.users,
                "ramp_up": self.load.ramp_up,
                "steady": self.load.steady,
                "ramp_down": self.load.ramp_down,
                "think_time": self.load.think_time,
                "spawn_rate": self.load.spawn_rate,
                "max_requests": self.load.max_requests,
            },
            "report_dir": self.report_dir,
            "name": self.name,
            "proxy": {
                "single": self.proxy,
                "file": self.proxy_file,
                "api": self.proxy_api,
                "sticky": self.proxy_sticky,
                "check_target": self.proxy_check_target,
                "check_timeout": self.proxy_check_timeout,
                "fail_threshold": self.proxy_fail_threshold,
            },
            "prometheus_port": self.prometheus_port,
            "args": self.args_text,
        }


# ---------------------------------------------------------------------------
# 解析辅助
# ---------------------------------------------------------------------------

_HEADER_RE = re.compile(r"^([^:]+):\s*(.*)$")


def parse_headers(headers_str: str) -> Dict[str, str]:
    """解析 --headers：支持 JSON 对象或 'K: V' 逗号分隔列表。"""
    headers_str = headers_str.strip()
    if not headers_str:
        return {}
    if headers_str.startswith("{"):
        try:
            data = json.loads(headers_str)
        except json.JSONDecodeError as e:
            raise ConfigError(f"--headers JSON 解析失败: {e}")
        if not isinstance(data, dict):
            raise ConfigError("--headers JSON 必须是对象")
        return {str(k): str(v) for k, v in data.items()}
    # 逗号 / 分号分隔的 K: V 列表
    result: Dict[str, str] = {}
    for part in re.split(r"[,;]", headers_str):
        part = part.strip()
        if not part:
            continue
        m = _HEADER_RE.match(part)
        if not m:
            raise ConfigError(f"--headers 无法解析: {part!r}（应为 'K: V' 或 JSON）")
        result[m.group(1).strip()] = m.group(2).strip()
    return result


def parse_kv_pairs(items: Optional[List[str]]) -> Dict[str, str]:
    """解析 --param 'k=v' 重复参数。"""
    result: Dict[str, str] = {}
    for item in items or []:
        if "=" not in item:
            raise ConfigError(f"参数格式错误: {item!r}（应为 k=v）")
        k, v = item.split("=", 1)
        result[k.strip()] = v
    return result


def parse_body(value: Optional[str]) -> Any:
    """解析 --body：JSON 字符串优先；支持 @file 读取文件内容。"""
    if value is None:
        return None
    value = value.strip()
    if value.startswith("@"):
        path = value[1:].strip()
        if not os.path.isfile(path):
            raise ConfigError(f"--body 文件不存在: {path}")
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return content
    stripped = value.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    return value


def validate_load(load: LoadSpec) -> None:
    if load.users < 1:
        raise ConfigError(f"--users 必须 ≥ 1，当前: {load.users}")
    if load.steady < 0:
        raise ConfigError("--duration 不能为负数")
    if load.ramp_up < 0 or load.ramp_down < 0:
        raise ConfigError("--ramp-up / --ramp-down 不能为负数")
    if load.think_time < 0:
        raise ConfigError("--think-time 不能为负数")
    if load.max_requests is not None and load.max_requests < 1:
        raise ConfigError("--max-requests 必须 ≥ 1")
    if load.ramp_up + load.steady + load.ramp_down <= 0 and load.max_requests is None:
        raise ConfigError("测试时长必须大于 0（--duration）")


def default_timings(duration: float) -> Tuple[float, float]:
    """根据总时长给出合理的默认 ramp-up / ramp-down。"""
    ramp_up = clamp(duration * 0.15, 5.0, 30.0)
    ramp_down = clamp(duration * 0.1, 0.0, 10.0)
    if duration <= 1:
        ramp_up, ramp_down = 0.0, 0.0
    return ramp_up, ramp_down