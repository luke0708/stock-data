"""pytdx 连接管理：自动选服务器、批量拉取封装"""

import logging
from contextlib import contextmanager
from typing import List, Tuple

logger = logging.getLogger(__name__)


@contextmanager
def tdx_connect(servers: List[Tuple[str, int]]):
    """
    Context manager，自动选择第一个可用的 TDX 服务器。

    用法：
        with tdx_connect(cfg.servers) as api:
            data = api.get_security_bars(...)
    """
    from pytdx.hq import TdxHq_API

    api = TdxHq_API()
    connected = False

    for host, port in servers:
        try:
            api.connect(str(host), int(port))
            logger.debug("TDX connected: %s:%s", host, port)
            connected = True
            break
        except Exception as e:
            logger.warning("TDX connect failed %s:%s — %s", host, port, e)

    if not connected:
        raise ConnectionError("无法连接任何 TDX 服务器，请检查网络或服务器列表。")

    try:
        yield api
    finally:
        try:
            api.disconnect()
        except Exception:
            pass


# ── 批量拉取封装 ─────────────────────────────────────


def fetch_bars(api, code: str, market: int, frequency: int, max_per_call: int = 800) -> list:
    """
    循环拉取 K 线，直到取完为止。
    frequency: 9=日线, 8=1分钟, 3=15分钟, 5=60分钟
    """
    all_data, start = [], 0
    while True:
        batch = api.get_security_bars(frequency, market, code, start, max_per_call)
        if not batch:
            break
        all_data.extend(batch)
        if len(batch) < max_per_call:
            break
        start += max_per_call
    return all_data


def fetch_bars_date_range(api, code: str, market: int, frequency: int,
                          start_date: str, end_date: str = None) -> list:
    """
    拉取指定日期区间的 K 线（日线 / 分钟线）。
    start_date / end_date: 'YYYYMMDD'
    """
    import pandas as pd
    all_data = fetch_bars(api, code, market, frequency)
    if not all_data:
        return []

    df = pd.DataFrame(all_data)
    if "datetime" not in df.columns:
        return all_data

    df["_d"] = df["datetime"].astype(str).str[:8]
    if start_date:
        df = df[df["_d"] >= start_date]
    if end_date:
        df = df[df["_d"] <= end_date]
    return df.drop(columns=["_d"]).to_dict("records")


def fetch_security_list(api, market: int) -> list:
    """拉取某市场全部股票列表（循环直到取完）"""
    all_stocks, offset = [], 0
    while True:
        batch = api.get_security_list(market, offset)
        if not batch:
            break
        all_stocks.extend(batch)
        if len(batch) < 1000:
            break
        offset += len(batch)
    return all_stocks


def fetch_tick(api, code: str, market: int, date_int: int = None,
               max_per_call: int = 2000) -> list:
    """
    拉取逐笔成交。
    date_int=None 拉今日，date_int=20260507 拉历史。
    """
    all_data, offset = [], 0
    while True:
        if date_int:
            batch = api.get_history_transaction_data(market, code, offset, max_per_call, date_int)
        else:
            batch = api.get_transaction_data(market, code, offset, max_per_call)
        if not batch:
            break
        all_data.extend(batch)
        if len(batch) < max_per_call:
            break
        offset += max_per_call
    return all_data
