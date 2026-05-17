"""
StockDB — 统一数据读取接口

所有项目只调用这里，内部自动处理：
  本地 Parquet 缓存（优先） → pytdx 拉取（miss） → akshare 兜底
  拉取成功后自动写盘（写透缓存），下次直接读本地。
"""

import logging
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Optional

import pandas as pd

from .config import Config
from .db import MetaDB
from .market import (
    code_to_tdx_market, detect_market, normalize_code, INDEX_MAP
)
from .tdx_client import tdx_connect, fetch_bars, fetch_tick

logger = logging.getLogger(__name__)


# ── 数据格式化工具 ────────────────────────────────────

def _bars_to_df(data: list, freq: str = "daily") -> pd.DataFrame:
    """将 pytdx 返回的 bar 列表转为标准 DataFrame"""
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    if "datetime" not in df.columns:
        return pd.DataFrame()

    if freq == "daily":
        # 兼容 pytdx 两种日期格式："20230103" 和 "2023-01-03 00:00"
        df["date"] = pd.to_datetime(df["datetime"], errors="coerce").dt.normalize()
        cols = ["date", "open", "high", "low", "close", "vol", "amount"]
        df = df[[c for c in cols if c in df.columns]].copy()
        df = df.dropna(subset=["date"]).sort_values("date").drop_duplicates(subset=["date"])
    else:  # minute
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        cols = ["datetime", "open", "high", "low", "close", "vol", "amount"]
        df = df[[c for c in cols if c in df.columns]].copy()
        df = df.dropna(subset=["datetime"]).sort_values("datetime").drop_duplicates(subset=["datetime"])

    return df.reset_index(drop=True)


def _tick_to_df(data: list, date_str: str = None) -> pd.DataFrame:
    """将 pytdx 返回的 tick 列表转为标准 DataFrame"""
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    df = df.rename(columns={"buyorsell": "direction"})
    if date_str and "time" in df.columns:
        try:
            df["datetime"] = pd.to_datetime(date_str + " " + df["time"].astype(str))
        except Exception:
            pass
    if "price" in df.columns and "vol" in df.columns:
        df["amount"] = df["price"] * df["vol"]
    return df.reset_index(drop=True)


def _is_day_complete(date_str: str) -> bool:
    """判断某交易日数据是否已完整（历史日期，或今天 16:00 后）"""
    today = datetime.now().strftime("%Y%m%d")
    if date_str < today:
        return True
    if date_str == today and datetime.now().time() >= dtime(16, 0):
        return True
    return False


def _append_parquet(path: Path, new_df: pd.DataFrame, dedup_col: str):
    """向已有 Parquet 追加数据（读→合并→去重→写）"""
    if path.exists():
        old_df = pd.read_parquet(path)
        df = pd.concat([old_df, new_df], ignore_index=True)
    else:
        df = new_df
    df = df.drop_duplicates(subset=[dedup_col]).sort_values(dedup_col)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)


# ── 主接口类 ─────────────────────────────────────────

class StockDB:
    """
    A股本地行情数据库统一接口。

    示例：
        from stockdb import StockDB
        db = StockDB()
        df = db.daily('300661', start='2024-01-01')
        df = db.minutes('300661', date='20260507')
        df = db.tick('300661')
    """

    def __init__(self, config_path: Optional[str] = None):
        self.cfg = Config(config_path)
        self.cfg.ensure_dirs()
        self._meta = MetaDB(self.cfg.db_path)

    # ── 日线 ─────────────────────────────────────────

    def daily(
        self,
        code: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> pd.DataFrame:
        """
        读日线 OHLCV。全市场可用，本地 Parquet 优先。
        首次调用若本地无数据，从 pytdx 拉取并存盘。
        """
        code = normalize_code(code)
        path = self.cfg.daily_path(code)

        if path.exists():
            df = pd.read_parquet(path)
        else:
            logger.info("daily cache miss %s, fetching from pytdx...", code)
            df = self._fetch_daily(code)
            if not df.empty:
                path.parent.mkdir(parents=True, exist_ok=True)
                df.to_parquet(path, index=False)

        # 日期过滤
        if "date" in df.columns:
            if start:
                df = df[df["date"] >= pd.to_datetime(start)]
            if end:
                df = df[df["date"] <= pd.to_datetime(end)]

        return df.reset_index(drop=True)

    # ── 分钟线（写透缓存） ────────────────────────────

    def minutes(
        self,
        code: str,
        date: Optional[str] = None,
        days: int = 1,
    ) -> pd.DataFrame:
        """
        读 1 分钟线。写透缓存：本地有则直接返回，无则 pytdx 拉取并存盘。
        date: 'YYYYMMDD' 或 'YYYY-MM-DD'，默认今天
        days: 拉取最近 N 天（合并返回）
        """
        code = normalize_code(code)
        if date is None:
            date_str = datetime.now().strftime("%Y%m%d")
        else:
            date_str = str(date).replace("-", "")

        # 单天模式
        if days == 1:
            return self._minutes_single(code, date_str)

        # 多天模式：简单拼接最近 days 天
        frames = []
        from datetime import timedelta
        base = datetime.strptime(date_str, "%Y%m%d")
        for i in range(days - 1, -1, -1):
            d = (base - timedelta(days=i)).strftime("%Y%m%d")
            df = self._minutes_single(code, d)
            if not df.empty:
                frames.append(df)
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def _minutes_single(self, code: str, date_str: str) -> pd.DataFrame:
        path = self.cfg.minutes_path(code, date_str)

        # 本地命中
        if path.exists():
            return pd.read_parquet(path)

        # 从 pytdx 拉取
        logger.info("minutes cache miss %s %s, fetching...", code, date_str)
        df = self._fetch_minutes(code, date_str)

        # 日期完整才存盘
        if not df.empty and _is_day_complete(date_str):
            path.parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(path, index=False)
            logger.debug("minutes cached: %s %s (%d rows)", code, date_str, len(df))

        return df

    # ── Tick ─────────────────────────────────────────

    def tick(
        self,
        code: str,
        date: Optional[str] = None,
    ) -> pd.DataFrame:
        """
        读 Tick 逐笔数据。
        date=None 拉今日实时；date='YYYYMMDD' 拉历史（先查缓存）。
        存盘行为由 config.yaml tick.cache_mode 控制。
        """
        code = normalize_code(code)
        if date is None:
            date_str = datetime.now().strftime("%Y%m%d")
        else:
            date_str = str(date).replace("-", "")

        cache_mode = self.cfg.tick_cache_mode
        path = self.cfg.tick_path(code, date_str)

        # 查缓存（非 none 模式）
        if cache_mode != "none" and path.exists():
            return pd.read_parquet(path)

        # 拉取
        logger.info("tick fetching %s %s ...", code, date_str)
        df = self._fetch_tick_data(code, date_str)

        # 存盘（日期完整 + cache_mode != none）
        if not df.empty and cache_mode != "none" and _is_day_complete(date_str):
            path.parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(path, index=False)
            logger.debug("tick cached: %s %s (%d rows)", code, date_str, len(df))

        return df

    # ── 指数 ─────────────────────────────────────────

    def index(
        self,
        code: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> pd.DataFrame:
        """读指数日线（上证指数、深证成指、创业板等）"""
        code = normalize_code(code)
        path = self.cfg.index_path(code)

        if path.exists():
            df = pd.read_parquet(path)
        else:
            logger.info("index cache miss %s, fetching...", code)
            market_str = INDEX_MAP.get(code, ("sh",))[0]
            market_int = 1 if market_str == "sh" else 0
            df = self._fetch_daily_raw(code, market_int)
            if not df.empty:
                path.parent.mkdir(parents=True, exist_ok=True)
                df.to_parquet(path, index=False)

        if "date" in df.columns:
            if start:
                df = df[df["date"] >= pd.to_datetime(start)]
            if end:
                df = df[df["date"] <= pd.to_datetime(end)]

        return df.reset_index(drop=True)

    # ── 股票列表 ─────────────────────────────────────

    def stock_list(self, market: Optional[str] = None) -> pd.DataFrame:
        """
        读股票列表（来自 SQLite）。
        需先运行 init_full.py 初始化。
        market: 'sh' / 'sz' / 'bj' / None（全部）
        """
        return self._meta.get_stocks(market)

    # ── 财务数据 ─────────────────────────────────────

    def financials(self, code: str) -> pd.DataFrame:
        """读财务摘要（来自 akshare，季度更新）"""
        code = normalize_code(code)
        path = self.cfg.data_dir / "finance" / f"{code}.parquet"
        if path.exists():
            return pd.read_parquet(path)
        logger.info("financials %s not cached, fetching from akshare...", code)
        return self._fetch_financials(code)

    # ── 内部：pytdx 拉取 ─────────────────────────────

    def _fetch_daily(self, code: str) -> pd.DataFrame:
        market = code_to_tdx_market(code)
        return self._fetch_daily_raw(code, market)

    def _fetch_daily_raw(self, code: str, market: int) -> pd.DataFrame:
        try:
            with tdx_connect(self.cfg.servers) as api:
                data = fetch_bars(api, code, market, frequency=9)
            return _bars_to_df(data, freq="daily")
        except Exception as e:
            logger.error("fetch daily failed %s: %s", code, e)
            return self._fallback_daily(code)

    def _fetch_minutes(self, code: str, date_str: str) -> pd.DataFrame:
        market = code_to_tdx_market(code)
        try:
            with tdx_connect(self.cfg.servers) as api:
                # frequency=8 = 1分钟
                data = fetch_bars(api, code, market, frequency=8)
            df = _bars_to_df(data, freq="minute")
            if not df.empty and "datetime" in df.columns:
                df = df[df["datetime"].dt.strftime("%Y%m%d") == date_str]
            return df
        except Exception as e:
            logger.error("fetch minutes failed %s %s: %s", code, date_str, e)
            return pd.DataFrame()

    def _fetch_tick_data(self, code: str, date_str: str) -> pd.DataFrame:
        market = code_to_tdx_market(code)
        today = datetime.now().strftime("%Y%m%d")
        try:
            with tdx_connect(self.cfg.servers) as api:
                if date_str == today:
                    data = fetch_tick(api, code, market, date_int=None)
                else:
                    data = fetch_tick(api, code, market, date_int=int(date_str))
            return _tick_to_df(data, date_str)
        except Exception as e:
            logger.error("fetch tick failed %s %s: %s", code, date_str, e)
            return pd.DataFrame()

    # ── 内部：akshare 兜底 ───────────────────────────

    def _fallback_daily(self, code: str) -> pd.DataFrame:
        """pytdx 失败时用 akshare 兜底"""
        try:
            import akshare as ak
            logger.info("fallback to akshare for %s", code)
            df = ak.stock_zh_a_hist(
                symbol=code, period="daily",
                adjust="qfq", start_date="2019-01-01"
            )
            # 标准化列名
            col_map = {
                "日期": "date", "开盘": "open", "最高": "high",
                "最低": "low", "收盘": "close", "成交量": "vol",
                "成交额": "amount"
            }
            df = df.rename(columns=col_map)
            df["date"] = pd.to_datetime(df["date"])
            cols = [c for c in ["date", "open", "high", "low", "close", "vol", "amount"] if c in df.columns]
            return df[cols].sort_values("date").reset_index(drop=True)
        except Exception as e:
            logger.error("akshare fallback failed %s: %s", code, e)
            return pd.DataFrame()

    def _fetch_financials(self, code: str) -> pd.DataFrame:
        try:
            import akshare as ak
            df = ak.stock_financial_abstract(symbol=code)
            path = self.cfg.data_dir / "finance" / f"{code}.parquet"
            path.parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(path, index=False)
            return df
        except Exception as e:
            logger.error("fetch financials failed %s: %s", code, e)
            return pd.DataFrame()

    # ── 工具方法 ─────────────────────────────────────

    def is_trade_day(self, date_str: str) -> bool:
        """判断某天是否为交易日"""
        return self._meta.is_trade_day(date_str)

    def last_trade_day(self) -> Optional[str]:
        """返回数据库中最近的交易日"""
        return self._meta.last_trade_day()

    def clean_old_ticks(self):
        """清理超过 keep_days 的 Tick 缓存"""
        from datetime import timedelta
        keep = self.cfg.tick_keep_days
        cutoff = (datetime.now() - timedelta(days=keep)).strftime("%Y%m%d")
        tick_root = self.cfg.data_dir / "tick"
        count = 0
        for f in tick_root.rglob("*.parquet"):
            date_part = f.stem  # filename = YYYYMMDD
            if date_part < cutoff:
                f.unlink()
                count += 1
        if count:
            logger.info("Cleaned %d old tick files (before %s)", count, cutoff)

    # ── 筹码分布 ──────────────────────────────────────

    def chip(
        self,
        code: str,
        date: Optional[str] = None,
        recalc: bool = False,
    ) -> pd.DataFrame:
        """
        筹码分布指标（本地计算，缓存到 data/chip/{code}.parquet）。

        算法：三角分布递推模型，从已有 OHLCV 日线数据离线计算，无需网络。

        Args:
            code    : 股票代码，6 位数字（如 '300661'）
            date    : 'YYYY-MM-DD' 或 'YYYYMMDD'；None 返回全部历史
            recalc  : True 时强制重算（忽略缓存），用于日更后刷新

        Returns:
            DataFrame，列：
                date, close, profit_ratio, concentration_90,
                concentration_70, avg_cost, peak_price

            profit_ratio      获利比例（0~1，越高说明套牢盘越少）
            concentration_90  90% 筹码价格区间 / 当前价（越小越集中）
            concentration_70  70% 筹码价格区间 / 当前价
            avg_cost          主力成本（加权均价）
            peak_price        筹码密集峰值价格

        示例：
            df  = db.chip('300661')                     # 全部历史
            row = db.chip('300661', date='2026-05-16')  # 指定日期
            db.chip('300661', recalc=True)              # 强制重算
        """
        from .chip import compute_chip_distribution

        code = normalize_code(code)
        cache_path = self.cfg.data_dir / "chip" / f"{code}.parquet"

        if not recalc and cache_path.exists():
            result = pd.read_parquet(cache_path)
        else:
            daily_df = self.daily(code)
            if daily_df.empty:
                logger.warning("chip: no daily data for %s", code)
                return pd.DataFrame()
            result = compute_chip_distribution(daily_df)
            if not result.empty:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                result.to_parquet(cache_path, index=False)
                logger.debug("chip cached: %s (%d rows)", code, len(result))

        if result.empty:
            return result

        # 日期过滤
        if date:
            target = pd.to_datetime(date)
            result = result[result["date"] == target]

        return result.reset_index(drop=True)
