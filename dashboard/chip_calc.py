import numpy as np
import pandas as pd
import time
from pathlib import Path
from stockdb import StockDB

def calculate_daily_chip(db: StockDB, code: str, calc_days: int = 500, show_days: int = 150):
    """
    基于日线数据的筹码分布（CYQ）三角分布递推算法。
    
    参数:
    - db: StockDB 实例
    - code: 股票代码，如 "sh600519" 或 "sz000001"
    - calc_days: 历史回溯计算天数（默认500天以完全消除冷启动误差）
    - show_days: 最终返回给前端展示的天数（默认最近150天，前350天作为预热静默消耗掉）
    """
    start_time = time.time()
    
    # 1. 获取日线数据
    df = db.daily(code)
    if df.empty:
        return {"success": False, "error": f"未找到股票 {code} 的本地日线行情数据"}
        
    df = df.sort_values("date").reset_index(drop=True)
    
    # 限制实际计算的天数
    df = df.tail(calc_days).reset_index(drop=True)
    n_days = len(df)
    if n_days < 20:
        return {"success": False, "error": f"该股票本地日线数据天数不足 20 天（当前 {n_days} 天），无法计算筹码分布"}
        
    # 2. 计算代理换手率 (Turnover Rate Proxy)
    # 真实换手率在日线Parquet中可能缺失，或为了算法平滑，我们使用 20 日均量作为换手基准
    vols = df["vol"].values
    vol_series = pd.Series(vols)
    vol_ma = vol_series.rolling(window=20, min_periods=1).mean().values
    
    # 代理换手率：今日成交量相对于20日均量的倍数 * 0.1，裁剪在 [0.005, 0.3] 之间以防极端异常值
    turnovers = (vols / np.clip(vol_ma, 1.0, None)) * 0.1
    turnovers = np.clip(turnovers, 0.005, 0.3)
    
    # 3. 确定全局价格网格区间并过滤脏数据
    lows = df["low"].values
    highs = df["high"].values
    closes = df["close"].values
    
    # 使用最新一天的收盘价作为基础锚（最新一天的收盘价通常是最可靠的，免除 999999.0 或负数等历史脏数据干扰）
    ref_price = closes[-1] if (len(closes) > 0 and closes[-1] > 0) else np.median(closes)
    if not (ref_price > 0):
        ref_price = 10.0  # 最终兜底
        
    # 定义过滤边界，允许最新收盘价的 0.1 倍至 10 倍（A股 500 天内振幅极少超出此范围，同时可以彻底隔离脏数据）
    lower_limit = ref_price * 0.1
    upper_limit = ref_price * 10.0
    
    # 清洗数据，将超出边界的极端异常价格 clip 到合理阈值
    clean_lows = np.clip(lows, lower_limit, upper_limit)
    clean_highs = np.clip(highs, lower_limit, upper_limit)
    clean_closes = np.clip(closes, lower_limit, upper_limit)
    
    # 为了保证筹码图在最近 show_days 天的画图分辨率，避免因 500 天前极大或极小的值压缩当前筹码峰
    # 我们的网格区间只以最近 show_days 天的价格极值为基础，并上下拓宽 15%
    show_len = min(show_days, len(df))
    recent_lows = clean_lows[-show_len:]
    recent_highs = clean_highs[-show_len:]
    
    global_min = recent_lows.min() * 0.85
    global_max = recent_highs.max() * 1.15
    
    # 确保 global_min 为正，且 global_max 大于 global_min
    global_min = max(0.01, global_min)
    if global_max <= global_min:
        global_max = global_min * 1.3
        
    N_PRICE_LEVELS = 200
    price_levels = np.linspace(global_min, global_max, N_PRICE_LEVELS)
    
    # 4. 初始化筹码分布
    chips = np.zeros(N_PRICE_LEVELS, dtype=float)
    
    # 第一天的筹码在第一天的 [low, high] 区间内均匀分布 (第一天也需要用清洗后的价格并裁剪到全局网格)
    first_low = np.clip(clean_lows[0], global_min, global_max)
    first_high = np.clip(clean_highs[0], global_min, global_max)
    if first_high < first_low:
        first_high = first_low
        
    in_first_range = (price_levels >= first_low) & (price_levels <= first_high)
    if in_first_range.any():
        chips[in_first_range] = 1.0 / in_first_range.sum()
    else:
        # 如果第一天范围极窄以至于没落在任何网格内，则单点落在第一天收盘价最接近的网格
        first_close = np.clip(clean_closes[0], global_min, global_max)
        idx = np.searchsorted(price_levels, first_close)
        idx = min(idx, N_PRICE_LEVELS - 1)
        chips[idx] = 1.0
        
    history_records = []
    
    # 5. 递推演进
    for i in range(n_days):
        # 递推价格需限幅在 [global_min, global_max] 之间，防止越界并自动堆积历史极值筹码到边界
        L = np.clip(clean_lows[i], global_min, global_max)
        H = np.clip(clean_highs[i], global_min, global_max)
        C = np.clip(clean_closes[i], global_min, global_max)
        
        if H < L:
            H = L
        C = np.clip(C, L, H)
        tr = turnovers[i]
        
        # 当天成交量的价格空间分布（三角分布模型）
        day_dist = np.zeros(N_PRICE_LEVELS, dtype=float)
        
        if H > L:
            mask = (price_levels >= L) & (price_levels <= H)
            if mask.any():
                p_sub = price_levels[mask]
                f = np.zeros_like(p_sub)
                
                denom_left = C - L
                denom_right = H - C
                
                left_mask = p_sub <= C
                right_mask = p_sub > C
                
                # 收盘价左半边（L -> C）线性递增，右半边（C -> H）线性递减
                if denom_left > 0:
                    f[left_mask] = (p_sub[left_mask] - L) / denom_left
                else:
                    f[left_mask] = 1.0
                    
                if denom_right > 0:
                    f[right_mask] = (H - p_sub[right_mask]) / denom_right
                else:
                    f[right_mask] = 1.0
                    
                # 归一化今日分布
                if f.sum() > 0:
                    f /= f.sum()
                else:
                    f = np.ones_like(f) / len(f)
                    
                day_dist[mask] = f
            else:
                # 兜底：如果今日区间太小没落在网格内
                idx = np.abs(price_levels - C).argmin()
                day_dist[idx] = 1.0
        else:
            # 一字涨跌停或无波动，今日成交全部堆积在收盘价（单点分布）
            idx = np.searchsorted(price_levels, C)
            idx = min(idx, N_PRICE_LEVELS - 1)
            day_dist[idx] = 1.0
            
        # 递推：历史筹码衰减（1 - tr），今日成交注入（tr）
        chips = chips * (1.0 - tr) + day_dist * tr
        chips /= chips.sum()  # 防止浮点数累加误差
        
        # 6. 计算当天的筹码统计指标
        cum_chips = np.cumsum(chips)
        close_idx = np.searchsorted(price_levels, C)
        close_idx = min(close_idx, N_PRICE_LEVELS - 1)
        
        # 获利盘比例 (当前收盘价以下的所有筹码比例)
        profit_ratio = float(chips[:close_idx].sum())
        
        # 集中度计算 (90% 和 70% 筹码区间)
        idx_lo90 = np.searchsorted(cum_chips, 0.05)
        idx_hi90 = np.searchsorted(cum_chips, 0.95)
        idx_lo70 = np.searchsorted(cum_chips, 0.15)
        idx_hi70 = np.searchsorted(cum_chips, 0.85)
        
        n = N_PRICE_LEVELS - 1
        p_lo90 = price_levels[min(idx_lo90, n)]
        p_hi90 = price_levels[min(idx_hi90, n)]
        p_lo70 = price_levels[min(idx_lo70, n)]
        p_hi70 = price_levels[min(idx_hi70, n)]
        
        conc90 = (p_hi90 - p_lo90) / max(C, 1e-6)
        conc70 = (p_hi70 - p_lo70) / max(C, 1e-6)
        
        avg_cost = float(np.dot(price_levels, chips))
        peak_price = float(price_levels[np.argmax(chips)])
        
        # 日期格式化，保留 YYYY-MM-DD
        date_str = str(df["date"].iloc[i])
        if " " in date_str:
            date_str = date_str.split(" ")[0]
            
        history_records.append({
            "date": date_str,
            "close": round(float(C), 2),
            "profit_ratio": round(float(profit_ratio) * 100, 2),
            "concentration_90": round(float(conc90) * 100, 2),
            "concentration_70": round(float(conc70) * 100, 2),
            "avg_cost": round(float(avg_cost), 2),
            "peak_price": round(float(peak_price), 2),
            "is_warmup": False  # 因为我们会在返回前裁剪冷启动区间，所以这里均为 False
        })
        
    # 只保留最近 show_days 天的历史数据返回给前端，前 350 天静默预热
    ret_history = history_records[-show_days:]
    
    elapsed = time.time() - start_time
    # print(f"Daily chip calculation elapsed for {code}: {elapsed*1000:.2f} ms")
    
    return {
        "success": True,
        "code": code,
        "price_levels": price_levels.tolist(),
        "final_chips": chips.tolist(),
        "history": ret_history,
        "stats": {
            "current_close": round(float(closes[-1]), 2),
            "profit_ratio": round(float(ret_history[-1]["profit_ratio"]), 2),
            "concentration_90": round(float(ret_history[-1]["concentration_90"]), 2),
            "concentration_70": round(float(ret_history[-1]["concentration_70"]), 2),
            "avg_cost": round(float(ret_history[-1]["avg_cost"]), 2),
            "peak_price": round(float(ret_history[-1]["peak_price"]), 2)
        }
    }
