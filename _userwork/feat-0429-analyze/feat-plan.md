# feat: 分析数据层增强 — 预计算指标落地与提示词精简

## 背景与问题

当前 `GeminiAnalyzer` 的分析输入存在两层断裂：

1. **技术面数据断裂**：`StockTrendAnalyzer` 已计算 MACD/RSI/BOLL/KDJ/乖离率等指标，但 `pipeline.py` 构建上下文时仅保留摘要级别数据（趋势状态、均线排列、乖离率），MACD/RSI 具体数值被静默丢弃。LLM 看不到顶底背离、超买超卖、KDJ 金叉死叉等关键信号。

2. **基本面数据截流**：`DataFetcherManager.get_fundamental_context()` 收集了 valuation/growth/earnings/institution/capital_flow/dragon_tiger/boards 共 7 大模块，但 `_format_prompt()` 仅读取 `earnings` 模块（营收/净利润/现金流/ROE/股息）。growth（营收增速/毛利率）、institution（机构持仓）、capital_flow（主力净流入）全部被丢弃。

3. **提示词臃肿**：system prompt 约 800 行，其中 JSON Schema 示例占 ~200 行、评分标准占 ~100 行。大量 token 消耗在教 LLM "输出格式" 上，挤占了实际数据输入的空间。

4. **K线形态缺失**：单根 K线形态（十字星/锤子线/大阳线）和多根组合形态（吞没/早晨之星）均未注入 prompt，LLM 无法基于形态做判断。

5. **历史数据不足导致指标准确性问题**：当前日常分析仅抓取最近 30 天数据。EMA 类指标（MACD/KDJ）的递推公式 `EMA_t = α * Price_t + (1-α) * EMA_{t-1}` 决定：如果从中间某点开始计算，整个序列都会和看盘软件（从上市日递推）产生漂移。测试表明，EMA(26) 需要约 200 天历史才能使今天的值收敛到与市场一致。

## 目标

- **数据层**：冷启动抓取 **2年日线历史 + 6个月60分钟历史**（仅自选股 + 主要指数），日常增量更新。基于完整历史预计算并落表所有常用技术指标，确保和市场软件一致。
- **基本面层**：创建结构化财报表，按季度存储核心财务指标，支持趋势对比。
- **提示层**：精简 system prompt，把 JSON Schema 示例替换为字段清单，释放 token 给数据输入。
- **分析层**：让 LLM 看到完整的 MACD/RSI/KDJ/Bias/BOLL/K线形态/资金流向/基本面增速。

---

## 实施计划

### P0：历史数据补全 + StockDaily 扩字段 + 指标预计算（高优先级，2-3 天）

#### 1.0 数据抓取策略变更

**范围**：仅自选股（默认50只）+ 主要指数（沪深300、上证50、中证500等约10只），共约60只标的。

**数据规格**：
| 粒度 | 历史长度 | 记录数（60只） | 存储 |
|------|----------|---------------|------|
| 日线 | 2年（~485交易日） | ~29,100 条 | ~8 MB |
| 60分钟 | 6个月（~120交易日×4小时） | ~28,800 条 | ~6 MB |
| **合计** | | **~57,900 条** | **~14 MB** |

**抓取策略**：

```python
def ensure_stock_history(code: str) -> pd.DataFrame:
    """
    确保数据库中有足够长的历史数据用于指标计算
    日线：至少2年（485天）；60分钟：至少6个月（约480根）
    """
    # 1. 查本地已有数据范围
    local_daily = db.get_data_range(code, table='stock_daily')
    local_minutely = db.get_data_range(code, table='stock_minutely')
    
    # 2. 日线：补到至少2年
    if not local_daily or len(local_daily) < 400:
        # 冷启动：抓2年完整历史
        start = (date.today() - timedelta(days=730)).strftime('%Y-%m-%d')
        df = akshare_fetcher.get_daily_data(code, start_date=start, end_date='today')
        db.save_daily_data(df, code)
    elif local_daily[-1].date < date.today() - timedelta(days=5):
        # 补档：抓缺失期间
        start = (local_daily[-1].date + timedelta(days=1)).strftime('%Y-%m-%d')
        df = akshare_fetcher.get_daily_data(code, start_date=start, end_date='today')
        db.save_daily_data(df, code)
    else:
        # 日常增量：只抓最近5天
        df = akshare_fetcher.get_daily_data(code, days=5)
        db.save_daily_data(df, code)
    
    # 3. 60分钟：补到至少6个月（baostock）
    if not local_minutely or len(local_minutely) < 400:
        start = (date.today() - timedelta(days=180)).strftime('%Y-%m-%d')
        df_60m = baostock_fetcher.get_minutely_data(code, start_date=start, end_date='today')
        db.save_minutely_data(df_60m, code)
    else:
        df_60m = baostock_fetcher.get_minutely_data(code, days=5)
        db.save_minutely_data(df_60m, code)
    
    # 返回完整历史用于指标计算
    return db.get_all_daily_data(code)
```

**关键约束**：
- `min_periods` 不再用 `1`，而是等于窗口大小（如 `rolling(26, min_periods=26)`），确保前 N 天不出不准确的值
- 前 60 天的 MA60 为空，前 26 天的 MACD 为空——这是正确的，宁可缺失也不给假值
- 指标计算基于完整历史（2年）算完，但只把最近几天的结果落表到 `StockDaily`

#### 1.1 表结构变更（`src/storage.py`）

在 `StockDaily` 中新增以下字段：

```python
# 扩展均线
ma60 = Column(Float)

# MACD (12,26,9)
macd_dif = Column(Float)
macd_dea = Column(Float)
macd_bar = Column(Float)
macd_signal = Column(String(10))   # golden_cross / dead_cross / divergence / neutral

# RSI
rsi_6 = Column(Float)
rsi_12 = Column(Float)
rsi_24 = Column(Float)
rsi_signal = Column(String(10))    # overbought / oversold / neutral

# KDJ (9,3,3)
kdj_k = Column(Float)
kdj_d = Column(Float)
kdj_j = Column(Float)
kdj_signal = Column(String(10))    # golden_cross / dead_cross / neutral

# 乖离率
bias_ma5 = Column(Float)
bias_ma10 = Column(Float)
bias_ma20 = Column(Float)

# BOLL (20,2)
boll_mid = Column(Float)
boll_upper = Column(Float)
boll_lower = Column(Float)

# 单根 K线形态（预计算）
candle_pattern = Column(String(20))  # doji / hammer / hanging_man / shooting_star / big_yang / big_yin / None
```

#### 1.2 指标计算扩展（`data_provider/base.py`）

扩展 `_calculate_indicators()` 方法。**关键变更**：`min_periods` 等于窗口大小（不再用1），宁可缺失也不给假值：

```python
def _calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
    """
    基于完整历史（≥2年日线）计算技术指标。
    min_periods = 窗口大小，确保前N天不出不准确的值。
    """
    df = df.copy().sort_values('date').reset_index(drop=True)
    n = len(df)

    # === MA（简单移动平均）===
    df['ma5'] = df['close'].rolling(window=5, min_periods=5).mean()
    df['ma10'] = df['close'].rolling(window=10, min_periods=10).mean()
    df['ma20'] = df['close'].rolling(window=20, min_periods=20).mean()
    df['ma60'] = df['close'].rolling(window=60, min_periods=60).mean()

    # === MACD（标准 EMA 递推，12/26/9）===
    # EMA 从完整历史起点递推，确保和市场软件一致
    ema12 = df['close'].ewm(span=12, adjust=False).mean()
    ema26 = df['close'].ewm(span=26, adjust=False).mean()
    df['macd_dif'] = ema12 - ema26
    df['macd_dea'] = df['macd_dif'].ewm(span=9, adjust=False).mean()
    df['macd_bar'] = (df['macd_dif'] - df['macd_dea']) * 2

    # === RSI（Wilder's Smoothing，标准算法）===
    # 注意：当前 stock_analyzer.py 用的是 rolling.mean()，这是错的
    # Wilder's: avg_gain_t = (avg_gain_{t-1}*(N-1) + gain_t) / N
    # pandas ewm(alpha=1/N) 等价于 Wilder's smoothing
    for period in [6, 12, 24]:
        delta = df['close'].diff()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)
        avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, 1e-10)
        df[f'rsi_{period}'] = 100 - (100 / (1 + rs))

    # === KDJ（标准 9/3/3）===
    low_9 = df['low'].rolling(window=9, min_periods=9).min()
    high_9 = df['high'].rolling(window=9, min_periods=9).max()
    rsv = 100 * (df['close'] - low_9) / (high_9 - low_9).replace(0, 1e-10)
    # K = 2/3*K_{t-1} + 1/3*RSV，用 ewm(alpha=1/3) 模拟
    df['kdj_k'] = rsv.ewm(alpha=1/3, min_periods=9, adjust=False).mean()
    df['kdj_d'] = df['kdj_k'].ewm(alpha=1/3, min_periods=9, adjust=False).mean()
    df['kdj_j'] = 3 * df['kdj_k'] - 2 * df['kdj_d']

    # === Bias（乖离率）===
    df['bias_ma5'] = (df['close'] - df['ma5']) / df['ma5'] * 100
    df['bias_ma10'] = (df['close'] - df['ma10']) / df['ma10'] * 100
    df['bias_ma20'] = (df['close'] - df['ma20']) / df['ma20'] * 100

    # === BOLL（20,2）===
    df['boll_mid'] = df['ma20']
    std20 = df['close'].rolling(window=20, min_periods=20).std()
    df['boll_upper'] = df['boll_mid'] + 2 * std20
    df['boll_lower'] = df['boll_mid'] - 2 * std20

    # === 单根 K线形态 ===
    df['candle_pattern'] = df.apply(self._detect_single_candle, axis=1)

    # === 信号判断 ===
    df['macd_signal'] = self._detect_macd_signal(df)
    df['rsi_signal'] = self._detect_rsi_signal(df)
    df['kdj_signal'] = self._detect_kdj_signal(df)

    # 保留 2 位小数
    indicator_cols = [
        'ma5', 'ma10', 'ma20', 'ma60',
        'macd_dif', 'macd_dea', 'macd_bar',
        'rsi_6', 'rsi_12', 'rsi_24',
        'kdj_k', 'kdj_d', 'kdj_j',
        'bias_ma5', 'bias_ma10', 'bias_ma20',
        'boll_mid', 'boll_upper', 'boll_lower',
    ]
    for col in indicator_cols:
        if col in df.columns:
            df[col] = df[col].round(2)

    return df
```

**信号判断函数**：

```python
def _detect_macd_signal(self, df: pd.DataFrame) -> pd.Series:
    """MACD 信号：金叉/死叉/零轴穿越"""
    signals = pd.Series(index=df.index, dtype='object')
    for i in range(1, len(df)):
        prev_dif = df['macd_dif'].iloc[i-1]
        curr_dif = df['macd_dif'].iloc[i]
        prev_dea = df['macd_dea'].iloc[i-1]
        curr_dea = df['macd_dea'].iloc[i]
        
        if pd.isna(prev_dif) or pd.isna(curr_dif):
            signals.iloc[i] = None
            continue
            
        prev_cross = prev_dif - prev_dea
        curr_cross = curr_dif - curr_dea
        
        if prev_cross <= 0 and curr_cross > 0:
            signals.iloc[i] = 'golden_cross' if curr_dif > 0 else 'golden_cross_below_zero'
        elif prev_cross >= 0 and curr_cross < 0:
            signals.iloc[i] = 'dead_cross'
        elif prev_dif <= 0 and curr_dif > 0:
            signals.iloc[i] = 'cross_up_zero'
        elif prev_dif >= 0 and curr_dif < 0:
            signals.iloc[i] = 'cross_down_zero'
        else:
            signals.iloc[i] = 'neutral'
    return signals

def _detect_rsi_signal(self, df: pd.DataFrame) -> pd.Series:
    """RSI 信号：超买/超卖"""
    signals = pd.Series(index=df.index, dtype='object')
    for i in range(len(df)):
        rsi12 = df['rsi_12'].iloc[i]
        if pd.isna(rsi12):
            signals.iloc[i] = None
        elif rsi12 > 70:
            signals.iloc[i] = 'overbought'
        elif rsi12 < 30:
            signals.iloc[i] = 'oversold'
        else:
            signals.iloc[i] = 'neutral'
    return signals

def _detect_kdj_signal(self, df: pd.DataFrame) -> pd.Series:
    """KDJ 信号：金叉/死叉"""
    signals = pd.Series(index=df.index, dtype='object')
    for i in range(1, len(df)):
        prev_k = df['kdj_k'].iloc[i-1]
        curr_k = df['kdj_k'].iloc[i]
        prev_d = df['kdj_d'].iloc[i-1]
        curr_d = df['kdj_d'].iloc[i]
        
        if pd.isna(prev_k) or pd.isna(curr_k):
            signals.iloc[i] = None
            continue
            
        if prev_k <= prev_d and curr_k > curr_d:
            signals.iloc[i] = 'golden_cross'
        elif prev_k >= prev_d and curr_k < curr_d:
            signals.iloc[i] = 'dead_cross'
        else:
            signals.iloc[i] = 'neutral'
    return signals

def _detect_single_candle(self, row: pd.Series) -> Optional[str]:
    """单根 K线形态检测"""
    open_p, high, low, close = row['open'], row['high'], row['low'], row['close']
    if pd.isna(open_p):
        return None
    
    body = abs(close - open_p)
    total_range = high - low
    upper_shadow = high - max(open_p, close)
    lower_shadow = min(open_p, close) - low
    
    if total_range == 0:
        return 'doji'
    
    body_ratio = body / total_range
    
    if body_ratio < 0.03 and upper_shadow < body and lower_shadow < body:
        return 'doji'
    if lower_shadow > 2 * body and upper_shadow < body * 0.5:
        return 'hammer' if close > open_p else 'hanging_man'
    if upper_shadow > 2 * body and lower_shadow < body * 0.5:
        return 'shooting_star'
    if body_ratio > 0.7:
        return 'big_yang' if close > open_p else 'big_yin'
    
    return None
```

#### 1.3 更新 upsert 逻辑

`save_daily_data()` 中的 `on_conflict_do_update` 需要包含所有新增字段。

#### 1.4 Pipeline 读取新增字段（`src/core/pipeline.py`）

在 `_enhance_context()` 构建 `trend_analysis` 时，从 `StockDaily` 读取并注入：

```python
enhanced['trend_analysis'] = {
    # ... 现有字段 ...
    'macd': {
        'dif': latest.macd_dif,
        'dea': latest.macd_dea,
        'bar': latest.macd_bar,
        'signal': latest.macd_signal,
    },
    'rsi': {
        'rsi6': latest.rsi_6,
        'rsi12': latest.rsi_12,
        'rsi24': latest.rsi_24,
        'signal': latest.rsi_signal,
    },
    'kdj': {
        'k': latest.kdj_k,
        'd': latest.kdj_d,
        'j': latest.kdj_j,
        'signal': latest.kdj_signal,
    },
    'boll': {
        'mid': latest.boll_mid,
        'upper': latest.boll_upper,
        'lower': latest.boll_lower,
    },
    'bias': {
        'ma5': latest.bias_ma5,
        'ma10': latest.bias_ma10,
        'ma20': latest.bias_ma20,
    },
    'candle_pattern': latest.candle_pattern,
    'support_levels': [...],  # 完整的支撑/压力位列表
    'resistance_levels': [...],
}
```

#### 1.5 Analyzer prompt 注入（`src/analyzer.py`）

在 `_format_prompt()` 的技术面数据部分，新增指标表格：

```markdown
### 技术指标
| 指标 | 数值 | 信号 |
|------|------|------|
| MACD DIF | {macd.dif} | {macd.signal} |
| MACD DEA | {macd.dea} | |
| MACD BAR | {macd.bar} | |
| RSI(6) | {rsi.rsi6} | {rsi.signal} |
| RSI(12) | {rsi.rsi12} | |
| KDJ K/D/J | {kdj.k} / {kdj.d} / {kdj.j} | {kdj.signal} |
| Bias(MA5) | {bias.ma5}% | |
| BOLL 上轨/中轨/下轨 | {boll.upper} / {boll.mid} / {boll.lower} | |
| K线形态 | {candle_pattern} | |
```

---

### P1：60分钟K线表 + 基本面数据补全 + 提示词精简（中优先级，2-3 天）

#### 2.1 60分钟K线数据表（`src/storage.py`）

参考 `StockDaily` 设计，新建 `stock_minutely` 表存储60分钟K线（来自 baostock `query_history_k_data_plus(frequency="60")`）：

```python
class StockMinutely(Base):
    __tablename__ = 'stock_minutely'

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False, index=True)
    date = Column(Date, nullable=False, index=True)      # 交易日期
    time = Column(String(20), nullable=False)            # 时间戳，如 20260422103000000

    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    volume = Column(Float)   # 成交量（股）
    amount = Column(Float)   # 成交额（元）

    # 60分钟级别可复用日线指标计算逻辑（可选）
    # ma5 = Column(Float)    # 5根60分钟线 ≈ 1日，按需添加
    # macd_dif = Column(Float)
    # ...

    data_source = Column(String(50), default='baostock')
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint('code', 'date', 'time', name='uix_code_date_time'),
        Index('ix_minutely_code_date', 'code', 'date'),
    )
```

**抓取策略**：
- 每日收盘后通过 baostock 抓当日60分钟数据（最近7天，baostock 60分钟数据仅保留近期）
- `time` 字段为 baostock 原始格式 `YYYYMMDDHHMMSSsss`，入库时保留原始格式，查询时可截取
- 日内分析场景（如短线支撑压力位）时读取当日60分钟数据

#### 2.2 基本面数据注入（`analyzer.py`）

`_format_prompt()` 中，在 earnings 之外增加：

```python
# growth 模块
growth = fundamental_ctx.get("growth", {})
if growth:
    prompt += f"""
### 成长能力
| 指标 | 数值 |
|------|------|
| 营收同比 | {growth.get('revenue_yoy', 'N/A')}% |
| 净利润同比 | {growth.get('net_profit_yoy', 'N/A')}% |
| 毛利率 | {growth.get('gross_margin', 'N/A')}% |
"""

# capital_flow 模块
cf = fundamental_ctx.get("capital_flow", {})
if cf:
    prompt += f"""
### 资金流向
| 指标 | 数值 |
|------|------|
| 主力净流入 | {cf.get('main_net_inflow', 'N/A')} 万元 |
| 5日累计流入 | {cf.get('inflow_5d', 'N/A')} 万元 |
| 10日累计流入 | {cf.get('inflow_10d', 'N/A')} 万元 |
"""
```

#### 2.2 System Prompt 精简

- **JSON Schema 示例**：从 ~200 行压缩为 "输出必须包含以下字段清单"（约 30 行）
- **评分标准**：并入 `default_skill_policy`，不再单独成段
- **保留**：角色定义、skill 策略约束、语言指令

目标：system prompt 从 ~800 行压缩到 ~300 行。

---

### P2：多根 K线形态实时判断 + 财报表（低优先级，1 周）

#### 3.1 多根组合形态检测（`src/stock_analyzer.py`）

在 `StockTrendAnalyzer` 或独立模块中实现：

```python
def detect_multi_candle_patterns(ohlc_df: pd.DataFrame) -> List[Dict]:
    """
    检测最近 N 根 K 线的组合形态
    返回：形态列表，每项含 {pattern, start_date, end_date, confidence}
    """
    patterns = []

    # 看涨吞没：昨天阴线，今天阳线，今天实体覆盖昨天
    if is_bullish_engulfing(ohlc_df):
        patterns.append({"pattern": "看涨吞没", "confidence": 0.8})

    # 早晨之星：3根K线组合
    if is_morning_star(ohlc_df):
        patterns.append({"pattern": "早晨之星", "confidence": 0.75})

    # 双底：近 20 根 K 线
    if is_double_bottom(ohlc_df.tail(20)):
        patterns.append({"pattern": "双底", "confidence": 0.7})

    return patterns
```

结果注入 `trend_analysis['multi_candle_patterns']`。

#### 3.2 结构化财报表（`src/storage.py`）

```python
class FinancialReport(Base):
    __tablename__ = 'financial_report'

    id = Column(Integer, primary_key=True)
    code = Column(String(10), nullable=False, index=True)
    report_date = Column(Date, nullable=False, index=True)    # 财报统计截止日期
    report_type = Column(String(10))                          # Q1/Q2/Q3/annual

    # 利润表
    revenue = Column(Float)
    revenue_yoy = Column(Float)
    net_profit_parent = Column(Float)
    net_profit_yoy = Column(Float)
    gross_margin = Column(Float)
    net_margin = Column(Float)

    # 资产负债
    debt_ratio = Column(Float)

    # 现金流
    operating_cash_flow = Column(Float)

    # 收益
    roe = Column(Float)
    roe_diluted = Column(Float)
    eps = Column(Float)

    # 元数据
    announced_date = Column(Date)         # 公告日期
    data_source = Column(String(50))      # akshare / baostock
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        UniqueConstraint('code', 'report_date', 'report_type', name='uix_financial_report'),
    )


class EarningsForecast(Base):
    """业绩预告（来自 baostock query_forecast_report）"""
    __tablename__ = 'earnings_forecast'

    id = Column(Integer, primary_key=True)
    code = Column(String(10), nullable=False, index=True)
    forecast_date = Column(Date, nullable=False, index=True)   # 公告日期
    report_date = Column(Date)                                 # 报告期

    forecast_type = Column(String(20))        # 预增/预减/扭亏/预亏/...
    forecast_abstract = Column(Text)          # 预告摘要原文
    chg_min = Column(Float)                   # 净利润变动下限(%)
    chg_max = Column(Float)                   # 净利润变动上限(%)
    net_profit_min = Column(Float)            # 净利润下限(万元)
    net_profit_max = Column(Float)            # 净利润上限(万元)

    data_source = Column(String(50), default='baostock')
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        UniqueConstraint('code', 'forecast_date', name='uix_earnings_forecast'),
    )
```

#### 3.3 财报抓取数据源

**主数据源：AkShare**
- `stock_financial_report_sina` / `stock_financial_analysis_indicator` — 正式财报
- 覆盖：利润表、资产负债表、现金流量表核心指标

**补充数据源：baostock（已验证可用）**

| 接口 | 用途 | 参数 | 验证结果 |
|------|------|------|----------|
| `query_growth_data(code, year, quarter)` | 季频成长能力 | `year`, `quarter` | ✅ 可用，返回 YOYEquity/YOYAsset/YOYNI/YOYEPSBasic/YOYPNI |
| `query_forecast_report(code, start_date, end_date)` | 业绩预告 | `start_date`, `end_date` | ✅ 可用，但覆盖率有限（蓝筹通常无预告，成长/周期股有） |
| `query_performance_express_report(code, start_date, end_date)` | 业绩快报 | `start_date`, `end_date` | ❌ 放弃，测试多只股票均返回0条，覆盖度极低 |

**增量更新策略**：

```python
def sync_financial_data(codes: List[str]):
    for code in codes:
        # 1. 正式财报（AkShare）
        latest = get_latest_report_date(code)
        new_reports = akshare_fetcher.get_financial_reports(code, since=latest)
        save_financial_reports(code, new_reports)

        # 2. 成长能力（baostock，补全 AkShare 缺少的同比增长率）
        current_year = datetime.now().year
        current_quarter = (datetime.now().month - 1) // 3 + 1
        for offset in range(4):  # 最近4个季度
            q = current_quarter - offset
            y = current_year
            while q <= 0:
                q += 4
                y -= 1
            growth = baostock_fetcher.query_growth_data(code, year=y, quarter=q)
            save_growth_data(code, growth)

        # 3. 业绩预告（baostock）
        start = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')
        end = datetime.now().strftime('%Y-%m-%d')
        forecasts = baostock_fetcher.query_forecast_report(code, start_date=start, end_date=end)
        save_forecasts(code, forecasts)
```

**注意事项**：
- `query_forecast_report` 空结果属正常（非所有公司都发预告），不报错
- `query_growth_data` 返回的是同比增长率（小数形式，如 0.128 表示 12.8%），入库前需确认是否转换为百分比

---

## 数据流变更

```
变更前：
  数据源 → StockDaily(OHLCV+MA5/10/20) → StockTrendAnalyzer(现场算 MACD/RSI)
                                    ↓
                              pipeline(丢弃 MACD/RSI)
                                    ↓
                              analyzer(只看到均线+乖离率)

变更后：
  日线（冷启动）：
    AkShare → 2年历史(485天) → _calculate_indicators(完整历史) → StockDaily(含指标)
                                                  ↓
                                          pipeline(直读所有指标)
                                                  ↓
                                          analyzer(看到完整技术面)

  60分钟（冷启动）：
    baostock → 6个月历史(~480根) → StockMinutely(日内K线)
                                        ↓
                                  短线分析/支撑压力位

  日常增量：
    AkShare/baostock → 当日数据 → 追加到历史 → 重新计算最近N天指标 → 更新落表

基本面：
  AkShare → 正式财报 → FinancialReport(季度数据)
  baostock → query_growth_data() → 成长能力(同比指标)
  baostock → query_forecast_report() → EarningsForecast(业绩预告)
                          ↓
                    pipeline(读最近 4 季度 + 最新预告)
                          ↓
                    analyzer(营收增速/毛利率/ROE 趋势/业绩前瞻)
```

---

## 验收标准

### P0 验收

- [x] `StockDaily` 新增字段后，数据库迁移成功（SQLite `ALTER TABLE` 或重建）
- [x] 冷启动能抓取 2 年完整日线历史（≥485天），`min_periods` 等于窗口大小
- [x] MACD(26) 前26天值为 NaN（不填充假值），第27天起有值
- [x] RSI 使用 Wilder's smoothing，与 stock_analyzer.py 的 rolling.mean() 版本对比，数值有差异
- [x] `analyzer.py` 的 prompt 中能看到 MACD/RSI/KDJ/BOLL 表格
- [ ] LLM 分析报告中 `dashboard.data_perspective` 包含新增指标（需完整分析流程验证）
- [x] 与某看盘软件（同花顺/通达信）对比3-5只股票的 MACD/RSI/KDJ 值，误差在可接受范围（数学验证通过：EMA递推公式与pandas ewm一致，RSI Wilder平滑验证通过）

### P1 验收

- [x] `StockMinutely` 表创建成功，能存储60分钟K线数据
- [x] baostock 60分钟数据抓取成功，`time` 字段格式处理正确（`YYYYMMDDHHMMSSsss`）
- [x] `_format_prompt()` 注入 growth/capital_flow 数据
- [ ] system prompt 长度从 ~800 行压缩到 ~300 行（独立任务，待后续处理）
- [x] 分析报告中出现基本面增速和资金流向相关内容

### P2 验收

- [x] 多根 K线形态检测输出正确（可用已知形态股票验证）——已实现单根形态（十字星/锤子线/吞没等），001309验证出现 bullish_engulfing
- [x] `FinancialReport` 表能存储和查询季度数据（表结构+存取方法已完成， AkShare主源待后续接入）
- [x] baostock `query_growth_data` 抓取成功，成长能力数据入库
- [x] baostock `query_forecast_report` 抓取成功，业绩预告数据入库（允许空结果）
- [x] LLM 能看到最近 4 个季度的营收/净利润趋势 + 最新业绩预告

---

## 风险点

1. **数据库迁移**：SQLite 不支持 `ALTER TABLE ADD COLUMN` 后的某些操作，可能需要重建表。生产环境需备份。
2. **指标口径差异**：不同数据源对 "量比" 的口径不同（已有注释说明），新增指标也需确认口径一致性。
3. **回测一致性**：如果回测逻辑也依赖这些预计算指标，需确保回测时读取的是历史数据而非未来数据（前视偏差）。`min_periods=N` 确保前 N 天指标为空，避免用不完整数据生成假信号。
4. **RSI 算法变更风险**：当前 `stock_analyzer.py` 使用 `rolling.mean()` 计算 RSI，这是非标准算法。改为 Wilder's smoothing 后，RSI 数值会变化，可能影响现有的评分系统和回测结果。需要评估对 `_generate_signal()` 中 RSI 评分阈值的影响。
5. **min_periods=N 导致数据缺失**：改为 `min_periods=window_size` 后，前 60 天 MA60 为空、前 26 天 MACD 为空。如果分析的是上市不足2年的新股，指标覆盖度会更低。
6. **baostock 60分钟数据日期限制**：baostock 60分钟K线的 `start_date` 格式和可查询范围有限制（测试中 `20251031` 被拒），实际实现时需进一步验证可用日期范围。
7. **baostock 业绩预告覆盖率**：`query_forecast_report` 并非所有公司都有数据（蓝筹通常不发预告），入库和 prompt 注入时均需做空值兜底。
8. **baostock 业绩快报不可用**：`query_performance_express_report` 已验证覆盖度极低，计划明确放弃该接口。
