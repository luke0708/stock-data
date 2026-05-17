"""
批量预计算全市场筹码分布缓存。

首次运行约 5~15 分钟（全市场 ~5000 只），后续按需增量刷新。

用法：
    python3 scripts/build_chip.py                    # 全市场 A 股（跳过已缓存）
    python3 scripts/build_chip.py --code 300661      # 单只股票
    python3 scripts/build_chip.py --market sz        # 单市场
    python3 scripts/build_chip.py --recalc           # 强制重算所有（覆盖旧缓存）
    python3 scripts/build_chip.py --workers 8        # 并行数（默认 4）
"""

import argparse
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from stockdb import StockDB

# A 股主板/创业板/科创板/北交所常见前缀正则
A_SHARE_RE = re.compile(
    r'^(000|001|002|003|300|301|600|601|603|605|688|430|831|832|833|834|835|836|837|838|839|870|871|872|873|874|875|876|877|878|879|880|881|882|883|884|885|886|887|888|889)\d{3}$'
)


def _process_one(code: str, db: StockDB, recalc: bool) -> tuple[str, str]:
    """处理单只股票，返回 (code, status)"""
    try:
        df = db.chip(code, recalc=recalc)
        if df.empty:
            return code, "empty"
        return code, "ok"
    except Exception as e:
        return code, f"err:{e}"


def main():
    parser = argparse.ArgumentParser(description="批量预计算 A 股筹码分布")
    parser.add_argument("--code",    default=None, help="单只股票代码")
    parser.add_argument("--market",  default=None, choices=["sh", "sz", "bj"],
                        help="单市场（sh/sz/bj）")
    parser.add_argument("--recalc",  action="store_true",
                        help="强制重算（覆盖已有缓存）")
    parser.add_argument("--workers", type=int, default=4,
                        help="并行线程数（默认 4，pytdx 是 IO 密集型）")
    args = parser.parse_args()

    db  = StockDB()
    cfg = db.cfg

    # ── 确定待处理代码列表 ──────────────────────────
    if args.code:
        codes = [args.code]
    else:
        markets = [args.market] if args.market else ["sh", "sz", "bj"]
        codes = []
        for m in markets:
            d = cfg.data_dir / "daily" / m
            if not d.exists():
                continue
            codes += [
                f.stem for f in d.glob("*.parquet")
                if A_SHARE_RE.match(f.stem)
            ]
        codes = sorted(set(codes))

    # 跳过已缓存（非强制重算模式）
    if not args.recalc:
        chip_dir = cfg.data_dir / "chip"
        pending  = [c for c in codes if not (chip_dir / f"{c}.parquet").exists()]
        skip_cnt = len(codes) - len(pending)
        codes    = pending
    else:
        skip_cnt = 0

    total = len(codes)
    print(f"筹码分布预计算")
    print(f"  待处理: {total} 只  |  已缓存跳过: {skip_cnt} 只  |  并行: {args.workers} 线程")
    if total == 0:
        print("全部已完成，无需重算。如需强制更新，使用 --recalc 参数。")
        return

    # ── 并行计算 ────────────────────────────────────
    ok = fail = empty = 0
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(_process_one, c, db, args.recalc): c for c in codes}
        done = 0
        for future in as_completed(futures):
            code, status = future.result()
            done += 1
            if status == "ok":
                ok += 1
            elif status == "empty":
                empty += 1
            else:
                fail += 1

            if done % 100 == 0 or done == total:
                elapsed = time.time() - t0
                rate    = done / elapsed if elapsed > 0 else 0
                eta     = (total - done) / rate if rate > 0 else 0
                print(
                    f"  [{done:>5}/{total}] "
                    f"✓{ok} ✗{fail} 空{empty}  "
                    f"速度 {rate:.1f}/s  剩余 {eta:.0f}s"
                )

    elapsed = time.time() - t0
    print(f"\n完成！耗时 {elapsed:.1f}s")
    print(f"  成功: {ok}  |  空数据: {empty}  |  失败: {fail}  |  跳过: {skip_cnt}")


if __name__ == "__main__":
    main()
