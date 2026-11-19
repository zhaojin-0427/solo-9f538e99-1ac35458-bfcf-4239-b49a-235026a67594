"""SQLite 连接、模式与事务助手。

设计：单进程单连接 + 进程级写锁。所有写路径在 BEGIN IMMEDIATE ...
COMMIT 的单个事务中执行，因此原文、校时依据、分析供数状态同次生效；
查询与（崩溃后）重启都不会看到半次重算的中间态。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager

DEFAULT_DB_PATH = "./buoy.db"

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS calibration_points (
    boot_segment TEXT NOT NULL,      -- 开机段
    counter      INTEGER NOT NULL,   -- 段内毫秒计数
    utc_ms       INTEGER NOT NULL,   -- 对应 UTC 毫秒
    PRIMARY KEY (boot_segment, counter)
);

CREATE TABLE IF NOT EXISTS observations (
    obs_id         TEXT PRIMARY KEY, -- 观测 ID
    boot_segment   TEXT NOT NULL,    -- 原文：开机段
    counter        INTEGER NOT NULL, -- 原文：段内计数
    tide_level     REAL NOT NULL,    -- 原文：潮位
    utc_ms         INTEGER,          -- 换算结果；NULL 表示待校时
    status         TEXT NOT NULL,    -- ok / pending_calibration / conflict
    reason         TEXT,             -- 状态原因
    cal_lo_counter INTEGER,          -- 校时依据：下界校时点
    cal_lo_utc_ms  INTEGER,
    cal_hi_counter INTEGER,          -- 校时依据：上界校时点
    cal_hi_utc_ms  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_observations_utc_ms ON observations(utc_ms);
CREATE INDEX IF NOT EXISTS idx_observations_segment ON observations(boot_segment);

-- 联测资料（整批替换）：海图零点、各开机段标尺、联测记录。
-- 三张表始终在同一个事务里整体替换，查询只读取同一已提交版本。
CREATE TABLE IF NOT EXISTS survey_meta (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    chart_datum  TEXT NOT NULL       -- 海图零点基准名
);

CREATE TABLE IF NOT EXISTS survey_staff (
    boot_segment TEXT PRIMARY KEY,   -- 开机段
    staff        TEXT NOT NULL       -- 该段所用标尺（基准名）
);

CREATE TABLE IF NOT EXISTS survey_connections (
    seq           INTEGER NOT NULL,  -- 资料内的原始顺序
    connection_id TEXT PRIMARY KEY,  -- 联测记录唯一编号
    from_datum    TEXT NOT NULL,     -- 起点基准
    to_datum      TEXT NOT NULL,     -- 终点基准
    delta_mm      INTEGER NOT NULL   -- 从起点换到终点要加的毫米数
);
"""


def get_conn() -> sqlite3.Connection:
    """返回进程内共享连接（惰性创建并建表）。"""
    global _conn
    if _conn is None:
        path = os.environ.get("BUOY_DB", DEFAULT_DB_PATH)
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(SCHEMA)
        _conn = conn
    return _conn


def lock() -> threading.RLock:
    """读写共用的进程级锁：写事务进行中，查询不会读到中间态。"""
    return _lock


@contextmanager
def read_transaction(conn: sqlite3.Connection):
    """单条只读快照：一条查询里的多次 SELECT 看到同一已提交状态。

    调用方需持有 lock()。SQLite 的 DEFERRED 事务在首个 SELECT 时确定快照，
    因此观测与联测资料不会来自替换前后两个版本。
    """
    conn.execute("BEGIN")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


@contextmanager
def transaction(conn: sqlite3.Connection):
    """单事务包裹一批写入与重算；异常即整体回滚。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def reset_for_tests() -> None:
    """测试用：丢弃当前连接（下次 get_conn 重新建库）。"""
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None
