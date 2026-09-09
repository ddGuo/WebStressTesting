# -*- coding: utf-8 -*-
"""存储层：MySQL（默认，pymysql）+ SQLite（测试/无 MySQL 环境替代）。

只保留有效代理的数据库实现：
- 入库前先校验（由上层保证）
- update_result()：校验成功 score+1，失败 score-1，延迟/计数更新
- score 低于阈值或连续失败达到 fail_threshold → 立即删除
- cleanup()：定期删除失效与超龄记录
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# 表结构
# ---------------------------------------------------------------------------
MYSQL_DDL = """
CREATE TABLE IF NOT EXISTS proxy (
  id            INT AUTO_INCREMENT PRIMARY KEY,
  ip            VARCHAR(45)  NOT NULL,
  port          INT          NOT NULL,
  protocol      VARCHAR(16)  NOT NULL DEFAULT 'http',
  source        VARCHAR(64)  NOT NULL DEFAULT 'manual',
  score         INT          NOT NULL DEFAULT 0,
  latency_ms    INT          NOT NULL DEFAULT 0,
  success_count INT          NOT NULL DEFAULT 0,
  fail_count    INT          NOT NULL DEFAULT 0,
  last_check    DATETIME     NULL,
  is_valid      TINYINT(1)   NOT NULL DEFAULT 1,
  created_at    DATETIME     DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_ip_port (ip, port)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS proxy (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ip            TEXT    NOT NULL,
  port          INTEGER NOT NULL,
  protocol      TEXT    NOT NULL DEFAULT 'http',
  source        TEXT    NOT NULL DEFAULT 'manual',
  score         INTEGER NOT NULL DEFAULT 0,
  latency_ms    INTEGER NOT NULL DEFAULT 0,
  success_count INTEGER NOT NULL DEFAULT 0,
  fail_count    INTEGER NOT NULL DEFAULT 0,
  last_check    TEXT,
  is_valid      INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT DEFAULT (datetime('now')),
  UNIQUE (ip, port)
)
"""

_COLS = "id, ip, port, protocol, source, score, latency_ms, success_count, fail_count, last_check, is_valid"


class BaseStorage:
    """存储接口基类（同步内核，外部用 asyncio.to_thread 包装）。"""

    def __init__(self, score_min: int = 0, fail_threshold: int = 3, ttl_seconds: int = 600,
                 score_max: int = 10):
        self.score_min = score_min
        self.fail_threshold = fail_threshold
        self.ttl_seconds = ttl_seconds
        self.score_max = score_max

    # ---- 方言点（子类实现）----
    def _connect(self):  # pragma: no cover
        raise NotImplementedError

    def _run(self, sql: str, args=(), fetch: bool = False):  # pragma: no cover
        raise NotImplementedError

    def _ddl(self) -> str:  # pragma: no cover
        raise NotImplementedError

    def _min_expr(self, a: str, b: str) -> str:
        """取最小值表达式（MySQL 用 LEAST，SQLite 用 MIN）。"""
        return f"MIN({a}, {b})"

    def _cutoff_as_arg(self, seconds: int):
        """返回 (SQL表达式占位, 参数)。MySQL 用 INTERVAL；SQLite 用具体时间字符串。"""
        raise NotImplementedError

    def _insert_ignore_sql(self, sql: str) -> str:
        return sql

    # ---- 通用方法 ----
    def init(self) -> None:
        self._run(self._ddl())

    def add(self, ip: str, port: int, protocol: str = "http", source: str = "manual") -> bool:
        sql = self._insert_ignore_sql(
            "INSERT IGNORE INTO proxy (ip, port, protocol, source) VALUES (%s, %s, %s, %s)")
        return (self._run(sql, (ip, int(port), protocol, source)) or 0) > 0

    def delete(self, ip: str, port: int) -> bool:
        n = self._run("DELETE FROM proxy WHERE ip=%s AND port=%s", (ip, int(port))) or 0
        return n > 0

    def count(self, only_valid: bool = True) -> int:
        if only_valid:
            return self._run(
                "SELECT COUNT(*) AS c FROM proxy WHERE is_valid=1 AND score>=%s",
                (self.score_min,), fetch=True)[0]["c"]
        return self._run("SELECT COUNT(*) AS c FROM proxy", fetch=True)[0]["c"]

    def get_random(self, count: int = 1) -> List[Dict[str, Any]]:
        expr, arg = self._cutoff_as_arg(self.ttl_seconds)
        rows = self._run(
            f"SELECT {_COLS} FROM proxy WHERE is_valid=1 AND score>=%s "
            f"AND (last_check IS NULL OR last_check >= {expr}) "
            f"ORDER BY {self._random_expr()} LIMIT %s",
            (self.score_min, arg, int(count)), fetch=True)
        return rows or []

    def get_all(self) -> List[Dict[str, Any]]:
        rows = self._run(
            f"SELECT {_COLS} FROM proxy WHERE is_valid=1 AND score>=%s "
            f"ORDER BY score DESC, latency_ms ASC",
            (self.score_min,), fetch=True)
        return rows or []

    def due_for_check(self, limit: int = 500) -> List[Dict[str, Any]]:
        """取需要重测的代理：从未校验或超过 TTL 未校验。"""
        expr, arg = self._cutoff_as_arg(self.ttl_seconds)
        rows = self._run(
            f"SELECT {_COLS} FROM proxy WHERE is_valid=1 "
            f"AND (last_check IS NULL OR last_check < {expr}) "
            f"ORDER BY last_check IS NOT NULL, id ASC LIMIT %s",
            (arg, int(limit)), fetch=True)
        return rows or []

    def update_result(self, ip: str, port: int, ok: bool, latency_ms: int,
                      exit_ip: Optional[str] = None) -> str:
        """记录一次校验结果。返回 'deleted'（已按失效规则删除）或 'kept'。"""
        if ok:
            min_expr = self._min_expr("score+1", "%s")
            self._run(
                f"UPDATE proxy SET score={min_expr}, latency_ms=%s, "
                "success_count=success_count+1, last_check=CURRENT_TIMESTAMP, "
                "is_valid=1 WHERE ip=%s AND port=%s",
                (self.score_max, int(latency_ms), ip, int(port)))
            return "kept"
        self._run(
            "UPDATE proxy SET score=score-1, fail_count=fail_count+1, "
            "last_check=CURRENT_TIMESTAMP WHERE ip=%s AND port=%s",
            (ip, int(port)))
        n = self._run(
            "DELETE FROM proxy WHERE ip=%s AND port=%s AND (score < %s OR fail_count >= %s)",
            (ip, int(port), self.score_min, self.fail_threshold)) or 0
        return "deleted" if n else "kept"

    def cleanup(self, stale_factor: int = 6) -> int:
        """删除 is_valid=0 的记录，以及长时间未通过校验的过期记录。"""
        n1 = self._run("DELETE FROM proxy WHERE is_valid=0") or 0
        expr, arg = self._cutoff_as_arg(self.ttl_seconds * stale_factor)
        n2 = self._run(
            f"DELETE FROM proxy WHERE last_check IS NOT NULL AND last_check < {expr}", (arg,)) or 0
        return n1 + n2

    def _random_expr(self) -> str:
        return "RANDOM()"

    def close(self) -> None:
        pass


class MyStorage(BaseStorage):
    """MySQL 后端（pymysql，单操作短连接）。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 3306, user: str = "proxy_pool",
                 password: str = "123456", db: str = "proxy_pool", **kw):
        super().__init__(**kw)
        self.dsn = dict(host=host, port=port, user=user, password=password, database=db,
                        charset="utf8mb4", connect_timeout=5, autocommit=True)

    def _ddl(self) -> str:
        return MYSQL_DDL

    def _min_expr(self, a: str, b: str) -> str:
        return f"LEAST({a}, {b})"

    def _random_expr(self) -> str:
        return "RAND()"

    def _cutoff_as_arg(self, seconds: int):
        return "NOW() - INTERVAL %s SECOND", seconds

    def _run(self, sql: str, args=(), fetch: bool = False):
        import pymysql
        conn = pymysql.connect(**self.dsn)
        try:
            cur = conn.cursor()
            cur.execute(sql, args)
            if fetch:
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


class SqliteStorage(BaseStorage):
    """SQLite 后端（无 MySQL 环境的替代，db_url=sqlite:///路径）。"""

    def __init__(self, path: str, **kw):
        super().__init__(**kw)
        self.path = path

    def _ddl(self) -> str:
        return SQLITE_DDL

    def _cutoff_as_arg(self, seconds: int):
        ts = (datetime.utcnow() - timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")
        return "?", ts

    def _insert_ignore_sql(self, sql: str) -> str:
        return sql.replace("INSERT IGNORE", "INSERT OR IGNORE")

    @staticmethod
    def _adapt(v):
        return v

    def _run(self, sql: str, args=(), fetch: bool = False):
        # %s -> ? 占位符转换（简单替换，SQL 中无其他 %s 字面量）
        qsql = sql.replace("%s", "?", 32)
        conn = sqlite3.connect(self.path)
        try:
            cur = conn.cursor()
            cur.execute(qsql, [self._adapt(a) for a in args])
            if fetch:
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


def create_storage(db_url: Optional[str] = None, *,
                   score_min: int = 0, fail_threshold: int = 3, ttl_seconds: int = 600,
                   score_max: int = 10) -> BaseStorage:
    """按配置创建存储后端。db_url=None 使用本机 MySQL 默认参数。"""
    common = dict(score_min=score_min, fail_threshold=fail_threshold,
                  ttl_seconds=ttl_seconds, score_max=score_max)
    if db_url and db_url.startswith("sqlite:///"):
        return SqliteStorage(db_url[len("sqlite:///"):], **common)
    if db_url and db_url.startswith("mysql://"):
        from urllib.parse import urlsplit
        u = urlsplit(db_url)
        return MyStorage(host=u.hostname or "127.0.0.1", port=u.port or 3306,
                         user=u.username or "proxy_pool", password=u.password or "123456",
                         db=(u.path or "/proxy_pool").lstrip("/"), **common)
    return MyStorage(**common)
