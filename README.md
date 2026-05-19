# stockdb — A股本地行情数据库

> 为所有本地项目提供**统一、稳定、离线可用**的 A 股数据接口。

---

## 核心理念

```
所有项目永远只调用 stockdb 接口，不直接碰 akshare / pytdx / yfinance。
```

- **日线**：全市场 5200 只，5 年历史，本地 Parquet，毫秒级读取
- **分钟线**：写透缓存，首次拉取自动存盘，越用越快
- **Tick**：当日实时，可配置保留最近 N 天历史
- **每日自动补充**：每个交易日 16:30 后自动更新所有数据
- **多项目共享**：`pip install -e .` 一次，所有本地项目通用

---

## 快速开始

### 安装依赖

```bash
# 复用已有 venv（推荐）
source /path/to/your/project/.venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

### 首次初始化（全量历史日线，约 30~60 分钟）

**关闭代理**后运行：

```bash
python3 scripts/init_full.py
```

已有数据时自动跳过下载，只补充缺失部分。

### 每日更新 / 周末补数

```bash
source .venv/bin/activate && python3 scripts/daily_update.py
#python3 scripts/daily_update.py
```

- 工作日运行：补当天数据
- 周六/周日运行：自动回溯到周五，补上周数据
- 缺失太多时会提示 → 改跑 `init_full.py`

### 数据缺失过多 / 重新初始化

```bash
python3 scripts/init_full.py
```

下载完整 ZIP，一次性补全所有历史（无论缺失多少天）。


---

## 接口文档

```python
from stockdb import StockDB

db = StockDB()
```

| 方法 | 说明 | 示例 |
|---|---|---|
| `db.daily(code, start, end)` | 日线 K 线（全市场，本地 Parquet） | `db.daily('300661', start='2024-01-01')` |
| `db.minutes(code, date, days)` | 分钟线（懒缓存，自动存盘） | `db.minutes('300661', date='20260507')` |
| `db.tick(code, date)` | Tick 逐笔（实时/历史缓存） | `db.tick('300661')` |
| `db.index(code, start, end)` | 指数日线 | `db.index('000001')` |
| `db.stock_list(market)` | 股票列表 | `db.stock_list(market='SZ')` |
| `db.financials(code)` | 财务摘要（季度） | `db.financials('300661')` |

**参数说明：**
- `code`：股票代码，不带市场前缀（如 `'300661'`、`'600000'`）
- `market`：`'SH'` / `'SZ'` / `'BJ'` / `None`（全部）
- `start` / `end`：日期字符串 `'YYYY-MM-DD'`，可省略

---

## 数据来源优先级

```
① 本地 Parquet / SQLite   ← 优先，毫秒级
② pytdx（通达信 TCP）     ← 主力网络源，无需代理
③ akshare                 ← 备用（财务数据）
```

分钟线 / Tick **首次拉取后自动缓存**，后续调用无需网络。

---

## 配置文件（`config.yaml`）

```yaml
# 数据存储路径
data_dir: ./data
db_path: ./db/meta.db

# Tick 缓存策略
tick:
  cache_mode: daily     # none | daily | all
  keep_days: 30         # daily 模式下保留天数

# pytdx 服务器列表（自动选择最快）
servers:
  - ['180.153.18.170', 7709]   # 上海，推荐
  - ['119.147.212.81', 7709]   # 广东
  - ['124.74.236.50',  7709]   # 上海
```

---

## 目录结构

```
stock-data/
├── stockdb/                # Python 包（核心）
│   ├── __init__.py
│   ├── reader.py           # 统一读取接口
│   ├── updater.py          # 增量更新逻辑
│   └── config.py           # 配置加载
├── scripts/
│   ├── init_full.py        # 全量初始化（首次运行）
│   ├── daily_update.py     # 每日增量更新（cron 调用）
│   └── setup_cron_mac.sh   # 一键配置 Mac 定时任务
├── data/
│   ├── daily/              # 日线 Parquet（全市场，~3GB）
│   │   ├── sh/600000.parquet
│   │   └── sz/300661.parquet
│   ├── minutes/            # 分钟线（懒缓存，按需积累）
│   │   └── sz/300661/20260507.parquet
│   ├── index/              # 指数日线
│   │   └── sh000001.parquet
│   └── tick/               # Tick 缓存（最近 N 天）
│       └── sz/300661/20260507.parquet
├── db/
│   └── meta.db             # SQLite：股票列表、交易日历、财务
├── logs/                   # 每日更新日志
├── config.yaml             # 用户配置
├── requirements.txt
├── setup.py
└── README.md
```

---

## 适配的现有项目

| 项目 | 接入方式 |
|---|---|
| `读取股票当天数据/`（资金流分析） | 日线 → `db.daily()`，Tick → `db.tick()` |
| `ptrade-t0-ml/` | `_load_daily_history` → `db.daily()` |
| `daily_stock_analysis/` | 全量日线本地读取 |
| 未来新项目 | 直接 `from stockdb import StockDB` |

---

## 依赖

```
pytdx>=1.72     # 通达信 TCP 协议
mootdx          # 读取通达信 .day 整包格式
pandas>=2.0
pyarrow>=14.0   # Parquet 读写
apscheduler     # 定时任务（可选，也可用系统 cron）
pyyaml          # 配置文件
akshare         # 财务数据备用源
```

安装：
```bash
pip install pytdx mootdx pandas pyarrow apscheduler pyyaml akshare
```

---

## 常见问题

**Q: 第一次初始化需要多久？**  
A: 下载通达信整包 ZIP（约 500MB）约 30~60 分钟，解析写入 Parquet 约 2~3 分钟。之后每日增量更新一样快。

**Q: 漏跑了好几天，数据怎么补全？**  
A: 直接运行一次即可，无需指定日期。TDX ZIP 始终是全量快照，一次下载包含所有历史：
```bash
python3 scripts/daily_update.py --force
```

**Q: 没有网络时能用吗？**  
A: 日线完全可用（本地 Parquet）；分钟线和 Tick 如果之前拉取过并缓存，也可离线使用。

**Q: 数据占多少空间？**  
A: 全市场日线 5 年约 500MB，ZIP 缓存约 500MB，分钟线/Tick 按需积累。

**Q: 如何移植到 Ubuntu / Linux？**  
A: 代码完全兼容 Linux，运行一键安装脚本：
```bash
git clone <repo> /path/to/stock-data
cd /path/to/stock-data
bash scripts/setup_linux.sh   # 自动安装依赖、配置 cron
```
然后关闭代理运行首次初始化：
```bash
.venv/bin/python3 scripts/init_full.py
```

**Q: 如何在多台机器上共享数据？**  
A: 有两种方案：  
- **OneDrive/NAS 共享**：将 `data/` 目录设为共享文件夹，各机器通过符号链接访问  
- **VPS 部署**：在服务器跑 `daily_update.py`，其他机器通过 rsync/SSH 同步 Parquet

