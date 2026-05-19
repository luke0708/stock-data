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
from stockdb.market import INDEX_MAP
from stockdb.reader import _bars_to_df, _append_parquet, StockDB
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

# A 股代码范围（排除债券、ETF、转债等）
A_SHARE_RE = re.compile(
    r'^(000|001|002|003|300|301|600|601|603|605|688)\d{3}$'
)

# 超过此天数提示使用 init_full.py（pytdx 最多返回约 800 根日线）
MAX_INCREMENTAL_DAYS = 60


# ── 交易日判断 ────────────────────────────────────────

def resolve_target_date() -> str:
    """
    确定本次更新的目标交易日：
      - 工作日 → 今天
      - 周六/周日 → 自动回溯到最近周五
    """
    now = datetime.now()
    target = now
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
        raw = raw[pd.to_numeric(raw["vol"], errors="coerce").fillna(0) > 0]

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

        # ── 预过滤：本地已有目标日期的直接跳过 ──────────
        need_update = [f for f in a_files
                       if not f.exists() or _peek_last_date(f) < date_str]
        already_done = len(a_files) - len(need_update)
        if already_done:
            logger.info("[%s] 跳过 %d 只（已有 %s 数据），实际拉取 %d 只",
                        market_str.upper(), already_done, date_str, len(need_update))
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


# ── 主程序 ───────────────────────────────────────────

def main():
    cfg = Config()
    meta = MetaDB(cfg.db_path)

    # 1. 确定目标日期（周末自动回溯）
    date_str = resolve_target_date()

    logger.info("=" * 50)
    logger.info("stockdb 每日更新 — %s", date_str)
    logger.info("=" * 50)

    # 2. 判断是否为交易日
    if not is_trade_day(meta, date_str):
        logger.info("%s 非交易日，无需更新", date_str)
        return

    # 3. 判断缺失天数（用 update_log 的实际数据日期，而非 trade_calendar 日历日期）
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

    if missing_days <= 0:
        logger.info("✅ %s 数据已是最新，无需更新", date_str)
        return

    logger.info("距上次更新 %d 天，开始增量补齐...", missing_days)

    # 4. pytdx 增量拉取（n_bars 留一定余量，最少 5 条）
    n_bars = max(5, min(missing_days * 2 + 5, 50))
    n = incremental_update(cfg, date_str, n_bars)

    if n == 0:
        logger.error("❌ pytdx 增量更新失败（无数据返回）")
        logger.error("   请检查网络连接，或运行：python3 scripts/init_full.py")
        sys.exit(1)

    # 5. 指数 + 日历
    update_indices(cfg, date_str)
    meta.upsert_calendar([date_str], is_open=1)
    StockDB().clean_old_ticks()

    meta.log_update(date_str, "ok", f"增量更新 {n} 只")
    logger.info("=" * 50)
    logger.info("✅ 更新完成！共补齐 %d 只 A 股数据", n)
    logger.info("=" * 50)


if __name__ == "__main__":
    main()
