"""SQLite 元数据操作：股票列表、交易日历、财务摘要"""

import sqlite3
import logging
from pathlib import Path
from typing import List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS stocks (
    code              TEXT PRIMARY KEY,
    name              TEXT,
    market            TEXT,        -- sh / sz / bj
    board             TEXT,        -- 主板 / 科创板 / 创业板 / 中小板 / 北交所
    industry          TEXT,
    list_date         TEXT,
    delist_date       TEXT,
    outstanding_shares REAL,
    free_float_shares  REAL,
    updated_at        TEXT
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

CREATE TABLE IF NOT EXISTS market_chip_stats (
    code        TEXT PRIMARY KEY,
    name        TEXT,
    board       TEXT,
    close       REAL,
    profit_ratio REAL,
    concentration_90 REAL,
    concentration_70 REAL,
    avg_cost    REAL,
    peak_price  REAL,
    deviation   REAL,
    shape       TEXT,
    mtime       REAL,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS scanner_status (
    key         TEXT PRIMARY KEY,
    value       TEXT
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
            # 自动迁移：检查 stocks 表是否缺少新增列
            try:
                cursor = conn.execute("PRAGMA table_info(stocks)")
                columns = [row["name"] for row in cursor.fetchall()]
                if "outstanding_shares" not in columns:
                    conn.execute("ALTER TABLE stocks ADD COLUMN outstanding_shares REAL")
                    logger.info("Database migration: Added outstanding_shares column to stocks table.")
                if "free_float_shares" not in columns:
                    conn.execute("ALTER TABLE stocks ADD COLUMN free_float_shares REAL")
                    logger.info("Database migration: Added free_float_shares column to stocks table.")
            except Exception as e:
                logger.error("Database migration failed: %s", e)

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
        sql = """
        INSERT INTO stocks (code, name, market, board, industry, list_date, delist_date, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(code) DO UPDATE SET
            name=excluded.name,
            market=excluded.market,
            board=excluded.board,
            industry=excluded.industry,
            list_date=excluded.list_date,
            delist_date=excluded.delist_date,
            updated_at=excluded.updated_at
        """
        with self._conn() as conn:
            conn.executemany(sql, rows)
        logger.info("Upserted %d stocks", len(rows))

    def update_stock_shares_data(self, shares_dict: dict):
        """
        批量更新股票的流通股本和自由流通股本
        shares_dict 结构: {code: (outstanding_shares, free_float_shares)}
        """
        rows = [
            (outstanding, free_float, code)
            for code, (outstanding, free_float) in shares_dict.items()
        ]
        with self._conn() as conn:
            conn.executemany(
                "UPDATE stocks SET outstanding_shares=?, free_float_shares=? WHERE code=?",
                rows
            )
        logger.info("Updated shares data for %d stocks", len(rows))

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

    # ── 筹码分布选股与状态 ─────────────────────────────────

    def get_scanner_status(self) -> dict:
        """获取筹码扫描器进度状态"""
        status = {
            "is_scanning": 0,
            "total_count": 0,
            "scanned_count": 0,
            "last_scan_time": "N/A",
            "status_message": "未在运行"
        }
        try:
            with self._conn() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT key, value FROM scanner_status")
                for row in cursor.fetchall():
                    key = row["key"]
                    val = row["value"]
                    if key in ["is_scanning", "total_count", "scanned_count"]:
                        status[key] = int(val)
                    else:
                        status[key] = val
        except Exception as e:
            logger.error("Error reading scanner status: %s", e)
        return status

    def update_scanner_status(self, status_updates: dict):
        """更新筹码扫描器运行状态"""
        try:
            with self._conn() as conn:
                for k, v in status_updates.items():
                    conn.execute(
                        "INSERT OR REPLACE INTO scanner_status (key, value) VALUES (?, ?)",
                        (k, str(v))
                    )
                conn.commit()
        except Exception as e:
            logger.error("Error updating scanner status: %s", e)

    def upsert_market_chip_stats(self, results: List[dict]):
        """批量更新/写入全市场个股最新筹码统计特征"""
        with self._conn() as conn:
            conn.executemany("""
            INSERT OR REPLACE INTO market_chip_stats (
                code, name, board, close, profit_ratio,
                concentration_90, concentration_70, avg_cost,
                peak_price, deviation, shape, mtime, updated_at
            ) VALUES (
                :code, :name, :board, :close, :profit_ratio,
                :concentration_90, :concentration_70, :avg_cost,
                :peak_price, :deviation, :shape, :mtime, datetime('now', 'localtime')
            )
            """, results)
            conn.commit()
        logger.info("Upserted %d market chip stats", len(results))

    def get_market_chip_mtimes(self) -> dict:
        """获取已缓存的股票mtime映射以实现增量更新"""
        mtimes = {}
        try:
            with self._conn() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT code, mtime FROM market_chip_stats")
                for code, mtime in cursor.fetchall():
                    mtimes[code] = int(mtime) if mtime is not None else 0
        except Exception as e:
            logger.warning("Failed to query market_chip_stats mtimes: %s", e)
        return mtimes

    def get_industry_stock_tree(self) -> List[dict]:
        """获取所有行业分类及其对应的个股列表，用于前端树形勾选"""
        tree = {}
        try:
            with self._conn() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT code, name, industry, board 
                    FROM stocks 
                    WHERE name IS NOT NULL AND name != '' AND code IS NOT NULL
                    ORDER BY industry, code
                """)
                for row in cursor.fetchall():
                    ind = row["industry"] or "未分类"
                    if ind not in tree:
                        tree[ind] = []
                    tree[ind].append({
                        "code": row["code"],
                        "name": row["name"],
                        "board": row["board"]
                    })
        except Exception as e:
            logger.error("Failed to get industry stock tree: %s", e)
            
        result = []
        for ind, stocks in tree.items():
            result.append({
                "industry": ind,
                "stocks": stocks
            })
        result.sort(key=lambda x: (x["industry"] == "未分类", x["industry"]))
        return result

