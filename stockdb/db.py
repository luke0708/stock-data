"""SQLite 元数据操作：股票列表、交易日历、财务摘要"""

import sqlite3
import logging
from pathlib import Path
from typing import List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS stocks (
    code        TEXT PRIMARY KEY,
    name        TEXT,
    market      TEXT,        -- sh / sz / bj
    board       TEXT,        -- 主板 / 科创板 / 创业板 / 中小板 / 北交所
    industry    TEXT,
    list_date   TEXT,
    delist_date TEXT,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS trade_calendar (
    date    TEXT PRIMARY KEY,  -- YYYYMMDD
    is_open INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS financials (
    code        TEXT,
    report_date TEXT,          -- YYYYMMDD
    revenue     REAL,
    net_profit  REAL,
    eps         REAL,
    roe         REAL,
    updated_at  TEXT,
    PRIMARY KEY (code, report_date)
);

CREATE TABLE IF NOT EXISTS update_log (
    date       TEXT PRIMARY KEY,  -- YYYYMMDD
    status     TEXT,              -- ok / error
    message    TEXT,
    updated_at TEXT
);
"""


class MetaDB:
    """SQLite 元数据数据库操作封装"""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self):
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    # ── 股票列表 ─────────────────────────────────────

    def upsert_stocks(self, stocks: List[dict]):
        """批量写入/更新股票列表"""
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rows = [
            (s["code"], s.get("name", ""), s.get("market", ""),
             s.get("board", ""), s.get("industry", ""),
             s.get("list_date", ""), s.get("delist_date", ""), now)
            for s in stocks
        ]
        with self._conn() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO stocks VALUES (?,?,?,?,?,?,?,?)", rows
            )
        logger.info("Upserted %d stocks", len(rows))

    def get_stocks(self, market: Optional[str] = None) -> pd.DataFrame:
        sql = "SELECT * FROM stocks"
        params = ()
        if market:
            sql += " WHERE market = ?"
            params = (market.lower(),)
        with self._conn() as conn:
            return pd.read_sql_query(sql, conn, params=params)

    # ── 交易日历 ─────────────────────────────────────

    def upsert_calendar(self, dates: List[str], is_open: int = 1):
        """写入交易日历（dates 格式 'YYYYMMDD'）"""
        rows = [(d, is_open) for d in dates]
        with self._conn() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO trade_calendar VALUES (?,?)", rows
            )

    def is_trade_day(self, date_str: str) -> bool:
        """判断某天是否为交易日，兼容 'YYYYMMDD' 和 'YYYY-MM-DD' 两种格式"""
        date_str = str(date_str).replace("-", "")  # 标准化：去採 → YYYYMMDD
        with self._conn() as conn:
            row = conn.execute(
                "SELECT is_open FROM trade_calendar WHERE date=?", (date_str,)
            ).fetchone()
        return bool(row and row["is_open"])

    def last_trade_day(self) -> Optional[str]:
        """返回日历中最近的交易日（不超过今天，避免日历预存未来日期干扰）"""
        from datetime import datetime
        today = datetime.now().strftime("%Y%m%d")
        with self._conn() as conn:
            row = conn.execute(
                "SELECT date FROM trade_calendar WHERE is_open=1 AND date <= ? ORDER BY date DESC LIMIT 1",
                (today,)
            ).fetchone()
        return row["date"] if row else None

    def last_updated_day(self) -> Optional[str]:
        """返回 update_log 中最近一次状态为 ok 的更新日期（即本地实际数据最新日期）"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT date FROM update_log WHERE status='ok' ORDER BY date DESC LIMIT 1"
            ).fetchone()
        return row["date"] if row else None

    def get_calendar(self) -> pd.DataFrame:
        with self._conn() as conn:
            return pd.read_sql_query("SELECT * FROM trade_calendar ORDER BY date", conn)

    # ── 更新日志 ─────────────────────────────────────

    def log_update(self, date_str: str, status: str, message: str = ""):
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO update_log VALUES (?,?,?,?)",
                (date_str, status, message, now)
            )
