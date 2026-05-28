"""
USStockDB — 美股本地行情数据库统一接口

与 A 股的 StockDB 完全隔离：
- 不调用 market.py 的 detect_market / normalize_code（那套逻辑只认 6 位数字 A 股代码）
- 存储路径由 cfg.us_daily_path(ticker) 自算，落在 data/daily/us/
- 元数据写入独立的 us_* 前缀表，通过 USMetaDB 操作

示例：
    from stockdb.us import USStockDB
    db = USStockDB()
    df = db.daily('AXTI', start='2024-01-01')
    print(db.splits('NIVF'))
    print(db.revenue_yoy('AXTI'))
"""

import logging
from pathlib import Path
from typing import Optional

import pandas as pd

from ..config import Config
from .db import USMetaDB
from .provider import DataProvider, build_provider

logger = logging.getLogger(__name__)


def _strip_tz(df: pd.DataFrame, col: str = "date") -> pd.DataFrame:
    """将 DataFrame 中的日期列统一去掉时区，避免新旧数据合并时 tz-naive/aware 冲突"""
    if col in df.columns and hasattr(df[col].dtype, "tz") and df[col].dtype.tz is not None:
        df = df.copy()
        df[col] = df[col].dt.tz_localize(None)
    return df


def _append_parquet(path: Path, new_df: pd.DataFrame, dedup_col: str = "date"):
    """向已有 Parquet 追加数据（读→合并→去重→写），独立实现避免与 A 股耦合"""
    if path.exists():
        old_df = _strip_tz(pd.read_parquet(path), dedup_col)
        new_df = _strip_tz(new_df, dedup_col)
        df = pd.concat([old_df, new_df], ignore_index=True)
    else:
        df = _strip_tz(new_df, dedup_col)
    df = df.drop_duplicates(subset=[dedup_col], keep="last").sort_values(dedup_col)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)


class USStockDB:
    """
    美股本地行情数据库统一接口。

    示例：
        db = USStockDB()
        df = db.daily('AXTI', start='2024-01-01')
        splits = db.splits('NIVF', lookback_months=24)
        rev = db.revenue_yoy('AXTI')
        ratio = db.adj_peak_ratio('NIVF')
    """

    def __init__(self, config_path: Optional[str] = None):
        self.cfg = Config(config_path)
        self._meta = USMetaDB(self.cfg.db_path)
        # 按 config.us_provider 实例化 provider，默认 yfinance
        self._provider: DataProvider = build_provider(self.cfg.us_provider)
        # 确保美股存储目录存在（不调用 A 股的 ensure_dirs）
        (self.cfg.data_dir / "daily" / "us").mkdir(parents=True, exist_ok=True)

    # ── 日线（本地优先，miss 从 provider 拉取写透）────────

    def daily(
        self,
        ticker: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        """读美股日线 OHLCV（含 adj_close, amount）。本地 Parquet 优先，miss 则拉取并存盘。"""
        ticker = ticker.upper()
        path = self.cfg.us_daily_path(ticker)

        if path.exists() and not force_refresh:
            df = pd.read_parquet(path)
        else:
            logger.info("daily cache miss %s，从 provider 拉取...", ticker)
            # 首次拉取时，若未指定 start，默认按配置的 history_years 全量拉取
            fetch_start = start
            if fetch_start is None:
                from datetime import datetime, timedelta
                fetch_start = (datetime.now() - timedelta(days=self.cfg.us_history_years * 365)).strftime("%Y-%m-%d")
            df = self._provider.get_daily(ticker, start=fetch_start, end=end)
            if not df.empty:
                _append_parquet(path, df, dedup_col="date")
                df = pd.read_parquet(path)

        if "date" in df.columns:
            if start:
                df = df[df["date"] >= pd.to_datetime(start)]
            if end:
                df = df[df["date"] <= pd.to_datetime(end)]

        return df.reset_index(drop=True)

    # ── 基本面 / 拆股（直接调 provider，顺带缓存到 us_ 表）──

    def market_cap(self, ticker: str) -> Optional[float]:
        """当前市值（美元）"""
        return self._provider.get_market_cap(ticker.upper())

    def avg_dollar_volume(self, ticker: str, window_days: int = 30) -> Optional[float]:
        """近 window_days 日均成交额（美元）"""
        return self._provider.get_avg_dollar_volume(ticker.upper(), window_days)

    def splits(self, ticker: str, lookback_months: int = 24) -> list:
        """
        近 lookback_months 个月的拆股记录。
        返回 [(date, factor)]，反向拆股 factor<1（NIVF 的指纹）。
        """
        ticker = ticker.upper()
        result = self._provider.get_splits(ticker, lookback_months)
        if result:
            self._meta.upsert_splits(ticker, result)
        return result

    def revenue_yoy(self, ticker: str) -> Optional[dict]:
        """
        营收同比。返回 {latest_q_rev, yoy_growth, prev_yoy_growth, fetched_at}。
        口径统一用 GAAP Total Revenue，不混。
        """
        ticker = ticker.upper()
        data = self._provider.get_revenue_yoy(ticker)
        if data:
            self._meta.upsert_financials(ticker, data)
        return data

    def adj_peak_ratio(self, ticker: str, lookback_years: int = 5) -> Optional[float]:
        """
        前复权历史最高价 / 当前价。
        NIVF 指纹值 >> 100（约 27 万），AXTI 应 < 100。
        """
        return self._provider.get_adj_price_peak_ratio(ticker.upper(), lookback_years)

    def meta(self, ticker: str) -> Optional[dict]:
        """股票基础信息（name, exchange, sector, industry, shares_outstanding）"""
        ticker = ticker.upper()
        data = self._provider.get_meta(ticker)
        if data:
            self._meta.upsert_us_stocks([{"ticker": ticker, **data}])
        return data

    # ── Universe 管理 ─────────────────────────────────

    def universe(self) -> list:
        """返回 config.us_watchlist（当前小批样本模式）"""
        return [t.upper() for t in self.cfg.us_watchlist]

    def sync_universe(self):
        """拉取 watchlist 里每只股票的 meta，写入 us_stocks 表"""
        for ticker in self.universe():
            logger.info("sync_universe: %s", ticker)
            self.meta(ticker)
