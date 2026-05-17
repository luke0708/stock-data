"""
筹码分布计算引擎 — 从 OHLCV 本地计算，无需网络

核心算法：三角分布递推模型
  每根 K 线的成交量在 [low, high] 区间按三角分布分配（峰值在 close），
  通过相对换手率递推更新全市场筹码积累。

输出指标：
  profit_ratio      获利比例（当前价以下筹码占比）
  concentration_90  90% 筹码所在价格区间宽度 / 当前价
  concentration_70  70% 筹码所在价格区间宽度 / 当前价
  avg_cost          主力成本（加权平均成本）
  peak_price        峰值筹码价（众数价格）
"""

import numpy as np
import pandas as pd
from typing import Optional


N_PRICE_LEVELS = 200   # 价格分桶数量
TURNOVER_WINDOW = 20   # 换手率平滑窗口（日）
LOOKBACK_DAYS   = 500  # 最多向前追溯天数（~2年，足够"洗筹"）


def triangle_weights(
    low: float, high: float, close: float, price_levels: np.ndarray
) -> np.ndarray:
    """
    单根 K 线的成交量在价格区间上的三角分布权重。

    分布形状：
        low → close  线性上升
        close → high 线性下降
        峰值在 close

    Args:
        low, high, close: K 线价格
        price_levels: 全局价格网格（升序 ndarray）

    Returns:
        归一化权重数组，与 price_levels 等长，总和为 1
    """
    if high <= low or high <= 0:
        # 无效 K 线（停牌等），集中在 close
        w = np.zeros(len(price_levels))
        idx = int(np.searchsorted(price_levels, close))
        idx = min(idx, len(w) - 1)
        w[idx] = 1.0
        return w

    w = np.zeros(len(price_levels))
    in_range = (price_levels >= low) & (price_levels <= high)
    ps = price_levels[in_range]
    if len(ps) == 0:
        # 价格网格粒度不足，集中在 close
        idx = int(np.searchsorted(price_levels, close))
        idx = min(idx, len(w) - 1)
        w[idx] = 1.0
        return w

    # 三角分布：低价→收盘 线性上升，收盘→高价 线性下降
    left  = (ps - low)  / max(close - low,  1e-6)
    right = (high - ps) / max(high - close, 1e-6)
    tri = np.where(ps <= close, left, right)
    tri = np.clip(tri, 0, None)

    s = tri.sum()
    if s > 0:
        tri /= s
    w[in_range] = tri
    return w


def compute_chip_distribution(df: pd.DataFrame) -> pd.DataFrame:
    """
    从单只股票的日线数据计算筹码分布。

    Args:
        df: 日线 DataFrame，必须含列 [date, open, high, low, close, vol]

    Returns:
        每交易日的筹码分布指标 DataFrame，列为：
            date, close, profit_ratio, concentration_90,
            concentration_70, avg_cost, peak_price
        若数据不足 10 行，返回空 DataFrame。

    注意：
        本算法用成交量相对比例代替真实换手率（不含流通股本），
        获利比例与实际误差约 ±5~10%，方向一致，适用于策略信号。
    """
    df = df.sort_values("date").reset_index(drop=True)
    if len(df) < 10:
        return pd.DataFrame()

    # 只取最近 LOOKBACK_DAYS 天（更早的数据对当前筹码贡献极小）
    df = df.tail(LOOKBACK_DAYS).reset_index(drop=True)

    # 价格区间：全历史最低-最高，留 5% 余量
    lo = df["low"].min() * 0.95
    hi = df["high"].max() * 1.05
    price_levels = np.linspace(lo, hi, N_PRICE_LEVELS)

    # 换手率代理：vol / vol_ma（平滑后的量），clip 到 [0, 1]
    vol_ma   = df["vol"].rolling(TURNOVER_WINDOW, min_periods=1).mean()
    turnover = (df["vol"] / vol_ma.clip(lower=1)).clip(0, 1).values

    # 递推
    chips = np.zeros(N_PRICE_LEVELS)   # 初始筹码为空
    records = []

    for i in range(len(df)):
        row    = df.iloc[i]
        low_   = float(row["low"])
        high_  = float(row["high"])
        close_ = float(row["close"])
        tr     = float(turnover[i])

        # 当日新增筹码分布（三角分布）
        day_w = triangle_weights(low_, high_, close_, price_levels)

        # 筹码更新：旧筹码保留 (1-tr)，新筹码注入 tr
        chips = chips * (1.0 - tr) + day_w * tr

        # 归一化（防止浮点累积误差）
        s = chips.sum()
        if s > 0:
            chips /= s

        # ── 指标计算 ──────────────────────────────────
        cum       = np.cumsum(chips)
        close_idx = int(np.searchsorted(price_levels, close_))

        # 获利比例：当前价以下的筹码占比
        profit_ratio = float(chips[:close_idx].sum())

        # 集中度：包含 90%/70% 筹码的价格区间宽度 / 当前价
        lo90_idx = int(np.searchsorted(cum, 0.05))
        hi90_idx = int(np.searchsorted(cum, 0.95))
        lo70_idx = int(np.searchsorted(cum, 0.15))
        hi70_idx = int(np.searchsorted(cum, 0.85))

        n = N_PRICE_LEVELS - 1
        p_lo90 = price_levels[min(lo90_idx, n)]
        p_hi90 = price_levels[min(hi90_idx, n)]
        p_lo70 = price_levels[min(lo70_idx, n)]
        p_hi70 = price_levels[min(hi70_idx, n)]

        conc90 = (p_hi90 - p_lo90) / max(close_, 1e-6)
        conc70 = (p_hi70 - p_lo70) / max(close_, 1e-6)

        # 主力成本（加权平均）
        avg_cost = float(np.dot(price_levels, chips))

        # 峰值筹码价
        peak_price = float(price_levels[np.argmax(chips)])

        records.append({
            "date":             row["date"],
            "close":            round(close_, 2),
            "profit_ratio":     round(profit_ratio, 4),
            "concentration_90": round(conc90, 4),
            "concentration_70": round(conc70, 4),
            "avg_cost":         round(avg_cost, 2),
            "peak_price":       round(peak_price, 2),
        })

    return pd.DataFrame(records)
