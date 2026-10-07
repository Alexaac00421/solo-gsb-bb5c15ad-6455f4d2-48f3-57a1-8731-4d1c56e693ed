"""数据库连接与启动等待。"""
from __future__ import annotations

import os
import time

import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://app:app@localhost:5432/seals",
)

DB_CONNECT_RETRIES = int(os.getenv("DB_CONNECT_RETRIES", "30"))
DB_CONNECT_DELAY = float(os.getenv("DB_CONNECT_DELAY", "1.0"))


def get_conn() -> psycopg.Connection:
    """新建一个连接（dict 行格式）。配合 `with` 使用：正常退出提交，异常回滚。"""
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def wait_for_db(retries: int = DB_CONNECT_RETRIES, delay: float = DB_CONNECT_DELAY) -> None:
    """启动时等待数据库就绪（Compose 中 db 健康检查之外的兜底）。"""
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with get_conn() as conn:
                conn.execute("SELECT 1")
            return
        except Exception as exc:  # noqa: BLE001 - 启动阶段兜底重试
            last_exc = exc
            time.sleep(delay)
    raise RuntimeError(f"数据库不可达（重试 {retries} 次后放弃）: {last_exc}")
