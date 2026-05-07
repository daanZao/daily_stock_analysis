# 📊 A 股数据抓取与持久化全景文档

本文档阐述本项目如何抓取 A 股数据、抓取什么数据、在什么时机抓取与保存、数据库如何设计、多数据源如何对齐、以及已抓取的数据在什么时机被谁消费。

> 💡 本文档面向需要理解数据链路、进行二次开发或排查数据问题的开发者。

---

## 目录

- [数据链路总览](#数据链路总览)
- [抓取了什么数据](#抓取了什么数据)
- [在什么时机抓取](#在什么时机抓取)
- [数据源与故障切换](#数据源与故障切换)
- [多数据源对齐策略](#多数据源对齐策略)
- [数据保存标准](#数据保存标准)
- [数据库设计](#数据库设计)
- [如何使用已抓取的数据](#如何使用已抓取的数据)
- [配置参考](#配置参考)

---

## 数据链路总览

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              收盘作业 (Closing Operations)                     │
│  main.py run_full_analysis()                                                │
│                                                                             │
│   ┌─────────────────┐    ┌──────────────────┐    ┌─────────────────────┐   │
│   │ 1. 个股分析      │ -> │ 2. 市场数据同步   │ -> │ 3. 大盘复盘+通知     │   │
│   │ pipeline.run()   │    │ MarketDataSync   │    │ run_market_review() │   │
│   └─────────────────┘    └──────────────────┘    └─────────────────────┘   │
│            │                       │                       │                │
│            ▼                       ▼                       ▼                │
│   ┌─────────────────┐    ┌──────────────────┐    ┌─────────────────────┐   │
│   │ DataFetcherManager│    │ DataFetcherManager│    │ DataFetcherManager  │   │
│   │ get_daily_data()  │    │ get_main_indices()│    │ get_market_stats()  │   │
│   │ get_minutely_data()│   │ get_sector_rankings│   │ get_sector_rankings │   │
│   │ get_fundamental() │    │                  │    │                     │   │
│   └─────────────────┘    └──────────────────┘    └─────────────────────┘   │
│            │                       │                       │                │
│            ▼                       ▼                       ▼                │
│   ┌─────────────────┐    ┌──────────────────┐    ┌─────────────────────┐   │
│   │ stock_daily      │    │ market_indices   │    │ 报告/通知/飞书文档   │   │
│   │ stock_minutely   │    │ market_boards    │    │                     │   │
│   │ financial_report │    │ zt_pool (P2)     │    │                     │   │
│   │ earnings_forecast│    │ lhb_* (P2)       │    │                     │   │
│   │ stock_basic_info │    │                  │    │                     │   │
│   └─────────────────┘    └──────────────────┘    └─────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘

                              ↓ 次日/后续使用 ↓

┌─────────────────────────────────────────────────────────────────────────────┐
│                           数据消费方                                          │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌────────────────┐  │
│  │ 个股技术分析  │  │ Agent K线工具 │  │ 自动回测      │  │ Web 历史报告   │  │
│  │ (pipeline)   │  │ (DB-first)   │  │ (backtest)   │  │ (history API)  │  │
│  └──────────────┘  └──────────────┘  └──────────────┘  └────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 抓取了什么数据

### 一、个股日线数据（核心，含预计算技术指标）

| 数据项 | 说明 | 来源 |
|--------|------|------|
| `date` | 交易日期 | 所有数据源 |
| `open` / `high` / `low` / `close` | OHLC | 所有数据源 |
| `volume` | 成交量（股） | 所有数据源 |
| `amount` | 成交额（元） | 所有数据源 |
| `pct_chg` | 涨跌幅（%） | 所有数据源 |
| `turnover` | 换手率 | 实时行情增强 |
| `volume_ratio` | 量比 | 实时行情增强 |
| `pe` / `pb` | 市盈率/市净率 | 实时行情增强 |
| `ma5/10/20/50/60/150/200` | 移动平均线 | 基于2年历史预计算 |
| `macd_dif/dea/bar/signal` | MACD(12,26,9) | 基于2年历史预计算 |
| `rsi_6/12/24/signal` | RSI(Wilder) | 基于2年历史预计算 |
| `kdj_k/d/j/signal` | KDJ(9,3,3) | 基于2年历史预计算 |
| `bias_ma5/10/20` | 乖离率 | 基于2年历史预计算 |
| `boll_mid/upper/lower` | 布林带(20,2) | 基于2年历史预计算 |
| `candle_pattern` | 单K线形态 | 基于2年历史预计算 |

**存储表**：`stock_daily`

> 注：`stock_daily` 除个股外，也存储 A 股主要指数的历史日线（如沪深300 `000300`），供 RS 相对强度、趋势对比等计算使用。指数数据通过 Tushare `index_daily` 接口获取，首次写入后由 `load_history_df` 自动缓存，不再重复走网络。

### 二、60分钟K线数据

| 数据项 | 说明 | 来源 |
|--------|------|------|
| `date` + `time` | 交易日期 + 时间(HHMMSS) | Baostock |
| `open` / `high` / `low` / `close` | OHLC | Baostock |
| `volume` / `amount` | 成交量/额 | Baostock |

**存储表**：`stock_minutely`

**注意**：Baostock 60分钟数据仅覆盖个股，指数（如创业板指399006）无60分钟数据。

### 三、个股基本信息

| 数据项 | 说明 | 来源 |
|--------|------|------|
| `name` | 股票名称 | Baostock / AkShare |
| `market` | 市场(sh/sz/bj/hk/us) | 自动识别 |
| `security_type` | 类型(stock/index/etf) | 自动识别 |
| `industry` | 所属行业 | Baostock |
| `list_date` | 上市日期 | Baostock |
| `total_shares` / `float_shares` | 总股本/流通股本 | Baostock |

**存储表**：`stock_basic_info`

### 四、基本面数据（季度）

| 数据项 | 说明 | 来源 |
|--------|------|------|
| **财报表** | 营收、净利润、毛利率、ROE、EPS 等季度指标 | AkShare（主源）→ Baostock fallback |
| **成长能力** | 营收同比、净利同比、净资产同比 | AkShare（主源）→ Baostock `query_growth_data` fallback |
| **业绩预告** | 预增/预减/扭亏/预亏及净利润变动区间 | AkShare（主源）→ Baostock `query_forecast_report` fallback |

**存储表**：`financial_report`, `earnings_forecast`

**多源 fallback 机制**：
`AkshareFundamentalAdapter` 内部优先尝试 AkShare 的多个接口候选（`stock_financial_abstract`、`stock_financial_analysis_indicator` 等）。当 AkShare 全部失败时，自动 fallback 到 Baostock：
- `query_profit_data` → ROE、毛利率、净利率、净利润
- `query_growth_data` → 净利润同比增长率
- `query_forecast_report` → 业绩预告摘要

Baostock fallback 的数据会回写到 `fundamental_context.earnings`，供 Pipeline SEPA 数据质量门禁和 LLM prompt 使用。

**注意**：`query_forecast_report` 并非所有公司都有数据（蓝筹通常不发预告），入库和 prompt 注入时均做空值兜底。

### 五、市场全景数据

| 数据项 | 说明 | 当前状态 |
|--------|------|----------|
| **主要指数** | 上证、深证、创业板、科创50、上证50、沪深300的日线快照 | Phase 1 ✅ 已接入 |
| **板块排行** | 行业板块涨跌幅排行（Top/Bottom） | Phase 1 ✅ 已接入 |
| **涨跌停池** | 涨停/跌停/炸板股票列表 | Phase 2 ⏳ 占位 |
| **强势股** | 连续上涨、放量突破等强势股 | Phase 2 ⏳ 占位 |
| **龙虎榜** | 上榜股票、席位明细、营业部排行 | Phase 2 ⏳ 占位 |
| **全市场证券列表** | 某日全部上市证券基本信息 | Phase 1 ✅ 已接入 (baostock) |

**存储表**：`market_indices`, `market_boards`, `zt_pool`, `strong_stocks`, `lhb_basic`, `lhb_stock_detail`, `lhb_stock_statistic`, `lhb_yyb_most`, `lhb_yyb_capital`

### 六、实时行情增强数据

在个股分析阶段，为每只股票额外获取一次实时行情（非历史 K 线）：

| 数据项 | 用途 | 默认优先级 |
|--------|------|-----------|
| 量比 (`volume_ratio`) | 判断放量/缩量 | tencent > akshare_sina > efinance > akshare_em |
| 换手率 (`turnover`) | 判断筹码活跃度 | 同上 |
| 市盈率/市净率 | 估值参考 | 同上 |

### 七、新闻与情报数据

| 数据项 | 说明 | 存储表 |
|--------|------|--------|
| 新闻搜索结果 | 多搜索引擎聚合的新闻摘要 | `news_intel` |
| 基本面快照 | 财务指标、业绩预期 | `fundamental_snapshot` |

### 八、分析结果与回测数据

| 数据项 | 说明 | 存储表 |
|--------|------|--------|
| 分析结果 | AI 分析结论、评分、操作建议 | `analysis_history` |
| 回测结果 | 历史分析准确率追踪 | `backtest_results`, `backtest_summaries` |

---

## 在什么时机抓取

### 时机一：收盘作业（Closing Operations）

**触发方式**：
- 本地运行 `python main.py` 或 `python main.py --market-review`
- GitHub Actions 定时任务（默认 18:00）
- 定时模式 `python main.py --schedule`

**执行顺序**（`main.py::run_full_analysis`）：

```
1. 交易日检查（跳过非交易日）
   ↓
2. 个股分析 pipeline.run()
   - 对每只股票：DB 断点续传检查 → 缺失则网络抓取 → 保存到 stock_daily
   - 并发数由 MAX_WORKERS 控制（默认 3）
   ↓
3. 市场数据同步 MarketDataSync.sync_all()  ← 本次新增
   - 主要指数 → market_indices
   - 板块排行 → market_boards
   - 涨跌停/强势股/龙虎榜 → Phase 2 占位
   - 失败不阻断后续流程
   ↓
4. 分析间隔延迟（ANALYSIS_DELAY，默认 0 秒）
   ↓
5. 大盘复盘 run_market_review()
   - 抓取指数、板块、市场统计 → LLM 生成复盘报告
   ↓
6. 通知推送 + 飞书文档生成
   ↓
7. 自动回测（BACKTEST_ENABLED=true 时）
```

### 时机二：Web/API 实时查询

**触发方式**：
- WebUI 点击"分析"
- API 调用 `/analyze/{code}`
- 机器人指令 `/analyze 600519`

**行为**：
- 优先读取 `stock_daily` 已有数据
- 若数据缺失或过期，实时调用 `DataFetcherManager.get_daily_data()`
- 新数据立即写入 DB

### 时机三：Agent 工具预热

**触发方式**：
- Pipeline Agent 模式分析前

**行为**：
- `_ensure_agent_history()` 检查 DB 中是否有至少 240 天历史
- 不足则通过网络抓取补全，写入 `stock_daily`
- 后续 Agent K 线工具改为 **DB-first**，避免重复 HTTP 请求

### 时机四：回测执行

**触发方式**：
- 收盘作业结束后自动运行（`BACKTEST_ENABLED=true`）
- 手动调用 `BacktestService.run_backtest()`

**行为**：
- 读取 `analysis_history` 中 N 天前的分析记录
- 对比 `stock_daily` 中的实际走势
- 计算准确率，写入 `backtest_results`

---

## 数据源与故障切换

### 数据源优先级

```
【配置了 TUSHARE_TOKEN】
Tushare (P0) → Efinance (P0) → Akshare (P1) → Pytdx (P2) → Baostock (P3) → Yfinance (P4)

【未配置 TUSHARE_TOKEN】
Efinance (P0) → Akshare (P1) → Pytdx (P2) → Baostock (P3) → Yfinance (P4)
```

### 各数据源能力矩阵

| 数据源 | 个股日线 | 60分钟 | 实时行情 | 指数行情 | 板块排行 | 全市场证券 | 成长/预告 | 特点 |
|--------|----------|--------|----------|----------|----------|------------|-----------|------|
| **Efinance** | ✅ | ❌ | ✅ | ✅ | ✅ | ❌ | ❌ | 东财数据最全，易被封 |
| **Akshare** | ✅ | ❌ | ✅ | ✅ | ✅ | ❌ | ❌ | 免费稳定，接口丰富 |
| **Tushare** | ✅ | ❌ | ❌ | ✅(index_daily) | ❌ | ❌ | ❌ | 需 Token，数据质量高 |
| **Pytdx** | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | 通达信，内网可用 |
| **Baostock** | ✅ | ✅(个股) | ❌ | ❌ | ❌ | ✅ | ✅(fallback) | 免费，需登录，有全市场列表；基本面数据作为 AkShare 失败后的 fallback |
| **Yfinance** | ✅ | ❌ | ❌ | ✅ | ❌ | ❌ | ❌ | 美股/港股兜底 |
| **TickFlow** | ❌ | ❌ | ❌ | ✅ | ❌ | ❌ | ❌ | 指数增强（可选） |

### 故障切换（Failover）

`DataFetcherManager` 实现自动切换：

1. 按优先级顺序尝试各数据源
2. 任一数据源失败（异常/超时/返回空），自动切换到下一个
3. 所有数据源失败时记录错误，不阻断单股分析（其他股票继续）
4. 熔断器：同一数据源连续失败多次后进入冷却期，避免无限重试

---

## 多数据源对齐策略

### 1. 代码标准化

所有数据源的股票代码在入口层统一标准化：

```python
from data_provider.base import normalize_stock_code, canonical_stock_code

# 输入: "SH600519" / "600519.SH" / "sh600519"
# 输出: "600519"
code = normalize_stock_code(raw_code)
```

### 2. 列名标准化

所有数据源返回的 DataFrame 必须映射到统一列名：

```python
STANDARD_COLUMNS = ['date', 'open', 'high', 'low', 'close', 'volume', 'amount', 'pct_chg']
```

各 Fetcher 的 `_normalize_data()` 方法负责：
- 列名映射（如 `pctChg` → `pct_chg`）
- 数值类型转换（字符串 → float）
- 添加 `code` 列

### 3. 数据去重与 UPSERT

数据库写入使用 **SQLite UPSERT**（`INSERT OR REPLACE`）：

```python
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

stmt = sqlite_insert(table).values(records)
stmt = stmt.on_conflict_do_update(index_elements=unique_cols, set_=update_dict)
```

- `stock_daily`：按 `(code, date)` 去重
- `market_indices`：按 `(trade_date, index_code)` 去重
- `market_boards`：按 `(trade_date, board_type, board_name, source)` 去重

### 4. 断点续传

`pipeline.py` 在抓取每只股票前检查：

```python
if self.db.has_today_data(code, target_date):
    logger.info(f"{code} {target_date} 数据已存在，跳过获取")
    return True
```

支持场景：
- 定时任务中途失败重启，已抓取的不再重复抓取
- 多进程/多线程并发时避免重复写入冲突

---

## 数据保存标准

### 存储介质

- **SQLite**（默认路径：`./data/stock_analysis.db`）
- WAL 模式已启用（`SQLITE_WAL_ENABLED=true`）
- 写入重试：指数退避，最多 3 次

### 技术指标预计算策略

所有技术指标在数据抓取阶段基于**完整历史**预计算并落表，而非分析时现场计算：

```
数据抓取(2年日线) → _calculate_indicators(完整历史) → StockDaily(含指标)
                                      ↓
                              pipeline(直读所有指标)
                                      ↓
                              analyzer(看到完整技术面)
```

**关键约束**：
- `min_periods` 严格等于窗口大小（如 `rolling(26, min_periods=26)`），前 N 天指标为空（NaN），宁可缺失也不给假值
- 日线指标基于约 485 天（2年）历史计算，确保 EMA 类指标（MACD/KDJ）收敛到与市场软件一致
- 前 60 天 MA60 为空、前 26 天 MACD 为空——这是正确的行为

### 数据精度

```python
# float 类型统一保留 4 位小数
def _clean_record(record):
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return None
        return round(v, 4)
```

### 时间标准

- 交易日期统一使用 `date` 类型（无时区）
- 创建时间使用 `datetime.now()`（本地时间）
- 跨时区场景由调用方处理

### CSV 导出

全市场证券列表支持 CSV 导出：

```python
from data_provider.baostock_fetcher import BaostockFetcher

fetcher = BaostockFetcher()
path = fetcher.get_all_securities_csv("2026-04-28")
# 输出: ./data/all_securities_2026-04-28.csv
```

CSV 包含列：`code`, `code_name`, `ipo_date`, `out_date`, `type`, `status`, `type_name`, `status_name`, `pure_code`

---

## 数据库设计

### ER 关系简图

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│stock_basic_info│   │  stock_daily  │     │ stock_minutely│
│  (基本信息)   │     │  (个股日线)   │     │ (60分钟K线)  │
├──────────────┤     ├──────────────┤     ├──────────────┤
│ code (UQ)    │◄────│ code + date  │     │ code+date+time│
│ name/industry│     │ (UQ)         │     │ (UQ)         │
│ list_date    │     │ OHLCV+指标   │     │ OHLCV        │
└──────────────┘     └──────┬───────┘     └──────────────┘
                            │
       ┌────────────────────┼────────────────────┐
       ▼                    ▼                    ▼
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│financial_report│   │earnings_forecast│   │analysis_history│
│  (季度财报)   │     │  (业绩预告)   │     │  (分析结果)   │
├──────────────┤     ├──────────────┤     ├──────────────┤
│ code+date+type│    │ code+forecast_date│  │ code + date  │
│ revenue/roe  │     │ forecast_type │    │ report_type  │
│ gross_margin │     │ chg_min/max   │    │ sentiment    │
└──────────────┘     └──────────────┘     └──────────────┘

┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│ market_indices│     │ market_boards │     │backtest_results│
│  (主要指数)   │     │  (板块排行)   │     │  (回测结果)   │
├──────────────┤     ├──────────────┤     ├──────────────┤
│ trade_date   │     │ trade_date   │     │ analysis_id  │
│ index_code   │     │ board_type   │     │ eval_date    │
│ (UQ)         │     │ board_name   │     │ actual_return│
│ latest_price │     │ source (UQ)  │     │ accuracy     │
└──────────────┘     └──────────────┘     └──────────────┘

┌──────────────┐
│  news_intel  │
│  (新闻情报)   │
├──────────────┤
│ code + date  │
│ headline     │
│ url/summary  │
└──────────────┘
```

### 核心表结构

#### `stock_daily` — 个股日线（含预计算技术指标）

```sql
CREATE TABLE stock_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code VARCHAR(16) NOT NULL,
    date DATE NOT NULL,
    open FLOAT, high FLOAT, low FLOAT, close FLOAT,
    volume BIGINT, amount FLOAT, pct_chg FLOAT,
    -- 移动平均线
    ma5 FLOAT, ma10 FLOAT, ma20 FLOAT, ma50 FLOAT, ma60 FLOAT, ma150 FLOAT, ma200 FLOAT,
    volume_ratio FLOAT,
    -- MACD(12,26,9)
    macd_dif FLOAT, macd_dea FLOAT, macd_bar FLOAT, macd_signal VARCHAR(8),
    -- RSI(Wilder's smoothing, 6/12/24)
    rsi_6 FLOAT, rsi_12 FLOAT, rsi_24 FLOAT, rsi_signal VARCHAR(8),
    -- KDJ(9,3,3)
    kdj_k FLOAT, kdj_d FLOAT, kdj_j FLOAT, kdj_signal VARCHAR(8),
    -- 乖离率
    bias_ma5 FLOAT, bias_ma10 FLOAT, bias_ma20 FLOAT,
    -- 布林带(20,2)
    boll_mid FLOAT, boll_upper FLOAT, boll_lower FLOAT,
    -- K线形态
    candle_pattern VARCHAR(32),
    data_source VARCHAR(50),
    UNIQUE(code, date)
);
```

#### `stock_minutely` — 60分钟K线

```sql
CREATE TABLE stock_minutely (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code VARCHAR(16) NOT NULL,
    date DATE NOT NULL,
    time VARCHAR(8) NOT NULL,              -- HHMMSS, e.g. "103000"
    open FLOAT, high FLOAT, low FLOAT, close FLOAT,
    volume FLOAT, amount FLOAT, pct_chg FLOAT,
    data_source VARCHAR(50),
    UNIQUE(code, date, time)
);
```

#### `stock_basic_info` — 个股基本信息

```sql
CREATE TABLE stock_basic_info (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code VARCHAR(16) NOT NULL UNIQUE,
    name VARCHAR(128),
    market VARCHAR(8),                     -- sh / sz / bj / hk / us
    security_type VARCHAR(16),             -- stock / index / etf
    industry VARCHAR(128),
    list_date DATE,
    status VARCHAR(16),                    -- listed / delisted / suspended
    total_shares BIGINT,
    float_shares BIGINT,
    data_source VARCHAR(50),
    created_at DATETIME,
    updated_at DATETIME
);
```

#### `financial_report` — 季度财报表

```sql
CREATE TABLE financial_report (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code VARCHAR(16) NOT NULL,
    report_date DATE NOT NULL,
    report_type VARCHAR(10),               -- Q1 / Q2 / Q3 / annual
    revenue FLOAT,
    revenue_yoy FLOAT,                     -- 营收同比增长率(%)
    net_profit_parent FLOAT,
    net_profit_yoy FLOAT,                  -- 归母净利同比(%)
    gross_margin FLOAT,                    -- 毛利率(%)
    net_margin FLOAT,                      -- 净利率(%)
    debt_ratio FLOAT,                      -- 资产负债率(%)
    operating_cash_flow FLOAT,
    roe FLOAT,
    roe_diluted FLOAT,
    eps FLOAT,
    announced_date DATE,
    data_source VARCHAR(50),
    UNIQUE(code, report_date, report_type)
);
```

#### `earnings_forecast` — 业绩预告

```sql
CREATE TABLE earnings_forecast (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code VARCHAR(16) NOT NULL,
    forecast_date DATE NOT NULL,
    report_date DATE,
    forecast_type VARCHAR(20),             -- 预增/预减/扭亏/预亏/...
    forecast_abstract TEXT,
    chg_min FLOAT,                         -- 净利润变动下限(%)
    chg_max FLOAT,                         -- 净利润变动上限(%)
    net_profit_min FLOAT,                  -- 净利润下限(万元)
    net_profit_max FLOAT,                  -- 净利润上限(万元)
    data_source VARCHAR(50),
    UNIQUE(code, forecast_date)
);
```

#### `market_indices` — 主要指数

```sql
CREATE TABLE market_indices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date DATE NOT NULL,
    index_code VARCHAR(32) NOT NULL,
    index_name VARCHAR(128) NOT NULL,
    latest_price FLOAT, change_percent FLOAT, change_amount FLOAT,
    volume BIGINT, amount FLOAT, amplitude FLOAT,
    high FLOAT, low FLOAT, open FLOAT, pre_close FLOAT, volume_ratio FLOAT,
    UNIQUE(trade_date, index_code)
);
```

#### `market_boards` — 板块排行

```sql
CREATE TABLE market_boards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date DATE NOT NULL,
    board_type VARCHAR(16) NOT NULL,   -- 'industry' / 'concept'
    board_code VARCHAR(32),
    board_name VARCHAR(128) NOT NULL,
    latest_price FLOAT, change_percent FLOAT,
    source VARCHAR(16) DEFAULT 'em',    -- 'em' / 'ths'
    UNIQUE(trade_date, board_type, board_name, source)
);
```

#### `zt_pool` / `strong_stocks` / `lhb_*` — Phase 2 表

结构已完成，详见 `src/storage.py`。当前为占位状态，待接入 akshare 专属接口后激活写入。

---

## 如何使用已抓取的数据

### 1. 个股分析时（Pipeline）

```python
# pipeline.py 内部逻辑
# 1. 先检查 DB
df = self.db.get_data_range(code, start_date, end_date)

# 2. 缺失时网络抓取（FetcherManager fallback）
df, source = self.fetcher_manager.get_daily_data(code, days=30)
self.db.save_daily_data(df, code, source)
```

### 2. Agent K 线工具（DB-first）

```python
from src.services.history_loader import load_history_df

# 优先读 DB，不足时自动 fallback 到网络
# fallback 成功后自动写入 stock_daily，后续读取即命中缓存
df = load_history_df(stock_code, days=60)
```

**优化效果**：单只股票在 Agent 模式下减少约 45 次重复 HTTP 请求。

### 3. 大盘复盘（Market Review）

```python
# 当前：直接从网络抓取
indices = fetcher_manager.get_main_indices(region="cn")
stats = fetcher_manager.get_market_stats()
top, bottom = fetcher_manager.get_sector_rankings(n=5)

# 未来演进：可从 market_indices / market_boards 读取，减少网络请求
```

### 4. 回测（Backtest）

```python
from src.services.backtest_service import BacktestService

service = BacktestService()
stats = service.run_backtest(
    eval_window_days=10,   # 评估未来 10 天走势
    min_age_days=14,       # 只回测 14 天前的分析
)
```

回测逻辑：
1. 读取 `analysis_history` 获取历史分析记录
2. 读取 `stock_daily` 获取分析日后实际走势
3. 计算准确率，写入 `backtest_results`

### 5. Web 历史报告查询

```python
from src.repositories.stock_repo import StockRepository

repo = StockRepository()
rows = repo.get_range(code, start_date, end_date)
```

### 6. 市场数据仓库（本次新增）

```python
from src.repositories.market_data_repo import MarketDataRepository
from datetime import date

repo = MarketDataRepository()

# 查某日指数
repo.get_indices_by_date(date(2026, 4, 28))

# 查某日板块
repo.get_boards_by_date(date(2026, 4, 28), source='em')

# 查某指数历史
repo.get_index_history('sh000001', date(2026, 4, 1), date(2026, 4, 28))

# 查数据缺失日期（回填用）
from src.storage import MarketIndexData
missing = repo.get_missing_dates(
    MarketIndexData, 'trade_date',
    date(2026, 4, 1), date(2026, 4, 28),
    trade_dates=[...]
)
```

---

## 配置参考

### 环境变量

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `DATABASE_PATH` | SQLite 数据库路径 | `./data/stock_analysis.db` |
| `SQLITE_WAL_ENABLED` | 启用 WAL 模式 | `true` |
| `SQLITE_BUSY_TIMEOUT_MS` | 等锁超时 | `5000` |
| `MARKET_DATA_SYNC_ENABLED` | 收盘市场数据同步开关 | `true` |
| `MARKET_REVIEW_ENABLED` | 大盘复盘开关 | `true` |
| `BACKTEST_ENABLED` | 自动回测开关 | `true` |
| `MAX_WORKERS` | 个股分析并发数 | `3` |

### 数据源优先级配置

```bash
# 个股日线优先级（数字越小越优先）
EFINANCE_PRIORITY=0
AKSHARE_PRIORITY=1
TUSHARE_PRIORITY=2
PYTDX_PRIORITY=2
BAOSTOCK_PRIORITY=3
YFINANCE_PRIORITY=4

# 实时行情优先级
REALTIME_SOURCE_PRIORITY=tencent,akshare_sina,efinance,akshare_em
```

---

## 附录：文件索引

| 文件 | 职责 |
|------|------|
| `data_provider/base.py` | `DataFetcherManager` 策略管理器、技术指标预计算 |
| `data_provider/akshare_fetcher.py` | Akshare 数据源（指数、板块、市场统计、财报） |
| `data_provider/baostock_fetcher.py` | Baostock 数据源（个股日线、60分钟K线、成长能力、业绩预告、全市场证券列表） |
| `data_provider/fundamental_adapter.py` | AkShare 基本面数据适配器（财报、成长、分红、机构、资金流向），含 Baostock fallback |
| `data_provider/efinance_fetcher.py` | Efinance 数据源（个股日线、实时行情） |
| `src/core/pipeline.py` | 个股分析主流程（含断点续传） |
| `src/core/market_data_sync.py` | 收盘市场数据同步 |
| `src/repositories/market_data_repo.py` | 市场全景数据仓库 |
| `src/repositories/stock_repo.py` | 个股数据仓库 |
| `src/services/history_loader.py` | DB-first K 线加载器 |
| `src/services/backtest_service.py` | 回测服务 |
| `src/storage.py` | 全部 ORM 模型定义（日线/60分钟/基本信息/财报/业绩预告） |
| `src/analyzer.py` | LLM 分析器（prompt 构建，含技术指标/基本面数据注入） |
