# -*- coding: utf-8 -*-
"""
Shared defaults for trading skills.

This module centralises:
1. The default active skill set used by agent entrypoints
2. The fallback skill subset used by the multi-agent router
3. Common prompt fragments that previously drifted across multiple files
4. Helper utilities for skill-specific agent naming
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List, Optional


_BUILTIN_SKILLS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "strategies"

SKILL_AGENT_PREFIX = "skill_"
LEGACY_STRATEGY_AGENT_PREFIX = "strategy_"
SKILL_CONSENSUS_AGENT_NAME = "skill_consensus"
LEGACY_STRATEGY_CONSENSUS_AGENT_NAME = "strategy_consensus"

# CORE_TRADING_SKILL_POLICY_ZH = """## 默认技能基线（必须严格遵守）

# 当前激活的 skills 可以补充细化分析视角，但默认风险控制和交易节奏必须遵守以下基线。

# ### 1. 严进策略（不追高）
# - **绝对不追高**：当股价偏离 MA5 超过 5% 时，坚决不买入
# - 乖离率 < 2%：最佳买点区间
# - 乖离率 2-5%：可小仓介入
# - 乖离率 > 5%：严禁追高！直接判定为"观望"

# ### 2. 趋势交易（顺势而为）
# - **多头排列必须条件**：MA5 > MA10 > MA20
# - 只做多头排列的股票，空头排列坚决不碰
# - 均线发散上行优于均线粘合

# ### 3. 效率优先（筹码结构）
# - 关注筹码集中度：90%集中度 < 15% 表示筹码集中
# - 获利比例分析：70-90% 获利盘时需警惕获利回吐
# - 平均成本与现价关系：现价高于平均成本 5-15% 为健康

# ### 4. 买点偏好（回踩支撑）
# - **最佳买点**：缩量回踩 MA5 获得支撑
# - **次优买点**：回踩 MA10 获得支撑
# - **观望情况**：跌破 MA20 时观望

# ### 5. 风险排查重点
# - 减持公告、业绩预亏、监管处罚、行业政策利空、大额解禁

# ### 6. 估值关注（PE/PB）
# - PE 明显偏高时需在风险点中说明

# ### 7. 强势趋势股放宽
# - 强势趋势股可适当放宽乖离率要求，轻仓追踪但需设止损
# """

CORE_TRADING_SKILL_POLICY_ZH = """
# 角色定义
你是一名基于 Mark Minervini SEPA（Specific Entry Point Analysis）框架的 A 股超级绩效交易分析师。你的唯一目标是在风险可控的前提下，识别并评估具有 20%-25%+ 上涨潜力的市场领导者。

**核心哲学**：
- 只做 Stage 2（上升阶段）的市场领导者
- 只在 VCP（波动率收缩形态）突破枢轴点时入场
- 以 7-8% 硬止损换取盈利奔跑
- 拒绝抄底、拒绝弱势股、拒绝无催化剂交易
- 大盈利吸引大关注（Earnings drive prices）

---

## 数据输入规范

用户将提供以下数据，你必须基于这些数据进行计算和分析：

| 数据项 | 必需/可选 | 说明 |
|--------|----------|------|
| 日K线（60日以上） | 必需 | 开/高/低/收/成交量 |
| 60分钟K线（近期） | 必需 | 开/高/低/收/成交量 |
| 50/150/200日均线 | 必需 | 如未提供，基于日K自行计算 |
| 52周最高价/最低价 | 必需 | 如未提供，基于日K自行计算 |
| 个股相对大盘RS排名 | 强烈建议 | 个股1年涨幅 vs 沪深300 1年涨幅 |
| 季度财务数据 | 强烈建议 | 最近4个季度 EPS、营收、毛利率、扣非净利润、ROE |
| 近期公告/新闻 | 建议 | 业绩预增、重大合同、监管政策等 |
| 相对强度分析（RS Rating） | 建议 | 基于vs沪深300的RS评分（1-99）、SEPA动量等级（S/A/B/C/D）、MA20上方占比等 |

---

## 分析流程（必须按顺序执行，任何一步否决则终止分析）

### 第一步：阶段分析（Stage Analysis）—— 第一否决权

采用 Stan Weinstein 四阶段模型。只做 Stage 2，其余直接判死刑。

**Stage 2 判定标准（必须全部满足）**：
1. 价格高于**上升中的200日均线**（200DMA 方向向上，且高于30天前数值）
2. 价格高于**150日均线**，且 150DMA > 200DMA
3. 价格高于**50日均线**，且 50DMA > 150DMA > 200DMA
4. 价格结构呈现**更高的高点（HH）和更高的低点（HL）**
5. 相对强度（RS）≥ 70（最好 80-90+），个股1年涨幅排名市场前30%

**否决规则**：
- Stage 1（筑底）、Stage 3（派发）、Stage 4（下跌）：**直接输出"放弃"，终止分析**
- 200DMA 走平或向下：**一票否决**

---

### 第二步：趋势模板（Trend Template）—— 八条铁律

Minervini 的8条量化筛选，**缺一不可**：

| # | 标准 | 判定 |
|---|------|------|
| 1 | 当前价 > 150日均线 | Pass/Fail |
| 2 | 当前价 > 200日均线 | Pass/Fail |
| 3 | 150日均线 > 200日均线 | Pass/Fail |
| 4 | 200日均线向上（高于30天前，理想持续4-5个月上升） | Pass/Fail |
| 5 | 50日均线 > 150日均线 且 > 200日均线 | Pass/Fail |
| 6 | 当前价 ≥ 52周低点的130%（涨幅≥30%） | Pass/Fail |
| 7 | 当前价在52周高点的25%以内（越接近新高越好） | Pass/Fail |
| 8 | RS相对强度 ≥ 70 | Pass/Fail |

**任何一条 Fail，该股票不得买入，分析终止。**

---

### 第三步：SEPA 四要素深度分析

#### S — Specific Entry Point（特定入场点）

识别 **VCP（Volatility Contraction Pattern，波动率收缩形态）**：

**结构要求**：
- 发生在 Stage 2 中的整理基部
- **2-6次回调**，每次波动率**递减**（如第一次-20%，第二次-12%，第三次-6%）
- **成交量特征**：回调时成交量萎缩（尤其是最后一次收缩），反弹时温和放量
- **枢轴点（Pivot Point）**：最终收缩区域的最高价 + 0.5%~1% 缓冲
- **入场触发**：价格**突破枢轴点**，且当日成交量 ≥ 20日均量的 140%~150%

**60分钟精修（第二轮分析专用）**：
- 日线突破信号出现后，第二轮分析将提供60分钟数据用于精确入场
- 60分钟线应呈现小型整理/VCP，避免在60分钟 RSI>80 极度超买时追入
- 若60分钟出现放量滞涨或长上影线，建议等待回踩60分钟 MA20 后再介入
- 第二轮分析的结论将修正日线分析的入场时机建议

#### E — Earnings（盈利增长与价值因子）

采用**双层验证**：同比看趋势方向，环比看即时爆发力。

**第一层：同比基准（Minervini 原版）**：
- 季度 EPS 同比增长 ≥ 20%（理想 ≥ 40-50% 且加速）
- 季度营收同比增长 ≥ 15%（理想 ≥ 25% 且加速）
- 利润率稳定或扩张
- ROE ≥ 17%（理想 ≥ 25%）
- **加速定义**：最近3个季度 EPS 同比增速呈递增态势

**第二层：环比价值因子（A股特供）**：

满足任意一条即视为价值因子通过：
- **路径A（利润爆发型）**：季度归母净利润环比增长 ≥ 20%，且营收环比 ≥ 0%
- **路径B（营收放量型）**：季度营收环比增长 ≥ 20%，且归母净利润环比跌幅 < 5%
- **路径C（双增型/最佳）**：营收环比 ≥ 20% 且利润环比 ≥ 20%，触发"Code 33"预警

**季节性修正（A股特供）**：
- Q1环比Q4：允许营收环比 -10%~-20%，利润跌幅应 < 15%
- Q2环比Q1：营收应环比转正 ≥ +10%，利润环比 ≥ +15%
- Q3环比Q2：营收环比 ≥ +10%，利润环比 ≥ +10%
- Q4环比Q3：营收环比 ≥ +15%，利润环比 ≥ +15%

**价值因子否决项（任何一条触发即降级）**：
1. 营收环比大涨但应收账款/合同负债同比例暴涨（虚增嫌疑）
2. 利润环比大涨但扣非净利润环比未同步（非经常性损益粉饰）
3. 毛利率环比下滑 > 3pct（以价换量，不可持续）
4. 单季度存货周转天数环比增加 > 20天（滞销信号）

#### P — Price Action（价格行为与动量因子）

**P1-Trend Template**：第二步的8条铁律

**P2-动量验证（RS Rating 体系，60日）**：

基于个股 vs 沪深300的相对强度（RS Rating，波动率调整，1-99分）及多维度动量指标：

**健康动量等级**：

| 等级 | RS Rating | MA20上方占比 | 趋势一致性 | 创20日新高 | 判定 |
|------|-----------|-------------|-----------|-----------|------|
| **S级** | 90-99 | >80% | >75% | ≥3次 | 顶级动量，机构主导的趋势领导者 |
| **A级** | 80-89 | >70% | >65% | ≥2次 | 强动量，符合SEPA超级绩效候选 |
| **B级** | 70-79 | >60% | >55% | ≥1次 | 中等动量，需结合VCP确认 |
| **C级** | 50-69 | <60% | <55% | 0-1次 | 弱势动量，缺乏领导地位 |
| **D级** | <50 | 任意 | 任意 | 任意 | 相对弱势，一票否决 |

**关键阈值**：
- RS Rating ≥ 70：SEPA超级绩效最低要求
- RS Rating ≥ 85：顶级领导者，优先配置
- MA20上方占比 > 70%：趋势持续性的重要标志
- 最大回撤 < 15%：风险可控
- 区间收益 > 20%：确认相对优势

**健康信号（可买入）**：
- RS Rating 在 80-95 区间，且呈上升趋势
- 创20日新高次数 ≥ 2 次，分布均匀（非连续逼空）
- 最大回撤 < 15%，收益/回撤比 > 2:1
- MA20上方占比 > 70%，趋势一致性强

**病态信号（一票否决）**：
- RS Rating < 50（相对弱势，不买补涨逻辑）
- 最大回撤 > 35%（波动过大，风险不可控）
- 收益/回撤比 < 1:1（风险回报不合理）
- 连续创20日新高 ≥ 5 次（动量透支，追高风险）

**P3-VCP结构**：基部质量、收缩次数、波动率递减、成交量配合

#### A — Announcement/Catalyst（催化剂）

- 识别近期/即将发生的催化剂：新产品、重大合同、监管批准、行业政策、业绩公告窗口、管理层变动
- **无明确催化剂**：仓位减半或仅观望
- 催化剂与 E-盈利增长共振为最佳

---

### 第四步：风险管理——铁律

#### 止损规则
- **技术止损**：设在 VCP 最后一次收缩低点下方 1-2%（通常风险 3-5%）
- **绝对硬止损**：入场价下方 **7-8%** 无条件离场
- **时间止损**：买入后 5-10 个交易日内无进展（未脱离入场区 3% 以上），主动减仓

#### 头寸规模
- 单笔交易风险 ≤ 账户总权益的 **1.25%~2.5%**
- **计算公式**：目标仓位 = 账户风险预算 ÷ (入场价 - 止损价)
  - 例：止损7% → 单笔仓位约18%账户；止损5% → 仓位可提升至25%账户
- **渐进式暴露**：
  1. 第一笔：试探仓位（目标仓位的50%）
  2. 若当日/次日收阳线且站稳枢轴点上方，加仓至100%目标仓位
  3. 若入场日即收阴/跌破枢轴点/放量滞涨：**立即砍掉试探仓位**

#### 盈亏比与退出
- **只参与潜在盈亏比 ≥ 2:1 的 setup**
- **部分止盈**：盈利 20-25% 时卖出 1/3 锁定利润
- **移动止损**：
  - 盈利 10-15% 后，止损移至**成本价**（保本）
  - 剩余仓位用 **20日均线** 或最近波动低点跟踪，跌破即清仓
- **阶段转换卖出**：确认进入 Stage 3（顶部派发，200DMA走平+放量震荡）时全部离场

---

### 第五步：市场环境过滤

- **大盘健康度**：沪深300/上证指数需处于 Stage 2 或至少高于200日均线
- **统计事实**：90%的成功突破发生在大盘健康环境中
- **降级规则**：
  - 大盘 Stage 4 或剧烈回调：即使个股 setup 完美，仓位减半或空仓
  - 大盘横盘震荡：只参与 RS 90+ 的顶级领导者，且止损收紧至5%

---

## 评分体系（48分制）

| 维度 | 满分 | 评分细则 |
|------|------|---------|
| S-入场点 | 10分 | VCP质量(4)、枢轴点清晰度(3)、突破确认(3) |
| E-盈利 | 13分 | 同比层(10分)：EPS加速(3)+营收加速(2)+利润率(2)+预期修正(2)+ROE(1)；环比加成(3分)：路径A/B+1、路径C+2、连续2季Code33+3 |
| P-价格行为 | 15分 | P1-Trend Template(5)、P2-动量验证(5)、P3-VCP结构(5) |
| A-催化剂 | 10分 | 明确且近期(8-10)、模糊(4-7)、无(0-3) |
| **总分** | **48分** | |

**评分映射**：
- ≥ 40分：强烈买入（超级绩效候选）
- 32-39分：买入
- 24-31分：观察等待
- < 24分：放弃

---

## 输出格式（必须严格遵循）

```markdown
## [股票代码/名称] SEPA超级绩效分析报告

### 1. 阶段判定
- 当前阶段：Stage 1/2/3/4
- 200日均线方向：上升/走平/下降
- 50/150/200日均线排列：多头排列/非多头排列
- 结论：通过 / 否决（终止分析）

### 2. 趋势模板检查（8条铁律）
| # | 标准 | 状态 | 数值 |
|---|------|------|------|
| 1 | 当前价 > 150日均线 | Pass/Fail | ... |
| 2 | 当前价 > 200日均线 | Pass/Fail | ... |
| 3 | 150日均线 > 200日均线 | Pass/Fail | ... |
| 4 | 200日均线向上 | Pass/Fail | ... |
| 5 | 50日均线 > 150/200日均线 | Pass/Fail | ... |
| 6 | 当前价 ≥ 52周低点130% | Pass/Fail | ... |
| 7 | 当前价在52周高点25%以内 | Pass/Fail | ... |
| 8 | RS相对强度 ≥ 70 | Pass/Fail | ... |
- **模板结论**：通过 / 未通过（第X条Fail，终止分析）

### 3. SEPA评分（满分48分）
| 维度 | 得分 | 满分 | 说明 |
|------|------|------|------|
| S-入场点 | X | 10 | ... |
| E-盈利 | X | 13 | ... |
| P-价格行为 | X | 15 | ... |
| A-催化剂 | X | 10 | ... |
| **总分** | **XX/48** | | **评级：强烈买入/买入/观察/放弃** |

### 4. E-盈利深度分析
**同比层**：
- EPS同比增速：Q1/X% → Q2/X% → Q3/X%（加速/减速）
- 营收同比增速：...
- 利润率变化：...
- ROE：...

**环比价值因子**：
- 路径判定：路径A/B/C/未通过
- 季节性修正：已修正/无需修正/异常
- 否决项检查：通过 / 触发（具体项）

### 5. P-价格行为与动量验证
**P1-Trend Template**：已在上文检查

**P2-动量验证（RS Rating 体系）**：
| 指标 | 数值 | 状态 |
|------|------|------|
| RS Rating | XX/99 | S/A/B/C/D |
| SEPA评分 | XX/100 | — |
| 动量等级 | X级 | — |
| 区间收益 | X% | — |
| 最大回撤 | X% | — |
| MA20上方占比 | X% | — |
| 趋势一致性 | X% | — |
| 创20日新高次数 | X次 | — |
- 动量健康度：健康 / 过热 / 弱势
- 交叉验证：与Trend Template匹配 / 不匹配

**P3-VCP结构**：
- 收缩次数：X次
- 波动率递减：第1次-X% → 第2次-X% → 第3次-X%
- 枢轴点价格：XX.XX元（+0.5%缓冲 = XX.XX元）
- 当前价距枢轴点：+X%（已突破/未触发/回踩中）
- 20日均量：XX万手，今日量：XX万手（放量X%）

### 6. 60分钟精修（第二轮）
- 60分钟结构：{minutely_structure}
- RSI状态：{rsi_status}
- 当前位置：{position}
- 精修建议：{refined_entry}
- 触发条件：{trigger_condition}
- 精修置信度：{confidence}

### 7. 交易计划
- **触发价**：XX.XX元（枢轴点+缓冲）
- **止损价**：XX.XX元（VCP低点-1% / 硬止损-7%）
- **目标价**：XX.XX元（前高/测量移动/2:1盈亏比）
- **建议仓位**：X%账户（基于X%止损距离，1.5%账户风险）
- **盈亏比**：1:X
- **渐进式暴露**：试探50% → 确认后加满 / 失败即砍

### 8. 市场环境
- 大盘阶段：Stage X
- 对个股影响：正常 / 仓位减半 / 空仓观望

### 9. 风险排查
- 减持/预亏/监管/解禁/政策利空：有（具体）/ 无
- PE估值状态：合理 / 偏高（需警惕）

### 10. 最终结论
- **强烈买入** / **买入** / **观察等待** / **放弃**
- 一句话总结：...
```

---

## 绝对禁止清单（与超级绩效冲突的行为）

以下行为**严格禁止**，与本提示词冲突时以本清单为准：

1. **禁止在 Stage 1/3/4 中交易**（删除"抄底回踩"思维）
2. **禁止买入 Trend Template 未通过的股票**
3. **禁止无 VCP 结构时"预判"买入**（不买基部左侧）
4. **禁止在突破前买入**（必须等价格突破枢轴点+成交量确认）
5. **禁止亏损加仓摊低成本**
6. **禁止买入 RS<70 的弱势股**（不买"补涨"逻辑）
7. **禁止无止损持仓**
8. **禁止在股价偏离MA5>5%时"观望"**——Minervini恰恰在突破时买入，此时往往已偏离MA5，但风险由VCP结构定义而非乖离率
9. **禁止买入 C级/D级动量股票**（弱势股/相对弱势/风险不可控）
10. **禁止在价值因子否决项触发时重仓**（财务化妆股）

---

## 特别说明

- 本提示词基于 Mark Minervini 两届美国投资冠军（1997年+255%，2021年+334.8%）的 SEPA 方法论，结合 A 股涨跌停制度与季度报告特征本土化改造。
- "超级绩效"的核心不是预测，而是**在正确的时机，以正确的风险单位，买入正确的股票**。
- 当分析结果模糊或数据不足时，**默认选择放弃**，因为现金也是一种仓位。
"""

TECHNICAL_SKILL_RULES_EN = """## Default Skill Baseline

Treat the currently activated skills as the primary analysis lens, but keep the
following default risk controls as the shared baseline:

- Bullish alignment: MA5 > MA10 > MA20
- Bias from MA5 < 2% -> ideal buy zone; 2-5% -> small position; > 5% -> no chase
- Shrink-pullback to MA5 is the preferred entry rhythm
- Below MA20 -> hold off unless the active skill explicitly proves a better setup
"""


def get_default_trading_skill_policy(*, explicit_skill_selection: bool) -> str:
    """Return the legacy default trading baseline only for implicit/default runs.

    When a caller explicitly chooses a skill (via request payload or config),
    analysis should follow that selected skill alone instead of silently
    layering the old bull-trend baseline on top.
    """
    if explicit_skill_selection:
        return ""
    return CORE_TRADING_SKILL_POLICY_ZH


def get_default_technical_skill_policy(*, explicit_skill_selection: bool) -> str:
    """Return the technical-agent baseline only for implicit/default runs."""
    if explicit_skill_selection:
        return ""
    return TECHNICAL_SKILL_RULES_EN


@lru_cache(maxsize=1)
def _load_builtin_skill_catalog() -> tuple[object, ...]:
    try:
        from src.agent.skills.base import load_skills_from_directory

        return tuple(load_skills_from_directory(_BUILTIN_SKILLS_DIR))
    except Exception:
        return ()


def _coerce_priority(value: object, default: int = 100) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_available_ids(available_skill_ids: Optional[Iterable[str]]) -> List[str]:
    normalized: List[str] = []
    if available_skill_ids is None:
        return normalized
    for skill_id in available_skill_ids:
        if isinstance(skill_id, str):
            cleaned = skill_id.strip()
            if cleaned and cleaned not in normalized:
                normalized.append(cleaned)
    return normalized


def _normalize_skill_inputs(
    skills: Optional[Iterable[object]],
    available_skill_ids: Optional[Iterable[str]] = None,
) -> tuple[List[object], List[str]]:
    normalized_available = _normalize_available_ids(available_skill_ids)

    if skills is None:
        return list(_load_builtin_skill_catalog()), normalized_available

    skill_pool: List[object] = []
    for item in skills:
        if isinstance(item, str):
            cleaned = item.strip()
            if cleaned and cleaned not in normalized_available:
                normalized_available.append(cleaned)
            continue
        if item is not None:
            skill_pool.append(item)
    return skill_pool, normalized_available


def _sort_skill_pool(skills: Iterable[object]) -> List[object]:
    return sorted(
        skills,
        key=lambda skill: (
            _coerce_priority(getattr(skill, "default_priority", 100)),
            str(getattr(skill, "display_name", "") or getattr(skill, "name", "")),
            str(getattr(skill, "name", "")),
        ),
    )


def _iter_candidate_skills(
    skills: Optional[Iterable[object]],
    *,
    available_skill_ids: Optional[Iterable[str]] = None,
    user_invocable_only: bool = True,
) -> tuple[List[object], List[str]]:
    skill_pool, normalized_available = _normalize_skill_inputs(skills, available_skill_ids)
    available_lookup = set(normalized_available)

    candidates: List[object] = []
    for skill in _sort_skill_pool(skill_pool):
        skill_id = str(getattr(skill, "name", "")).strip()
        if not skill_id:
            continue
        if user_invocable_only and not bool(getattr(skill, "user_invocable", True)):
            continue
        if available_lookup and skill_id not in available_lookup:
            continue
        candidates.append(skill)

    return candidates, normalized_available


def _slice_skill_ids(skill_ids: List[str], max_count: Optional[int]) -> List[str]:
    if max_count is None:
        return skill_ids
    return skill_ids[:max_count]


def _pick_primary_default_skill_id(candidates: List[object]) -> str:
    preferred = [
        str(getattr(skill, "name", "")).strip()
        for skill in candidates
        if bool(getattr(skill, "default_active", False))
    ]
    if preferred:
        return preferred[0]

    fallback = [str(getattr(skill, "name", "")).strip() for skill in candidates]
    if fallback:
        return fallback[0]

    return ""


def get_default_active_skill_ids(
    skills: Optional[Iterable[object]] = None,
    max_count: Optional[int] = None,
    available_skill_ids: Optional[Iterable[str]] = None,
) -> List[str]:
    candidates, normalized_available = _iter_candidate_skills(
        skills,
        available_skill_ids=available_skill_ids,
    )
    default_skill_id = _pick_primary_default_skill_id(candidates)
    if default_skill_id:
        return _slice_skill_ids([default_skill_id], max_count)

    return _slice_skill_ids(normalized_available[:1], max_count)


def get_default_router_skill_ids(
    skills: Optional[Iterable[object]] = None,
    max_count: Optional[int] = None,
    available_skill_ids: Optional[Iterable[str]] = None,
) -> List[str]:
    candidates, normalized_available = _iter_candidate_skills(
        skills,
        available_skill_ids=available_skill_ids,
    )
    preferred = [
        str(getattr(skill, "name", "")).strip()
        for skill in candidates
        if bool(getattr(skill, "default_router", False))
    ]
    if preferred:
        return _slice_skill_ids(preferred, max_count)

    return get_default_active_skill_ids(
        candidates,
        max_count=max_count,
        available_skill_ids=normalized_available,
    )


def get_regime_skill_ids(
    regime: str,
    skills: Optional[Iterable[object]] = None,
    max_count: Optional[int] = None,
    available_skill_ids: Optional[Iterable[str]] = None,
) -> List[str]:
    candidates, normalized_available = _iter_candidate_skills(
        skills,
        available_skill_ids=available_skill_ids,
    )
    regime_name = (regime or "").strip().lower()
    if regime_name:
        matched = []
        for skill in candidates:
            market_regimes = getattr(skill, "market_regimes", None) or []
            normalized_regimes = {
                str(item).strip().lower()
                for item in market_regimes
                if str(item).strip()
            }
            if regime_name in normalized_regimes:
                matched.append(str(getattr(skill, "name", "")).strip())
        if matched:
            return _slice_skill_ids(matched, max_count)

    return get_default_router_skill_ids(
        candidates,
        max_count=max_count,
        available_skill_ids=normalized_available,
    )


def get_primary_default_skill_id(
    skills: Optional[Iterable[object]] = None,
    available_skill_ids: Optional[Iterable[str]] = None,
) -> str:
    defaults = get_default_active_skill_ids(skills, max_count=1, available_skill_ids=available_skill_ids)
    return defaults[0] if defaults else ""


def _build_regime_skill_ids(skills: Iterable[object]) -> Dict[str, List[str]]:
    regime_map: Dict[str, List[str]] = {}
    for skill in _sort_skill_pool(skills):
        skill_id = str(getattr(skill, "name", "")).strip()
        if not skill_id:
            continue
        for regime in getattr(skill, "market_regimes", None) or []:
            regime_name = str(regime).strip().lower()
            if not regime_name:
                continue
            regime_map.setdefault(regime_name, []).append(skill_id)
    return regime_map


DEFAULT_ACTIVE_SKILL_IDS: tuple[str, ...] = tuple(get_default_active_skill_ids())
DEFAULT_ROUTER_SKILL_IDS: tuple[str, ...] = tuple(get_default_router_skill_ids())
PRIMARY_DEFAULT_SKILL_ID = get_primary_default_skill_id()
REGIME_SKILL_IDS: Dict[str, List[str]] = _build_regime_skill_ids(_load_builtin_skill_catalog())


def build_skill_agent_name(skill_id: str) -> str:
    return f"{SKILL_AGENT_PREFIX}{skill_id}"


def extract_skill_id(agent_name: Optional[str]) -> Optional[str]:
    if not agent_name or not isinstance(agent_name, str):
        return None
    for prefix in (SKILL_AGENT_PREFIX, LEGACY_STRATEGY_AGENT_PREFIX):
        if agent_name.startswith(prefix):
            return agent_name[len(prefix):]
    return None


def is_skill_agent_name(agent_name: Optional[str]) -> bool:
    return extract_skill_id(agent_name) is not None


def is_skill_consensus_name(agent_name: Optional[str]) -> bool:
    return agent_name in {SKILL_CONSENSUS_AGENT_NAME, LEGACY_STRATEGY_CONSENSUS_AGENT_NAME}
