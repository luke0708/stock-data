#!/usr/bin/env python3
"""
美股数据更新脚本（独立入口，与 daily_update.py 完全隔离）

功能：
  - 初始化 + 增量合一（小样本下足够）
  - 幂等：重复运行只追加/覆盖，不产生重复数据
  - 更新内容：日线 Parquet、us_stocks、us_splits、us_financials、us_calendar

用法：
  python scripts/us_update.py              # 更新 config.yaml 里的 us.watchlist
  python scripts/us_update.py --tickers AXTI NIVF AAPL   # 指定标的
  python scripts/us_update.py --force-refresh             # 强制重拉日线
"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

# 使当前工作目录不在 PATH 时也能找到项目包
sys.path.insert(0, str(Path(__file__).parent.parent))

from stockdb.config import Config
from stockdb.us import USStockDB
from stockdb.us.calendar import fetch_us_calendar

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("us_update")

# 美股收盘时间判断（美东时间 16:00，UTC-4/UTC-5，此处用简单 UTC 偏移估算）
# 精确时区处理需 pip install pytz；这里用保守的 21:00 UTC 作为"今日数据完整"阈值
_US_CLOSE_UTC_HOUR = 21


def _today_data_complete() -> bool:
    """判断今天的美股数据是否已完整（美东 16:00 = UTC 20:00~21:00）"""
    return datetime.utcnow().hour >= _US_CLOSE_UTC_HOUR


def update_ticker(db: USStockDB, ticker: str, force_refresh: bool = False):
    """拉取并更新单只美股的所有数据"""
    logger.info("── 处理 %s ──", ticker)

    # 1. 日线（本地优先，miss 或 force 时重拉）
    df = db.daily(ticker, force_refresh=force_refresh)
    if df.empty:
        logger.warning("%s 日线数据为空，跳过后续步骤", ticker)
        return
    logger.info("%s 日线: %d 行，最新日期 %s", ticker, len(df), df["date"].max())

    # 2. meta（name, exchange, sector 等）→ us_stocks 表
    meta = db.meta(ticker)
    if meta:
        logger.info("%s meta: %s / %s", ticker, meta.get("name"), meta.get("exchange"))
    else:
        logger.warning("%s meta 获取失败", ticker)

    # 3. 拆股记录（含缩股检测）→ us_splits 表
    splits = db.splits(ticker, lookback_months=24)
    reverse = [(d, f) for d, f in splits if f < 1]
    if reverse:
        logger.warning("%s 近 24 个月检测到 %d 次反向拆股（缩股）: %s", ticker, len(reverse), reverse)
    else:
        logger.info("%s 近 24 个月无缩股记录", ticker)

    # 4. 前复权峰值比（NIVF 指纹）
    ratio = db.adj_peak_ratio(ticker)
    if ratio is not None:
        flag = " ⚠️ 疑似反复缩股垃圾" if ratio > 100 else ""
        logger.info("%s 前复权峰值/现价 = %.1f%s", ticker, ratio, flag)

    # 5. 营收同比 → us_financials 表
    rev = db.revenue_yoy(ticker)
    if rev:
        yoy_pct = f"{rev['yoy_growth'] * 100:.1f}%" if rev.get("yoy_growth") is not None else "N/A"
        logger.info("%s 营收同比: %s", ticker, yoy_pct)
    else:
        logger.warning("%s 营收数据获取失败（可能是无季报数据的标的）", ticker)


def main():
    parser = argparse.ArgumentParser(description="美股数据更新（独立于 A 股模块）")
    parser.add_argument(
        "--tickers", nargs="+", metavar="TICKER",
        help="指定标的（不指定则用 config.yaml 的 us.watchlist）",
    )
    parser.add_argument(
        "--force-refresh", action="store_true",
        help="强制重拉日线（忽略本地缓存）",
    )
    parser.add_argument(
        "--config", default=None,
        help="config.yaml 路径（默认自动查找）",
    )
    args = parser.parse_args()

    cfg = Config(args.config)
    if not cfg.us_enabled:
        logger.error("config.yaml 中 us.enabled=false，退出。若要启用请设为 true。")
        sys.exit(1)

    db = USStockDB(config_path=args.config)
    tickers = [t.upper() for t in args.tickers] if args.tickers else db.universe()

    if not tickers:
        logger.error("没有要处理的标的。请在 config.yaml 的 us.watchlist 中添加，或用 --tickers 指定。")
        sys.exit(1)

    logger.info("开始美股数据更新，标的: %s，force_refresh=%s", tickers, args.force_refresh)
    logger.info("今日数据完整: %s（美东 16:00 后）", _today_data_complete())

    # 刷新美股交易日历
    logger.info("刷新美股交易日历（^GSPC）...")
    calendar_dates = fetch_us_calendar(db._provider, history_years=cfg.us_history_years)
    if calendar_dates:
        db._meta.upsert_calendar(calendar_dates)
        logger.info("交易日历更新完成，共 %d 个交易日", len(calendar_dates))
    else:
        logger.warning("交易日历更新失败，继续处理行情数据")

    # 逐只更新
    ok, failed = [], []
    for ticker in tickers:
        try:
            update_ticker(db, ticker, force_refresh=args.force_refresh)
            ok.append(ticker)
        except Exception as e:
            logger.error("处理 %s 时发生异常: %s", ticker, e, exc_info=True)
            failed.append(ticker)

    logger.info("完成。成功: %s，失败: %s", ok, failed)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
