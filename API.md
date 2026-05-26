# stockdb API 调用文档

> 本地 A 股数据库统一接口，供所有项目调用。  
> 数据优先从本地 Parquet/SQLite 读取（毫秒级），本地缺失时自动从 pytdx 拉取并存盘。

---

## 安装

```bash
pip install -e /Users/wangluke/Localprojects/stock-data
```

安装后在任意项目中：

```python
from stockdb import StockDB
db = StockDB()
```

---

## 接口速览

| 方法 | 用途 | 返回列 |
|---|---|---|
| `db.daily()` | 日线 OHLCV，全市场，5年历史 | date, open, high, low, close, vol, amount |
| `db.minutes()` | 1分钟线，写透缓存 | datetime, open, high, low, close, vol, amount |
| `db.tick()` | 逐笔成交，当日实时或历史缓存 | time, price, vol, direction, amount |
| `db.index()` | 指数日线（沪深300/上证50等） | date, open, high, low, close, vol, amount |
| `db.stock_list()` | 股票列表（来自 SQLite） | code, name, market, board, outstanding_shares, free_float_shares |
| `db.get_shares()` | 获取单股最新股本信息 | dict `{"outstanding_shares": float, "free_float_shares": float/None}` |
| `db.financials()` | 财务摘要（akshare，季度） | 各财务指标列 |
| `db.chip()` | 筹码分布（本地计算，按需缓存） | date, close, profit_ratio, concentration_90, concentration_70, avg_cost, peak_price |
| `db.is_trade_day()` | 判断某天是否交易日 | bool |
| `db.last_trade_day()` | 数据库最近交易日 | str `'YYYYMMDD'` |

---

## 日线数据 `db.daily()`

```python
# 基本用法
df = db.daily('300661')                        # 全部历史（5年）
df = db.daily('300661', start='2025-01-01')    # 指定起始日期
df = db.daily('600519', start='2024-01-01', end='2024-12-31')  # 区间

# 返回列：date(datetime64), open, high, low, close, vol, amount(float)
print(df.tail())
#         date   open   high    low  close      vol       amount
# 1289 2026-05-07  338.5  342.0  336.8  340.2  1523400  5.18e+08
```

**注意事项：**

- `code` 不带交易所前缀，直接传 6 位数字代码
- 数据为**不复权**原始价格（来自通达信）
- 如需前复权（建议 ML 训练使用），需自行用复权因子处理（后续版本将支持 `adjust='qfq'`）
- 本地无数据时自动拉取 pytdx，拉取后存盘，下次毫秒级

**批量读取多只股票：**

```python
import pandas as pd

codes = ['600000', '600519', '601318', '000001', '300661']
dfs = {code: db.daily(code, start='2025-01-01') for code in codes}

# 提取收盘价矩阵
close_matrix = pd.DataFrame({c: dfs[c].set_index('date')['close'] for c in codes})
print(close_matrix.tail())
```

---

## 分钟线数据 `db.minutes()`

```python
# 今日分钟线
df = db.minutes('300661')

# 指定日期
df = db.minutes('300661', date='20260507')
df = db.minutes('300661', date='2026-05-07')   # 两种日期格式均可

# 最近 N 天（合并返回）
df = db.minutes('300661', days=5)

# 返回列：datetime(datetime64), open, high, low, close, vol, amount
print(df.head())
#             datetime   open   high    low  close    vol   amount
# 0  2026-05-07 09:31  336.0  337.5  335.8  337.2  23400  7.88e6
```

**缓存行为与防断档机制：**

- **更新策略**：分为“懒缓存”（查询时按需拉取并缓存）与“主动更新沉淀”（配置于 `config.yaml` 里的 `watchlist` 自选股列表，在每日增量更新时被主动拉取并保存，以防 pytdx 最多只保留 100 天分钟线而产生数据断档）。
- **收盘安全过滤**：强收盘校验线定位在 **`15:10`**。在交易日 15:10 之前调用不会写入当日未收盘分钟线，确保只有完整收盘的分钟数据才落盘；其余历史日期和收盘后（15:10 后）则正常幂等落盘。
- **断档检测**：分钟数据在增量写入时会校验与本地最后一天数据的跨度，相差 $\ge 90$ 天发出 `WARNING` 警告，相差 $\ge 100$ 天发出 `CRITICAL` 严重断档报警，提醒重新使用全量工具补足。
- **存盘路径**：`data/minutes/{code}/{YYYYMMDD}.parquet`
- **多源瀑布流数据源**：首选 pytdx TCP 直连，备用网页多源 HTTP 瀑布流（东财 ➔ 腾讯 ➔ 新浪）。核心查询逻辑内置 `disable_proxy` 自动避让全局代理，完全无需手动关闭代理。

---

## Tick 逐笔数据 `db.tick()`

```python
# 当日实时 Tick
df = db.tick('300661')

# 历史 Tick（需先有缓存，或代理关闭状态下拉取）
df = db.tick('300661', date='20260507')

# 返回列：time, price, vol, direction(0买/1卖/2中性), amount
print(df.head())
#      time   price  vol  direction    amount
# 0  09:25:00  336.5  100       1    33650.0
```

**使用场景：**

- 资金流向分析（大单/小单识别）
- 主力行为分析
- 高频信号提取

**缓存控制（`config.yaml`）：**

```yaml
tick:
  cache_mode: "all"    # all=全部缓存 | recent=只缓存近N天 | none=不缓存
  keep_days: 7         # 保留最近7天，自动清理旧缓存
```

---

## 指数日线 `db.index()`

```python
# 支持的指数代码
INDICES = {
    '000001': '上证指数',
    '000300': '沪深300',
    '000016': '上证50',
    '000905': '中证500',
    '000852': '中证1000',
    '399001': '深证成指',
    '399006': '创业板指',
}

df = db.index('000300', start='2024-01-01')
print(df.tail())
#         date    open    high     low   close       vol       amount
# 1288 2026-05-07  3812.5  3856.2  3801.0  3843.7  3.2e+10   5.6e+11
```

---

## 股票列表 `db.stock_list()`

```python
# 全市场
all_stocks = db.stock_list()             # 约 7000 条
sh_stocks = db.stock_list(market='sh')  # 沪市
sz_stocks = db.stock_list(market='sz')  # 深市（含创业板）
bj_stocks = db.stock_list(market='bj')  # 北交所

print(all_stocks.head())
#      code  name market  board
# 0  000001  平安银行     sz    主板
# 1  000002   万科A     sz    主板
# 2  300661  圣邦股份     sz   创业板
# 3  688981  中芯国际     sh   科创板
```

**按板块过滤（推荐用 `board` 字段）：**

```python
# board 取值：主板 / 科创板 / 创业板 / 中小板 / 北交所
kcb   = all_stocks[all_stocks['board'] == '科创板']   # 科创板
cyb   = all_stocks[all_stocks['board'] == '创业板']   # 创业板
bse   = all_stocks[all_stocks['board'] == '北交所']   # 北交所
main  = all_stocks[all_stocks['board'] == '主板']     # 主板

print(f'科创板: {len(kcb)} 只，创业板: {len(cyb)} 只，北交所: {len(bse)} 只')
```

**注意：** 当前列表包含 A 股、债券、ETF 等品种。过滤纯 A 股：

```python
# 通过代码规律过滤（更精准）
a_shares = all_stocks[all_stocks['code'].str.match(r'^(000|001|002|003|300|301|600|601|603|605|688)\d{3}$')]
```

---

## 股本数据查询 `db.get_shares()` 与流通股本字段

为了支持高精度筹码分布（CYQ）算法的推进，数据库引入了个股的**流通股本**（`outstanding_shares`）与**自由流通股本**（`free_float_shares`，目前暂设为 `None`）数据，单位均为 **“股”**。

### 1. 单股查询 `db.get_shares()`

返回一个包含个股流通股本和自由流通股本字典：

```python
shares = db.get_shares('600519')
print(shares)
# {'outstanding_shares': 1252270234.375, 'free_float_shares': None}
```

### 2. 批量股票列表包含股本列 `db.stock_list()`

通过 `db.stock_list()` 获取的股票列表中自动包含这二个股本字段：

```python
df = db.stock_list()
print(df[['code', 'name', 'outstanding_shares', 'free_float_shares']].head())
#      code  name  outstanding_shares  free_float_shares
# 0  000001  平安银行        1.940592e+10               None
# 1  000002   万科A        9.724197e+09               None
```

### 3. 数据更新与同步维护

- **后台自动同步**：全量初始化脚本 `scripts/init_full.py` 和每日增量更新脚本 `scripts/daily_update.py` 中已无缝挂载了股本同步流程，每个交易日收盘后增量更新时会自动调用 `db.update_stock_shares()` 极速拉取并持久化校准。
- **高性能连接复用模式**：内置多线程 Chunk 分包同步模式，每线程仅建立一次 TCP 物理长连接即可循环拉取批量股票的股本信息，对全市场 11000+ 个代码及品种的同步过程仅需 **34 秒**，且支持网络波动下的断线重连和重试逻辑。
- **手动触发同步**：
  ```python
  db.update_stock_shares(max_workers=20)
  ```

---

## 筹码分布 `db.chip()`

> 从本地 OHLCV 日线数据离线计算，**无需网络**。首次调用临时计算（约 0.02s），结果自动缓存到 `data/chip/{code}.parquet`，后续调用毫秒级。

```python
# 获取全部历史筹码分布
df = db.chip('300661')
print(df.tail())
#         date   close  profit_ratio  concentration_90  concentration_70  avg_cost  peak_price
# 498 2026-05-14  108.5        0.3821            0.0412            0.0287    105.32      106.80
# 499 2026-05-15  107.2        0.2954            0.0389            0.0261    104.87      106.80

# 获取指定日期（策略信号常用）
row = db.chip('300661', date='2026-05-15')
profit  = row['profit_ratio'].iloc[0]       # 0.30 = 30% 筹码处于获利
conc90  = row['concentration_90'].iloc[0]   # 筹码集中度，越小越集中
conc70  = row['concentration_70'].iloc[0]
avg_cost = row['avg_cost'].iloc[0]          # 主力平均成本
peak    = row['peak_price'].iloc[0]         # 筹码密集峰值价

# 强制重算（日线更新后刷新缓存）
db.chip('300661', recalc=True)
```

**返回列说明：**

| 列名 | 含义 | 取值范围 |
|---|---|---|
| `profit_ratio` | 获利比例：当前价以下筹码占比 | 0~1，越高套牢盘越少 |
| `concentration_90` | 90% 筹码价格区间宽度 / 当前价 | 越小越集中，筹码越稳定 |
| `concentration_70` | 70% 筹码价格区间宽度 / 当前价 | 同上，更敏感 |
| `avg_cost` | 主力加权平均成本 | 单位：元 |
| `peak_price` | 筹码密集度最高的价格（众数） | 单位：元 |

**批量获取多只股票当天筹码：**

```python
codes = ['600000', '600519', '000001', '300661']
today = '2026-05-15'

result = []
for c in codes:
    row = db.chip(c, date=today)
    if not row.empty:
        result.append({'code': c, **row.iloc[0].to_dict()})

import pandas as pd
print(pd.DataFrame(result))
```

**算法说明：**

- 核心模型：**三角分布递推**（行业标准 OHLCV 筹码算法）
- 每根 K 线的成交量在 `[low, high]` 区间按三角分布分配，峰值在收盘价
- 换手率用相对成交量代理（无需流通股本），获利比例与东方财富误差约 ±5~10%，方向一致
- 追溯最近 500 个交易日（约 2 年），更早历史对当前筹码贡献极小

**缓存路径：** `data/chip/{code}.parquet`

---

## 工具方法

```python
# 判断交易日
db.is_trade_day('20260507')   # True
db.is_trade_day('20260506')   # False（休市）

# 数据库最近一个交易日
last = db.last_trade_day()    # '20260507'

# 清理旧 Tick 缓存（daily_update.py 自动调用）
db.clean_old_ticks()
```

---

## 完整示例：替换 akshare 调用

**改造前（直接用 akshare）：**

```python
import akshare as ak

df = ak.stock_zh_a_hist(symbol='300661', period='daily',
                         adjust='qfq', start_date='2024-01-01')
df = df.rename(columns={'日期': 'date', '收盘': 'close', ...})
```

**改造后（用 stockdb）：**

```python
from stockdb import StockDB
db = StockDB()

df = db.daily('300661', start='2024-01-01')
# 列名已标准化：date, open, high, low, close, vol, amount
# 本地有缓存：毫秒级；无缓存：自动拉取并存盘
```

---

## 用于 `ptrade-t0-ml` 项目

```python
from stockdb import StockDB

db = StockDB()

# 批量拉取训练数据
def load_train_data(codes: list, start: str) -> dict:
    return {code: db.daily(code, start=start) for code in codes}

# 实时分钟线（策略执行时）
def get_today_minutes(code: str):
    return db.minutes(code)

# 资金流向（用 Tick）
def get_tick_flow(code: str, date: str = None):
    return db.tick(code, date=date)
```

---

## 数据刷新规则

| 数据类型 | 更新频率 | 触发方式 |
|---|---|---|
| 日线 | 每个交易日 | `python3 scripts/daily_update.py`（内置代理绕过，16:30后） |
| 分钟线 | 按需 | 首次调用自动拉取并存盘 |
| Tick | 按需 | 首次调用自动拉取，保留7天 |
| 指数 | 每个交易日 | `daily_update.py` 自动更新 |
| 股票列表 | 手动 | `python3 scripts/init_full.py` |
| 筹码分布 | 按需（自动缓存） | `db.chip(code)` 首次自动算并缓存；日更后用 `recalc=True` 刷新 |

---

## 常见问题

**Q: 数据是否复权？**  
A: 日线数据为不复权原始价格（来自通达信）。若用于 ML 训练，建议等待 `adjust='qfq'` 参数支持，或自行计算。

**Q: 为什么有些股票行数很少（<50行）？**  
A: TDX 整包含债券、ETF 等非股票品种，这些数据少是正常的。实际 A 股主板股票应有 1200+ 行。

**Q: 调用时提示网络错误？**  
A: `stockdb` 核心读取逻辑与增量更新脚本内已深度集成了 `disable_proxy` 代理自动避让机制，并配置了多级 HTTP 瀑布流 Fallback 兜底（东财 ➔ 腾讯 ➔ 新浪），一般情况下**无需手动关闭全局系统代理**（如 Clash 等）。如果极个别情况下仍报网络错误，请验证当前 pytdx 节点连接状态或检查网络物理连通性。

**Q: 如何判断数据是否是最新的？**  
```python
df = db.daily('300661')
print(f"最新数据日期: {df['date'].max().date()}")
print(f"今天是否已更新: {str(df['date'].max().date()) == db.last_trade_day()[:4]+'-'+db.last_trade_day()[4:6]+'-'+db.last_trade_day()[6:]}")
```

---

## 历史重大 Bug 修复记录与维护工具

为了保障数据库的高鲁棒性与高准确度，本项目对以下历史重大 Bug 进行了针对性重构，并附带了一键修复工具。

### 1. Mac M系列芯片并发加载 akshare 导致 Python 崩溃 (V8 Crash)
- **现象描述**：在 macOS (特别是 Apple Silicon M系列) 下，多线程并发 `import akshare` 或调用其底层 JavaScript 接口（例如依赖 `py_mini_racer` 的接口）时，极易触发 C++ 层的 V8 引擎并发冲突，抛出 `FATAL Check failed: !pool->IsInitialized()` 并导致 Python 进程异常闪退。
- **修复方案**：
  1. **全局进程锁**：在核心读取接口 [reader.py](file:///Users/wangluke/Localprojects/stock-data/stockdb/reader.py) 与每日更新脚本 [daily_update.py](file:///Users/wangluke/Localprojects/stock-data/scripts/daily_update.py) 中引入了全局互斥锁 `akshare_lock`，确保对 `akshare` 的加载与调用以独占排他的单线程同步方式安全执行。
  2. **分钟线单线程化**：将分钟线的主动更新沉淀流程从多线程 `ThreadPoolExecutor` 重构为单线程同步串行循环。由于分钟线只轮询自选股和少部分缓存股，单线程在网络直连与瀑布流机制下速度极快，同时彻底根治了多线程下的 Crash 隐患。

### 2. 增量日线成交量 (vol) 单位不一致 Bug (“手”与“股”断层)
- **现象描述**：通达信历史全量 Parquet 中的成交量（vol）单位是 **“股”**，但增量更新拉取的日线成交量单位是 **“手”**（缩水了 100 倍）。导致数据在增量拼接位置产生了严重的成交量断层。
- **修复方案**：
  1. **代码统一缩放**：在核心包 [reader.py](file:///Users/wangluke/Localprojects/stock-data/stockdb/reader.py) 的个股 `_fetch_daily_raw`、`_fallback_daily`、网页瀑布流接口以及每日更新脚本 [daily_update.py](file:///Users/wangluke/Localprojects/stock-data/scripts/daily_update.py) 中，强制将获取的个股增量日线成交量进行统一 `* 100` 换算（单位统一转换并对齐为“股”，但指数和北交所由于本身是“股”所以不做处理）。
  2. **历史数据一键清洗工具**：项目内置了清洗工具 [fix_daily_volume.py](file:///Users/wangluke/Localprojects/stock-data/scripts/fix_daily_volume.py)，可用于一键纠正本地历史脏数据。
- **一键修复历史数据**：
  ```bash
  python3 scripts/fix_daily_volume.py
  ```
  该工具会对本地所有日线 Parquet 进行**单位自适应判定**（如果非零行满足 `vol * close * 10 < amount`，则判定原成交量为“手”单位，自动 `* 100` 并原地覆写，其余已是“股”单位的行则保持幂等不动），秒级完成全市场历史数据的无痛清洗与精度对齐。
