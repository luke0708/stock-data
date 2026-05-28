# stockdb — 本地行情数据库（A 股 + 美股）

> 为所有本地项目提供**统一、稳定、离线可用**的行情数据接口。  
> **A 股**与**美股**完全独立——两套模块、两套存储、互不干扰。

---

## 核心理念

```
所有项目永远只调用 stockdb 接口，不直接碰 akshare / pytdx / yfinance。
```

### A 股（`StockDB`）
- **日线**：全市场 5200 只，5 年历史，本地 Parquet，毫秒级读取
- **分钟线**：写透缓存，首次拉取自动存盘，越用越快
- **Tick**：当日实时，可配置保留最近 N 天历史
- **每日自动补充**：每个交易日 16:30 后自动更新所有数据

### 美股（`USStockDB`）—— 与 A 股完全独立
- **日线 + 前复权价**：yfinance 拉取，存储在 `data/daily/us/`，不影响 A 股任何文件
- **基本面**：市值、营收同比（GAAP 季报），写入独立的 `us_*` 前缀 SQLite 表
- **拆股 / 缩股检测**：自动记录反向拆股因子（factor < 1），配合峰值比指纹过滤稀释垃圾
- **独立更新脚本**：`scripts/us_update.py`，与 `daily_update.py` 互不干扰

**多项目共享**：`pip install -e .` 一次，所有本地项目同时可用 A 股和美股接口。

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

内置代理自动绕过，直接运行：

```bash
python3 scripts/init_full.py
```

已有数据时自动跳过下载，只补充缺失部分。

### A 股：每日更新 / 周末补数

```bash
source .venv/bin/activate && python3 scripts/daily_update.py
```

- 工作日运行：补当天数据
- 周六/周日运行：自动回溯到周五，补上周数据
- 缺失太多时会提示 → 改跑 `init_full.py`

### A 股：数据缺失过多 / 重新初始化

```bash
python3 scripts/init_full.py
```

下载完整 ZIP，一次性补全所有历史（无论缺失多少天）。

### 美股：初始化 & 每日更新（完全独立，不影响 A 股）

```bash
# 首次初始化 / 强制重拉全量历史（5 年）
python scripts/us_update.py --force-refresh

# 每日增量更新（美东收盘后运行）
python scripts/us_update.py

# 只更新指定标的
python scripts/us_update.py --tickers AXTI NVDA AAPL
```

美股更新脚本与 A 股 `daily_update.py` **完全隔离**：不共享代码路径、不共享存储目录、不共享 SQLite 表。两者可同时运行，互不影响。


---

## 接口文档

### A 股 `StockDB`

```python
from stockdb import StockDB
db = StockDB()
```

| 方法 | 说明 | 示例 |
|---|---|---|
| `db.daily(code, start, end)` | 日线 K 线（全市场，本地 Parquet，成交量单位统一为”股”） | `db.daily('300661', start='2024-01-01')` |
| `db.minutes(code, date, days)` | 分钟线（支持懒缓存与 `watchlist` 主动增量更新，防 100 天断档） | `db.minutes('300661', date='20260507')` |
| `db.tick(code, date)` | Tick 逐笔（实时/历史缓存） | `db.tick('300661')` |
| `db.index(code, start, end)` | 指数日线 | `db.index('000001')` |
| `db.stock_list(market)` | 股票列表 | `db.stock_list(market='SZ')` |
| `db.financials(code)` | 财务摘要（季度） | `db.financials('300661')` |

**参数说明：**
- `code`：股票代码，不带市场前缀（如 `'300661'`、`'600000'`）
- `market`：`'SH'` / `'SZ'` / `'BJ'` / `None`（全部）
- `start` / `end`：日期字符串 `'YYYY-MM-DD'`，可省略

### 美股 `USStockDB`（独立模块，与 A 股零耦合）

```python
from stockdb.us import USStockDB
usdb = USStockDB()
```

| 方法 | 说明 | 示例 |
|---|---|---|
| `usdb.daily(ticker, start, end)` | 日线 OHLCV + 前复权价 + 成交额（美元） | `usdb.daily('AXTI', start='2024-01-01')` |
| `usdb.splits(ticker, lookback_months)` | 拆股记录，factor < 1 = 缩股 | `usdb.splits('NIVF')` |
| `usdb.adj_peak_ratio(ticker)` | 前复权峰值 / 现价（> 100 = 缩股垃圾指纹） | `usdb.adj_peak_ratio('NIVF')` |
| `usdb.revenue_yoy(ticker)` | 营收同比（GAAP 季报） | `usdb.revenue_yoy('AXTI')` |
| `usdb.market_cap(ticker)` | 当前市值（美元） | `usdb.market_cap('AXTI')` |
| `usdb.avg_dollar_volume(ticker)` | 近 30 日均成交额（美元） | `usdb.avg_dollar_volume('AXTI')` |
| `usdb.meta(ticker)` | 基础信息（名称、交易所、行业） | `usdb.meta('AXTI')` |

> 详细说明见 [API.md](./API.md) 的「美股数据 USStockDB」章节。

---

## 数据来源优先级

### A 股
```
① 本地 Parquet / SQLite   ← 优先，毫秒级
② pytdx（通达信 TCP）     ← 主力网络源，自动绕过代理
③ 网页 HTTP 多源瀑布流    ← 备用兜底（东财 ➔ 腾讯 ➔ 新浪），自动绕过代理
④ akshare                 ← 备用（财务数据等，支持全局进程锁）
```
分钟线 / Tick **首次拉取后自动缓存**，后续调用无需网络。

### 美股
```
① 本地 Parquet / SQLite   ← 优先，毫秒级
② yfinance                ← 免费原型源（日线延迟约 15 分钟）
```
provider 可切换（在 `config.yaml` 的 `us.provider` 配置）：当前支持 `yfinance`，后续可扩展 `polygon` / `fmp` 等付费源。

---

## 配置文件（`config.yaml`）

```yaml
# 数据存储路径
data_dir: ./data
db_path: ./db/meta.db

# Tick 缓存策略（A 股）
tick:
  cache_mode: daily     # none | daily | all
  keep_days: 30         # daily 模式下保留天数

# pytdx 服务器列表（A 股，自动选择最快）
servers:
  - ['180.153.18.170', 7709]   # 上海，推荐
  - ['119.147.212.81', 7709]   # 广东
  - ['124.74.236.50',  7709]   # 上海

# 美股（独立配置块，不影响上方 A 股任何配置项）
us:
  enabled: true
  provider: yfinance        # 可切换: polygon / fmp
  history_years: 5
  watchlist:
    - "AXTI"                # 半导体衬底，筛选器正样本
    - "NIVF"                # 反复缩股垃圾，筛选器反样本
    - "AAPL"
    - "NVDA"
```

---

## 目录结构

```
stock-data/
├── stockdb/                # Python 包（核心）
│   ├── __init__.py
│   ├── reader.py           # A 股统一读取接口（内置代理绕过与并发安全锁）
│   ├── chip.py             # 筹码分布三角计算模块
│   ├── db.py               # A 股 SQLite 元数据库接口
│   ├── market.py           # A 股市场与板块检测（6位数字代码，不影响美股）
│   ├── config.py           # 配置加载（含 us_* 只读属性）
│   └── us/                 # ── 美股独立子模块（与上方 A 股完全隔离）──
│       ├── __init__.py
│       ├── provider.py     # DataProvider ABC + YFinanceProvider（可切换数据源）
│       ├── db.py           # USMetaDB（us_* 前缀表，同一 meta.db，不动 A 股表）
│       ├── calendar.py     # 美股交易日历（从 ^GSPC 推导）
│       └── reader.py       # USStockDB 门面（本地 Parquet 优先 + 写透缓存）
├── scripts/
│   ├── init_full.py        # A 股全量初始化（首次运行）
│   ├── daily_update.py     # A 股每日增量更新
│   ├── us_update.py        # 美股更新（独立，与 daily_update.py 互不干扰）
│   ├── fix_daily_volume.py # 一键纠正个股成交量”手/股”单位修复脚本
│   └── setup_cron_mac.sh   # 一键配置 Mac 定时任务
├── data/
│   ├── daily/
│   │   ├── sh/600000.parquet   # A 股日线
│   │   ├── sz/300661.parquet
│   │   └── us/AXTI.parquet     # 美股日线（独立目录）
│   ├── minutes/            # A 股分钟线（懒缓存，按需积累）
│   ├── index/              # A 股指数日线
│   └── tick/               # A 股 Tick 缓存
├── db/
│   └── meta.db             # SQLite：A 股表（stocks/trade_calendar/...）
│                           #        + 美股表（us_stocks/us_splits/us_financials/us_calendar）
├── tests/
│   └── test_us_data.py     # 美股黄金样本断言（AXTI/NIVF）
├── logs/
├── config.yaml
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
pytdx>=1.72     # 通达信 TCP 协议（A 股专用）
mootdx          # 读取通达信 .day 整包格式（A 股专用）
pandas>=2.0
pyarrow>=14.0   # Parquet 读写
pyyaml          # 配置文件
akshare         # A 股财务数据备用源
yfinance>=0.2.40 # 美股数据源（独立，不影响 A 股）
```

安装：
```bash
pip install -e .
# 或
pip install -r requirements.txt
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
然后直接运行首次初始化：
```bash
.venv/bin/python3 scripts/init_full.py
```

**Q: 美股模块会影响 A 股数据吗？**  
A: 不会。两者完全隔离：美股用独立子模块 `stockdb/us/`，独立存储目录 `data/daily/us/`，独立 SQLite 表（`us_` 前缀），独立更新脚本 `us_update.py`。A 股的 `market.py`、`reader.py`、`tdx_client.py`、`daily_update.py` 均未做任何修改。

**Q: 美股和 A 股能同时更新吗？**  
A: 可以，两个脚本完全独立，可并行运行：
```bash
python3 scripts/daily_update.py &   # A 股
python3 scripts/us_update.py        # 美股
```

**Q: 如何在多台机器上共享数据？**  
A: 有两种方案：  
- **OneDrive/NAS 共享**：将 `data/` 目录设为共享文件夹，各机器通过符号链接访问  
- **VPS 部署**：在服务器跑 `daily_update.py`，其他机器通过 rsync/SSH 同步 Parquet


---

## 技术维护备忘与高阶避坑经验（2026-05-23 重构记）

在开发与迭代增量同步系统时，我们总结沉淀了以下极具系统健壮性价值的避坑方案：

### 1. 交易日盘中数据污染（收盘安全锁）
- **痛点**：若在交易日下午 15:30 结算完毕前运行 `daily_update.py`，会判定今天为目标日期，将盘中未收盘的临时行情追加到 Parquet 中。到盘后再次更新时，因本地最大日期已等于今天而跳过，造成当天行情永远停留在错误的未收盘阶段。
- **方案**：引入 15:30 收盘安全锁。在 15:30 前数据上限回溯至昨天，并结合元数据库 `trade_calendar` 精准匹配上一个开市交易日，从根源规避盘中污染。

### 2. Clash 全局代理网络干扰（网页 HTTP 与 TCP 二进制的分别避让）
- **痛点**：开启科学上网/全局代理（如 Clash 代理）时，使用 `akshare` 或东方财富网页快照 HTTP 直连接口极易被拦截并报反爬错误；而 pytdx 二进制 TCP 流量被代理接管时，也会因为非标准 7709 端口转发失败而造成同步中断，致使普通股日线断更。
- **方案**：
  1. 将快速通道 `_bulk_update_from_spot`、akshare 兜底 `_fetch_akshare_day` 以及分钟线 trends HTTP 块用 `disable_proxy()` 包裹。通过临时清空进程代理环境变量并 Mock `urllib`/`requests` 的代理检测函数，彻底在进程内部隔绝 Clash 代理干扰。
  2. 保证了在有全局代理的开发/生产环境下，依然拥有最坚固的高可靠网络直连直通性。

### 3. 多线程并发引发的 V8 段错误崩溃 (V8 Thread-safety)
- **痛点**：在多线程中并发导入 `akshare` 并调用基于 Javascript 的接口时，会因为 C++ 层 V8 引擎在同一进程内的多线程重复初始化冲突引发 `Check failed: !pool->IsInitialized()` 段错误崩溃退出。
- **方案**：
  1. 使用全局进程互斥锁 `akshare_lock` 保证导入和关键 akshare API 的独占串行。
  2. 对分钟线更新轮询同步进行串行单线程化设计。由于使用了高效的 trends API，单线程对多股的同步速度完全能够满足秒级响应，同时彻底清除了多线程 Crash 隐患。

### 4. 停牌股误报警与退市个股性能过滤
- **停牌股处理**：如果个股长期停牌，单纯按自然日跨度计算更新相差天数（如 $\ge 100$ 天）会持续报错 `CRITICAL` 并频繁向网络发起无用查询。我们将其优化为 **“分钟线最新日期与本地日线最新日期对齐过滤”** 逻辑：若停牌期间分钟线与日线均保持在停牌前最后一天（`last_date >= last_daily_date`），说明数据已是当前最新交易状态，不做报警和无意义网络查询，完全遵循市场客观交易事实。
- **退市股处理**：在日线增量和分钟线同步前，从 SQLite 过滤已退市股票名单并从拉取列表移除，防止为已退市股票发起成百上千次无效网络请求，实现了极佳的性能累积优化。

