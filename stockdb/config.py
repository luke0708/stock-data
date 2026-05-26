"""配置加载：读取 config.yaml，提供路径计算方法"""

from pathlib import Path
from typing import List, Tuple

import yaml

_DEFAULT = {
    "data_dir": "./data",
    "db_path": "./db/meta.db",
    "history_years": 5,
    "tick": {"cache_mode": "daily", "keep_days": 30},
    "servers": [
        ["180.153.18.170", 7709],
        ["119.147.212.81", 7709],
        ["124.74.236.50", 7709],
        ["218.75.126.89", 7709],
        ["125.39.80.41", 7709],
    ],
    "index_codes": ["000001", "000300", "000016", "399001", "399006", "000905", "000852"],
    "log_dir": "./logs",
}


class Config:
    """加载 config.yaml，计算各类数据的存储路径"""

    def __init__(self, config_path: str = None):
        # 找到 stock-data 根目录
        if config_path:
            self.root = Path(config_path).parent.resolve()
        else:
            # 从 stockdb/ 往上一级即为根目录
            self.root = Path(__file__).parent.parent.resolve()

        self._cfg = dict(_DEFAULT)
        cfg_file = Path(config_path) if config_path else self.root / "config.yaml"
        if cfg_file.exists():
            with open(cfg_file, encoding="utf-8") as f:
                user = yaml.safe_load(f) or {}
            self._cfg.update(user)
            if "tick" in user:
                self._cfg["tick"] = {**_DEFAULT["tick"], **user["tick"]}

    # ── 路径属性 ─────────────────────────────────────

    @property
    def data_dir(self) -> Path:
        return (self.root / self._cfg["data_dir"]).resolve()

    @property
    def db_path(self) -> Path:
        return (self.root / self._cfg["db_path"]).resolve()

    @property
    def log_dir(self) -> Path:
        return (self.root / self._cfg["log_dir"]).resolve()

    # ── 参数属性 ─────────────────────────────────────

    @property
    def servers(self) -> List[Tuple[str, int]]:
        return [tuple(s) for s in self._cfg["servers"]]

    @property
    def history_years(self) -> int:
        return int(self._cfg.get("history_years", 5))

    @property
    def tick_cache_mode(self) -> str:
        return self._cfg["tick"].get("cache_mode", "daily")

    @property
    def tick_keep_days(self) -> int:
        return int(self._cfg["tick"].get("keep_days", 30))

    @property
    def index_codes(self) -> List[str]:
        return self._cfg.get("index_codes", [])

    @property
    def watchlist(self) -> List[str]:
        return self._cfg.get("watchlist", [])

    # ── 路径计算 ─────────────────────────────────────

    def daily_path(self, code: str) -> Path:
        from .market import detect_market
        market = detect_market(code)
        return self.data_dir / "daily" / market / f"{code}.parquet"

    def minutes_path(self, code: str, date_str: str) -> Path:
        from .market import detect_market
        market = detect_market(code)
        return self.data_dir / "minutes" / market / code / f"{date_str}.parquet"

    def tick_path(self, code: str, date_str: str) -> Path:
        from .market import detect_market
        market = detect_market(code)
        return self.data_dir / "tick" / market / code / f"{date_str}.parquet"

    def index_path(self, code: str) -> Path:
        return self.data_dir / "index" / f"{code}.parquet"

    def ensure_dirs(self):
        """创建所有必要目录"""
        for sub in ("daily/sh", "daily/sz", "daily/bj",
                    "minutes", "tick", "index"):
            (self.data_dir / sub).mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
