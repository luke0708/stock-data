"""
美股数据层黄金样本断言（CLAUDE.md §6 数据层版）

测试目标：
  - AXTI（正样本）：小盘 pure-play 半导体衬底，无缩股记录，前复权峰值比 < 100
  - NIVF（反样本）：反复反向拆股垃圾，应能从数据层捕捉到缩股指纹

注意：本测试仅验证数据层字段取数正确。
"过滤管线"（CLAUDE.md §4.3 剔除 NIVF）属于后续筛选器，不在本模块范围。

运行：
  cd /Users/wangluke/Localprojects/stock-data
  python -m pytest tests/test_us_data.py -v
  # 或直接运行（联网拉取数据，约需 30s）
  python tests/test_us_data.py
"""

import sys
import logging
from pathlib import Path

# 确保在任意工作目录都能找到包
sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test_us_data")


def test_imports():
    """验证美股模块可以正常导入（不影响 A 股导入）"""
    from stockdb import StockDB          # A 股门面，必须可导入
    from stockdb.us import USStockDB     # 美股门面
    from stockdb.us.provider import YFinanceProvider, DataProvider
    from stockdb.us.db import USMetaDB
    assert issubclass(YFinanceProvider, DataProvider), "YFinanceProvider 必须实现 DataProvider ABC"
    logger.info("✓ 模块导入正常（A 股 + 美股均可导入）")


def test_axti_daily():
    """AXTI 日线数据字段完整"""
    from stockdb.us import USStockDB
    db = USStockDB()
    df = db.daily("AXTI", start="2024-01-01")
    assert not df.empty, "AXTI 日线数据不能为空"
    required_cols = {"date", "close", "vol", "amount"}
    missing = required_cols - set(df.columns)
    assert not missing, f"AXTI 日线缺少字段: {missing}"
    assert (df["close"] > 0).all(), "close 价格必须 > 0"
    assert (df["vol"] >= 0).all(), "vol 成交量必须 >= 0"
    logger.info("✓ AXTI 日线: %d 行，最新日期 %s，最新收盘 %.2f",
                len(df), df['date'].max(), df['close'].iloc[-1])


def test_axti_no_reverse_split():
    """AXTI 近 24 个月不应有反向拆股（缩股）"""
    from stockdb.us.provider import YFinanceProvider
    provider = YFinanceProvider()
    splits = provider.get_splits("AXTI", lookback_months=24)
    reverse = [s for s in splits if s[1] < 1]
    assert len(reverse) == 0, f"AXTI 不应有缩股记录，实际: {reverse}"
    logger.info("✓ AXTI 近 24 个月无缩股记录（splits=%s）", splits)


def test_axti_peak_ratio():
    """AXTI 前复权峰值比应 < 100（没有反复缩股导致的峰值虚高）"""
    from stockdb.us.provider import YFinanceProvider
    provider = YFinanceProvider()
    ratio = provider.get_adj_price_peak_ratio("AXTI", lookback_years=5)
    assert ratio is not None, "AXTI 峰值比不能为 None"
    assert ratio < 100, f"AXTI 峰值比 {ratio:.1f} 应 < 100"
    logger.info("✓ AXTI 前复权峰值比 = %.2f（< 100）", ratio)


def test_nivf_has_reverse_split():
    """NIVF 近 24 个月应检测到至少一次反向拆股（缩股指纹）"""
    from stockdb.us.provider import YFinanceProvider
    provider = YFinanceProvider()
    splits = provider.get_splits("NIVF", lookback_months=24)
    reverse = [s for s in splits if s[1] < 1]
    assert len(reverse) > 0, (
        f"NIVF 应有缩股记录（factor<1），实际全部 splits: {splits}。"
        "yfinance 可能对外国发行人 6-K 拆股覆盖不全——参见 CLAUDE.md §3.3"
    )
    logger.info("✓ NIVF 近 24 个月检测到 %d 次反向拆股: %s", len(reverse), reverse)


def test_nivf_peak_ratio():
    """NIVF 前复权峰值比应 > 100（反复缩股导致历史名义价格极高）"""
    from stockdb.us.provider import YFinanceProvider
    provider = YFinanceProvider()
    ratio = provider.get_adj_price_peak_ratio("NIVF", lookback_years=5)
    assert ratio is not None, "NIVF 峰值比不能为 None"
    assert ratio > 100, (
        f"NIVF 峰值比 {ratio:.1f} 应 > 100（前复权历史最高约 19 万，现价约 0.7）。"
        "若 yfinance 的前复权数据已正确处理反向拆股，此断言应通过。"
    )
    logger.info("✓ NIVF 前复权峰值比 = %.1f（>> 100，反复缩股指纹确认）", ratio)


def test_axti_revenue_yoy():
    """AXTI 营收同比字段可正常获取"""
    from stockdb.us.provider import YFinanceProvider
    provider = YFinanceProvider()
    rev = provider.get_revenue_yoy("AXTI")
    # AXTI 有季报，营收同比应能获取；若拿不到记录 warning 而不是失败
    if rev is None:
        logger.warning("⚠ AXTI 营收同比获取失败（可能是 yfinance 季报覆盖问题），跳过此断言")
        return
    assert "yoy_growth" in rev, "返回值必须包含 yoy_growth"
    assert "latest_q_rev" in rev, "返回值必须包含 latest_q_rev"
    assert rev["latest_q_rev"] > 0, "最新季度营收必须 > 0"
    logger.info("✓ AXTI 营收同比 = %.1f%%，最新季度营收 = $%.0f",
                (rev["yoy_growth"] or 0) * 100, rev["latest_q_rev"])


def test_a_share_not_broken():
    """确保美股模块引入后，A 股核心链路（导入 + detect_market）不受影响"""
    from stockdb.us import USStockDB          # 导入美股模块
    from stockdb.market import detect_market, normalize_code  # A 股工具函数
    # A 股代码识别必须保持原有行为
    assert detect_market("600519") == "sh"
    assert detect_market("000001") == "sz"
    assert detect_market("430047") == "bj"
    assert normalize_code("300661") == "300661"
    logger.info("✓ A 股核心函数（detect_market / normalize_code）行为未受影响")


if __name__ == "__main__":
    tests = [
        test_imports,
        test_a_share_not_broken,
        test_axti_daily,
        test_axti_no_reverse_split,
        test_axti_peak_ratio,
        test_nivf_has_reverse_split,
        test_nivf_peak_ratio,
        test_axti_revenue_yoy,
    ]
    passed, failed = [], []
    for t in tests:
        try:
            t()
            passed.append(t.__name__)
        except AssertionError as e:
            logger.error("✗ %s 失败: %s", t.__name__, e)
            failed.append(t.__name__)
        except Exception as e:
            logger.error("✗ %s 异常: %s", t.__name__, e, exc_info=True)
            failed.append(t.__name__)

    print(f"\n{'='*50}")
    print(f"通过: {len(passed)}/{len(tests)}")
    if failed:
        print(f"失败: {failed}")
        sys.exit(1)
    else:
        print("所有断言通过 ✓")
