"""
init_full.py — 全量初始化

步骤：
  1. 下载通达信整包 ZIP（shlday.zip / szlday.zip / bjlday.zip）
  2. 解析 .day 二进制文件 → 写 Parquet
  3. 拉取股票列表 → 写 SQLite
  4. 拉取交易日历 → 写 SQLite
  5. 拉取主要指数日线 → 写 Parquet

首次运行约需 15~40 分钟（取决于网速）。
"""

import logging
import struct
import sys
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path

import pandas as pd
import requests
from tqdm import tqdm

# 把上级目录加入 path，方便直接运行此脚本
sys.path.insert(0, str(Path(__file__).parent.parent))

from stockdb.config import Config
from stockdb.db import MetaDB
from stockdb.market import detect_market, detect_board, market_to_tdx, INDEX_MAP
from stockdb.tdx_client import tdx_connect, fetch_bars, fetch_security_list

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("init_full")

# ── 通达信整包 URL ────────────────────────────────────

TDX_ZIP_URLS = {
    "sh": "https://www.tdx.com.cn/products/data/data/vipdoc/shlday.zip",
    "sz": "https://www.tdx.com.cn/products/data/data/vipdoc/szlday.zip",
    "bj": "https://www.tdx.com.cn/products/data/data/vipdoc/bjlday.zip",
}


# ── .day 文件解析 ─────────────────────────────────────

def parse_day_bytes(data: bytes) -> pd.DataFrame:
    """
    解析通达信 .day 二进制文件。
    每条记录 32 字节：date(I) open(I) high(I) low(I) close(I) amount(f) vol(I) reserved(I)
    价格字段需 ÷ 100 还原为元。
    """
    record_size = 32
    n = len(data) // record_size
    if n == 0:
        return pd.DataFrame()

    records = []
    for i in range(n):
        chunk = data[i * record_size: (i + 1) * record_size]
        date_int, open_, high, low, close, amount, vol, _ = struct.unpack("<IIIIIfII", chunk)
        if date_int == 0:
            continue
        records.append({
            "date": pd.to_datetime(str(date_int), format="%Y%m%d", errors="coerce"),
            "open":   round(open_  / 100.0, 2),
            "high":   round(high   / 100.0, 2),
            "low":    round(low    / 100.0, 2),
            "close":  round(close  / 100.0, 2),
            "amount": round(amount, 2),
            "vol":    vol,
        })

    df = pd.DataFrame(records).dropna(subset=["date"])
    return df.sort_values("date").reset_index(drop=True)


# ── 下载 + 解析整包 ───────────────────────────────────

def download_zip(url: str) -> bytes:
    logger.info("Downloading %s ...", url)
    # proxies={} 强制绕过系统代理
    resp = requests.get(url, timeout=300, stream=True, proxies={})
    resp.raise_for_status()
    total = int(resp.headers.get("content-length", 0))
    buf = BytesIO()
    with tqdm(total=total, unit="B", unit_scale=True, desc=url.split("/")[-1]) as pbar:
        for chunk in resp.iter_content(chunk_size=65536):
            buf.write(chunk)
            pbar.update(len(chunk))
    return buf.getvalue()


def process_zip(zip_bytes: bytes, market: str, data_dir: Path, cutoff_date: str):
    """解析 ZIP 内所有 .day 文件，写入 Parquet"""
    daily_dir = data_dir / "daily" / market
    daily_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
        day_files = [n for n in zf.namelist() if n.lower().endswith(".day")]
        logger.info("[%s] ZIP contains %d .day files", market, len(day_files))

        for name in tqdm(day_files, desc=f"Parsing {market}"):
            stem = Path(name).stem.lower()
            code = stem.replace("sh", "").replace("sz", "").replace("bj", "").zfill(6)
            parquet_path = daily_dir / f"{code}.parquet"

            # 已存在的跳过（断点续传）
            if parquet_path.exists():
                continue

            try:
                raw = zf.read(name)
                df = parse_day_bytes(raw)
                if df.empty:
                    continue
                df = df[df["date"].dt.strftime("%Y%m%d") >= cutoff_date]
                if df.empty:
                    continue
                df.to_parquet(parquet_path, index=False)
            except Exception as e:
                logger.warning("Skip %s: %s", name, e)

    logger.info("[%s] Done writing Parquet files", market)


# ── 股票列表 ─────────────────────────────────────────

def _fetch_stock_list_akshare() -> list:
    """用 akshare 拉取全市场股票列表（主源，数据更全、板块更准）"""
    import akshare as ak
    stocks = []

    # 沪深主板 + 科创板 + 创业板（一次调用返回全部 A 股）
    df = ak.stock_info_a_code_name()
    for _, row in df.iterrows():
        code = str(row["code"]).strip().zfill(6)
        name = str(row.get("name", "")).strip()
        market = "sh" if code.startswith(("6", "5", "688")) else "sz"
        stocks.append({
            "code":   code,
            "name":   name,
            "market": market,
            "board":  detect_board(code),
        })
    logger.info("[akshare] %d stocks (SH+SZ+科创+创业)", len(stocks))

    # 北交所单独拉
    try:
        bj_df = ak.stock_info_bj_name_code()
        for _, row in bj_df.iterrows():
            code = str(row.get("证券代码", row.get("code", ""))).strip().zfill(6)
            name = str(row.get("证券简称", row.get("name", ""))).strip()
            stocks.append({
                "code":   code,
                "name":   name,
                "market": "bj",
                "board":  detect_board(code, market="bj"),  # 传入 market 兜底
            })
        logger.info("[akshare] 北交所 %d stocks", len(bj_df))
    except Exception as e:
        logger.warning("北交所列表拉取失败: %s", e)

    return stocks


def _fetch_stock_list_pytdx(cfg: Config) -> list:
    """pytdx 兜底方式（部分服务器 get_security_list 可能返回空）"""
    stocks = []
    with tdx_connect(cfg.servers) as api:
        for market_int, market_str in [(1, "sh"), (0, "sz")]:
            raw = fetch_security_list(api, market_int)
            for s in raw:
                code = str(s.get("code", "")).zfill(6)
                stocks.append({
                    "code":   code,
                    "name":   s.get("name", ""),
                    "market": market_str,
                    "board":  detect_board(code),
                })
            logger.info("[pytdx/%s] %d stocks", market_str, len(raw))
    return stocks


def init_stock_list(cfg: Config, meta: MetaDB):
    """优先用 akshare 拉股票列表，失败时用 pytdx 兜底"""
    stocks = []

    try:
        logger.info("Fetching stock list from akshare ...")
        stocks = _fetch_stock_list_akshare()
    except Exception as e:
        logger.warning("akshare 股票列表失败: %s，切换到 pytdx ...", e)

    # akshare 失败或返回数量异常时用 pytdx
    if len(stocks) < 100:
        logger.info("Fetching stock list from pytdx (fallback) ...")
        try:
            stocks = _fetch_stock_list_pytdx(cfg)
        except Exception as e:
            logger.error("pytdx 股票列表也失败: %s", e)

    if not stocks:
        logger.error("股票列表获取失败，跳过写入")
        return

    meta.upsert_stocks(stocks)
    logger.info("Stock list: %d total", len(stocks))


# ── 交易日历 ─────────────────────────────────────────

def init_trade_calendar(meta: MetaDB):
    """从 akshare 获取交易日历"""
    logger.info("Fetching trade calendar from akshare ...")
    try:
        import akshare as ak
        df = ak.tool_trade_date_hist_sina()
        dates = df.iloc[:, 0].astype(str).str.replace("-", "").tolist()
        meta.upsert_calendar(dates, is_open=1)
        logger.info("Trade calendar: %d days", len(dates))
    except Exception as e:
        logger.warning("akshare calendar failed: %s. Deriving from data...", e)
        # 兜底：从现有 Parquet 数据中推导
        _derive_calendar_from_data(meta)


def _derive_calendar_from_data(meta: MetaDB):
    """从已有日线 Parquet 推导交易日历"""
    sample_files = list((Path(__file__).parent.parent / "data" / "daily" / "sh").glob("*.parquet"))
    if not sample_files:
        return
    df = pd.read_parquet(sample_files[0])
    if "date" in df.columns:
        dates = df["date"].dt.strftime("%Y%m%d").tolist()
        meta.upsert_calendar(dates)
        logger.info("Derived %d trade days from local data", len(dates))


# ── 指数日线 ─────────────────────────────────────────

def init_indices(cfg: Config):
    logger.info("Fetching index daily bars ...")
    from stockdb.reader import _bars_to_df

    for code, (market_str, name) in INDEX_MAP.items():
        path = cfg.index_path(code)
        if path.exists():
            logger.info("Index %s (%s) already cached, skip.", code, name)
            continue
        market_int = 1 if market_str == "sh" else 0
        try:
            with tdx_connect(cfg.servers) as api:
                data = fetch_bars(api, code, market_int, frequency=9)
            df = _bars_to_df(data, freq="daily")
            if not df.empty:
                path.parent.mkdir(parents=True, exist_ok=True)
                df.to_parquet(path, index=False)
                logger.info("Index %s (%s): %d rows", code, name, len(df))
        except Exception as e:
            logger.error("Index %s failed: %s", code, e)


# ── 主程序 ───────────────────────────────────────────

def main():
    from datetime import datetime
    from dateutil.relativedelta import relativedelta

    cfg = Config()
    cfg.ensure_dirs()
    meta = MetaDB(cfg.db_path)

    # 计算历史截止日期
    cutoff = (datetime.now() - relativedelta(years=cfg.history_years)).strftime("%Y%m%d")
    logger.info("=" * 60)
    logger.info("stockdb 全量初始化")
    logger.info("数据根目录: %s", cfg.data_dir)
    logger.info("历史起始:   %s", cutoff)
    logger.info("=" * 60)

    # Step 1: 下载整包日线（已有数据则跳过 ZIP 下载）
    for market, url in TDX_ZIP_URLS.items():
        daily_dir = cfg.data_dir / "daily" / market
        existing_count = len(list(daily_dir.glob("*.parquet"))) if daily_dir.exists() else 0
        if existing_count > 100:
            logger.info("[%s] 已有 %d 个 Parquet 文件，跳过 ZIP 下载", market, existing_count)
            continue
        try:
            zip_bytes = download_zip(url)
            process_zip(zip_bytes, market, cfg.data_dir, cutoff)
        except Exception as e:
            logger.error("[%s] ZIP download/parse failed: %s", market, e)

    # Step 2: 股票列表
    try:
        init_stock_list(cfg, meta)
        try:
            from scripts.update_industries import update_industries
            logger.info("Initializing stock industries from Sina...")
            update_industries()
        except Exception as e:
            logger.error("Stock industries init failed: %s", e)
    except Exception as e:
        logger.error("Stock list init failed: %s", e)

    # Step 3: 交易日历
    try:
        init_trade_calendar(meta)
    except Exception as e:
        logger.error("Trade calendar init failed: %s", e)

    # Step 4: 指数日线
    try:
        init_indices(cfg)
    except Exception as e:
        logger.error("Index init failed: %s", e)

    # Step 5: 股票流通股本
    try:
        from stockdb import StockDB
        db = StockDB()
        logger.info("Syncing stock shares outstanding...")
        db.update_stock_shares()
    except Exception as e:
        logger.error("Stock shares sync failed: %s", e)

    logger.info("=" * 60)
    logger.info("✅ 初始化完成！运行 daily_update.py 可补充今日数据。")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
