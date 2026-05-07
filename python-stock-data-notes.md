# 通达信股票数据 Python 接口对比笔记

> 整理日期: 2026-05-07
> 适用场景: 股票分析软件获取 A 股行情数据

---

## 一、三个库的关系

```
pytdx             底层库，直接对接通达信行情服务器 TCP 协议
   ↑
tdxpy             基于 pytdx 的轻量封装 (pytdx 原作者弃坑后 fork)
   ↑
mootdx            基于 tdxpy 的更高层封装，带自动找服务器、行情阅读器等
```

| 库 | 包名 | pip 安装 | Stars | 最后更新 | 维护状态 |
|---|---|---|---|---|---|
| **pytdx** | `pytdx` | `pip install pytdx` | ~3k | 2022 | 停更 |
| **tdxpy** | `tdxpy` | `pip install tdxpy` | ~500 | 2023 | 慢速维护 |
| **mootdx** | `mootdx` | `pip install mootdx` | ~1.6k | 2024 | 停更 |

**建议**: 小型项目直接用 `mootdx` 上手最快，底层问题排查时回退到 `pytdx`。

---

## 二、安装

```bash
# 安装 pytdx（底层）
pip install pytdx

# 安装 mootdx（高层封装，会自动装 tdxpy/pytdx 依赖）
pip install 'mootdx[all]'
```

---

## 三、获取逐笔成交数据（Tick/Transaction）

### 3.1 pytdx 方式

```python
from pytdx.hq import TdxHq_API
import pytdx.params as P

api = TdxHq_API()
api.connect('180.153.18.170', 7709)

# --- 今日实时逐笔 ---
data = api.get_transaction_data(
    P.TDXParams.MARKET_SZ,  # 0=深圳, 1=上海
    '300661',               # 股票代码（字符串）
    0,                      # 起始位置
    100                     # 返回条数（最大2000）
)

# --- 历史某天逐笔 ---
data = api.get_history_transaction_data(
    P.TDXParams.MARKET_SZ,
    '300661',
    0,
    100,
    20260506  # 日期：整数 YYYYMMDD
)

# 转 DataFrame
import pandas as pd
df = pd.DataFrame(data)
print(df.columns)
# ['time', 'price', 'vol', 'num', 'buyorsell']

api.disconnect()
```

### 3.2 mootdx 方式

```python
from mootdx.quotes import Quotes

# factory 会自动寻找最快的服务器（耗时约3-5秒）
client = Quotes.factory(market='std', multithread=False, heartbeat=False)

# --- 今日实时逐笔 ---
df = client.transaction(symbol='300661', start=0, offset=2000)

# --- 历史某天逐笔 ---
df = client.transactions(symbol='300661', date='20260506', start=0, offset=2000)
```

**字段说明:**

| 字段 | 含义 | 值 |
|---|---|---|
| `time` | 成交时间 | `09:30` ~ `15:00` |
| `price` | 成交价格 | 浮点数 |
| `vol` | 成交量 | 手 |
| `num` | 成交笔数 | 笔 |
| `buyorsell` | 买卖方向 | `0`=外盘(主动买), `1`=内盘(主动卖), `2`=中性盘, `8`=集合竞价 |
| `volume` | 累计成交量(mootdx特有) | 从当日开始累计 |

---

## 四、获取 K 线数据

### pytdx

```python
# 日线
data = api.get_security_bars(9, 0, '300661', 0, 10)
# 周线
data = api.get_security_bars(7, 0, '300661', 0, 10)
# 月线
data = api.get_security_bars(6, 0, '300661', 0, 10)

# frequency 参数:
#   9=daily, 7=weekly, 6=monthly
#   8=5min, 3=15min, 4=30min, 5=60min

# market 参数: 0=深圳, 1=上海
```

### mootdx

```python
# 日线
df = client.bars(symbol='300661', frequency=9, offset=10)
# 指数
df = client.index(symbol='000001', frequency=9)
# 分钟
df = client.minute(symbol='000001')
```

**K线DataFrame列**: `['open', 'close', 'high', 'low', 'vol', 'amount', 'year', 'month', 'day', 'hour', 'minute', 'datetime']`

---

## 五、获取分时数据

### pytdx

```python
# 今日分时
data = api.get_minute_time_data(P.TDXParams.MARKET_SZ, '300661')

# 历史分时
data = api.get_history_minute_time_data(P.TDXParams.MARKET_SZ, '300661', 20260506)
```

### mootdx

```python
# 今日分时
df = client.minute(symbol='300661')

# 历史分时
df = client.minutes(symbol='300661', date='20260506')
```

---

## 六、获取全量日线（从TDX数据中心下载）

通达信官网每日发布全市场日线数据包（盘后更新）：

```
https://www.tdx.com.cn/products/data/data/vipdoc/shlday.zip       # 上证所有证券日线
https://www.tdx.com.cn/products/data/data/vipdoc/szlday.zip       # 深证所有证券日线
https://www.tdx.com.cn/products/data/data/vipdoc/bjlday.zip       # 北证所有证券日线
https://www.tdx.com.cn/products/data/data/vipdoc/tdxzs_day.zip    # 板块指数日线
https://www.tdx.com.cn/products/data/data/vipdoc/shzsday.zip      # 上证指数日线
https://www.tdx.com.cn/products/data/data/vipdoc/szzsday.zip      # 深证指数日线
https://www.tdx.com.cn/products/data/data/vipdoc/ggtday.zip       # 港股通日线
https://www.tdx.com.cn/products/data/data/vipdoc/33day.zip        # 开放式基金日线
https://www.tdx.com.cn/products/data/data/vipdoc/38day.zip        # 宏观指标日线
```

ZIP 内为通达信专有 `.day` 二进制格式，可用 `mootdx.reader.Reader` 读取：

```python
from mootdx.reader import Reader

# 本地已下载解压的情况下
reader = Reader.factory(market='std', tdxdir='./vipdoc')
df = reader.daily(symbol='300661')
```

---

## 七、Tick 逐笔全量下载（不推荐）

TDX 数据中心提供每日 TIC 文件（所有股票合在一个 ~100MB 的 `.htc` 文件）：

```
https://www.tdx.com.cn/products/data/data/g3tic/20260506.zip   # 三代TIC
https://www.tdx.com.cn/products/data/data/g4tic/20260506.zip   # 四代TIC
```

**不推荐这种方式**，因为：
- 单个文件 100MB，下载 + 解压慢
- HTC 格式需要额外解析
- 用 pytdx/mootdx 直接拉个股逐笔，又快又准

---

## 八、服务器列表（2026年5月实测可用）

```python
SERVERS = [
    ('180.153.18.170', 7709),   # 上海  ✅ 推荐，稳定
    ('119.147.212.81', 7709),   # 广东
    ('124.74.236.50', 7709),    # 上海
    ('218.75.126.89', 7709),    # 浙江
    ('125.39.80.41', 7709),     # 北京
    ('106.120.97.33', 7709),    # 北京
    ('61.135.142.82', 7709),    # 北京
]
```

---

## 九、完整示例：拉取指定股票今日全部逐笔

### 方式1：pytdx（推荐，稳定可控）

```python
from pytdx.hq import TdxHq_API

api = TdxHq_API()
api.connect('180.153.18.170', 7709)

def get_all_transactions(market, code):
    """拉取某只股票今日全部逐笔成交"""
    all_data = []
    offset = 0
    while True:
        batch = api.get_transaction_data(market, code, offset, 2000)
        if not batch or len(batch) == 0:
            break
        all_data.extend(batch)
        if len(batch) < 2000:
            break
        offset += 2000
    return api.to_df(all_data)

df = get_all_transactions(0, '300661')
print(f"总 {len(df)} 笔, 价格 {df['price'].min():.2f}~{df['price'].max():.2f}")

# 统计买卖方向
# buyorsell: 0=外盘(主动买), 1=内盘(主动卖), 2=中性, 8=集合竞价
real = df[df['vol'] > 0]
print(f"外盘: {len(real[real['buyorsell']==0])}笔, "
      f"内盘: {len(real[real['buyorsell']==1])}笔")
print(f"总成交量: {int(real['vol'].sum())}手")

api.disconnect()
```

### 方式2：mootdx（代码更短）

```python
from mootdx.quotes import Quotes

client = Quotes.factory(market='std')
df = client.transaction(symbol='300661', start=0, offset=8000)
```

---

## 十、获取全市场股票列表

```python
# pytdx
data = api.get_security_list(P.TDXParams.MARKET_SZ, 0)  # 0=从第0只开始
# 返回包含所有深圳股票的列表（需要循环 offset+=500 取全部）
```

---

## 十一、总结对比

| 功能 | pytdx | mootdx |
|---|---|---|
| 安装大小 | 小 | 偏大（带 tdxpy 依赖） |
| 上手难度 | 中等 | 简单 |
| 自动找服务器 | 否 | 是（慢3-5秒） |
| 自动识别市场 | 否，需手动传 0/1 | 是 |
| 返回格式 | list→自转DataFrame | pandas DataFrame |
| 稳定性 | 高，底层协议稳定 | 中，依赖较新但维护少 |
| 代码量 | 略多 | 少 |
| 历史逐笔支持 | ✅ | ✅ |
| 分时数据 | ✅ | ✅ |
| 财务数据 | ✅ (get_finance) | ✅ (Affair) |
| `pip install` | pytdx | mootdx[all] |

**我的建议**: 
- **新项目** → 用 `mootdx` 快速原型，API 省心
- **生产环境** → 用 `pytdx`，控制力更强、依赖更少
- **两者切换成本很低**，底层都是通达信协议，接口名一一对应

---

> 以上数据来源: 通达信数据中心 https://www.tdx.com.cn/article/datacenter.html
> 逐笔数据实时获取: 通过通达信行情服务器 TCP 协议
