# feat-sub: SEPA 提示词数据层适配 — 子任务清单

> 父任务: `feat-plan.md` (分析数据层增强)
> 目标: 让现有数据层 + Agent 工具链完整适配新版 `CORE_TRADING_SKILL_POLICY_ZH` (SEPA 框架)

---

## 一、数据需求总览

新版 SEPA 提示词要求 LLM 看到以下数据输入：

| # | 数据项 | 必需/可选 | 当前状态 |
|---|--------|----------|----------|
| 1 | 日K线（60日以上） | 必需 | ✅ 已有 `get_daily_history` |
| 2 | 60分钟K线（近期） | 必需 | ⚠️ `StockMinutely` 表已建，但 **无 Agent 工具** |
| 3 | 50/150/200日均线 | 必需 | ⚠️ `calculate_ma` 可算，但 **默认无 50/150/200**，`StockDaily` 未落表 |
| 4 | 52周最高价/最低价 | 必需 | ❌ 未计算，无工具 |
| 5 | 个股相对大盘RS排名 | 强烈建议 | ❌ 未计算，无工具 |
| 6 | 季度财务数据（EPS、营收、毛利率、**扣非净利润**、ROE） | 强烈建议 | ⚠️ `FinancialReport` 已有大部分字段，**缺扣非净利润**，**无 4 季趋势工具** |
| 7 | 近期公告/新闻 | 建议 | ✅ 已有 `search_stock_news` |
| 8 | 60日内涨停/跌停/炸板/连板记录 | 建议 | ❌ 未计算，无工具 |

---

## 二、开发任务清单

### Phase 1: Agent 数据工具（最高优先级）

#### 1.1 `get_minutely_history` — 60分钟K线读取工具 ✅
- **文件**: `src/agent/tools/data_tools.py`
- **功能**: 读取 `StockMinutely` 表，返回最近 1-5 日 60分钟K线（OHLCV）
- **输入**: `stock_code`, `days=5`
- **输出**: `{code, records: [{date, time, open, high, low, close, volume, amount}]}`
- **兜底**: 表为空时调用 `DataFetcherManager.get_minutely_data()` 补录
- **注意事项**: baostock 60分钟仅保留近期 3-7 天，空结果不报错
- **测试**: mock + DB 查询均通过

#### 1.2 `get_financial_history` — 季度财报趋势工具 ✅
- **文件**: `src/agent/tools/data_tools.py`
- **功能**: 读取 `FinancialReport` 表，返回最近 4 个季度数据
- **输入**: `stock_code`
- **输出**: `{code, quarters: [{report_date, report_type, revenue, revenue_yoy, net_profit_parent, net_profit_yoy, net_profit_deducted, gross_margin, net_margin, roe, eps}]}`
- **依赖**: `FinancialReport` 需新增 `net_profit_deducted` 字段（见 2.3）
- **测试**: DB 查询通过（本地有财务数据时验证）

#### 1.3 `get_52w_range` — 52周高低点工具 ✅
- **文件**: `src/agent/tools/data_tools.py`
- **功能**: 基于 `load_history_df(days=300)` 计算 52 周最高价、最低价、当前距高/低比例
- **输入**: `stock_code`
- **输出**: `{code, high_52w, low_52w, current_price, pct_from_52w_high, pct_from_52w_low}`
- **口径**: 252 个交易日（约 52 周）
- **测试**: mock 数据验证通过

#### 1.4 `get_relative_strength` — RS 相对强度工具 ✅
- **文件**: `src/agent/tools/data_tools.py`
- **功能**: 计算个股 1 年涨幅 vs 沪深300 1 年涨幅的 RS 排名
- **输入**: `stock_code`
- **输出**: `{code, stock_return_1y_pct, index_return_1y_pct, rs_ratio, rs_rank_pct}`
- **口径**: 使用收盘价计算累计涨幅；指数无数据时返回 stock_return only
- **指数代码**: 沪深300 = `000300`
- **测试**: mock 数据验证通过

#### 1.5 `get_limit_up_down_stats` — 60日涨停统计工具 ✅
- **文件**: `src/agent/tools/analysis_tools.py`
- **功能**: 基于 `load_history_df(days=80)` 统计 60 个交易日内涨停/跌停/炸板/连板
- **输入**: `stock_code`, `days=60`
- **输出**: `{code, period_days, limit_up_count, limit_down_count, failed_limit_up_count, max_consecutive_limit_up, limit_up_down_ratio, momentum_grade}`
- **涨跌幅规则**: 自动根据代码判断（科创板/创业板 688/30 开头 ±20%，主板 ±10%，ST ±5%）
- **炸板定义**: 盘中最高价触及涨停价但未以涨停价收盘
- **测试**: mock 数据验证通过（10%/20%/5% 规则、连板、S级判定）

#### 1.6 `calculate_ma` 默认参数调整 ✅
- **文件**: `src/agent/tools/analysis_tools.py`
- **变更**: 默认 `periods` 从 `"5,10,20,30,60,120,250"` → `"5,10,20,50,60,150,200"`
- **原因**: SEPA Trend Template 的 8 条铁律需要 50/150/200 日均线
- **测试**: mock 数据验证通过，MA keys 包含 ma50/ma150/ma200

---

### Phase 2: 数据表结构扩展

#### 2.1 `StockDaily` 新增均线字段 ✅
- **文件**: `src/storage.py`
- **新增字段**:
  ```python
  ma50 = Column(Float)
  ma150 = Column(Float)
  ma200 = Column(Float)
  ```
- **原因**: SEPA 核心依赖 50/150/200 日均线，预计算落表避免 LLM 自行计算漂移
- **测试**: ORM 模型 + DB ALTER TABLE + save_daily_data upsert 均验证通过

#### 2.2 `_calculate_indicators` 扩展 MA 计算 ✅
- **文件**: `data_provider/base.py`
- **新增**:
  ```python
  df['ma50'] = close.rolling(window=50, min_periods=50).mean()
  df['ma150'] = close.rolling(window=150, min_periods=150).mean()
  df['ma200'] = close.rolling(window=200, min_periods=200).mean()
  ```
- **约束**: `min_periods` 等于窗口大小，前 N 天返回 NaN（宁可缺失不给假值）
- **测试**: 滚动窗口边界验证通过（ma50@idx49, ma150@idx149, ma200@idx199 首个非空值）

#### 2.3 `FinancialReport` 新增扣非净利润 ✅
- **文件**: `src/storage.py`
- **新增字段**:
  ```python
  net_profit_deducted = Column(Float)  # 扣除非经常性损益后的净利润
  ```
- **原因**: SEPA "价值因子否决项" 明确要求检查扣非净利润环比是否同步
- **状态**: 已在 Phase 1 中完成（含 DB migration + save_financial_report 更新）

#### 2.4 `save_daily_data` upsert 逻辑更新
- **文件**: `src/storage.py`
- **变更**: `on_conflict_do_update` 包含所有新增字段（ma50/ma150/ma200）

---

### Phase 3: Pipeline 上下文注入

#### 3.1 `_ensure_agent_history` 扩大历史长度 ✅
- **文件**: `src/core/pipeline.py`
- **变更**: `min_days` 默认值从 `240` → `300`
- **原因**: SEPA 需要 52 周(252日) + MA200(200日) + RS 排名(242日) + 安全垫

#### 3.2 `_enhance_context` 注入 SEPA 专用字段 ✅
- **文件**: `src/core/pipeline.py`
- **新增 `enhanced['sepa_analysis']`**:
  - `ma50`, `ma150`, `ma200`（从 `today` 读取，已预计算落表）
  - `high_52w`, `low_52w`, `pct_from_52w_high`, `pct_from_52w_low`
  - `rs_stock_return_1y`, `rs_index_return_1y`, `rs_ratio`, `rs_rank_pct`, `pass_sepa_rs_70`
  - `limit_up_count`, `limit_down_count`, `failed_limit_up_count`, `max_consecutive_limit_up`, `momentum_grade`, `grade_meaning`
- **测试**: mock 注入验证通过，handler 失败时不阻断 pipeline

#### 3.3 `_format_prompt` 扩展 SEPA 数据表格 ✅
- **文件**: `src/analyzer.py`
- **新增段落**（在现有技术指标表格之后）:
  ```markdown
  ### 趋势模板数据（SEPA）
  | 指标 | 数值 | 状态 |
  |------|------|------|
  | MA50 | {ma50} | {price_above_ma50} |
  | MA150 | {ma150} | {price_above_ma150} |
  | MA200 | {ma200} | {price_above_ma200} |
  | 52周高点 | {high_52w} | 距高点 {pct_from_52w_high}% |
  | 52周低点 | {low_52w} | 距低点 {pct_from_52w_low}% |
  | RS相对强度 | {relative_strength} | {rs_grade} |

  ### 动量验证（60日）
  | 涨停次数 | {limit_up_count} |
  | 跌停次数 | {limit_down_count} |
  | 炸板次数 | {failed_limit_up_count} |
  | 最大连板天数 | {max_consecutive_limit_up} |
  | 动量等级 | {momentum_grade} |
  ```
- **测试**: prompt 输出包含两个 SEPA 表格，mock 数据渲染正确

---

### Phase 4: Agent System Prompt 精简 ✅

#### 4.1 精简 JSON Schema 示例 ✅
- **文件**: `src/agent/executor.py`（`LEGACY_DEFAULT_AGENT_SYSTEM_PROMPT` / `AGENT_SYSTEM_PROMPT`）
- **变更**: ~110 行 JSON 示例 + ~25 行评分标准 → 压缩为 ~15 行字段清单
- **效果**: `executor.py` 从 658 行 → 511 行（-147 行），释放 token 给数据输入

#### 4.2 评分标准迁移 ✅
- **变更**: 评分标准从 system prompt 移除，由激活的 `skill_policy` 定义（SEPA policy 已含 48 分制评分）
- **效果**: 基础 system prompt 不再含评分规则，避免与 skill policy 重复；无 skill 激活时由 Agent 自主判断

---

## 三、测试核对清单

### 3.1 单元测试

- [x] `get_minutely_history` 返回格式正确，空表时触发 fallback（mock + DB 验证通过）
- [x] `get_financial_history` 返回最近 4 个季度，按 `report_date` 降序（DB 验证通过）
- [x] `get_52w_range` 计算值与 mock 核对一致（252日高低点、距高/低百分比正确）
- [x] `get_relative_strength` 个股涨幅 vs 沪深300 涨幅计算正确（mock 验证通过）
- [x] `get_limit_up_down_stats` 涨跌幅规则正确（688/30 开头 → 20%，ST → 5%，mock 验证通过）
- [x] `calculate_ma` 默认返回包含 ma50/150/200（mock 验证通过）
- [x] `StockDaily` upsert 包含 ma50/150/200 字段（DB 写入 + upsert 验证通过）

### 3.2 集成测试

- [ ] Agent 完整链路跑通一只股票：`get_daily_history` → `get_52w_range` → `get_relative_strength` → `get_limit_up_down_stats` → `analyze_trend` → SEPA 报告
- [ ] `_ensure_agent_history(300)` 冷启动成功，DB 中历史数据 ≥300 条
- [ ] `get_minutely_history` 与 baostock 原始数据对比，条数/价格一致
- [ ] `get_financial_history` 与 AkShare 原始财报对比，数值一致

### 3.3 数学验证

- [ ] MA50/150/200 与同花顺/通达信对比，误差 < 0.5%
- [ ] 52周高低点与交易软件对比，误差 < 1%
- [ ] RS 排名计算：手动验证 3 只股票的 1 年涨幅 vs 沪深300

### 3.4 Prompt 注入验证

- [x] `_format_prompt` 输出中包含 "趋势模板数据" 表格（mock 验证通过）
- [x] `_format_prompt` 输出中包含 "动量验证（60日）" 表格（mock 验证通过）
- [ ] Agent tool 结果中 `macd_dif/dea/bar`、`rsi_6/12/24`、`kdj_k/d/j` 数值非空
- [ ] growth/capital_flow 数据出现在 prompt 中（已有功能回归测试）

### 3.5 性能与边界

- [ ] 单只股票 Agent 工具链总调用时间 < 10s（本地 DB 命中时）
- [ ] 新股/上市不足 200 天：MA200 返回 NaN，不填充假值
- [ ] 无财报数据股票：`get_financial_history` 返回空数组，不报错
- [ ] ST 股票涨停统计使用 ±5% 规则

---

## 四、文件改动汇总

| 文件 | 改动类型 | 说明 |
|------|----------|------|
| `src/agent/tools/data_tools.py` | 新增 | 5 个新工具 + 注册到 `ALL_DATA_TOOLS` |
| `src/agent/tools/analysis_tools.py` | 修改 | `calculate_ma` 默认参数；新增 `get_limit_up_down_stats` |
| `src/storage.py` | 修改 | `StockDaily` 新增 ma50/150/200；`FinancialReport` 新增 net_profit_deducted；upsert 更新 |
| `data_provider/base.py` | 修改 | `_calculate_indicators` 新增 MA50/150/200 计算 |
| `src/core/pipeline.py` | 修改 | `_ensure_agent_history` 扩大天数；`_enhance_context` 注入新字段 |
| `src/analyzer.py` | 修改 | `_format_prompt` 新增 SEPA 数据表格 |
| `src/agent/executor.py` | 修改 | system prompt 精简（Phase 4） |
| `src/agent/factory.py` | 修改 | 新工具注册到 `ToolRegistry` |
| `docs/agentTOOLS.md` | 新增 | Agent 原生工具完整清单（23个工具） |
| `docs/DATA_PIPELINE.md` | 修改 | `stock_daily` schema 补充 ma50/150/200 |
| `docs/CHANGELOG.md` | 修改 | `[Unreleased]` 段添加条目 |

---

## 五、风险与依赖

1. **RS 排名口径**: 如 DB 无后复权数据，RS 排名可能失真。需确认 `load_history_df` 返回的是前复权还是未复权。
2. **沪深300指数数据**: `get_relative_strength` 依赖 DB 中有 `000300` 的日线数据。如没有，需要从数据源拉取。
3. **扣非净利润数据源**: AkShare `stock_financial_report_sina` 是否包含扣非净利润，需先行验证。
4. **baostock 60分钟限制**: 仅保留近期数据，`get_minutely_history` 必须做空值兜底。
5. **SQLite ALTER TABLE**: 如生产环境使用 SQLite，`ADD COLUMN` 对现有表直接生效，无需重建。

---

## 六、回滚方案

- 数据表字段新增为**追加式**，不影响旧代码读写（旧字段仍在，新字段读取时走 `getattr(obj, 'ma50', None)` 兜底）
- Agent 工具新增为**注册式**，旧 executor 未调用新工具则无任何影响
- 如 `_format_prompt` 新表格导致 token 超限，可快速注释新增段落回退
