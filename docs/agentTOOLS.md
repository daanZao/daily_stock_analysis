# Agent 原生工具清单

本文档列出本项目 Agent 分析链路中所有可用的原生工具（Native Tools）。Agent 通过 Tool Calling（ReAct 循环）按需调用这些工具获取数据，所有工具均通过 `ToolRegistry` 注册并在 `src/agent/factory.py` 中自动加载。

> 当前注册工具总数：**23 个**

---

## 一、数据工具（Data Tools）

共 **12 个**，位于 `src/agent/tools/data_tools.py`。

---

### get_realtime_quote

获取股票实时行情。

| 属性 | 说明 |
|------|------|
| 参数 | `stock_code: string` — 股票代码，如 `'600519'`、`'AAPL'`、`'hk00700'` |
| 返回 | `{code, price, change_pct, volume_ratio, turnover, pe_ratio, pb_ratio, market_cap, ...}` |
| 用途 | 获取最新价格、量比、换手率、估值指标 |

---

### get_daily_history

获取个股日K线历史数据（含预计算指标）。

| 属性 | 说明 |
|------|------|
| 参数 | `stock_code: string` — 股票代码；`days: int` — 交易日数量（默认 60） |
| 返回 | `{code, source, cache_hit, actual_records, data: [{date, open, high, low, close, volume, amount, pct_chg, ma5, ma10, ma20, ma50, ma60, ma150, ma200, macd_dif/dea/bar, rsi_6/12/24, kdj_k/d/j, ...}]}` |
| 用途 | Agent 获取完整日K线+技术指标数据；优先读 DB，不足时网络补录 |

---

### get_minutely_history

获取 60 分钟K线数据。

| 属性 | 说明 |
|------|------|
| 参数 | `stock_code: string`；`days: int` — 最近日历天数（默认 5） |
| 返回 | `{stock_code, status, period_days, record_count, records: [{date, time, open, high, low, close, volume, amount}]}` |
| 用途 | SEPA 60分钟精修、日内 VCP 确认、短周期支撑阻力 |
| 兜底 | DB 无数据时调用 `DataFetcherManager.get_minutely_data()` 补录 |

---

### get_financial_history

获取最近 N 个季度财报数据。

| 属性 | 说明 |
|------|------|
| 参数 | `stock_code: string`；`quarters: int` — 季度数（默认 4） |
| 返回 | `{stock_code, status, quarter_count, quarters: [{report_date, report_type, revenue, revenue_yoy, net_profit_parent, net_profit_yoy, net_profit_deducted, gross_margin, net_margin, roe, eps}]}` |
| 用途 | SEPA Earnings 分析、同比/环比趋势判断 |

---

### get_52w_range

计算 52 周最高价/最低价及当前位置。

| 属性 | 说明 |
|------|------|
| 参数 | `stock_code: string` |
| 返回 | `{stock_code, status, current_price, high_52w, low_52w, pct_from_52w_high, pct_from_52w_low, within_25pct_of_high, above_130pct_of_low}` |
| 用途 | SEPA Trend Template 规则 #6（≥52周低点130%）和 #7（在52周高点25%以内） |

---

### get_relative_strength

计算个股 1 年涨幅相对沪深300 的 RS 排名。

| 属性 | 说明 |
|------|------|
| 参数 | `stock_code: string` |
| 返回 | `{stock_code, status, stock_return_1y_pct, index_return_1y_pct, rs_ratio, rs_rank_pct, pass_sepa_rs_70}` |
| 用途 | SEPA Trend Template 规则 #8（RS ≥ 70）；沪深300指数（`000300`）通过 `index_daily` 获取并缓存至 `stock_daily`，首次失败后不再重复遍历数据源 |

---

### get_stock_info

获取个股基本面信息。

| 属性 | 说明 |
|------|------|
| 参数 | `stock_code: string` |
| 返回 | `{code, name, pe_ratio, pb_ratio, total_mv, circ_mv, fundamental_context, belong_boards, sector_rankings}` |
| 用途 | 估值、所属板块、行业排名 |

---

### get_chip_distribution

获取筹码分布分析。

| 属性 | 说明 |
|------|------|
| 参数 | `stock_code: string` |
| 返回 | `{code, date, profit_ratio, avg_cost, cost_90_low/high, concentration_90, cost_70_low/high, concentration_70}` |
| 用途 | 判断支撑/阻力、持仓结构 |

---

### get_capital_flow

获取主力资金流向。

| 属性 | 说明 |
|------|------|
| 参数 | `stock_code: string` |
| 返回 | `{stock_code, status, main_net_inflow, inflow_5d, inflow_10d, sector_rankings: {top_inflow_sectors, top_outflow_sectors}, errors}` |
| 用途 | 主力净流入、板块资金流向；仅支持 A 股个股 |

---

### get_analysis_context

获取数据库中存储的分析上下文。

| 属性 | 说明 |
|------|------|
| 参数 | `stock_code: string` |
| 返回 | `{code, date, open/high/low/close/volume, ma_alignment, ...}` |
| 用途 | 获取 pipeline 已存储的今日/昨日 OHLCV 及技术指标对齐状态 |

---

### get_portfolio_snapshot

获取投资组合快照（如已启用持仓模块）。

| 属性 | 说明 |
|------|------|
| 参数 | `account_id: int?`, `cost_method: string` (`fifo`/`avg`), `include_positions: bool`, `include_risk: bool`, `as_of: string?` (YYYY-MM-DD) |
| 返回 | `{status, snapshot, risk}` |
| 用途 | 账户-aware 建议；未启用持仓模块时返回 `not_supported` |

---

## 二、分析工具（Analysis Tools）

共 **5 个**，位于 `src/agent/tools/analysis_tools.py`。

---

### analyze_trend

综合技术分析（趋势分析）。

| 属性 | 说明 |
|------|------|
| 参数 | `stock_code: string` |
| 返回 | `{trend_status, score, ma_alignment, macd_status, rsi_status, volume_status, support_resistance, signal}` |
| 用途 | 快速获取技术面综合评分（0-100）及买卖信号 |

---

### calculate_ma

灵活计算任意周期移动平均线。

| 属性 | 说明 |
|------|------|
| 参数 | `stock_code: string`；`periods: string?` — 逗号分隔周期（默认 `"5,10,20,50,60,150,200"`）；`days: int` — 历史天数（默认 120） |
| 返回 | `{code, source, current_price, data_points, ma: {ma50: {value, bias_pct, price_above}, ...}, above_ma_count, total_ma_count, ma_alignment}` |
| 用途 | SEPA Trend Template 需要 50/150/200 日均线；默认参数已适配 SEPA |

---

### get_volume_analysis

量价关系分析。

| 属性 | 说明 |
|------|------|
| 参数 | `stock_code: string`；`days: int` — 分析天数（默认 30） |
| 返回 | `{code, latest_volume, avg_volume_5d/20d, volume_ratio_vs_5d/20d, volume_trend, volume_price_corr, pattern}` |
| 用途 | 量价配合/背离判断、放量/缩量识别 |

---

### analyze_pattern

K线与图表形态识别。

| 属性 | 说明 |
|------|------|
| 参数 | `stock_code: string`；`days: int` — 扫描天数（默认 60） |
| 返回 | `{patterns: [{pattern, type, day_offset, strength, desc}]}` |
| 用途 | 识别十字星、锤子线、早晨之星、吞没形态、双底、箱体震荡、向上突破等 |

---

### get_limit_up_down_stats

60 日涨停/跌停/炸板/连板统计。

| 属性 | 说明 |
|------|------|
| 参数 | `stock_code: string`；`days: int` — 统计天数（默认 60） |
| 返回 | `{stock_code, status, period_days, limit_pct, limit_up_count, limit_down_count, failed_limit_up_count, max_consecutive_limit_up, limit_up_down_ratio, momentum_grade, grade_meaning}` |
| 用途 | SEPA P2-动量验证；自动检测涨跌幅规则（科创/创业板 20%、ST 5%、主板 10%） |

---

## 三、搜索工具（Search Tools）

共 **2 个**，位于 `src/agent/tools/search_tools.py`。

---

### search_stock_news

搜索个股最新新闻。

| 属性 | 说明 |
|------|------|
| 参数 | `stock_code: string`；`stock_name: string` — 中文名称 |
| 返回 | `{articles: [{title, snippet, source, url, published_date}]}` |
| 用途 | 获取业绩预增、重大合同、监管政策等近期公告/新闻 |

---

### search_comprehensive_intel

多维度情报搜索。

| 属性 | 说明 |
|------|------|
| 参数 | `stock_code: string`；`stock_name: string` |
| 返回 | `{report, dimensions: {news, analysis, risk, earnings, industry}}` |
| 用途 | 综合搜索新闻、市场分析、风险检查、盈利预期、行业趋势 |

---

## 四、市场工具（Market Tools）

共 **2 个**，位于 `src/agent/tools/market_tools.py`。

---

### get_market_indices

获取主要市场指数行情。

| 属性 | 说明 |
|------|------|
| 参数 | `region: string` — `'cn'`（中国）或 `'us'`（美国，默认 `'cn'`） |
| 返回 | `{indices: [{code, name, price, change_pct, volume, ...}]}` |
| 用途 | 市场环境过滤（大盘阶段判定） |

---

### get_sector_rankings

获取板块涨跌幅排行。

| 属性 | 说明 |
|------|------|
| 参数 | `top_n: int` — 返回前 N 和后 N 板块（默认 10） |
| 返回 | `{top_sectors: [...], bottom_sectors: [...]}` |
| 用途 | 板块轮动分析、行业资金流向 |

---

## 五、回测工具（Backtest Tools）

共 **3 个**，位于 `src/agent/tools/backtest_tools.py`。

---

### get_stock_backtest_summary

获取个股回测摘要。

| 属性 | 说明 |
|------|------|
| 参数 | `stock_code: string`；`eval_window_days: int`（默认 30）；`limit: int`（默认 10） |
| 返回 | `{summary: {code, total_evaluations, win_rate_pct, direction_accuracy_pct, ...}, recent_evaluations: [...]}` |
| 用途 | 只读查询，不触发新回测 |

---

### get_strategy_backtest_summary

获取整体策略回测摘要（Legacy）。

| 属性 | 说明 |
|------|------|
| 参数 | `eval_window_days: int`（默认 30） |
| 返回 | 整体回测表现摘要 |

---

### get_skill_backtest_summary

获取指定 Skill 的回测摘要。

| 属性 | 说明 |
|------|------|
| 参数 | `skill_id: string` — Skill 标识符；`eval_window_days: int`（默认 30） |
| 返回 | Skill 级别的回测统计 |

---

## 附录：按 SEPA 分析流程映射工具

| SEPA 步骤 | 所需数据 | 对应工具 |
|-----------|----------|----------|
| 阶段分析（Stage） | 日K线 + MA50/150/200 | `get_daily_history`, `calculate_ma` |
| 趋势模板（8条铁律） | 52周高低点 + RS排名 + MA | `get_52w_range`, `get_relative_strength`, `calculate_ma` |
| E-盈利分析 | 季度财报趋势 | `get_financial_history`, `get_stock_info` |
| P-价格行为 | 动量验证 + VCP | `get_limit_up_down_stats`, `analyze_pattern`, `get_volume_analysis` |
| A-催化剂 | 新闻/公告 | `search_stock_news`, `search_comprehensive_intel` |
| 市场环境 | 大盘指数 | `get_market_indices`, `get_sector_rankings` |
| 60分钟精修 | 60分钟K线 | `get_minutely_history` |

---

## 附录：文件索引

| 文件 | 工具数 | 说明 |
|------|--------|------|
| `src/agent/tools/data_tools.py` | 12 | 数据获取工具（行情、K线、财报、资金流向等） |
| `src/agent/tools/analysis_tools.py` | 5 | 技术分析工具（趋势、MA、量价、形态、涨停统计） |
| `src/agent/tools/search_tools.py` | 2 | 新闻与情报搜索工具 |
| `src/agent/tools/market_tools.py` | 2 | 市场指数与板块排行工具 |
| `src/agent/tools/backtest_tools.py` | 3 | 回测数据查询工具 |
| `src/agent/tools/registry.py` | — | `ToolDefinition` 定义与 `@tool` 装饰器 |
| `src/agent/factory.py` | — | `ToolRegistry` 初始化与工具加载入口 |
