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

def incremental_update(cfg: Config, date_str: str, n_bars: int) -> int:
    """
    用 pytdx 拉取每只 A 股近 n_bars 条日线，追加到本地 Parquet。
    返回成功更新的股票数量。
    """
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

        logger.info("[%s] 增量更新 %d 只 A 股（最近 %d 条）...",
                    market_str.upper(), len(a_files), n_bars)

        updated = failed = 0
        try:
            with tdx_connect(cfg.servers) as api:
                for parquet_path in a_files:
                    code = parquet_path.stem
                    try:
                        data = api.get_security_bars(9, market_int, code, 0, n_bars)
                        if not data:
                            continue
                        new_df = _bars_to_df(data, freq="daily")
                        if new_df.empty:
                            continue
                        new_df = new_df[
                            (new_df["date"].dt.strftime("%Y%m%d") >= cutoff) &
                            (new_df["date"].dt.strftime("%Y%m%d") <= date_str)
                        ]
                        if new_df.empty:
                            continue
                        _append_parquet(parquet_path, new_df, dedup_col="date")
                        updated += 1
                    except Exception as e:
                        failed += 1
                        logger.debug("Skip %s: %s", code, e)
        except Exception as e:
            logger.error("[%s] pytdx 连接失败: %s", market_str.upper(), e)
            return 0

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
                data = api.get_security_bars(9, market_int, code, 0, 10)
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

    # 3. 判断缺失天数
    last = meta.last_trade_day()
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
