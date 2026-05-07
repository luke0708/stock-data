"""
daily_update.py — 每日增量更新

每个交易日 16:30 后，关闭代理运行一次即可：

    python3 scripts/daily_update.py

内部自动完成：
  1. 判断今天是否为交易日
  2. 下载通达信整包 ZIP（当天已下载则跳过）
  3. 解析 ZIP → 更新全市场日线 Parquet
  4. 更新指数日线（pytdx，7只）
  5. 更新交易日历
  6. 清理过期 Tick 缓存

补录指定日期：
    python3 scripts/daily_update.py --date 20260506

强制运行（调试用，跳过交易日检查）：
    python3 scripts/daily_update.py --force
"""

import argparse
import logging
import struct
import sys
import zipfile
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

from stockdb.config import Config
from stockdb.db import MetaDB
from stockdb.market import INDEX_MAP, market_to_tdx
from stockdb.reader import StockDB, _bars_to_df, _append_parquet
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

TDX_ZIP_URLS = {
    "sh": "https://www.tdx.com.cn/products/data/data/vipdoc/shlday.zip",
    "sz": "https://www.tdx.com.cn/products/data/data/vipdoc/szlday.zip",
    "bj": "https://www.tdx.com.cn/products/data/data/vipdoc/bjlday.zip",
}


# ── 交易日判断 ────────────────────────────────────────

def check_trade_day(meta: MetaDB, date_str: str, force: bool = False) -> bool:
    """自动判断是否为交易日。优先级：force > SQLite > akshare > 工作日兜底"""
    if force:
        logger.info("%s 强制模式，跳过交易日检查", date_str)
        return True
    if meta.is_trade_day(date_str):
        return True
    try:
        import akshare as ak
        df = ak.tool_trade_date_hist_sina()
        dates = set(df.iloc[:, 0].astype(str).str.replace("-", ""))
        if date_str in dates:
            meta.upsert_calendar([date_str], is_open=1)
            return True
        else:
            logger.info("%s 不在 akshare 交易日历中，跳过更新", date_str)
            return False
    except Exception as e:
        logger.warning("akshare 日历查询失败（%s），使用工作日兜底", e)

    dt = datetime.strptime(date_str, "%Y%m%d")
    if dt.weekday() < 5:
        last = meta.last_trade_day()
        if last and abs((dt - datetime.strptime(last, "%Y%m%d")).days) <= 7:
            logger.warning("akshare 不可用，%s 是工作日且日历连续，假定为交易日", date_str)
            return True

    logger.info("%s 判断为非交易日，跳过（调试用：加 --force 强制运行）", date_str)
    return False


# ── ZIP 缓存管理 ──────────────────────────────────────

def _zip_cache_path(cfg: Config, market: str) -> Path:
    cache_dir = cfg.data_dir / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"{market}lday.zip"


def _zip_is_fresh(path: Path, max_age_hours: int = 20) -> bool:
    """ZIP 是否在当天内下载（20小时内视为有效）"""
    if not path.exists():
        return False
    age_hours = (datetime.now().timestamp() - path.stat().st_mtime) / 3600
    return age_hours < max_age_hours


def _try_download_zip(cfg: Config, market: str) -> bool:
    """尝试下载单个市场的 ZIP，成功返回 True"""
    url = TDX_ZIP_URLS[market]
    cache_path = _zip_cache_path(cfg, market)
    try:
        logger.info("[%s] 下载 %s ...", market.upper(), url)
        resp = requests.get(url, timeout=600, stream=True)
        resp.raise_for_status()
        cache_path.write_bytes(resp.content)
        logger.info("[%s] ✅ 下载完成 %.1f MB", market.upper(),
                    cache_path.stat().st_size / 1024 / 1024)
        return True
    except Exception as e:
        logger.warning("[%s] 下载失败: %s", market.upper(), e)
        return False


# ── .day 文件解析 ─────────────────────────────────────

def parse_day_bytes(data: bytes) -> pd.DataFrame:
    """解析通达信 .day 二进制文件（每条记录 32 字节）"""
    record_size = 32
    n = len(data) // record_size
    if n == 0:
        return pd.DataFrame()
    records = []
    for i in range(n):
        chunk = data[i * record_size:(i + 1) * record_size]
        date_int, open_, high, low, close, amount, vol, _ = struct.unpack("<IIIIIfII", chunk)
        if date_int == 0:
            continue
        records.append({
            "date":   pd.to_datetime(str(date_int), format="%Y%m%d", errors="coerce"),
            "open":   round(open_  / 100.0, 2),
            "high":   round(high   / 100.0, 2),
            "low":    round(low    / 100.0, 2),
            "close":  round(close  / 100.0, 2),
            "amount": round(amount, 2),
            "vol":    vol,
        })
    df = pd.DataFrame(records).dropna(subset=["date"])
    return df.sort_values("date").reset_index(drop=True)


# ── 全市场日线更新（整包方式）────────────────────────

def update_daily_all(cfg: Config, date_str: str):
    """
    全自动流程：
      1. 当天缓存有效 → 直接处理，无需下载
      2. 缓存过期     → 下载新 ZIP 再处理
      3. 下载失败     → 用旧缓存降级（并提示）
      4. 无任何缓存   → 抛出异常
    """
    cutoff_date = f"{datetime.now().year - cfg.history_years}0101"

    for market in TDX_ZIP_URLS:
        cache_path = _zip_cache_path(cfg, market)

        if _zip_is_fresh(cache_path):
            logger.info("[%s] 使用当天缓存 ZIP (%.1f MB)",
                        market.upper(), cache_path.stat().st_size / 1024 / 1024)
        else:
            # 尝试下载
            ok = _try_download_zip(cfg, market)
            if not ok:
                if cache_path.exists():
                    age_h = (datetime.now().timestamp() - cache_path.stat().st_mtime) / 3600
                    logger.warning("[%s] 下载失败，使用 %.0fh 前的旧缓存（数据可能不是最新）",
                                   market.upper(), age_h)
                    logger.warning("    建议关闭代理后重新运行: python3 scripts/daily_update.py")
                else:
                    raise RuntimeError(
                        f"[{market}] 无缓存且下载失败。\n"
                        "请关闭代理后运行: python3 scripts/init_full.py"
                    )

        # 解析 ZIP → 更新 Parquet
        daily_dir = cfg.data_dir / "daily" / market
        daily_dir.mkdir(parents=True, exist_ok=True)
        updated = 0

        with zipfile.ZipFile(cache_path) as zf:
            day_files = [n for n in zf.namelist() if n.lower().endswith(".day")]
            logger.info("[%s] 解析 %d 个 .day 文件 ...", market.upper(), len(day_files))
            for name in day_files:
                stem = Path(name).stem.lower()
                code = stem.replace("sh", "").replace("sz", "").replace("bj", "").zfill(6)
                parquet_path = daily_dir / f"{code}.parquet"
                try:
                    raw = zf.read(name)
                    df = parse_day_bytes(raw)
                    if df.empty:
                        continue
                    df = df[df["date"].dt.strftime("%Y%m%d") >= cutoff_date]
                    if df.empty:
                        continue
                    # 只覆写今天有新数据的文件
                    if df["date"].max().strftime("%Y%m%d") >= date_str:
                        df.to_parquet(parquet_path, index=False)
                        updated += 1
                except Exception as e:
                    logger.debug("Skip %s: %s", name, e)

        logger.info("[%s] 更新完成: %d 只", market.upper(), updated)


# ── 指数日线（pytdx）────────────────────────────────

def update_indices(cfg: Config, date_str: str):
    logger.info("更新指数日线 ...")
    for code, (market_str, name) in INDEX_MAP.items():
        path = cfg.index_path(code)
        market_int = 1 if market_str == "sh" else 0
        try:
            with tdx_connect(cfg.servers) as api:
                data = api.get_security_bars(9, market_int, code, 0, 10)
            if not data:
                logger.warning("Index %s (%s): pytdx 返回空", code, name)
                continue
            df = _bars_to_df(data, freq="daily")
            if df.empty:
                continue
            df = df[df["date"].dt.strftime("%Y%m%d") >= date_str]
            if not df.empty:
                _append_parquet(path, df, dedup_col="date")
                logger.info("Index %s (%s) ✅", code, name)
        except Exception as e:
            logger.warning("Index %s update failed: %s", code, e)


# ── 主程序 ───────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="stockdb 每日更新 — 每个交易日收盘后，关闭代理运行一次即可"
    )
    parser.add_argument("--date", type=str, default=None,
                        help="补录指定日期（格式 YYYYMMDD），默认今天")
    parser.add_argument("--force", action="store_true",
                        help="强制运行（跳过交易日检查，调试用）")
    args = parser.parse_args()

    now = datetime.now()
    date_str = args.date if args.date else now.strftime("%Y%m%d")

    cfg = Config()
    meta = MetaDB(cfg.db_path)

    logger.info("=" * 55)
    logger.info("stockdb 每日更新 — %s", date_str)
    logger.info("=" * 55)

    # 1. 判断交易日
    if not check_trade_day(meta, date_str, force=args.force):
        meta.log_update(date_str, "skip", "非交易日")
        return

    if now.hour < 16 and not args.force:
        logger.warning("当前时间 %s < 16:00，数据可能不完整", now.strftime("%H:%M"))

    try:
        db = StockDB()

        # 2. 全市场日线（整包，全自动）
        update_daily_all(cfg, date_str)

        # 3. 指数日线（pytdx，7只）
        update_indices(cfg, date_str)

        # 4. 交易日历
        meta.upsert_calendar([date_str], is_open=1)
        logger.info("交易日历已更新: %s", date_str)

        # 5. 清理过期 Tick
        db.clean_old_ticks()

        meta.log_update(date_str, "ok", "全量更新成功")
        logger.info("=" * 55)
        logger.info("✅ 每日更新完成！")
        logger.info("=" * 55)

    except Exception as e:
        logger.error("每日更新异常: %s", e, exc_info=True)
        meta.log_update(date_str, "error", str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
