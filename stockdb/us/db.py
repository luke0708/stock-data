"""
美股元数据 SQLite 操作（us_ 前缀表，与 A 股表完全隔离）

所有表名以 us_ 开头，写入同一个 meta.db，
用 CREATE TABLE IF NOT EXISTS 纯追加，不修改 stockdb/db.py 中的任何 A 股表。
"""

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

US_SCHEMA = """
CREATE TABLE IF NOT EXISTS us_stocks (
    ticker              TEXT PRIMARY KEY,
    name                TEXT,
    exchange            TEXT,
    sector              TEXT,
    industry            TEXT,
    list_date           TEXT,
    shares_outstanding  REAL,
    market_cap          REAL,
    updated_at          TEXT
);

CREATE TABLE IF NOT EXISTS us_splits (
    ticker      TEXT,
    split_date  TEXT,
    factor      REAL,
    updated_at  TEXT,
    PRIMARY KEY (ticker, split_date)
);

CREATE TABLE IF NOT EXISTS us_financials (
    ticker          TEXT,
    report_date     TEXT,
    revenue         REAL,
    yoy_growth      REAL,
    prev_yoy_growth REAL,
    updated_at      TEXT,
    PRIMARY KEY (ticker, report_date)
);

CREATE TABLE IF NOT EXISTS us_calendar (
    date    TEXT PRIMARY KEY,   -- YYYYMMDD
    is_open INTEGER DEFAULT 1
);
"""


class USMetaDB:
    """美股元数据库操作封装，复用 cfg.db_path 但只操作 us_ 前缀表"""

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
            conn.executescript(US_SCHEMA)

    def _now(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── 股票列表 ─────────────────────────────────────

    def upsert_us_stocks(self, stocks: List[dict]):
        """批量写入/更新美股股票信息"""
        rows = [
            (
                s["ticker"].upper(),
                s.get("name", ""),
                s.get("exchange", ""),
                s.get("sector", ""),
                s.get("industry", ""),
                s.get("list_date", ""),
                s.get("shares_outstanding"),
                s.get("market_cap"),
                self._now(),
            )
            for s in stocks
        ]
        sql = """
        INSERT INTO us_stocks
            (ticker, name, exchange, sector, industry, list_date, shares_outstanding, market_cap, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            name=excluded.name, exchange=excluded.exchange,
            sector=excluded.sector, industry=excluded.industry,
            list_date=excluded.list_date,
            shares_outstanding=excluded.shares_outstanding,
            market_cap=excluded.market_cap,
            updated_at=excluded.updated_at
        """
        with self._conn() as conn:
            conn.executemany(sql, rows)
        logger.info("upsert_us_stocks: %d 条", len(rows))

    def get_us_stocks(self, ticker: Optional[str] = None) -> pd.DataFrame:
        sql = "SELECT * FROM us_stocks"
        params = ()
        if ticker:
            sql += " WHERE ticker = ?"
            params = (ticker.upper(),)
        with self._conn() as conn:
            return pd.read_sql_query(sql, conn, params=params)

    # ── 拆股记录 ─────────────────────────────────────

    def upsert_splits(self, ticker: str, splits: list):
        """写入拆股记录，splits = [(date, factor), ...]"""
        rows = [
            (ticker.upper(), str(d), float(f), self._now())
            for d, f in splits
        ]
        if not rows:
            return
        sql = """
        INSERT INTO us_splits (ticker, split_date, factor, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(ticker, split_date) DO UPDATE SET
            factor=excluded.factor, updated_at=excluded.updated_at
        """
        with self._conn() as conn:
            conn.executemany(sql, rows)
        logger.info("upsert_splits %s: %d 条", ticker, len(rows))

    def get_splits(self, ticker: str) -> pd.DataFrame:
        with self._conn() as conn:
            return pd.read_sql_query(
                "SELECT * FROM us_splits WHERE ticker=? ORDER BY split_date",
                conn, params=(ticker.upper(),)
            )

    # ── 财务数据 ─────────────────────────────────────

    def upsert_financials(self, ticker: str, data: dict):
        """写入营收同比数据，data = get_revenue_yoy() 的返回值"""
        if not data:
            return
        report_date = datetime.now().strftime("%Y%m%d")
        sql = """
        INSERT INTO us_financials
            (ticker, report_date, revenue, yoy_growth, prev_yoy_growth, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, report_date) DO UPDATE SET
            revenue=excluded.revenue,
            yoy_growth=excluded.yoy_growth,
            prev_yoy_growth=excluded.prev_yoy_growth,
            updated_at=excluded.updated_at
        """
        with self._conn() as conn:
            conn.execute(sql, (
                ticker.upper(), report_date,
                data.get("latest_q_rev"),
                data.get("yoy_growth"),
                data.get("prev_yoy_growth"),
                self._now(),
            ))
        logger.info("upsert_financials %s 完成", ticker)

    def get_financials(self, ticker: str) -> pd.DataFrame:
        with self._conn() as conn:
            return pd.read_sql_query(
                "SELECT * FROM us_financials WHERE ticker=? ORDER BY report_date DESC",
                conn, params=(ticker.upper(),)
            )

    # ── 交易日历 ─────────────────────────────────────

    def upsert_calendar(self, dates: List[str], is_open: int = 1):
        """写入美股交易日历，dates 格式 'YYYYMMDD'"""
        rows = [(d, is_open) for d in dates]
        with self._conn() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO us_calendar VALUES (?,?)", rows
            )
        logger.info("upsert_calendar: %d 条", len(rows))

    def is_trade_day(self, date_str: str) -> bool:
        """判断是否为美股交易日，兼容 YYYYMMDD 和 YYYY-MM-DD"""
        date_str = str(date_str).replace("-", "")
        with self._conn() as conn:
            row = conn.execute(
                "SELECT is_open FROM us_calendar WHERE date=?", (date_str,)
            ).fetchone()
        return bool(row and row["is_open"])

    def last_trade_day(self) -> Optional[str]:
        """返回日历中最近美股交易日"""
        today = datetime.now().strftime("%Y%m%d")
        with self._conn() as conn:
            row = conn.execute(
                "SELECT date FROM us_calendar WHERE is_open=1 AND date<=? ORDER BY date DESC LIMIT 1",
                (today,)
            ).fetchone()
        return row["date"] if row else None
