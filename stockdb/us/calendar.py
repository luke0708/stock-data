"""
美股交易日历工具

从 ^GSPC（标普500指数）的历史日线推导美股交易日集合，
镜像 A 股 init_full.py 的 _derive_calendar_from_data 思路，零新依赖。

注：若需精确节假日，可选装 pandas_market_calendars 并替换此实现。
"""

import logging
from typing import List

logger = logging.getLogger(__name__)

SPX_TICKER = "^GSPC"


def fetch_us_calendar(provider, history_years: int = 5) -> List[str]:
    """
    拉取 ^GSPC 近 history_years 年的交易日列表。
    返回 ['YYYYMMDD', ...] 格式。
    """
    from datetime import datetime, timedelta
    start = (datetime.now() - timedelta(days=history_years * 365 + 30)).strftime("%Y-%m-%d")
    try:
        df = provider.get_daily(SPX_TICKER, start=start)
        if df.empty:
            logger.warning("fetch_us_calendar: ^GSPC 无数据，日历为空")
            return []
        dates = df["date"].dt.strftime("%Y%m%d").tolist()
        logger.info("fetch_us_calendar: 获取 %d 个美股交易日", len(dates))
        return dates
    except Exception as e:
        logger.error("fetch_us_calendar 失败: %s", e)
        return []
