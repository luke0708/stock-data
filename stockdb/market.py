"""市场工具函数：代码识别、市场编号转换等"""

from typing import Dict, Tuple

# 板块名称常量
BOARD_MAIN    = "主板"    # 上交所主板 6xxxxx / 深交所主板 0xxxxx
BOARD_STAR    = "科创板"  # 688xxx
BOARD_GEM     = "创业板"  # 300xxx / 301xxx
BOARD_SME     = "中小板"  # 002xxx（历史遗留，现已并入主板）
BOARD_BSE     = "北交所"  # 4xxxxx / 8xxxxx


def detect_market(code: str) -> str:
    """根据代码推断市场（sh / sz / bj）"""
    code = str(code).strip().zfill(6)
    if code.startswith(("6", "5", "11", "51", "58")):
        return "sh"
    elif code.startswith(("4", "8", "87", "88", "43", "83")):
        return "bj"
    else:
        return "sz"


def detect_board(code: str, market: str = None) -> str:
    """
    根据股票代码前缀推断所属板块。

    规则（按优先级）：
      688xxx          → 科创板
      300xxx / 301xxx → 创业板
      002xxx / 003xxx → 中小板（历史，现并入主板）
      920xxx          → 北交所（2024年北交所新代码段）
      4xxxxx / 8xxxxx → 北交所（旧代码段）
      market='bj'     → 北交所（兜底，akshare 已标注市场）
      其余            → 主板
    """
    code = str(code).strip().zfill(6)
    if code.startswith("688"):
        return BOARD_STAR
    elif code.startswith(("300", "301")):
        return BOARD_GEM
    elif code.startswith(("002", "003")):
        return BOARD_SME
    elif code.startswith("920") or code.startswith(("4", "8")):
        return BOARD_BSE
    elif market and str(market).lower() == "bj":
        return BOARD_BSE
    else:
        return BOARD_MAIN


def market_to_tdx(market: str) -> int:
    """市场字符串 → pytdx market int（SH=1, SZ=0）"""
    return {"sh": 1, "sz": 0, "bj": 0}.get(market.lower(), 0)


def code_to_tdx_market(code: str) -> int:
    """股票代码 → pytdx market int"""
    return market_to_tdx(detect_market(code))


def normalize_code(code: str) -> str:
    """代码补零至6位"""
    return str(code).strip().zfill(6)


# 常用指数及其市场归属
INDEX_MAP: Dict[str, Tuple[str, str]] = {
    "000001": ("sh", "上证指数"),
    "000300": ("sh", "沪深300"),
    "000016": ("sh", "上证50"),
    "000905": ("sh", "中证500"),
    "000852": ("sh", "中证1000"),
    "399001": ("sz", "深证成指"),
    "399006": ("sz", "创业板指"),
    "399005": ("sz", "中小板指"),
}


def is_index(code: str) -> bool:
    return normalize_code(code) in INDEX_MAP
