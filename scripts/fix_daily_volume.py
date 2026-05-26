"""
fix_daily_volume.py — 修复由于增量更新把成交量计为“手”而产生的脏数据。
遍历 data/daily/{sh,sz,bj} 下的所有个股 Parquet 文件，
对于满足 vol * close * 10 < amount 且 vol > 0 的行，
把其 vol 乘以 100 还原为“股”，写回 Parquet 文件。
"""
import logging
from pathlib import Path
import pandas as pd
from tqdm import tqdm

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("fix_daily_volume")

def main():
    data_dir = Path(__file__).parent.parent / "data" / "daily"
    if not data_dir.exists():
        logger.error("Data directory %s does not exist", data_dir)
        return

    # 扫描所有 parquet 文件
    files = list(data_dir.rglob("*.parquet"))
    logger.info("Scanning %d Parquet files...", len(files))

    fixed_files_count = 0
    fixed_rows_count = 0

    for f in tqdm(files, desc="Fixing files"):
        try:
            df = pd.read_parquet(f)
            if df.empty or "vol" not in df.columns or "close" not in df.columns or "amount" not in df.columns:
                continue

            # 找到需要乘以 100 的行：
            # 1. vol * close * 10 < amount（大约相差 100 倍）
            # 2. 避免零值干扰
            mask = (df["vol"] > 0) & (df["close"] > 0) & (df["amount"] > 0) & (df["vol"] * df["close"] * 10.0 < df["amount"])
            
            if mask.any():
                df.loc[mask, "vol"] = df.loc[mask, "vol"] * 100
                df.to_parquet(f, index=False)
                fixed_files_count += 1
                fixed_rows_count += mask.sum()
        except Exception as e:
            logger.warning("Error processing %s: %s", f, e)

    logger.info("Fix complete! Fixed %d rows in %d files.", fixed_rows_count, fixed_files_count)

if __name__ == "__main__":
    main()
