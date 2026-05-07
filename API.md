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
| `db.stock_list()` | 股票列表（来自 SQLite） | code, name, market |
| `db.financials()` | 财务摘要（akshare，季度） | 各财务指标列 |
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

**缓存行为：**

- 当天交易日结束后（16:00 之后）才存盘，避免缓存不完整数据
- 存盘路径：`data/minutes/{code}/{YYYYMMDD}.parquet`
- 数据来源：pytdx TCP，无需代理

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
#      code  name market
# 0  000001  平安银行     sz
# 1  000002   万科A     sz
```

**注意：** 当前列表包含 A 股、债券、ETF 等品种。过滤纯 A 股：

```python
# 代码规律：主板 000/001/002/003/600/601/603/605/688，创业板 300/301
a_shares = all_stocks[all_stocks['code'].str.match(r'^(000|001|002|003|300|301|600|601|603|605|688)\d{3}$')]
```

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
| 日线 | 每个交易日 | `python3 scripts/daily_update.py`（关代理，16:30后） |
| 分钟线 | 按需 | 首次调用自动拉取并存盘 |
| Tick | 按需 | 首次调用自动拉取，保留7天 |
| 指数 | 每个交易日 | `daily_update.py` 自动更新 |
| 股票列表 | 手动 | `python3 scripts/init_full.py` |

---

## 常见问题

**Q: 数据是否复权？**  
A: 日线数据为不复权原始价格（来自通达信）。若用于 ML 训练，建议等待 `adjust='qfq'` 参数支持，或自行计算。

**Q: 为什么有些股票行数很少（<50行）？**  
A: TDX 整包含债券、ETF 等非股票品种，这些数据少是正常的。实际 A 股主板股票应有 1200+ 行。

**Q: 调用时提示网络错误？**  
A: 检查是否开启了系统代理（pytdx 走 TCP 不受代理影响，但 akshare 兜底需要 HTTP）。建议在代理关闭状态下运行 `daily_update.py`。

**Q: 如何判断数据是否是最新的？**  
```python
df = db.daily('300661')
print(f"最新数据日期: {df['date'].max().date()}")
print(f"今天是否已更新: {str(df['date'].max().date()) == db.last_trade_day()[:4]+'-'+db.last_trade_day()[4:6]+'-'+db.last_trade_day()[6:]}")
```
