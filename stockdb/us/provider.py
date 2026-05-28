"""
美股数据 Provider 抽象层

DataProvider — provider-agnostic ABC，后续可切换 Polygon / FMP / EODHD。
YFinanceProvider — 基于 yfinance 的免费原型实现。

设计原则（来自 CLAUDE.md §3.1）：
- 拿不到的字段返回 None 并记日志，不用旧值糊弄。
- 不把 yfinance 写死到上层，上层只依赖 DataProvider 接口。
"""

import logging
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


class DataProvider(ABC):
    """美股数据 provider 抽象基类"""

    @abstractmethod
    def get_daily(
        self,
        ticker: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> pd.DataFrame:
        """
        日线 OHLCV。
        返回列：date(datetime64), open, high, low, close, adj_close, vol, amount(close*vol, 美元)
        """

    @abstractmethod
    def get_market_cap(self, ticker: str) -> Optional[float]:
        """当前市值（美元）。拿不到返回 None。"""

    @abstractmethod
    def get_avg_dollar_volume(
        self, ticker: str, window_days: int = 30
    ) -> Optional[float]:
        """近 window_days 日均成交额（close * volume，美元）。"""

    @abstractmethod
    def get_splits(
        self, ticker: str, lookback_months: int = 24
    ) -> list:
        """
        近 lookback_months 个月的拆股记录。
        返回 [(date, factor)]，正向拆股 factor>1（2:1→2.0），
        反向拆股 factor<1（1合20→0.05）。
        """

    @abstractmethod
    def get_revenue_yoy(self, ticker: str) -> Optional[dict]:
        """
        营收同比。返回 {
            'latest_q_rev': float,    # 最新季度营收（美元）
            'yoy_growth': float,      # 同比增速（小数，0.25 = 25%）
            'prev_yoy_growth': float, # 上一季度同比增速（判断加速用）
            'fetched_at': str,        # 获取时间（ISO 格式）
        }
        拿不到返回 None。
        """

    @abstractmethod
    def get_adj_price_peak_ratio(
        self, ticker: str, lookback_years: int = 5
    ) -> Optional[float]:
        """
        前复权历史最高价 / 当前价。
        NIVF 指纹：19万 / 0.7 ≈ 27万 >> 100。
        拿不到返回 None。
        """

    @abstractmethod
    def get_meta(self, ticker: str) -> Optional[dict]:
        """
        股票基础信息。返回 {
            'name', 'exchange', 'sector', 'industry',
            'shares_outstanding', 'fetched_at'
        }
        """


# ─────────────────────────────────────────────────────────────
# yfinance 实现
# ─────────────────────────────────────────────────────────────

class YFinanceProvider(DataProvider):
    """基于 yfinance 的免费数据源实现（原型/回测用）"""

    def _ticker(self, t: str):
        """懒加载 yfinance，镜像项目对 akshare 的延迟 import 风格"""
        import yfinance as yf  # noqa: PLC0415
        return yf.Ticker(t.upper())

    def get_daily(
        self,
        ticker: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> pd.DataFrame:
        try:
            tk = self._ticker(ticker)
            # auto_adjust=False 保留原始价和复权价，以便同时计算 amount 和 adj_close
            df = tk.history(start=start, end=end, auto_adjust=False)
            if df.empty:
                logger.warning("get_daily: %s 无数据 (start=%s end=%s)", ticker, start, end)
                return pd.DataFrame()
            df = df.reset_index()
            df.columns = [c.strip() for c in df.columns]
            # 列映射
            rename = {
                "Date": "date",
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
                "Adj Close": "adj_close",
                "Volume": "vol",
            }
            df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
            # 去掉时区（yfinance 返回 America/New_York），存盘和比较统一用 tz-naive 日期
            df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None).dt.normalize()
            df["amount"] = df["close"] * df["vol"]  # 日成交额（美元近似）
            cols = [c for c in ["date", "open", "high", "low", "close", "adj_close", "vol", "amount"] if c in df.columns]
            df = df[cols].dropna(subset=["date"]).sort_values("date").drop_duplicates(subset=["date"])
            return df.reset_index(drop=True)
        except Exception as e:
            logger.error("get_daily %s 失败: %s", ticker, e)
            return pd.DataFrame()

    def get_market_cap(self, ticker: str) -> Optional[float]:
        try:
            tk = self._ticker(ticker)
            # fast_info 速度快，优先用
            mc = getattr(tk.fast_info, "market_cap", None)
            if mc and mc > 0:
                return float(mc)
            # 兜底 info（较慢）
            mc = tk.info.get("marketCap")
            return float(mc) if mc else None
        except Exception as e:
            logger.warning("get_market_cap %s 失败: %s", ticker, e)
            return None

    def get_avg_dollar_volume(
        self, ticker: str, window_days: int = 30
    ) -> Optional[float]:
        try:
            end = datetime.now()
            start = end - timedelta(days=window_days * 2)  # 多拿几天保证够 window_days 个交易日
            df = self.get_daily(ticker, start=start.strftime("%Y-%m-%d"))
            if df.empty:
                return None
            df = df.tail(window_days)
            adv = (df["close"] * df["vol"]).mean()
            return float(adv) if not pd.isna(adv) else None
        except Exception as e:
            logger.warning("get_avg_dollar_volume %s 失败: %s", ticker, e)
            return None

    def get_splits(
        self, ticker: str, lookback_months: int = 24
    ) -> list:
        try:
            tk = self._ticker(ticker)
            splits = tk.splits  # pandas Series，index=日期，value=拆股比例
            if splits is None or splits.empty:
                return []
            cutoff = datetime.now() - timedelta(days=lookback_months * 30)
            # yfinance 返回的 splits index 可能带时区
            splits.index = pd.to_datetime(splits.index).tz_localize(None)
            result = []
            for dt, factor in splits.items():
                if dt >= pd.Timestamp(cutoff) and factor != 0:
                    result.append((dt.date(), float(factor)))
            return result
        except Exception as e:
            logger.warning("get_splits %s 失败: %s", ticker, e)
            return []

    def get_revenue_yoy(self, ticker: str) -> Optional[dict]:
        try:
            tk = self._ticker(ticker)
            stmt = tk.quarterly_income_stmt
            if stmt is None or stmt.empty:
                logger.warning("get_revenue_yoy %s: 无季报数据", ticker)
                return None
            # 找 Total Revenue 行（大小写宽容）
            rev_row = None
            for idx in stmt.index:
                if "total revenue" in str(idx).lower():
                    rev_row = stmt.loc[idx]
                    break
            if rev_row is None or len(rev_row) < 5:
                logger.warning("get_revenue_yoy %s: 季报数据不足 5 季", ticker)
                return None
            # 按时间降序排列（最新在前）
            rev_row = rev_row.sort_index(ascending=False)
            cols = rev_row.index.tolist()
            # 最新季度营收
            r0 = float(rev_row.iloc[0])   # Q0（最新）
            r1 = float(rev_row.iloc[1])   # Q-1
            r4 = float(rev_row.iloc[4])   # Q-4（去年同季）
            r5 = float(rev_row.iloc[5]) if len(rev_row) > 5 else None  # Q-5
            if r4 == 0:
                return None
            yoy = (r0 - r4) / abs(r4)
            prev_yoy = ((r1 - r5) / abs(r5)) if (r5 is not None and r5 != 0) else None
            return {
                "latest_q_rev": r0,
                "yoy_growth": round(yoy, 4),
                "prev_yoy_growth": round(prev_yoy, 4) if prev_yoy is not None else None,
                "fetched_at": datetime.now().isoformat(),
            }
        except Exception as e:
            logger.warning("get_revenue_yoy %s 失败: %s", ticker, e)
            return None

    def get_adj_price_peak_ratio(
        self, ticker: str, lookback_years: int = 5
    ) -> Optional[float]:
        """
        前复权历史最高价 / 当前价（越大说明从峰值跌越多，NIVF 是极端案例）。
        用 auto_adjust=True 拉前复权价序列。
        """
        try:
            tk = self._ticker(ticker)
            df = tk.history(period=f"{lookback_years}y", auto_adjust=True)
            if df.empty or "Close" not in df.columns:
                return None
            peak = float(df["Close"].max())
            current = float(df["Close"].iloc[-1])
            if current <= 0:
                return None
            return round(peak / current, 2)
        except Exception as e:
            logger.warning("get_adj_price_peak_ratio %s 失败: %s", ticker, e)
            return None

    def get_meta(self, ticker: str) -> Optional[dict]:
        try:
            tk = self._ticker(ticker)
            info = tk.info or {}
            return {
                "name": info.get("longName") or info.get("shortName", ""),
                "exchange": info.get("exchange", ""),
                "sector": info.get("sector", ""),
                "industry": info.get("industry", ""),
                "shares_outstanding": info.get("sharesOutstanding"),
                "fetched_at": datetime.now().isoformat(),
            }
        except Exception as e:
            logger.warning("get_meta %s 失败: %s", ticker, e)
            return None


def build_provider(name: str = "yfinance") -> DataProvider:
    """根据配置名称实例化 provider，预留切换接口"""
    if name == "yfinance":
        return YFinanceProvider()
    raise ValueError(f"未知 provider: {name}，目前仅支持 yfinance")
