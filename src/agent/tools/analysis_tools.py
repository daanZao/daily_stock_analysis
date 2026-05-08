# -*- coding: utf-8 -*-
"""
Analysis tools — wraps StockTrendAnalyzer as an agent-callable tool.

Tools:
- analyze_trend: comprehensive technical trend analysis
"""

import logging
from typing import Optional, Dict, Any

import numpy as np

from src.agent.tools.registry import ToolParameter, ToolDefinition

logger = logging.getLogger(__name__)


def _fetch_trend_data(stock_code: str):
    """Fetch historical OHLCV (DataFrame) for trend analysis. DB first, then DataFetcher fallback."""
    from src.services.history_loader import load_history_df

    df, _ = load_history_df(stock_code, days=60)
    return df


def _handle_analyze_trend(stock_code: str) -> dict:
    """Run technical trend analysis on a stock."""
    from src.stock_analyzer import StockTrendAnalyzer

    if not (stock_code and str(stock_code).strip()):
        return {"error": "stock_code is required"}

    df = _fetch_trend_data(stock_code)
    if df is None or df.empty:
        return {"error": f"No historical data available for trend analysis on {stock_code}"}

    if len(df) < 20:
        return {"error": f"Insufficient data for trend analysis on {stock_code} (need >= 20 days)"}

    analyzer = StockTrendAnalyzer()
    try:
        result = analyzer.analyze(df, stock_code)
    except Exception:
        logger.warning("analyze_trend(%s): Trend analysis failed", stock_code, exc_info=True)
        return {"error": f"Trend analysis failed for {stock_code}"}

    return {
        "code": result.code,
        "trend_status": result.trend_status.value,
        "ma_alignment": result.ma_alignment,
        "trend_strength": result.trend_strength,
        "ma5": result.ma5,
        "ma10": result.ma10,
        "ma20": result.ma20,
        "ma60": result.ma60,
        "current_price": result.current_price,
        "bias_ma5": round(result.bias_ma5, 2),
        "bias_ma10": round(result.bias_ma10, 2),
        "bias_ma20": round(result.bias_ma20, 2),
        "volume_status": result.volume_status.value,
        "volume_ratio_5d": round(result.volume_ratio_5d, 2),
        "volume_trend": result.volume_trend,
        "support_ma5": result.support_ma5,
        "support_ma10": result.support_ma10,
        "resistance_levels": result.resistance_levels,
        "support_levels": result.support_levels,
        "macd_dif": round(result.macd_dif, 4),
        "macd_dea": round(result.macd_dea, 4),
        "macd_bar": round(result.macd_bar, 4),
        "macd_status": result.macd_status.value,
        "macd_signal": result.macd_signal,
        "rsi_6": round(result.rsi_6, 2),
        "rsi_12": round(result.rsi_12, 2),
        "rsi_24": round(result.rsi_24, 2),
        "rsi_status": result.rsi_status.value,
        "rsi_signal": result.rsi_signal,
        "buy_signal": result.buy_signal.value,
        "signal_score": result.signal_score,
        "signal_reasons": result.signal_reasons,
        "risk_factors": result.risk_factors,
    }


analyze_trend_tool = ToolDefinition(
    name="analyze_trend",
    description="对股票进行全面的技术面趋势分析。"
                "从数据库或数据源获取历史数据。"
                "返回均线排列、乖离率、MACD 状态、RSI 水平、"
                "量能分析、支撑/阻力位，以及买卖信号评分（0-100）。",
    parameters=[
        ToolParameter(
            name="stock_code",
            type="string",
            description="要分析的股票代码，例如 '600519'",
        ),
    ],
    handler=_handle_analyze_trend,
    category="analysis",
)


# ============================================================
# calculate_ma — flexible moving average calculator
# ============================================================

def _handle_calculate_ma(stock_code: str, periods: Optional[str] = None, days: int = 120) -> dict:
    """Calculate moving averages for arbitrary periods from historical K-line data."""
    from src.services.history_loader import load_history_df

    df, source = load_history_df(stock_code, days=days)

    if df is None or df.empty:
        return {"error": f"No historical data for {stock_code}"}

    # Parse requested periods (default: 5,10,20,30,60,120,250)
    default_periods = [5, 10, 20, 50, 60, 150, 200]
    if periods:
        try:
            requested = [int(p.strip()) for p in periods.split(",") if p.strip().isdigit()]
            period_list = sorted(set(requested)) if requested else default_periods
        except Exception:
            period_list = default_periods
    else:
        period_list = default_periods

    close = df["close"]
    current_price = float(close.iloc[-1])
    result: dict = {
        "code": stock_code,
        "source": source,
        "current_price": round(current_price, 2),
        "data_points": len(df),
        "ma": {},
    }

    for period in period_list:
        if len(close) < period:
            result["ma"][f"ma{period}"] = None
            continue
        ma_val = float(close.rolling(window=period).mean().iloc[-1])
        bias = round((current_price - ma_val) / ma_val * 100, 2) if ma_val else None
        result["ma"][f"ma{period}"] = {
            "value": round(ma_val, 2),
            "bias_pct": bias,
            "price_above": current_price > ma_val,
        }

    # Summary: how many MAs is the price above?
    ma_values = [v for v in result["ma"].values() if v is not None]
    above_count = sum(1 for v in ma_values if v["price_above"])
    result["above_ma_count"] = above_count
    result["total_ma_count"] = len(ma_values)
    result["ma_alignment"] = (
        "多头排列" if above_count == len(ma_values)
        else "空头排列" if above_count == 0
        else f"混合({above_count}/{len(ma_values)}条均线上方)"
    )
    return result


calculate_ma_tool = ToolDefinition(
    name="calculate_ma",
    description="计算股票的移动平均线（MA5/10/20/50/60/150/200 或自定义周期）。"
                "返回各均线数值、价格乖离率、以及价格是否在各均线上方。"
                "同时返回整体均线排列状态（多头/空头/混合）。"
                "SEPA 趋势模板需要 50/150/200 日均线。",
    parameters=[
        ToolParameter(
            name="stock_code",
            type="string",
            description="股票代码，例如 '600519'",
        ),
        ToolParameter(
            name="periods",
            type="string",
            description="要计算的均线周期，逗号分隔（默认：'5,10,20,50,60,150,200'）。"
                        "例如：'5,10,20,60'",
            required=False,
            default="5,10,20,50,60,150,200",
        ),
        ToolParameter(
            name="days",
            type="integer",
            description="获取历史数据的交易天数（默认：120）",
            required=False,
            default=120,
        ),
    ],
    handler=_handle_calculate_ma,
    category="analysis",
)


# ============================================================
# get_volume_analysis — volume-price relationship analysis
# ============================================================

def _handle_get_volume_analysis(stock_code: str, days: int = 30) -> dict:
    """量价分析：全局结构 -> 特殊单日 -> 最近5日演变。"""
    from src.services.history_loader import load_history_df
    import pandas as pd
    import numpy as np

    # 加载足够长的历史用于计算20日基准和平台识别
    df, source = load_history_df(stock_code, days=max(days + 40, 90))
    if df is None or df.empty:
        return {"error": f"{stock_code} 无历史数据"}

    required = ['open', 'high', 'low', 'close', 'volume']
    if not all(c in df.columns for c in required):
        return {"error": f"数据列缺失，需要 {required}"}

    for c in required:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df = df.dropna(subset=required)
    if len(df) < 25:
        return {"error": f"数据不足（仅 {len(df)} 天，需 >= 25 天）"}

    # ========== 基础指标计算（基于完整历史） ==========
    df['ma20'] = df['close'].rolling(20).mean()
    df['ma20_vol'] = df['volume'].rolling(20).mean()
    df['pct_change'] = df['close'].pct_change() * 100
    df['body'] = df['close'] - df['open']
    df['body_pct'] = abs(df['body']) / df['open'] * 100
    df['upper_shadow'] = df['high'] - df[['open', 'close']].max(axis=1)
    df['lower_shadow'] = df[['open', 'close']].min(axis=1) - df['low']
    df['is_up'] = df['close'] > df['open']
    df['is_yang'] = df['close'] >= df['open']

    # 平台与极值（用于识别突破）
    df['high_20'] = df['high'].rolling(20).max()
    df['low_20'] = df['low'].rolling(20).min()
    df['vol_ratio'] = df['volume'] / df['ma20_vol']
    df['max_vol_20'] = df['volume'].rolling(20).max()
    df['min_vol_20'] = df['volume'].rolling(20).min()
    df['is_new_high'] = df['close'] >= df['high_20'].shift(1) * 0.998
    df['is_new_low'] = df['close'] <= df['low_20'].shift(1) * 1.002
    df['is_above_ma20'] = df['close'] > df['ma20']
    df['platform_high_5'] = df['high'].rolling(5).max().shift(1)

    # 截取分析窗口
    adf = df.tail(days).copy()

    # ========== 第一步：全局量价配合全貌 ==========
    # 1.1 健康量价同步率（涨放量+跌缩量 的天数占比）
    adf['healthy_pv'] = (
        (adf['is_up'] & (adf['volume'] > adf['ma20_vol'])) |
        (~adf['is_up'] & (adf['volume'] < adf['ma20_vol']))
    )
    healthy_pv_ratio = round(adf['healthy_pv'].mean(), 2)

    # 1.2 量价趋势一致性（价格MA5斜率 vs 成交量MA5斜率）
    adf['price_ma5'] = adf['close'].rolling(5).mean()
    adf['vol_ma5'] = adf['volume'].rolling(5).mean()
    adf['price_slope'] = adf['price_ma5'].diff()
    adf['vol_slope'] = adf['vol_ma5'].diff()

    aligned = ((adf['price_slope'] > 0) & (adf['vol_slope'] > 0)) | \
              ((adf['price_slope'] < 0) & (adf['vol_slope'] < 0))
    aligned_count = int(aligned.sum())
    valid_slope_days = int((adf['price_slope'].notna() & adf['vol_slope'].notna()).sum())
    alignment_ratio = round(aligned_count / valid_slope_days, 2) if valid_slope_days > 0 else 0

    # 1.3 持续性背离检测（连续3天以上价涨量缩/价跌量增）
    adf['vol_vs_ma20'] = adf['volume'] > adf['ma20_vol']
    adf['price_up'] = adf['pct_change'] > 0
    adf['divergence'] = (
        (adf['price_up'] & ~adf['vol_vs_ma20']) |   # 涨缩量
        (~adf['price_up'] & adf['vol_vs_ma20'])      # 跌放量
    )

    divergences = []
    streak = 0
    streak_start = None
    for idx, val in adf['divergence'].items():
        if val:
            if streak == 0:
                streak_start = idx
            streak += 1
        else:
            if streak >= 3:
                t = "涨缩量" if adf.loc[streak_start, 'price_up'] else "跌放量"
                divergences.append({
                    "start": str(streak_start),
                    "duration": streak,
                    "type": t,
                    "avg_price_chg": round(adf.loc[streak_start:idx].iloc[:streak]['pct_change'].mean(), 2)
                })
            streak = 0
    if streak >= 3:
        t = "涨缩量" if adf.loc[streak_start, 'price_up'] else "跌放量"
        divergences.append({
            "start": str(streak_start),
            "duration": streak,
            "type": t,
            "avg_price_chg": round(adf.loc[streak_start:]['pct_change'].mean(), 2)
        })

    # 1.4 波段量能对比（上涨日 vs 下跌日的量能，非简单平均，带权重）
    up_days = adf[adf['price_up']]
    down_days = adf[~adf['price_up']]
    up_vol_avg = float(up_days['volume'].mean()) if len(up_days) > 0 else 0
    down_vol_avg = float(down_days['volume'].mean()) if len(down_days) > 0 else 0
    up_vol_sum = float(up_days['volume'].sum()) if len(up_days) > 0 else 0
    down_vol_sum = float(down_days['volume'].sum()) if len(down_days) > 0 else 0

    # 1.5 量价相关系数（涨跌幅与成交量，而非价格与成交量）
    vp_corr = None
    if len(adf.dropna(subset=['pct_change', 'volume'])) >= 10:
        vp_corr = round(float(adf['pct_change'].corr(adf['volume'])), 3)

    # 全局定性
    if healthy_pv_ratio > 0.6 and alignment_ratio > 0.6:
        global_pattern = "全局量价配合良好，趋势与量能同步"
    elif len(divergences) >= 2:
        global_pattern = f"全局出现{len(divergences)}次持续性量价背离，趋势可信度低"
    elif up_vol_avg > down_vol_avg * 1.5:
        global_pattern = "上涨放量、下跌缩量，多头主导"
    elif down_vol_avg > up_vol_avg * 1.5:
        global_pattern = "下跌放量、上涨缩量，空头主导或派发阶段"
    else:
        global_pattern = "全局量价关系中性，无明显主导力量"

    # ========== 第二步：特殊单日识别 ==========
    special_days = []
    avg_vol_20 = float(df['ma20_vol'].iloc[-1])

    for idx, row in adf.iterrows():
        date_str = str(idx)
        vr = row['vol_ratio']
        if pd.isna(vr):
            continue

        is_huge = vr > 2.0
        is_large = vr > 1.5 and (row['volume'] >= row['max_vol_20'] * 0.999)
        is_shrink = vr < 0.5
        is_tiny = vr < 0.3 and (row['volume'] <= row['min_vol_20'] * 1.001)

        day_type = None
        desc = None

        # --- 巨量分类 ---
        if is_huge or is_large:
            # 突破型：放量+大阳线+创高/突破平台
            if (row['body_pct'] > 2 or row['pct_change'] > 3) and row['is_yang'] and \
               (row['is_new_high'] or row['close'] > row['platform_high_5']):
                day_type = "巨量突破"
                desc = f"放量{vr:.1f}倍，大阳线突破近期平台/高点，多头进攻"

            # 天量天价/滞涨：放量+长上影+高位
            elif row['upper_shadow'] > abs(row['body']) * 1.0 and row['is_new_high']:
                day_type = "天量天价/滞涨"
                desc = f"放量{vr:.1f}倍，创高但长上影({row['upper_shadow']:.2f})，高位抛压显现"

            # 巨量下影吸筹：放量+长下影+拉起
            elif row['lower_shadow'] > abs(row['body']) * 1.2 and row['close'] > row['low'] + (row['high']-row['low'])*0.6:
                day_type = "巨量下影吸筹"
                desc = f"放量{vr:.1f}倍，长下影({row['lower_shadow']:.2f})后拉起，恐慌盘涌出后被承接"

            # 巨量恐慌：放量+大阴线+破低
            elif (row['body_pct'] > 2 or row['pct_change'] < -3) and not row['is_yang'] and row['is_new_low']:
                day_type = "巨量恐慌"
                desc = f"放量{vr:.1f}倍，大阴线跌破近期低点，恐慌抛售"

            # 巨量换手：放量+小实体
            elif row['body_pct'] < 1.5:
                day_type = "巨量换手"
                desc = f"放量{vr:.1f}倍，实体极小，多空激烈博弈或主力对倒"

            else:
                day_type = "巨量异动"
                desc = f"成交量异常放大{vr:.1f}倍，需结合位置判断"

        # --- 缩量分类 ---
        elif is_tiny or is_shrink:
            # 缩量整理
            if abs(row['pct_change']) < 1.5 and row['body_pct'] < 1.5:
                day_type = "缩量整理"
                desc = f"缩量至{vr:.1f}倍，波动极小，蓄势或无人问津"

            # 缩量暴涨/惜售
            elif row['pct_change'] > 3 and row['is_yang']:
                day_type = "缩量暴涨/惜售"
                desc = f"缩量至{vr:.1f}倍，大涨{row['pct_change']:.1f}%，筹码锁定良好，抛压轻"

            # 缩量暴跌/流动性枯竭
            elif row['pct_change'] < -3 and not row['is_yang']:
                day_type = "缩量暴跌/流动性枯竭"
                desc = f"缩量至{vr:.1f}倍，大跌{abs(row['pct_change']):.1f}%，无量空跌，无人接盘"

            # 地量
            elif row['volume'] <= row['min_vol_20'] * 1.001:
                day_type = "地量"
                desc = f"地量（{vr:.1f}倍20日均量），变盘前兆或极度低迷"

            else:
                day_type = "明显缩量"
                desc = f"成交量明显萎缩至{vr:.1f}倍"

        if day_type:
            special_days.append({
                "date": date_str,
                "type": day_type,
                "description": desc,
                "volume_ratio": round(vr, 2),
                "change_pct": round(row['pct_change'], 2),
                "close": round(row['close'], 2)
            })

    # ========== 第三步：最近5日量能与量价演变 ==========
    r5 = adf.tail(5).copy()
    r5_list = []

    for idx, row in r5.iterrows():
        vr = row['vol_ratio'] if not pd.isna(row['vol_ratio']) else 1.0
        chg = row['pct_change']
        is_up = row['is_up']

        # 精细分类
        if is_up and vr > 1.5:
            relation = "强上涨放量"
        elif is_up and vr > 1.0:
            relation = "温和上涨放量"
        elif is_up and vr < 0.6:
            relation = "上涨极度缩量"
        elif is_up:
            relation = "上涨平量"
        elif not is_up and vr > 1.5:
            relation = "强下跌放量"
        elif not is_up and vr > 1.0:
            relation = "温和下跌放量"
        elif not is_up and vr < 0.6:
            relation = "下跌极度缩量"
        else:
            relation = "下跌平量"

        r5_list.append({
            "date": str(idx),
            "change_pct": round(chg, 2),
            "volume_ratio": round(vr, 2),
            "relation": relation
        })

    # 最近5日量能趋势（逐日环比）
    vol_chgs = r5['volume'].pct_change().dropna() * 100
    if len(vol_chgs) >= 3:
        recent_3 = vol_chgs.tail(3).tolist()
        if all(x > 5 for x in recent_3):
            vol_trend = "量能连续递增，资金持续流入"
        elif all(x < -5 for x in recent_3):
            vol_trend = "量能连续递减，参与度降温"
        elif vol_chgs.mean() > 20:
            vol_trend = "整体显著放量"
        elif vol_chgs.mean() < -20:
            vol_trend = "整体显著缩量"
        else:
            vol_trend = "量能波动无序，无明显方向"
    else:
        vol_trend = "数据不足"

    # 最近5日量价质量
    r5_up = r5[r5['is_up']]
    r5_down = r5[~r5['is_up']]
    if len(r5_up) >= 2 and len(r5_down) >= 1:
        r5_up_vol = r5_up['volume'].mean()
        r5_down_vol = r5_down['volume'].mean()
        if r5_up_vol > r5_down_vol * 1.5:
            r5_quality = "上涨放量、回调缩量，短期健康"
        elif r5_up_vol < r5_down_vol * 0.7:
            r5_quality = "上涨缩量、回调/下跌放量，短期背离"
        else:
            r5_quality = "短期量价关系中性"
    else:
        r5_quality = "涨跌样本不均，短期难以定性"

    # 最近5日关键信号提取
    r5_signals = [d for d in special_days if d['date'] in [str(i) for i in r5.index]]

    # ========== 综合结论 ==========
    parts = []
    parts.append(f"【全局】{global_pattern}。健康量价同步率{healthy_pv_ratio*100:.0f}%，趋势-量能一致性{align_ratio*100:.0f}%。")
    if divergences:
        parts.append(f"期间出现{len(divergences)}次持续性量价背离（最长{divergences[-1]['duration']}天）。")
    if special_days:
        parts.append(f"【特殊日】共识别{len(special_days)}个异常量能日："
                     f"{', '.join(list(dict.fromkeys([d['type'] for d in special_days]))[:5])}等。")
    else:
        parts.append("【特殊日】期间无显著异常量能日。")
    parts.append(f"【近5日】{vol_trend}；{r5_quality}。"
                 f"最新量能/20日均量={r5_list[-1]['volume_ratio']:.2f}倍。")
    if r5_signals:
        parts.append(f"近5日含{r5_signals[-1]['type']}（{r5_signals[-1]['date']}），需警惕。")

    summary = "".join(parts)

    return {
        "code": stock_code,
        "source": source,
        "period_days": len(adf),
        "summary": summary,
        "global": {
            "pattern": global_pattern,
            "healthy_sync_ratio": healthy_pv_ratio,
            "trend_alignment_ratio": alignment_ratio,
            "divergence_events": divergences,
            "up_day_avg_volume": round(up_vol_avg, 0),
            "down_day_avg_volume": round(down_vol_avg, 0),
            "up_vs_down_vol_ratio": round(up_vol_avg / down_vol_avg, 2) if down_vol_avg > 0 else None,
            "volume_price_corr": vp_corr
        },
        "special_days": {
            "count": len(special_days),
            "days": special_days
        },
        "recent_5": {
            "daily": r5_list,
            "vol_trend": vol_trend,
            "pv_quality": r5_quality,
            "special_signals_in_r5": r5_signals,
            "latest_volume_ratio_vs_20d": round(float(r5['vol_ratio'].iloc[-1]), 2)
        }
    }


get_volume_analysis_tool = ToolDefinition(
    name="get_volume_analysis",
    description="分析股票的量价关系。返回量能比率、"
                "上涨日 vs 下跌日的平均成交量、量能趋势（放大/萎缩），"
                "以及量价形态解读（量价配合/背离）。用于确认趋势"
                "强度和识别派发或吸筹阶段。",
    parameters=[
        ToolParameter(
            name="stock_code",
            type="string",
            description="股票代码，例如 '600519'",
        ),
        ToolParameter(
            name="days",
            type="integer",
            description="分析的最近交易天数（默认：30）",
            required=False,
            default=30,
        ),
    ],
    handler=_handle_get_volume_analysis,
    category="analysis",
)


# ============================================================
# analyze_pattern — candlestick / chart pattern recognition
# ============================================================

def _handle_analyze_pattern(stock_code: str, days: int = 60) -> dict:
    """Detect common candlestick and chart patterns in recent price history."""
    from src.services.history_loader import load_history_df

    df, source = load_history_df(stock_code, days=max(days, 120))

    if df is None or df.empty:
        return {"error": f"No historical data for {stock_code}"}

    df = df.tail(days).copy().reset_index(drop=True)
    if len(df) < 10:
        return {"error": f"Insufficient data for pattern analysis (got {len(df)} days, need >= 10)"}

    o = df["open"].values
    h = df["high"].values
    l = df["low"].values   # noqa: E741
    c = df["close"].values
    v = df["volume"].values if "volume" in df.columns else None

    patterns_detected = []
    n = len(c)

    # ---- Helpers ----
    def body(i):
        return abs(c[i] - o[i])

    def upper_shadow(i):
        return h[i] - max(c[i], o[i])

    def lower_shadow(i):
        return min(c[i], o[i]) - l[i]

    def is_bullish(i):
        return c[i] > o[i]

    def is_bearish(i):
        return c[i] < o[i]

    avg_body = sum(body(i) for i in range(n)) / n if n > 0 else 1

    # --- Single-candle patterns (last 3 days) ---
    for i in range(max(0, n - 3), n):
        bd = body(i)
        us = upper_shadow(i)
        ls = lower_shadow(i)

        # Doji
        if bd < avg_body * 0.1 and (us + ls) > bd * 3:
            patterns_detected.append({
                "pattern": "十字星 (Doji)", "type": "reversal_signal",
                "day_offset": -(n - 1 - i),
                "strength": "弱", "desc": "多空平衡，可能变盘信号"
            })

        # Hammer / Hanging Man
        if ls > body(i) * 2 and us < body(i) * 0.5:
            label = "锤子线 (Hammer)" if i == 0 or c[i] >= c[i - 1] else "上吊线 (Hanging Man)"
            patterns_detected.append({
                "pattern": label, "type": "reversal_signal",
                "day_offset": -(n - 1 - i),
                "strength": "中", "desc": "下影线长，潜在支撑/反转"
            })

        # Shooting Star / Inverted Hammer
        if us > body(i) * 2 and ls < body(i) * 0.5:
            label = "流星线 (Shooting Star)" if is_bearish(i) else "倒锤子"
            patterns_detected.append({
                "pattern": label, "type": "bearish_signal",
                "day_offset": -(n - 1 - i),
                "strength": "中", "desc": "上影线长，潜在压力/反转"
            })

        # Big bullish / bearish candle
        if bd > avg_body * 2.5:
            label = "大阳线" if is_bullish(i) else "大阴线"
            t = "bullish" if is_bullish(i) else "bearish"
            patterns_detected.append({
                "pattern": label, "type": t,
                "day_offset": -(n - 1 - i),
                "strength": "强", "desc": "实体大，方向明确"
            })

    # --- Multi-candle patterns (use last 10 days) ---
    if n >= 3:
        i = n - 1
        # Morning Star (早晨之星) — bottom reversal
        if (is_bearish(i - 2) and body(i - 2) > avg_body * 1.5
                and body(i - 1) < avg_body * 0.4
                and is_bullish(i) and body(i) > avg_body * 1.5
                and c[i] > (o[i - 2] + c[i - 2]) / 2):
            patterns_detected.append({
                "pattern": "早晨之星 (Morning Star)", "type": "bullish_reversal",
                "day_offset": -2, "strength": "强", "desc": "三根K线底部反转形态"
            })

        # Evening Star (黄昏之星) — top reversal
        if (is_bullish(i - 2) and body(i - 2) > avg_body * 1.5
                and body(i - 1) < avg_body * 0.4
                and is_bearish(i) and body(i) > avg_body * 1.5
                and c[i] < (o[i - 2] + c[i - 2]) / 2):
            patterns_detected.append({
                "pattern": "黄昏之星 (Evening Star)", "type": "bearish_reversal",
                "day_offset": -2, "strength": "强", "desc": "三根K线顶部反转形态"
            })

        # Engulfing (吞没形态)
        if (is_bullish(i) and is_bearish(i - 1)
                and o[i] < c[i - 1] and c[i] > o[i - 1]):
            patterns_detected.append({
                "pattern": "看涨吞没 (Bullish Engulfing)", "type": "bullish_reversal",
                "day_offset": -1, "strength": "强", "desc": "阳线完全覆盖前一阴线"
            })
        elif (is_bearish(i) and is_bullish(i - 1)
              and o[i] > c[i - 1] and c[i] < o[i - 1]):
            patterns_detected.append({
                "pattern": "看跌吞没 (Bearish Engulfing)", "type": "bearish_reversal",
                "day_offset": -1, "strength": "强", "desc": "阴线完全覆盖前一阳线"
            })

    # --- Chart patterns over the window ---
    # Double bottom detection (简化版: 两个相近低点 + 中间高点)
    recent_lows_idx = sorted(range(n), key=lambda i: l[i])[:5]
    if len(recent_lows_idx) >= 2:
        lo1, lo2 = sorted(recent_lows_idx[:2])
        if lo2 - lo1 >= 5 and abs(l[lo1] - l[lo2]) / max(l[lo1], l[lo2]) < 0.03:
            mid_high = max(h[lo1:lo2 + 1])
            if mid_high > l[lo1] * 1.03:
                patterns_detected.append({
                    "pattern": "双底 (Double Bottom)", "type": "bullish_reversal",
                    "day_offset": -(n - 1 - lo2),
                    "strength": "强", "desc": "两个相近低点，W型底部形态"
                })

    # Upward breakout: closes above 20d high (excluding last day itself)
    if n >= 21:
        high_20d = max(h[n - 21:n - 1])
        if c[-1] > high_20d and (v is None or v[-1] > sum(v[n - 6:n - 1]) / 5 * 1.5):
            patterns_detected.append({
                "pattern": "放量突破20日高点", "type": "bullish_breakout",
                "day_offset": 0, "strength": "强", "desc": "收盘突破近20日最高，量能配合"
            })

    # Price in consolidation box (box oscillation)
    if n >= 10:
        recent_high = max(h[n - 10:])
        recent_low = min(l[n - 10:])
        box_range_pct = (recent_high - recent_low) / recent_low * 100 if recent_low > 0 else 0
        if box_range_pct < 8:
            patterns_detected.append({
                "pattern": "箱体震荡", "type": "consolidation",
                "day_offset": 0, "strength": "中",
                "desc": f"近10日波幅 {box_range_pct:.1f}%，价格在区间内震荡"
            })

    # Deduplicate by pattern name, keep most recent
    seen = set()
    unique_patterns = []
    for p in reversed(patterns_detected):
        if p["pattern"] not in seen:
            seen.add(p["pattern"])
            unique_patterns.append(p)
    unique_patterns = list(reversed(unique_patterns))

    return {
        "code": stock_code,
        "source": source,
        "period_days": len(df),
        "current_price": round(float(c[-1]), 2),
        "patterns_count": len(unique_patterns),
        "patterns": unique_patterns,
        "summary": (
            "未发现明显形态" if not unique_patterns
            else "、".join(p["pattern"] for p in unique_patterns)
        ),
    }


analyze_pattern_tool = ToolDefinition(
    name="analyze_pattern",
    description="识别近期价格历史中的 K 线形态和图表形态。"
                "可识别：十字星、锤子线、射击之星、晨星/暮星、吞没形态、"
                "双底、向上突破、箱体震荡等。"
                "返回形态列表，含类型（看多/看空/反转）和强度。",
    parameters=[
        ToolParameter(
            name="stock_code",
            type="string",
            description="股票代码，例如 '600519'",
        ),
        ToolParameter(
            name="days",
            type="integer",
            description="扫描的最近交易天数（默认：60）",
            required=False,
            default=60,
        ),
    ],
    handler=_handle_analyze_pattern,
    category="analysis",
)


# ============================================================
# analyze_relative_strength — Relative strength & trend quality
# ============================================================

def _handle_analyze_relative_strength(stock_code: str, days: int = 60) -> dict:
    """
    Analyze stock relative strength vs CSI300 and trend quality.

    Replaces the old limit-up/down counter with actual SEPA-aligned metrics:
    - RS Rating (vs market, volatility-adjusted)
    - Trend quality (above MA20 ratio, new highs, consistency)
    - Risk metrics (max drawdown, volatility, return/drawdown ratio)
    - SEPA grade (S/A/B/C/D) via multi-factor scoring

    Returns both backward-compatible fields (limit_up_count -> mapped from new_highs,
    momentum_grade) and new fields (rs_rating, total_return, max_drawdown, etc.)
    """
    from src.services.history_loader import load_history_df
    import pandas as pd

    # Load stock and market (CSI300) data
    stock_df, stock_source = load_history_df(stock_code, days=max(days + 20, 100))
    index_df, index_source = load_history_df("000300", days=max(days + 20, 100))

    if stock_df is None or stock_df.empty or len(stock_df) < 20:
        return {"error": f"Insufficient stock data for {stock_code}"}

    stock_df = stock_df.sort_values("date").reset_index(drop=True)
    df = stock_df.tail(days).copy().reset_index(drop=True)
    if len(df) < 10:
        return {"error": f"Insufficient data for relative strength (got {len(df)} days, need >= 10)"}

    # Ensure numeric columns
    for col in ["open", "high", "low", "close", "volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df["pct_change"] = df["close"].pct_change() * 100
    df["ma20"] = df["close"].rolling(window=20, min_periods=1).mean()

    # ---------- 1. Absolute return & risk ----------
    total_return = float((df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100)
    annualized_return = float(total_return * 252 / len(df))
    volatility = float(df["pct_change"].std() * np.sqrt(252))

    # Max drawdown
    cumulative = (1 + df["pct_change"] / 100).cumprod()
    running_max = cumulative.expanding().max()
    drawdown = (cumulative - running_max) / running_max * 100
    max_drawdown = float(drawdown.min())

    # ---------- 2. Relative strength vs market ----------
    rs_rating = 50.0
    alpha = 0.0
    beta = 1.0
    excess_return = total_return
    daily_alpha: list = []

    if index_df is not None and not index_df.empty and len(index_df) >= len(df):
        index_df = index_df.sort_values("date").reset_index(drop=True)
        mkt = index_df.tail(len(df)).copy().reset_index(drop=True)
        mkt["pct_change"] = mkt["close"].pct_change() * 100

        # Align by date if available
        if "date" in df.columns and "date" in mkt.columns:
            s = df[["date", "pct_change"]].copy()
            m = mkt[["date", "pct_change"]].copy()
            s["date"] = s["date"].astype(str)
            m["date"] = m["date"].astype(str)
            merged = pd.merge(
                s.rename(columns={"pct_change": "stock_ret"}),
                m.rename(columns={"pct_change": "mkt_ret"}),
                on="date",
            )
        else:
            merged = pd.DataFrame({
                "stock_ret": df["pct_change"].values,
                "mkt_ret": mkt["pct_change"].values[:len(df)],
            })

        merged = merged.dropna()
        if len(merged) >= 10:
            # Beta & Alpha (CAPM simplified)
            cov = np.cov(merged["stock_ret"], merged["mkt_ret"])
            if cov[1, 1] != 0:
                beta = float(cov[0, 1] / cov[1, 1])

            mkt_mean = merged["mkt_ret"].mean()
            stock_mean = merged["stock_ret"].mean()
            alpha = float(stock_mean - beta * mkt_mean)

            mkt_total = float((mkt["close"].iloc[-1] / mkt["close"].iloc[0] - 1) * 100)
            excess_return = float(total_return - mkt_total)

            # RS Rating (volatility-adjusted excess return, mapped 1-99)
            window = min(60, len(merged))
            sr = merged["stock_ret"].tail(window)
            mr = merged["mkt_ret"].tail(window)
            stock_cum = (1 + sr / 100).cumprod().iloc[-1] - 1
            mkt_cum = (1 + mr / 100).cumprod().iloc[-1] - 1
            if mkt_cum != 0:
                vol = sr.std() * np.sqrt(252)
                excess = stock_cum - mkt_cum
                raw_score = 50.0 + (excess / vol * 50.0) if vol != 0 else 50.0
                if stock_cum > 0 and mkt_cum > 0 and stock_cum / mkt_cum > 2:
                    raw_score += 20
                rs_rating = float(np.clip(raw_score, 1.0, 99.0))

            daily_alpha = (merged["stock_ret"] - merged["mkt_ret"]).tolist()

    # ---------- 3. Trend quality (SEPA Trend Template) ----------
    up_days_ratio = float((df["pct_change"] > 0).mean())

    # New 20-day highs
    df["high_20"] = df["high"].rolling(window=20, min_periods=1).max()
    df["is_new_high_20"] = df["high"] >= df["high_20"].shift(1).fillna(0) * 0.999
    new_high_count = int(df["is_new_high_20"].sum())

    # Above MA20 ratio (SEPA: price > MA20)
    df["above_ma20"] = df["close"] > df["ma20"]
    above_ma20_ratio = float(df["above_ma20"].mean())

    # Trend consistency (price & MA20 move in same direction)
    df["price_dir"] = df["close"].diff().fillna(0)
    df["ma20_dir"] = df["ma20"].diff().fillna(0)
    df["aligned"] = (df["price_dir"] * df["ma20_dir"]) > 0
    trend_consistency = float(df["aligned"].mean())

    # ---------- 4. SEPA grade scoring ----------
    grade_scores = []
    reasons = []
    risks = []

    # RS Rating (highest weight)
    if rs_rating >= 90:
        grade_scores.append(30)
        reasons.append(f"RS Rating {rs_rating:.0f}，市场前10%超级强势股")
    elif rs_rating >= 80:
        grade_scores.append(25)
        reasons.append(f"RS Rating {rs_rating:.0f}，符合SEPA强势标准")
    elif rs_rating >= 60:
        grade_scores.append(15)
        reasons.append(f"RS Rating {rs_rating:.0f}，中等强度")
    elif rs_rating >= 40:
        grade_scores.append(8)
        risks.append(f"RS Rating {rs_rating:.0f}，弱于市场平均")
    else:
        grade_scores.append(2)
        risks.append(f"RS Rating {rs_rating:.0f}，严重跑输市场")

    # Trend persistence
    if above_ma20_ratio >= 0.9:
        grade_scores.append(20)
        reasons.append(f"90%时间运行在MA20上方，趋势极强")
    elif above_ma20_ratio >= 0.75:
        grade_scores.append(15)
        reasons.append(f"75%时间运行在MA20上方，趋势良好")
    elif above_ma20_ratio >= 0.5:
        grade_scores.append(8)
        risks.append(f"仅{above_ma20_ratio*100:.0f}%时间在MA20上方，趋势不稳")
    else:
        grade_scores.append(0)
        risks.append(f"长期低于MA20，处于下降通道")

    # New high momentum
    if new_high_count >= 8:
        grade_scores.append(15)
        reasons.append(f"{days}天内{new_high_count}次创20日新高，动量持续")
    elif new_high_count >= 4:
        grade_scores.append(10)
        reasons.append(f"{new_high_count}次创20日新高，有一定动量")
    elif new_high_count >= 1:
        grade_scores.append(5)
    else:
        grade_scores.append(0)
        risks.append("无20日新高，缺乏上涨动能")

    # Return/drawdown ratio
    if max_drawdown != 0:
        rr_ratio = abs(annualized_return / max_drawdown)
        if rr_ratio >= 3:
            grade_scores.append(15)
            reasons.append(f"收益回撤比{rr_ratio:.1f}，风险控制好")
        elif rr_ratio >= 1.5:
            grade_scores.append(10)
        elif rr_ratio >= 0.5:
            grade_scores.append(5)
        else:
            risks.append(f"收益回撤比{rr_ratio:.1f}，风险收益比差")
    else:
        grade_scores.append(10)

    # Volatility adjustment (penalty for extreme volatility)
    if volatility > 80:
        grade_scores.append(-10)
        risks.append(f"年化波动率{volatility:.0f}%，妖股特征")
    elif volatility > 50:
        grade_scores.append(5)
    elif volatility > 30:
        grade_scores.append(10)
        reasons.append(f"波动率{volatility:.0f}%，适中")
    else:
        grade_scores.append(12)
        reasons.append(f"波动率{volatility:.0f}%，稳健")

    total_score = sum(grade_scores)

    # Map to SEPA grade
    if total_score >= 80 and rs_rating >= 80 and above_ma20_ratio >= 0.8:
        grade = "S"
        grade_meaning = "超级强势股：高RS+强趋势+低风险，SEPA核心候选"
    elif total_score >= 65 and rs_rating >= 70:
        grade = "A"
        grade_meaning = "强势股：趋势良好，RS较高，可纳入观察"
    elif total_score >= 50:
        grade = "B"
        grade_meaning = "中等强度：有潜力但需等待更好的买点或RS提升"
    elif total_score >= 35:
        grade = "C"
        grade_meaning = "弱势或震荡：不建议参与"
    else:
        grade = "D"
        grade_meaning = "严重弱势：跑输市场，一票否决"

    # Hard vetos (true SEPA red lines)
    if rs_rating < 40:
        grade = "D"
        grade_meaning = f"RS Rating仅{rs_rating:.0f}，严重跑输大盘，SEPA一票否决"
    elif max_drawdown < -30 and total_return < 0:
        grade = "D"
        grade_meaning = f"深度亏损且回撤{max_drawdown:.1f}%，趋势破坏"
    elif above_ma20_ratio < 0.3 and trend_consistency < 0.4:
        grade = "D"
        grade_meaning = "长期空头排列，无趋势可言"

    # ---------- 5. Backward-compatible + new fields ----------
    # Map new metrics to legacy field names so existing prompts still work
    # "limit_up_count" -> proxy: new_high_count (both measure upward momentum)
    # "limit_down_count" -> proxy: days with negative return
    down_days = int((df["pct_change"] < 0).sum())
    failed_limit_up = int((df["high"] >= df["close"].shift(1) * 1.095).sum()) if len(df) > 1 else 0

    return {
        # Backward-compatible fields (existing prompts reference these)
        "stock_code": stock_code,
        "status": "ok",
        "source": stock_source,
        "period_days": len(df),
        "limit_up_count": new_high_count,           # mapped from new_high_count
        "limit_down_count": down_days,              # mapped from down days
        "failed_limit_up_count": failed_limit_up,   # best-effort proxy
        "max_consecutive_limit_up": 0,              # no longer computed
        "limit_up_down_ratio": round(new_high_count / max(down_days, 1), 1),
        "momentum_grade": grade,
        "grade_meaning": grade_meaning,

        # New fields (richer analysis)
        "rs_rating": round(rs_rating, 1),
        "total_return_pct": round(total_return, 2),
        "annualized_return_pct": round(annualized_return, 2),
        "volatility_pct": round(volatility, 2),
        "max_drawdown_pct": round(max_drawdown, 2),
        "excess_return_pct": round(excess_return, 2),
        "alpha": round(alpha, 3),
        "beta": round(beta, 2),
        "above_ma20_ratio": round(above_ma20_ratio, 3),
        "trend_consistency": round(trend_consistency, 3),
        "new_high_count": new_high_count,
        "up_days_ratio": round(up_days_ratio, 3),
        "sepa_score": total_score,
        "sepa_reasons": reasons,
        "sepa_risks": risks,
        "daily_alpha": daily_alpha[-10:] if daily_alpha else [],
    }


analyze_relative_strength_tool = ToolDefinition(
    name="analyze_relative_strength",
    description="分析股票相对强度和趋势质量（替代传统的涨停计数）。"
                "基于个股 vs 沪深300的相对强度(RS Rating)、趋势持续性(MA20上方占比)、"
                "新高次数、收益回撤比、波动率等多维度评分。"
                "返回SEPA动量等级(S/A/B/C/D)及详细指标。",
    parameters=[
        ToolParameter(
            name="stock_code",
            type="string",
            description="股票代码，例如 '600519'",
        ),
        ToolParameter(
            name="days",
            type="integer",
            description="分析周期天数（默认：60）",
            required=False,
            default=60,
        ),
    ],
    handler=_handle_analyze_relative_strength,
    category="analysis",
)


ALL_ANALYSIS_TOOLS = [
    analyze_trend_tool,
    calculate_ma_tool,
    get_volume_analysis_tool,
    analyze_pattern_tool,
    analyze_relative_strength_tool,
]
