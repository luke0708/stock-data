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
        # 年份合法性校验：过滤採用错误 API 导致的乱码行（1990~2100）
        valid_years = (df["date"].dt.year >= 1990) & (df["date"].dt.year <= 2100)
        dropped = (~valid_years).sum()
        if dropped:
            logger.warning("_bars_to_df: 过滤 %d 行非法日期（年份超出 1990~2100），请检查指数拉取 API", dropped)
        df = df[valid_years]
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
    df = df.drop_duplicates(subset=[dedup_col], keep="last").sort_values(dedup_col)
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
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        """
        读日线 OHLCV。全市场可用，本地 Parquet 优先。
        首次调用若本地无数据，或 force_refresh=True 时，从 pytdx 拉取并存盘。
        """
        code = normalize_code(code)
        path = self.cfg.daily_path(code)

        if path.exists() and not force_refresh:
            df = pd.read_parquet(path)
        else:
            logger.info("daily cache miss or forced refresh %s, fetching from pytdx...", code)
            df = self._fetch_daily(code)
            if not df.empty:
                _append_parquet(path, df, dedup_col="date")
                df = pd.read_parquet(path)

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
            # 合法性公卡：旧缓存可能用错误 API 写入了乱码数据
            if "date" in df.columns and not df.empty:
                valid = (df["date"].dt.year >= 1990) & (df["date"].dt.year <= 2100)
                if not valid.all():
                    logger.warning("指数 %s 缓存包含乱码数据，自动重新拉取...", code)
                    path.unlink()  # 删除却废文件
                    df = pd.DataFrame()
        else:
            df = pd.DataFrame()

        if df.empty:
            logger.info("index cache miss %s, fetching...", code)
            market_str = INDEX_MAP.get(code, ("sh",))[0]
            market_int = 1 if market_str == "sh" else 0
            df = self._fetch_index_raw(code, market_int)
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

    def get_shares(self, code: str) -> dict:
        """
        获取单只股票的最新流通股本与自由流通股本。
        返回 dict: {"outstanding_shares": float or None, "free_float_shares": float or None}
        """
        code = normalize_code(code)
        with self._meta._conn() as conn:
            row = conn.execute(
                "SELECT outstanding_shares, free_float_shares FROM stocks WHERE code = ?",
                (code,)
            ).fetchone()
            
        if row:
            return {
                "outstanding_shares": row["outstanding_shares"],
                "free_float_shares": row["free_float_shares"]
            }
        return {
            "outstanding_shares": None,
            "free_float_shares": None
        }

    def update_stock_shares(self, max_workers: int = 20):
        """
        从 pytdx 批量拉取全市场股票的流通股本，并更新到本地 SQLite 元数据库中。
        使用多线程与连接复用机制提高效率。
        """
        import time
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from pytdx.hq import TdxHq_API

        df_stocks = self.stock_list()
        if df_stocks.empty:
            logger.warning("No stocks found in database, skip updating shares.")
            return

        logger.info("Starting update of stock shares for %d stocks...", len(df_stocks))
        start_time = time.time()

        # 分包划分
        codes = df_stocks["code"].tolist()
        chunk_size = (len(codes) + max_workers - 1) // max_workers
        chunks = [codes[i : i + chunk_size] for i in range(0, len(codes), chunk_size)]

        def fetch_shares_chunk_worker(chunk_codes):
            chunk_results = {}
            api = None
            i = 0
            retry_count = 0
            max_retries_per_code = 2
            
            while i < len(chunk_codes):
                code = chunk_codes[i]
                market_int = code_to_tdx_market(code)
                
                if api is None:
                    api = TdxHq_API()
                    connected = False
                    for host, port in self.cfg.servers:
                        try:
                            api.connect(str(host), int(port))
                            connected = True
                            break
                        except Exception:
                            pass
                    if not connected:
                        api = None
                        logger.warning("Worker failed to connect to any TDX server. Retry count: %d", retry_count)
                        time.sleep(1)
                        retry_count += 1
                        if retry_count > max_retries_per_code:
                            i += 1
                            retry_count = 0
                        continue
                
                try:
                    info = api.get_finance_info(market_int, code)
                    if info and "liutongguben" in info:
                        chunk_results[code] = (float(info["liutongguben"]), None)
                    i += 1
                    retry_count = 0
                except Exception as e:
                    logger.debug("Error fetching info for %s, disconnecting: %s", code, e)
                    try:
                        api.disconnect()
                    except Exception:
                        pass
                    api = None
                    retry_count += 1
                    if retry_count > max_retries_per_code:
                        i += 1
                        retry_count = 0
            
            if api is not None:
                try:
                    api.disconnect()
                except Exception:
                    pass
            return chunk_results

        shares_data = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(fetch_shares_chunk_worker, chunk): chunk for chunk in chunks}
            for fut in as_completed(futures):
                try:
                    chunk_res = fut.result()
                    shares_data.update(chunk_res)
                except Exception as e:
                    logger.error("Chunk worker raised exception: %s", e)

        elapsed = time.time() - start_time
        logger.info("Fetched shares for %d/%d stocks in %.2f seconds.", len(shares_data), len(df_stocks), elapsed)

        if shares_data:
            self._meta.update_stock_shares_data(shares_data)

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

    def _fetch_index_raw(self, code: str, market: int) -> pd.DataFrame:
        """
        拉取指数日线，使用 get_index_bars() API。
        注意：指数不能用 get_security_bars()，其二进制协议格式不同，会导致日期和价格字段乱码。
        """
        try:
            with tdx_connect(self.cfg.servers) as api:
                all_data, start = [], 0
                while True:
                    batch = api.get_index_bars(9, market, code, start, 800)
                    if not batch:
                        break
                    all_data.extend(batch)
                    if len(batch) < 800:
                        break
                    start += 800
            return _bars_to_df(all_data, freq="daily")
        except Exception as e:
            logger.error("fetch index failed %s: %s", code, e)
            return pd.DataFrame()

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
            # 自动检测缓存是否落后于 daily 数据
            if not result.empty:
                daily_path = self.cfg.daily_path(code)
                if daily_path.exists():
                    try:
                        import pyarrow.parquet as pq
                        pf = pq.read_table(str(daily_path), columns=["date"])
                        if pf.num_rows > 0:
                            latest_daily = pd.to_datetime(pf.column("date")[-1].as_py())
                            latest_chip = pd.to_datetime(result["date"].max())
                            if latest_daily > latest_chip:
                                logger.debug("chip cache stale for %s (%s < %s), recalculating...",
                                             code, latest_chip.date(), latest_daily.date())
                                recalc = True
                    except Exception:
                        pass
        else:
            result = pd.DataFrame()

        if recalc or result.empty:
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


# ── 网络多源直连与代理绕过工具 ───────────────────────────

import contextlib
import os
import urllib.request
import requests

@contextlib.contextmanager
def disable_proxy():
    """
    临时禁用全局代理的上下文管理器，
    通过清空代理环境变量并 Mock 相关的获取代理函数来达到彻底绕过 Clash 等全局代理的作用。
    """
    # 备份环境变量
    env_keys = ['http_proxy', 'https_proxy', 'all_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY']
    saved_env = {k: os.environ.get(k) for k in env_keys}
    
    # 清空环境变量
    for k in env_keys:
        if k in os.environ:
            del os.environ[k]
            
    # Mock urllib 和 requests 的 getproxies
    orig_urllib_getproxies = urllib.request.getproxies
    orig_requests_getproxies = requests.utils.getproxies
    orig_compat_getproxies = getattr(requests.compat, 'getproxies', None)
    
    urllib.request.getproxies = lambda: {}
    requests.utils.getproxies = lambda: {}
    if orig_compat_getproxies:
        requests.compat.getproxies = lambda: {}
        
    try:
        yield
    finally:
        # 恢复环境变量
        for k, v in saved_env.items():
            if v is not None:
                os.environ[k] = v
            elif k in os.environ:
                del os.environ[k]
        # 恢复函数
        urllib.request.getproxies = orig_urllib_getproxies
        requests.utils.getproxies = orig_requests_getproxies
        if orig_compat_getproxies:
            requests.compat.getproxies = orig_compat_getproxies


def fetch_daily_from_web(code: str, cutoff: str, date_str: str) -> pd.DataFrame:
    """
    网页多源日线瀑布流（东财 ➔ 腾讯 ➔ 新浪），不复权
    """
    code = normalize_code(code)
    market = detect_market(code)
    secid = f"1.{code}" if market == "sh" else f"0.{code}"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Referer": "https://quote.eastmoney.com/"
    }
    
    # 1. 优先：东财 API (不复权)
    try:
        url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        params = {
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116",
            "ut": "7eea3edcaed734bea9cbfc24409ed989",
            "klt": "101",  # 日线
            "fqt": "0",    # 不复权
            "secid": secid,
            "beg": cutoff,
            "end": date_str,
        }
        with disable_proxy():
            resp = requests.get(url, params=params, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            klines = data.get("data", {}).get("klines", [])
            if klines:
                rows = []
                for item in klines:
                    p = item.split(",")
                    rows.append({
                        "date": pd.to_datetime(p[0]),
                        "open": float(p[1]),
                        "close": float(p[2]),
                        "high": float(p[3]),
                        "low": float(p[4]),
                        "vol": int(p[5]) * 100,  # 转换为“股”
                        "amount": float(p[6])
                     })
                df = pd.DataFrame(rows)
                df = df[(df["date"].dt.strftime("%Y%m%d") >= cutoff) & (df["date"].dt.strftime("%Y%m%d") <= date_str)]
                if not df.empty:
                    return df.sort_values("date").reset_index(drop=True)
    except Exception as e:
        logger.debug("fetch_daily_from_web: 东财失败 %s: %s", code, e)
        
    # 2. 备用一：腾讯 API
    try:
        symbol = f"{market}{code}"
        url = f"https://web.ifzq.gtimg.cn/app/kline/kline?q={symbol}&type=day"
        with disable_proxy():
            resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            res_json = resp.json()
            symbol_data = res_json.get("data", {}).get(symbol, {})
            kdata = symbol_data.get("day", [])
            if not kdata:
                kdata = symbol_data.get("latest", [])
            if kdata:
                rows = []
                for item in kdata:
                    date_val = pd.to_datetime(item[0])
                    rows.append({
                        "date": date_val,
                        "open": float(item[1]),
                        "close": float(item[2]),
                        "high": float(item[3]),
                        "low": float(item[4]),
                        "vol": int(float(item[5]) * 100) if market != "bj" else int(float(item[5])),
                        "amount": float(item[6]) if len(item) > 6 else 0.0
                    })
                df = pd.DataFrame(rows)
                df = df[(df["date"].dt.strftime("%Y%m%d") >= cutoff) & (df["date"].dt.strftime("%Y%m%d") <= date_str)]
                if not df.empty:
                    return df.sort_values("date").reset_index(drop=True)
    except Exception as e:
        logger.debug("fetch_daily_from_web: 腾讯失败 %s: %s", code, e)

    # 3. 备用二：新浪 API
    try:
        symbol = f"{market}{code}"
        url = f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={symbol}"
        with disable_proxy():
            resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            kdata = resp.json()
            if kdata:
                rows = []
                for item in kdata:
                    date_val = pd.to_datetime(item["day"])
                    rows.append({
                        "date": date_val,
                        "open": float(item["open"]),
                        "close": float(item["close"]),
                        "high": float(item["high"]),
                        "low": float(item["low"]),
                        "vol": int(float(item["volume"])),
                        "amount": float(item.get("amount", 0.0))
                    })
                df = pd.DataFrame(rows)
                df = df[(df["date"].dt.strftime("%Y%m%d") >= cutoff) & (df["date"].dt.strftime("%Y%m%d") <= date_str)]
                if not df.empty:
                    return df.sort_values("date").reset_index(drop=True)
    except Exception as e:
        logger.debug("fetch_daily_from_web: 新浪失败 %s: %s", code, e)
        
    return pd.DataFrame()


def fetch_minutes_from_web(code: str, ndays: int = 5) -> pd.DataFrame:
    """
    网页多源分钟线瀑布流（东财 ➔ 腾讯 ➔ 新浪），不复权
    """
    code = normalize_code(code)
    market = detect_market(code)
    secid = f"1.{code}" if market == "sh" else f"0.{code}"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Referer": "https://quote.eastmoney.com/"
    }
    
    # 1. 优先：东财 trends 接口 (包含 5 天的分钟趋势数据)
    try:
        url = "https://push2his.eastmoney.com/api/qt/stock/trends2/get"
        params = {
            "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
            "ut": "7eea3edcaed734bea9cbfc24409ed989",
            "ndays": str(ndays),
            "iscr": "0",
            "secid": secid
        }
        with disable_proxy():
            resp = requests.get(url, params=params, headers=headers, timeout=10)
        if resp.status_code == 200:
            res_json = resp.json()
            trends = res_json.get("data", {}).get("trends", [])
            if trends:
                rows = []
                for item in trends:
                    p = item.split(",")
                    open_val = float(p[1])
                    close_val = float(p[2])
                    rows.append({
                        "datetime": pd.to_datetime(p[0]),
                        "open": close_val if open_val == 0.0 else open_val,
                        "high": float(p[3]),
                        "low": float(p[4]),
                        "close": close_val,
                        "vol": int(p[5]),
                        "amount": float(p[6])
                    })
                df = pd.DataFrame(rows)
                if not df.empty:
                    return df.sort_values("datetime").reset_index(drop=True)
    except Exception as e:
        logger.debug("fetch_minutes_from_web: 东财失败 %s: %s", code, e)

    # 2. 备用一：新浪分钟线 (akshare 包装过的)
    try:
        with akshare_lock:
            import akshare as ak
            symbol = f"{market}{code}"
            with disable_proxy():
                fallback_df = ak.stock_zh_a_minute(symbol=symbol, period="1", adjust="qfq")
        if fallback_df is not None and not fallback_df.empty:
            rename_map = {
                "day": "datetime",
                "open": "open",
                "high": "high",
                "low": "low",
                "close": "close",
                "volume": "vol",
            }
            df = fallback_df.rename(columns=rename_map).copy()
            if "amount" not in df.columns:
                df["amount"] = df["close"] * df["vol"]
            df["datetime"] = pd.to_datetime(df["datetime"])
            if "open" in df.columns and "close" in df.columns:
                df["open"] = df.apply(lambda r: r["close"] if r["open"] == 0.0 else r["open"], axis=1)
            cols = ["datetime", "open", "high", "low", "close", "vol", "amount"]
            return df[cols].sort_values("datetime").reset_index(drop=True)
    except Exception as e:
        logger.debug("fetch_minutes_from_web: 新浪失败 %s: %s", code, e)

    return pd.DataFrame()
