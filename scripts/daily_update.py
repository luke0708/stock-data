"""
daily_update.py — 增量补齐最近交易日数据

用法：
    python3 scripts/daily_update.py        # 补齐最近交易日（含周末自动回溯）

缺失数据过多时会提示运行 init_full.py。
"""

import logging
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from stockdb.config import Config
from stockdb.db import MetaDB
from stockdb.market import INDEX_MAP, code_to_tdx_market
from stockdb.reader import _bars_to_df, _append_parquet, StockDB, fetch_minutes_from_web, disable_proxy
from stockdb.tdx_client import tdx_connect

# ── 日志 ─────────────────────────────────────────────

cfg_global = Config()
cfg_global.ensure_dirs()
today_str = datetime.now().strftime("%Y%m%d")
log_file = cfg_global.log_dir / f"daily_update_{today_str}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(str(log_file), encoding="utf-8"),
    ],
)
logger = logging.getLogger("daily_update")

# A 股代码范围（含主板、科创、创业板以及北交所代码如 43/83/87 开头，排除债券、ETF、转债等）
A_SHARE_RE = re.compile(
    r'^(000|001|002|003|300|301|600|601|603|605|688|43\d|83\d|87\d)\d{3}$'
)

# 超过此天数提示使用 init_full.py（pytdx 最多返回约 800 根日线）
MAX_INCREMENTAL_DAYS = 60


# ── 交易日判断 ────────────────────────────────────────

def resolve_target_date(meta: MetaDB) -> str:
    """
    确定本次更新的目标交易日：
      - 如果当前时间在 15:30 之后，目标日期上限为今天。
      - 如果当前时间在 15:30 之前，目标日期上限为昨天。
      - 然后在 trade_calendar 中寻找不大于该上限的最近一个交易日，防止盘中数据污染。
    """
    from datetime import time as dtime
    now = datetime.now()
    
    # 确定查询上限日期
    if now.time() >= dtime(15, 30):
        limit_date = now.strftime("%Y%m%d")
    else:
        limit_date = (now - timedelta(days=1)).strftime("%Y%m%d")
        
    # 从元数据库交易日历寻找最近的已开市交易日
    try:
        with meta._conn() as conn:
            row = conn.execute(
                "SELECT date FROM trade_calendar WHERE is_open=1 AND date <= ? ORDER BY date DESC LIMIT 1",
                (limit_date,)
            ).fetchone()
        if row:
            return row["date"]
    except Exception as e:
        logger.warning("Query trade calendar failed: %s", e)
        
    # 兜底回溯逻辑（若日历表未初始化或空）
    target = now if now.time() >= dtime(15, 30) else now - timedelta(days=1)
    if target.weekday() == 5:   # 周六
        target = target - timedelta(days=1)
    elif target.weekday() == 6: # 周日
        target = target - timedelta(days=2)
    return target.strftime("%Y%m%d")


def is_trade_day(meta: MetaDB, date_str: str) -> bool:
    """判断目标日期是否为交易日，akshare 失败时工作日兜底。"""
    if meta.is_trade_day(date_str):
        return True
    try:
        import akshare as ak
        df = ak.tool_trade_date_hist_sina()
        dates = set(df.iloc[:, 0].astype(str).str.replace("-", ""))
        result = date_str in dates
        if result:
            meta.upsert_calendar([date_str], is_open=1)
        return result
    except Exception as e:
        logger.warning("akshare 日历不可用（%s），工作日兜底", e)
        dt = datetime.strptime(date_str, "%Y%m%d")
        return dt.weekday() < 5  # 周一到周五假定为交易日


# ── pytdx 增量更新 ────────────────────────────────────

# 并发连接数（每个 worker 独立持一条 TDX TCP 连接）
CONCURRENT_WORKERS = 8


def _peek_last_date(parquet_path) -> str:
    """
    快速读取 Parquet 文件中最新的 date 字符串（格式 YYYYMMDD）。
    不加载全表，只读 date 列，用于跳过已有最新数据的股票。
    """
    try:
        import pyarrow.parquet as pq
        pf = pq.read_table(str(parquet_path), columns=["date"])
        if pf.num_rows == 0:
            return ""
        # 取最后一个值（数据已按日期升序排列）
        last = pf.column("date")[-1].as_py()
        if hasattr(last, "strftime"):
            return last.strftime("%Y%m%d")
        return str(last)[:10].replace("-", "")
    except Exception:
        return ""


def _fetch_akshare_day(code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """
    akshare 兜底：拉取指定股票的日线数据（不复权，与 pytdx 格式一致）。
    start_date / end_date 格式均为 YYYYMMDD。
    """
    try:
        import akshare as ak
        with disable_proxy():
            df = ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust="",          # 不复权，与 pytdx 保持一致
            )
        if df is None or df.empty:
            return pd.DataFrame()
        col_map = {
            "日期": "date", "开盘": "open", "最高": "high",
            "最低": "low",  "收盘": "close", "成交量": "vol", "成交额": "amount",
        }
        df = df.rename(columns=col_map)
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        cols = [c for c in ["date", "open", "high", "low", "close", "vol", "amount"]
                if c in df.columns]
        return df[cols].dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    except Exception as e:
        logger.debug("akshare fallback failed %s: %s", code, e)
        return pd.DataFrame()


def _update_one(args) -> bool:
    """
    单只股票更新任务（在线程池中执行）。
    主力：pytdx；fallback：akshare（pytdx 数据为空或全部被过滤时自动切换）。
    args = (api, parquet_path, market_int, n_bars, cutoff, date_str)
    返回 True 表示成功写入，False 表示跳过或失败。
    """
    api, parquet_path, market_int, n_bars, cutoff, date_str = args
    code = parquet_path.stem

    # ── 快速预检：本地已有目标日期，直接跳过网络请求 ──
    if parquet_path.exists() and _peek_last_date(parquet_path) >= date_str:
        return False  # 数据已是最新，无需拉取

    new_df = pd.DataFrame()

    # ── 第一优先：pytdx ──────────────────────────────
    try:
        data = api.get_security_bars(9, market_int, code, 0, n_bars)
        if data:
            df = _bars_to_df(data, freq="daily")
            if not df.empty:
                new_df = df[
                    (df["date"].dt.strftime("%Y%m%d") >= cutoff) &
                    (df["date"].dt.strftime("%Y%m%d") <= date_str)
                ]
    except Exception as e:
        logger.debug("[tdx] Skip %s: %s", code, e)

    # ── 第二优先：akshare fallback ───────────────────
    if new_df.empty:
        fb = _fetch_akshare_day(code, cutoff, date_str)
        if not fb.empty:
            new_df = fb
            logger.debug("[akshare fallback] %s: %d 行", code, len(new_df))

    if new_df.empty:
        return False

    _append_parquet(parquet_path, new_df, dedup_col="date")
    return True


def _bulk_update_from_spot(cfg: Config, date_str: str) -> int:
    """
    快速通道：akshare stock_zh_a_spot_em() 一次 HTTP 请求获取全市场当日行情，
    并行写入各 A 股 Parquet。

    适用条件：target_date == 今天 AND 15:30 后（确保收盘数据稳定）。
    速度：1 次 HTTP（~3s） + 并行 IO，远快于 pytdx 5600 次请求（~35s）。

    返回：成功写入数量；-1 表示拉取失败需降级到 pytdx。
    """
    from concurrent.futures import ThreadPoolExecutor
    import akshare as ak

    logger.info("📡 快速通道：akshare 全市场快照（单次 HTTP 请求）...")
    try:
        with disable_proxy():
            raw = ak.stock_zh_a_spot_em()
    except Exception as e:
        logger.warning("akshare 快照失败: %s，降级到 pytdx", e)
        return -1

    if raw is None or raw.empty:
        logger.warning("akshare 快照返回空，降级到 pytdx")
        return -1

    # ── 列名标准化 ─────────────────────────────────────
    col_map = {
        "代码": "code", "今开": "open", "最高": "high",
        "最低": "low",  "最新价": "close",
        "成交量": "vol", "成交额": "amount",
    }
    raw = raw.rename(columns=col_map)
    raw["code"]  = raw["code"].astype(str).str.zfill(6)
    raw["date"]  = pd.to_datetime(date_str, format="%Y%m%d")

    # 过滤：只保留 A 股正常交易（成交量 > 0）
    raw = raw[raw["code"].str.match(A_SHARE_RE)]
    if "vol" in raw.columns:
        raw["vol"] = pd.to_numeric(raw["vol"], errors="coerce").fillna(0)
        raw = raw[raw["vol"] > 0]
        # 北交所股票代码以 43/83/87 开头，其成交量单位已经是股，其余个股成交量为手，需乘以 100 对齐
        raw["vol"] = raw.apply(
            lambda r: r["vol"] if r["code"].startswith(("43", "83", "87")) else r["vol"] * 100,
            axis=1
        )

    valid_cols = [c for c in ["date", "open", "high", "low", "close", "vol", "amount"]
                  if c in raw.columns]
    spot = raw.set_index("code")[valid_cols]
    logger.info("快照含 %d 只 A 股有效数据", len(spot))

    # ── 并行写入 Parquet ───────────────────────────────
    def _write_one(parquet_path: Path) -> bool:
        code = parquet_path.stem
        if _peek_last_date(parquet_path) >= date_str:
            return False
        if code not in spot.index:
            return False
        row = spot.loc[code]
        new_df = pd.DataFrame([row]).reset_index(drop=True)
        if new_df["close"].isna().all():
            return False
        _append_parquet(parquet_path, new_df, dedup_col="date")
        return True

    updated = 0
    for daily_dir in [cfg.data_dir / "daily" / m for m in ("sh", "sz", "bj")]:
        if not daily_dir.exists():
            continue
        files = [f for f in daily_dir.glob("*.parquet") if A_SHARE_RE.match(f.stem)]
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(_write_one, files))
        updated += sum(results)

    logger.info("📡 快速通道完成：写入 %d 只", updated)
    return updated


def incremental_update(cfg: Config, date_str: str, n_bars: int) -> int:
    """
    用 pytdx 拉取每只 A 股近 n_bars 条日线，追加到本地 Parquet。
    多线程并发模式：开 CONCURRENT_WORKERS 个连接同时处理，大幅缩短耗时。
    返回成功更新的股票数量。

    快速通道：补今天数据 + 15:30 后 → akshare 全市场快照（1 次 HTTP），大幅提速。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from datetime import time as dtime
    import threading

    # ── 快速通道判断 ──────────────────────────────────
    today_str = datetime.now().strftime("%Y%m%d")
    if date_str == today_str and datetime.now().time() >= dtime(15, 30):
        n = _bulk_update_from_spot(cfg, date_str)
        if n >= 0:
            return n
        logger.info("快速通道失败，降级到 pytdx 逐只模式...")

    meta = MetaDB(cfg.db_path)
    
    # ── 获取已退市代码列表，防止无效网络同步 ──
    delisted_codes = set()
    try:
        with meta._conn() as conn:
            rows = conn.execute(
                "SELECT code FROM stocks WHERE delist_date IS NOT NULL AND delist_date != '' AND delist_date <= ?",
                (date_str,)
            ).fetchall()
            delisted_codes = {r["code"] for r in rows}
    except Exception as e:
        logger.warning("Failed to query delisted stocks: %s", e)

    cutoff = f"{datetime.now().year - cfg.history_years}0101"
    total = 0

    market_cfg = {
        "sh": (1, cfg.data_dir / "daily" / "sh"),
        "sz": (0, cfg.data_dir / "daily" / "sz"),
        "bj": (0, cfg.data_dir / "daily" / "bj"),
    }

    for market_str, (market_int, daily_dir) in market_cfg.items():
        if not daily_dir.exists():
            continue

        a_files = [f for f in daily_dir.glob("*.parquet")
                   if A_SHARE_RE.match(f.stem)]
        if not a_files:
            continue

        logger.info("[%s] 并发更新 %d 只 A 股（%d workers，最近 %d 条）...",
                    market_str.upper(), len(a_files), CONCURRENT_WORKERS, n_bars)

        # ── 预过滤：本地已有目标日期或属于已退市股票的直接跳过 ──────────
        need_update = [
            f for f in a_files
            if (not f.exists() or _peek_last_date(f) < date_str) and f.stem not in delisted_codes
        ]
        already_done = len(a_files) - len(need_update)
        if already_done:
            logger.info("[%s] 跳过 %d 只（已有数据或已退市），实际拉取 %d 只",
                        market_str.upper(), already_done, len(need_update))
        if not need_update:
            logger.info("[%s] 全部已是最新，跳过", market_str.upper())
            continue

        # 建立 CONCURRENT_WORKERS 条独立 TDX 连接
        apis = []
        try:
            from pytdx.hq import TdxHq_API
            for _ in range(CONCURRENT_WORKERS):
                api = TdxHq_API()
                for host, port in cfg.servers:
                    try:
                        api.connect(str(host), int(port))
                        apis.append(api)
                        break
                    except Exception:
                        pass
            if not apis:
                logger.error("[%s] pytdx 连接失败，跳过", market_str.upper())
                return 0
        except Exception as e:
            logger.error("[%s] pytdx 初始化失败: %s", market_str.upper(), e)
            return 0

        # 轮询分配连接（线程安全计数器）
        counter = 0
        counter_lock = threading.Lock()

        def pick_api():
            nonlocal counter
            with counter_lock:
                idx = counter % len(apis)
                counter += 1
            return apis[idx]

        updated = failed = 0
        try:
            with ThreadPoolExecutor(max_workers=CONCURRENT_WORKERS) as pool:
                futures = {
                    pool.submit(
                        _update_one,
                        (pick_api(), p, market_int, n_bars, cutoff, date_str)
                    ): p
                    for p in need_update
                }
                for fut in as_completed(futures):
                    try:
                        if fut.result():
                            updated += 1
                        else:
                            failed += 1
                    except Exception as e:
                        failed += 1
                        logger.debug("Future error: %s", e)
        finally:
            for api in apis:
                try:
                    api.disconnect()
                except Exception:
                    pass

        logger.info("[%s] 完成: 更新 %d 只，失败 %d", market_str.upper(), updated, failed)
        total += updated

    return total


# ── 指数更新 ─────────────────────────────────────────

def update_indices(cfg: Config, date_str: str):
    logger.info("更新指数日线（7只）...")
    for code, (market_str, name) in INDEX_MAP.items():
        path = cfg.index_path(code)
        market_int = 1 if market_str == "sh" else 0
        try:
            with tdx_connect(cfg.servers) as api:
                # 指数必须用 get_index_bars()，get_security_bars() 会返回乱码
                data = api.get_index_bars(9, market_int, code, 0, 10)
            if not data:
                continue
            df = _bars_to_df(data, freq="daily")
            if df.empty:
                continue
            df = df[df["date"].dt.strftime("%Y%m%d") >= date_str]
            if not df.empty:
                _append_parquet(path, df, dedup_col="date")
                logger.info("Index %s (%s) ✅", code, name)
        except Exception as e:
            logger.warning("Index %s failed: %s", code, e)


# ── 分钟线主动沉淀与增量沉淀 ─────────────────────────

def get_watchlist_and_cached_codes(cfg: Config) -> set:
    codes = set(cfg.watchlist)
    minutes_dir = cfg.data_dir / "minutes"
    if minutes_dir.exists():
        for m in ("sh", "sz", "bj"):
            m_dir = minutes_dir / m
            if m_dir.exists():
                for f in m_dir.iterdir():
                    if f.is_dir() and A_SHARE_RE.match(f.name):
                        codes.add(f.name)
    return codes


def get_last_minutes_date(cfg: Config, code: str) -> str:
    from stockdb.market import detect_market
    market = detect_market(code)
    code_dir = cfg.data_dir / "minutes" / market / code
    if not code_dir.exists():
        return ""
    parquet_files = sorted(code_dir.glob("*.parquet"))
    if not parquet_files:
        return ""
    return parquet_files[-1].stem  # 返回 YYYYMMDD


def update_minutes(cfg: Config, date_str: str, meta: MetaDB):
    """
    主动更新分钟线（Watchlist + 已缓存股），防止 100 天断档风险。
    """
    logger.info("=" * 50)
    logger.info("📡 开始增量补齐 1 分钟线 data...")
    logger.info("=" * 50)

    # 1. 查找需要更新的所有股票
    codes = get_watchlist_and_cached_codes(cfg)
    if not codes:
        logger.info("没有需要更新分钟线的股票，跳过。")
        return

    logger.info("共找到 %d 只股票需要更新分钟线...", len(codes))

    # 判断当前时间是否允许写入今日（date_str）的分钟线 Parquet
    # 分钟线强收盘线定位在 15:10
    today_str = datetime.now().strftime("%Y%m%d")
    allow_today_write = True
    if date_str == today_str and datetime.now().time() < datetime.strptime("15:10", "%H:%M").time():
        allow_today_write = False
        logger.info("当前时间未到 15:10，今日分钟数据不完整，今日 Parquet 将不予写入。")

    updated_count = 0

    # 获取已退市代码
    delisted_codes = set()
    try:
        with meta._conn() as conn:
            rows = conn.execute(
                "SELECT code FROM stocks WHERE delist_date IS NOT NULL AND delist_date != '' AND delist_date <= ?",
                (date_str,)
            ).fetchall()
            delisted_codes = {r["code"] for r in rows}
    except Exception as e:
        logger.warning("Query delisted stocks failed in update_minutes: %s", e)

    def _update_one_minute(code: str) -> bool:
        try:
            if code in delisted_codes:
                logger.info("股票 [%s] 已退市，跳过分钟线更新。", code)
                return False

            # 检查本地最后更新日期
            last_date = get_last_minutes_date(cfg, code)
            
            # 如果本地已经是最新的，无需拉取
            if last_date and last_date >= date_str:
                return False
                
            # 检查本地日线的最新日期
            daily_path = cfg.daily_path(code)
            last_daily_date = _peek_last_date(daily_path)
            
            # 如果已同步到日线最新进度，跳过（停牌无新交易）
            if last_date and last_daily_date and last_date >= last_daily_date:
                return False
            
            # 计算缺失天数
            missing_days = 999
            if last_date:
                missing_days = (
                    datetime.strptime(date_str, "%Y%m%d") -
                    datetime.strptime(last_date, "%Y%m%d")
                ).days
                
            # 断档检测报警：只有当本地日线领先于分钟线才报警
            if last_daily_date and last_date and last_daily_date > last_date:
                if missing_days >= 100:
                    logger.critical("⚠️  股票 [%s] 分钟线已超过 100 天未更新（上次: %s），存在数据断档！", code, last_date)
                elif missing_days >= 90:
                    logger.warning("⚠️  股票 [%s] 分钟线已连续 %d 天未更新，请及时补齐！", code, missing_days)
                
            df_new = pd.DataFrame()
            
            # 1. 缺失少于等于 5 天，走极速 HTTP 趋势
            if missing_days <= 5:
                df_new = fetch_minutes_from_web(code, ndays=5)
            
            # 2. trends 失败，或者缺失天数大于 5 天，走 pytdx 批量拉取
            if df_new.empty:
                try:
                    db_inst = StockDB()
                    market = code_to_tdx_market(code)
                    with tdx_connect(db_inst.cfg.servers) as api:
                        from stockdb.tdx_client import fetch_bars
                        data = fetch_bars(api, code, market, frequency=8)
                    df_new = _bars_to_df(data, freq="minute")
                except Exception as ex:
                    logger.debug("Tdx minutes pull failed for %s: %s", code, ex)
            
            # 3. 网页直连接口兜底
            if df_new.empty:
                df_new = fetch_minutes_from_web(code, ndays=5)

            if df_new.empty:
                return False
                
            # 分组写盘
            df_new["_date"] = df_new["datetime"].dt.strftime("%Y%m%d")
            grouped = df_new.groupby("_date")
            cached_days = 0
            for day_str, day_df in grouped:
                if day_str == today_str and not allow_today_write:
                    continue
                
                day_df = day_df.drop(columns=["_date"]).reset_index(drop=True)
                path = cfg.minutes_path(code, day_str)
                if not path.exists():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    day_df.to_parquet(path, index=False)
                    cached_days += 1
            return cached_days > 0
        except Exception as err:
            logger.error("更新股票 [%s] 分钟线时发生异常: %s", code, err)
            return False

    # 串行同步轮询，防范多线程 V8 内存冲突
    results = []
    for code in sorted(codes):
        results.append(_update_one_minute(code))
        
    updated_count = sum(results)
    logger.info("分钟线更新完成！共更新 %d 只股票的分钟数据。", updated_count)
    logger.info("=" * 50)


# ── 主程序 ───────────────────────────────────────────

def main():
    cfg = Config()
    meta = MetaDB(cfg.db_path)

    # 1. 确定目标日期（周末自动回溯）
    date_str = resolve_target_date(meta)

    logger.info("=" * 50)
    logger.info("stockdb 每日更新 — %s", date_str)
    logger.info("=" * 50)

    # 2. 判断是否为交易日
    if not is_trade_day(meta, date_str):
        logger.info("%s 非交易日，无需更新", date_str)
        return

    # 3. 判断缺失天数
    last = meta.last_updated_day()
    if last:
        missing_days = (
            datetime.strptime(date_str, "%Y%m%d") -
            datetime.strptime(last, "%Y%m%d")
        ).days
    else:
        missing_days = 9999  # 从未初始化

    if missing_days > MAX_INCREMENTAL_DAYS:
        logger.error(
            "❌ 本地数据缺失超过 %d 天（上次更新: %s）",
            missing_days, last or "无"
        )
        logger.error("   请运行全量初始化：python3 scripts/init_full.py")
        sys.exit(1)

    # 4. 日线更新控制流
    if missing_days > 0:
        logger.info("距上次更新 %d 天，开始增量补齐日线数据...", missing_days)
        # pytdx 增量拉取（n_bars 留一定余量，最少 5 条）
        n_bars = max(5, min(missing_days * 2 + 5, 50))
        n = incremental_update(cfg, date_str, n_bars)

        if n == 0:
            logger.error("❌ pytdx 增量更新失败（无数据返回）")
            logger.error("   请检查网络连接，或运行：python3 scripts/init_full.py")
            sys.exit(1)

        # 5. 指数 + 日历
        update_indices(cfg, date_str)
        meta.upsert_calendar([date_str], is_open=1)
        meta.log_update(date_str, "ok", f"增量更新 {n} 只")
        logger.info("=" * 50)
        logger.info("✅ 日线更新完成！共补齐 %d 只 A 股数据", n)
        logger.info("=" * 50)
    else:
        logger.info("✅ %s 日线数据已是最新，无需更新。", date_str)

    # 5. 分钟线增量沉淀
    try:
        update_minutes(cfg, date_str, meta)
    except Exception as e:
        logger.warning("分钟线增量沉淀失败: %s", e)

    # 6. Tick 清理
    try:
        db = StockDB()
        db.clean_old_ticks()
    except Exception as e:
        logger.warning("清理旧 Tick 缓存失败: %s", e)

    # 7. 股票流通股本更新
    try:
        logger.info("📡 开始同步股票流通股本...")
        db = StockDB()
        db.update_stock_shares()
    except Exception as e:
        logger.warning("同步股票流通股本失败: %s", e)


if __name__ == "__main__":
    main()
