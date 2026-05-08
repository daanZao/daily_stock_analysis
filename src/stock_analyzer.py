# -*- coding: utf-8 -*-
"""
===================================
趋势交易分析器 - 深度技术面分析版
===================================

交易理念核心原则：
1. 严进策略 - 不追高，追求每笔交易成功率
2. 趋势交易 - MA5>MA10>MA20 多头排列，顺势而为
3. 效率优先 - 关注筹码结构好的股票
4. 买点偏好 - 在 MA5/MA10 附近回踩买入

技术标准：
- 多头排列：MA5 > MA10 > MA20
- 乖离率：(Close - MA5) / MA5 < 5%（不追高），趋势市放宽、震荡市收紧
- 量能形态：缩量回调优先
- MACD背离分级：首次顶背离观察，二次顶背离强制降档
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Tuple
from enum import Enum

import pandas as pd
import numpy as np

from src.config import get_config

logger = logging.getLogger(__name__)


class TrendStatus(Enum):
    """趋势状态枚举"""
    STRONG_BULL = "强势多头"
    BULL = "多头排列"
    WEAK_BULL = "弱势多头"
    CONSOLIDATION = "盘整"
    WEAK_BEAR = "弱势空头"
    BEAR = "空头排列"
    STRONG_BEAR = "强势空头"
    BOTTOMING = "底部转折"
    TOPPING = "顶部转折"


class VolumeStatus(Enum):
    """量能状态枚举"""
    HEAVY_VOLUME_UP = "放量上涨"
    HEAVY_VOLUME_DOWN = "放量下跌"
    SHRINK_VOLUME_UP = "缩量上涨"
    SHRINK_VOLUME_DOWN = "缩量回调"
    NORMAL = "量能正常"


class BuySignal(Enum):
    """买卖信号枚举"""
    STRONG_BUY = "强烈买入"
    BUY = "买入"
    WATCH = "密切观察"
    HOLD = "持有"
    WAIT = "观望"
    REDUCE = "减仓"
    SELL = "卖出"
    STRONG_SELL = "强烈卖出"


class MACDStatus(Enum):
    """MACD状态枚举"""
    GOLDEN_CROSS_ZERO = "零轴上金叉"
    GOLDEN_CROSS = "金叉"
    BULLISH_ACCEL = "多头加速"
    BULLISH_DECEL = "多头减速"
    CROSSING_UP = "上穿零轴"
    CROSSING_DOWN = "下穿零轴"
    BEARISH_ACCEL = "空头加速"
    BEARISH_DECEL = "空头减速"
    DEATH_CROSS = "死叉"
    GOLDEN_CROSS_ZERO_BELOW = "零轴下金叉"
    BULLISH = "多头"  # 兼容别名，供下游测试使用


class RSIStatus(Enum):
    """RSI状态枚举"""
    OVERBOUGHT_EXTREME = "严重超买"
    OVERBOUGHT = "超买"
    STRONG_BUY = "强势"
    NEUTRAL = "中性"
    WEAK = "弱势"
    OVERSOLD = "超卖"
    OVERSOLD_EXTREME = "严重超卖"


@dataclass
class TrendAnalysisResult:
    """趋势分析结果"""
    code: str

    trend_status: TrendStatus = TrendStatus.CONSOLIDATION
    ma_alignment: str = ""
    trend_strength: float = 0.0

    ma5: float = 0.0
    ma10: float = 0.0
    ma20: float = 0.0
    ma60: float = 0.0
    current_price: float = 0.0

    bias_ma5: float = 0.0
    bias_ma10: float = 0.0
    bias_ma20: float = 0.0
    bias_trend: str = ""
    bias_historical_pct: float = 0.0

    volume_status: VolumeStatus = VolumeStatus.NORMAL
    volume_ratio_5d: float = 0.0
    volume_ratio_20d: float = 0.0
    volume_trend: str = ""

    support_ma5: bool = False
    support_ma10: bool = False
    support_ma20: bool = False
    resistance_levels: List[float] = field(default_factory=list)
    support_levels: List[float] = field(default_factory=list)
    platform_zone: Optional[Tuple[float, float]] = None
    ma5_touch_count_10d: int = 0
    ma10_touch_count_10d: int = 0

    macd_dif: float = 0.0
    macd_dea: float = 0.0
    macd_bar: float = 0.0
    macd_status: MACDStatus = MACDStatus.BULLISH_DECEL
    macd_signal: str = ""
    macd_divergence: str = ""
    macd_divergence_count: int = 0
    macd_divergence_severity: str = ""
    macd_momentum: str = ""

    rsi_6: float = 0.0
    rsi_12: float = 0.0
    rsi_24: float = 0.0
    rsi_status: RSIStatus = RSIStatus.NEUTRAL
    rsi_signal: str = ""
    rsi_divergence: str = ""
    rsi_alignment: str = ""

    buy_signal: BuySignal = BuySignal.WAIT
    signal_score: int = 0
    signal_reasons: List[str] = field(default_factory=list)
    risk_factors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'code': self.code,
            'trend_status': self.trend_status.value,
            'ma_alignment': self.ma_alignment,
            'trend_strength': self.trend_strength,
            'ma5': self.ma5, 'ma10': self.ma10, 'ma20': self.ma20, 'ma60': self.ma60,
            'current_price': self.current_price,
            'bias_ma5': self.bias_ma5, 'bias_ma10': self.bias_ma10, 'bias_ma20': self.bias_ma20,
            'bias_trend': self.bias_trend, 'bias_historical_pct': self.bias_historical_pct,
            'volume_status': self.volume_status.value,
            'volume_ratio_5d': self.volume_ratio_5d,
            'volume_ratio_20d': self.volume_ratio_20d,
            'volume_trend': self.volume_trend,
            'support_ma5': self.support_ma5, 'support_ma10': self.support_ma10, 'support_ma20': self.support_ma20,
            'resistance_levels': self.resistance_levels, 'support_levels': self.support_levels,
            'platform_zone': self.platform_zone,
            'ma5_touch_count_10d': self.ma5_touch_count_10d, 'ma10_touch_count_10d': self.ma10_touch_count_10d,
            'macd_dif': self.macd_dif, 'macd_dea': self.macd_dea, 'macd_bar': self.macd_bar,
            'macd_status': self.macd_status.value, 'macd_signal': self.macd_signal,
            'macd_divergence': self.macd_divergence, 'macd_divergence_count': self.macd_divergence_count,
            'macd_divergence_severity': self.macd_divergence_severity, 'macd_momentum': self.macd_momentum,
            'rsi_6': self.rsi_6, 'rsi_12': self.rsi_12, 'rsi_24': self.rsi_24,
            'rsi_status': self.rsi_status.value, 'rsi_signal': self.rsi_signal,
            'rsi_divergence': self.rsi_divergence, 'rsi_alignment': self.rsi_alignment,
            'buy_signal': self.buy_signal.value, 'signal_score': self.signal_score,
            'signal_reasons': self.signal_reasons, 'risk_factors': self.risk_factors,
        }


class StockTrendAnalyzer:
    """股票深度趋势分析器"""

    MA_SUPPORT_TOLERANCE = 0.02
    MACD_FAST = 12
    MACD_SLOW = 26
    MACD_SIGNAL = 9
    RSI_SHORT = 6
    RSI_MID = 12
    RSI_LONG = 24

    def __init__(self):
        pass

    def analyze(self, df: pd.DataFrame, code: str) -> TrendAnalysisResult:
        result = TrendAnalysisResult(code=code)
        if df is None or df.empty or len(df) < 30:
            result.risk_factors.append("数据不足（需>=30天），分析可靠性低")
            return result

        df = df.sort_values('date').reset_index(drop=True)
        df = self._calculate_all_indicators(df)
        result.current_price = float(df.iloc[-1]['close'])

        ma_state = self._deep_analyze_ma(df, result)
        self._deep_analyze_bias(df, result, ma_state)
        self._deep_analyze_macd(df, result)
        self._deep_analyze_rsi(df, result)
        self._deep_analyze_support_resistance(df, result)
        self._basic_volume_reference(df, result)
        self._generate_deep_signal(result, ma_state)

        return result

    def _calculate_all_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        for period in [5, 10, 20, 60]:
            df[f'MA{period}'] = df['close'].rolling(window=period).mean()

        for period in [5, 10, 20]:
            ma_col = f'MA{period}'
            df[f'MA{period}_SLOPE'] = (df[ma_col] - df[ma_col].shift(5)) / df[ma_col].shift(5) * 100

        ema_fast = df['close'].ewm(span=self.MACD_FAST, adjust=False).mean()
        ema_slow = df['close'].ewm(span=self.MACD_SLOW, adjust=False).mean()
        df['MACD_DIF'] = ema_fast - ema_slow
        df['MACD_DEA'] = df['MACD_DIF'].ewm(span=self.MACD_SIGNAL, adjust=False).mean()
        df['MACD_BAR'] = (df['MACD_DIF'] - df['MACD_DEA']) * 2
        df['MACD_BAR_PREV'] = df['MACD_BAR'].shift(1)
        df['MACD_BAR_PREV2'] = df['MACD_BAR'].shift(2)

        for period in [self.RSI_SHORT, self.RSI_MID, self.RSI_LONG]:
            delta = df['close'].diff()
            gain = delta.where(delta > 0, 0)
            loss = -delta.where(delta < 0, 0)
            avg_gain = gain.rolling(window=period).mean()
            avg_loss = loss.rolling(window=period).mean()
            rs = avg_gain / avg_loss.replace(0, np.nan)
            df[f'RSI_{period}'] = 100 - (100 / (1 + rs))
            df[f'RSI_{period}'] = df[f'RSI_{period}'].fillna(50)

        df['PCT_CHANGE'] = df['close'].pct_change() * 100
        df['HIGH_20'] = df['high'].rolling(20).max()
        df['LOW_20'] = df['low'].rolling(20).min()

        for period in [5, 10, 20]:
            df[f'BIAS_MA{period}'] = (df['close'] - df[f'MA{period}']) / df[f'MA{period}'] * 100

        return df

    def _deep_analyze_ma(self, df: pd.DataFrame, result: TrendAnalysisResult) -> dict:
        latest = df.iloc[-1]
        prev5 = df.iloc[-5] if len(df) >= 5 else latest

        ma5, ma10, ma20, ma60 = latest['MA5'], latest['MA10'], latest['MA20'], latest.get('MA60', latest['MA20'])
        result.ma5, result.ma10, result.ma20, result.ma60 = ma5, ma10, ma20, ma60

        s5, s10, s20 = latest['MA5_SLOPE'], latest['MA10_SLOPE'], latest['MA20_SLOPE']

        spread_5_20 = (ma5 - ma20) / ma20 * 100 if ma20 > 0 else 0
        prev_spread_5_20 = (prev5['MA5'] - prev5['MA20']) / prev5['MA20'] * 100 if prev5['MA20'] > 0 else 0
        spread_expanding = spread_5_20 > prev_spread_5_20

        mas = [ma5, ma10, ma20, ma60]
        mean_ma = np.mean(mas)
        std_ma = np.std(mas)
        cohesion = std_ma / mean_ma * 100 if mean_ma > 0 else 0

        ma5_recent = df['MA5'].tail(3).values
        ma5_turning_up = len(ma5_recent) >= 3 and all(ma5_recent[i] < ma5_recent[i+1] for i in range(len(ma5_recent)-1))
        ma5_turning_down = len(ma5_recent) >= 3 and all(ma5_recent[i] > ma5_recent[i+1] for i in range(len(ma5_recent)-1))

        is_bull_arrange = ma5 > ma10 > ma20
        is_bear_arrange = ma5 < ma10 < ma20

        if is_bull_arrange:
            if s5 > 0 and s10 > 0 and spread_expanding and spread_5_20 > 3:
                result.trend_status = TrendStatus.STRONG_BULL
                result.ma_alignment = f"强势多头，均线发散(spread={spread_5_20:.1f}%)"
                result.trend_strength = min(95, 70 + spread_5_20 * 3)
            elif s5 > 0 and s10 > 0:
                result.trend_status = TrendStatus.BULL
                result.ma_alignment = "多头排列，斜率向上"
                result.trend_strength = 75
            else:
                result.trend_status = TrendStatus.WEAK_BULL
                result.ma_alignment = "多头排列但斜率放缓，注意动能"
                result.trend_strength = 55
        elif is_bear_arrange:
            if s5 < 0 and s10 < 0 and spread_expanding and abs(spread_5_20) > 3:
                result.trend_status = TrendStatus.STRONG_BEAR
                result.ma_alignment = "强势空头，均线发散"
                result.trend_strength = 5
            elif s5 < 0 and s10 < 0:
                result.trend_status = TrendStatus.BEAR
                result.ma_alignment = "空头排列"
                result.trend_strength = 20
            else:
                result.trend_status = TrendStatus.WEAK_BEAR
                result.ma_alignment = "空头排列但斜率放缓"
                result.trend_strength = 35
        else:
            if ma5_turning_up and ma10 < ma20 and s5 > 0:
                result.trend_status = TrendStatus.BOTTOMING
                result.ma_alignment = "底部转折，MA5拐头向上"
                result.trend_strength = 45
            elif ma5_turning_down and ma10 > ma20 and s5 < 0:
                result.trend_status = TrendStatus.TOPPING
                result.ma_alignment = "顶部转折，MA5拐头向下"
                result.trend_strength = 50
            elif cohesion < 1.5:
                result.trend_status = TrendStatus.CONSOLIDATION
                result.ma_alignment = f"均线粘合(cohesion={cohesion:.2f}%)，变盘前兆"
                result.trend_strength = 50
            else:
                result.trend_status = TrendStatus.CONSOLIDATION
                result.ma_alignment = "均线缠绕，趋势不明"
                result.trend_strength = 50

        return {
            'is_bull_arrange': is_bull_arrange, 'is_bear_arrange': is_bear_arrange,
            'spread_5_20': spread_5_20, 'spread_expanding': spread_expanding,
            'cohesion': cohesion, 'ma5_turning_up': ma5_turning_up, 'ma5_turning_down': ma5_turning_down,
            's5': s5, 's10': s10, 's20': s20
        }

    def _deep_analyze_bias(self, df: pd.DataFrame, result: TrendAnalysisResult, ma_state: dict) -> None:
        latest = df.iloc[-1]
        price = latest['close']

        result.bias_ma5 = (price - result.ma5) / result.ma5 * 100 if result.ma5 > 0 else 0
        result.bias_ma10 = (price - result.ma10) / result.ma10 * 100 if result.ma10 > 0 else 0
        result.bias_ma20 = (price - result.ma20) / result.ma20 * 100 if result.ma20 > 0 else 0

        bias5_series = df['BIAS_MA5'].tail(5)
        if len(bias5_series) >= 3:
            recent_3 = bias5_series.tail(3).values
            if recent_3[-1] - recent_3[0] > 1:
                result.bias_trend = "扩大（加速偏离）"
            elif recent_3[-1] - recent_3[0] < -1:
                result.bias_trend = "收敛（向均线回归）"
            else:
                result.bias_trend = "稳定"

        hist_bias = df['BIAS_MA5'].tail(60).dropna()
        if len(hist_bias) > 10:
            result.bias_historical_pct = float((hist_bias <= result.bias_ma5).mean() * 100)

        base_threshold = getattr(get_config(), 'bias_threshold', 5.0)

        if result.trend_status == TrendStatus.STRONG_BULL:
            eff_th = base_threshold * 1.6
        elif result.trend_status == TrendStatus.BULL:
            eff_th = base_threshold * 1.2
        elif result.trend_status in [TrendStatus.CONSOLIDATION, TrendStatus.BOTTOMING, TrendStatus.TOPPING]:
            eff_th = base_threshold * 0.8
        else:
            eff_th = base_threshold

        bias = result.bias_ma5
        if result.trend_status in [TrendStatus.STRONG_BULL, TrendStatus.BULL]:
            if bias > eff_th and result.bias_trend == "扩大（加速偏离）":
                result.risk_factors.append(f"乖离率{bias:.1f}%且加速扩大，短期过热")
            elif bias > eff_th and result.bias_trend == "收敛（向均线回归）":
                result.signal_reasons.append(f"乖离率{bias:.1f}%但开始收敛，强势整理中")
            elif 0 < bias <= eff_th:
                result.signal_reasons.append(f"乖离率{bias:.1f}%，趋势健康")
            elif -eff_th <= bias <= 0:
                result.signal_reasons.append(f"乖离率{bias:.1f}%，回踩MA5买点")
            elif bias < -eff_th:
                result.risk_factors.append(f"乖离率{bias:.1f}%，跌破MA5过深，可能破位")
        else:
            if abs(bias) > eff_th:
                result.risk_factors.append(f"震荡市中乖离率{bias:.1f}%偏离过大，警惕回归")
            else:
                result.signal_reasons.append(f"乖离率{bias:.1f}%，处于合理区间")

    def _deep_analyze_macd(self, df: pd.DataFrame, result: TrendAnalysisResult) -> None:
        if len(df) < self.MACD_SLOW + 10:
            result.macd_signal = "数据不足"
            return

        latest = df.iloc[-1]
        prev = df.iloc[-2]
        result.macd_dif = float(latest['MACD_DIF'])
        result.macd_dea = float(latest['MACD_DEA'])
        result.macd_bar = float(latest['MACD_BAR'])

        prev_diff = prev['MACD_DIF'] - prev['MACD_DEA']
        curr_diff = result.macd_dif - result.macd_dea
        is_golden = prev_diff <= 0 and curr_diff > 0
        is_death = prev_diff >= 0 and curr_diff < 0
        prev_zero = prev['MACD_DIF']
        curr_zero = result.macd_dif
        is_cross_up = prev_zero <= 0 and curr_zero > 0
        is_cross_down = prev_zero >= 0 and curr_zero < 0

        bars = df['MACD_BAR'].tail(3).values
        if len(bars) >= 3:
            if all(bars[i] < bars[i+1] for i in range(len(bars)-1)) and bars[-1] > 0:
                result.macd_momentum = "加速扩张"
            elif all(bars[i] > bars[i+1] for i in range(len(bars)-1)) and bars[-1] > 0:
                result.macd_momentum = "多头减速"
            elif all(bars[i] > bars[i+1] for i in range(len(bars)-1)) and bars[-1] < 0:
                result.macd_momentum = "加速下跌"
            elif all(bars[i] < bars[i+1] for i in range(len(bars)-1)) and bars[-1] < 0:
                result.macd_momentum = "空头减速"
            else:
                result.macd_momentum = "动能平稳"

        # ===== 背离检测升级：区分首次与二次顶背离 =====
        recent = df.tail(30).copy()
        recent['is_local_high'] = (
            (recent['close'] >= recent['close'].shift(1)) &
            (recent['close'] >= recent['close'].shift(2)) &
            (recent['close'] >= recent['close'].shift(-1)) &
            (recent['close'] >= recent['close'].shift(-2))
        )
        sig_highs = recent[recent['is_local_high']].copy()

        if len(sig_highs) >= 2:
            hp = sig_highs.tail(2)
            p1, p2 = float(hp['close'].iloc[0]), float(hp['close'].iloc[1])
            d1, d2 = float(hp['MACD_DIF'].iloc[0]), float(hp['MACD_DIF'].iloc[1])
            if p2 > p1 * 1.01 and d2 < d1 * 0.98:
                result.macd_divergence = "二次顶背离"
                result.macd_divergence_count = 2
                result.macd_divergence_severity = "严重"
                result.risk_factors.append(
                    f"MACD二次顶背离：价格新高({p2:.2f}>{p1:.2f})，DIF连续走低({d2:.3f}<{d1:.3f})，M头概率极高"
                )
            elif latest['close'] >= recent['close'].max() * 0.995:
                recent_20 = df.tail(20)
                prev_dif_high = float(recent_20['MACD_DIF'].max())
                prev_dif_high_idx = recent_20['MACD_DIF'].idxmax()
                price_high_idx = recent['close'].idxmax()
                if prev_dif_high_idx < price_high_idx and latest['MACD_DIF'] < prev_dif_high * 0.95:
                    result.macd_divergence = "首次顶背离"
                    result.macd_divergence_count = 1
                    result.macd_divergence_severity = "轻微"
                    result.signal_reasons.append("MACD首次顶背离：价格新高但动能减弱，观察是否为强势整理")

        # 兜底简化逻辑
        if not result.macd_divergence:
            price_high_idx = recent['close'].idxmax()
            dif_high_idx = recent['MACD_DIF'].idxmax()
            if price_high_idx > dif_high_idx and latest['close'] >= recent['close'].max() * 0.98:
                if latest['MACD_DIF'] < recent.loc[dif_high_idx, 'MACD_DIF'] * 0.95:
                    result.macd_divergence = "首次顶背离"
                    result.macd_divergence_count = 1
                    result.macd_divergence_severity = "轻微"
                    result.signal_reasons.append("MACD首次顶背离：价格新高但动能减弱，观察是否为强势整理")

        # 底背离
        if not result.macd_divergence or result.macd_divergence == "无":
            price_low_idx = recent['close'].idxmin()
            dif_low_idx = recent['MACD_DIF'].idxmin()
            if price_low_idx > dif_low_idx and latest['close'] <= recent['close'].min() * 1.02:
                if latest['MACD_DIF'] > recent.loc[dif_low_idx, 'MACD_DIF'] * 1.05:
                    result.macd_divergence = "底背离"
                    result.macd_divergence_count = 1
                    result.macd_divergence_severity = ""
                    result.signal_reasons.append("MACD底背离：价格新低但动能改善")

        if not result.macd_divergence:
            result.macd_divergence = "无"
            result.macd_divergence_count = 0
            result.macd_divergence_severity = ""

        # 状态判断
        if is_golden and curr_zero > 0:
            result.macd_status = MACDStatus.GOLDEN_CROSS_ZERO
            result.macd_signal = "零轴上金叉，趋势确认"
        elif is_golden and curr_zero < 0:
            result.macd_status = MACDStatus.GOLDEN_CROSS_ZERO_BELOW
            result.macd_signal = "零轴下金叉，反弹信号"
        elif is_cross_up:
            result.macd_status = MACDStatus.CROSSING_UP
            result.macd_signal = "DIF上穿零轴，趋势转强"
        elif is_golden:
            result.macd_status = MACDStatus.GOLDEN_CROSS
            result.macd_signal = "金叉，趋势向上"
        elif is_death:
            result.macd_status = MACDStatus.DEATH_CROSS
            result.macd_signal = "死叉，趋势向下"
        elif is_cross_down:
            result.macd_status = MACDStatus.CROSSING_DOWN
            result.macd_signal = "DIF下穿零轴，趋势转弱"
        elif result.macd_dif > 0 and result.macd_dea > 0:
            if result.macd_momentum == "加速扩张":
                result.macd_status = MACDStatus.BULLISH_ACCEL
                result.macd_signal = "零轴上多头加速"
            else:
                result.macd_status = MACDStatus.BULLISH_DECEL
                result.macd_signal = "零轴上多头但动能减弱"
        elif result.macd_dif < 0 and result.macd_dea < 0:
            if result.macd_momentum == "加速下跌":
                result.macd_status = MACDStatus.BEARISH_ACCEL
                result.macd_signal = "零轴下空头加速"
            else:
                result.macd_status = MACDStatus.BEARISH_DECEL
                result.macd_signal = "零轴下空头但动能减弱"
        else:
            result.macd_status = MACDStatus.BULLISH_DECEL
            result.macd_signal = "MACD中性区域"

    def _deep_analyze_rsi(self, df: pd.DataFrame, result: TrendAnalysisResult) -> None:
        if len(df) < self.RSI_LONG + 5:
            result.rsi_signal = "数据不足"
            return

        latest = df.iloc[-1]
        result.rsi_6 = float(latest[f'RSI_{self.RSI_SHORT}'])
        result.rsi_12 = float(latest[f'RSI_{self.RSI_MID}'])
        result.rsi_24 = float(latest[f'RSI_{self.RSI_LONG}'])
        rsi6, rsi12, rsi24 = result.rsi_6, result.rsi_12, result.rsi_24

        if rsi6 > rsi12 > rsi24:
            result.rsi_alignment = "短>中>长，动量增强"
        elif rsi6 < rsi12 < rsi24:
            result.rsi_alignment = "短<中<长，动量衰减"
        elif rsi6 > rsi12 and rsi12 < rsi24:
            result.rsi_alignment = "中期RSI谷底，可能转折"
        else:
            result.rsi_alignment = "三周期交织，动量不明"

        if rsi12 > 80 or rsi6 > 85:
            result.rsi_status = RSIStatus.OVERBOUGHT_EXTREME
            result.rsi_signal = f"RSI严重超买(6:{rsi6:.0f},12:{rsi12:.0f})"
        elif rsi12 > 70 or rsi6 > 75:
            result.rsi_status = RSIStatus.OVERBOUGHT
            result.rsi_signal = f"RSI超买(6:{rsi6:.0f},12:{rsi12:.0f})，谨慎追高"
        elif rsi12 > 60:
            result.rsi_status = RSIStatus.STRONG_BUY
            result.rsi_signal = f"RSI强势(6:{rsi6:.0f},12:{rsi12:.0f})"
        elif 40 <= rsi12 <= 60:
            result.rsi_status = RSIStatus.NEUTRAL
            result.rsi_signal = f"RSI中性(6:{rsi6:.0f},12:{rsi12:.0f})"
        elif rsi12 < 20 or rsi6 < 15:
            result.rsi_status = RSIStatus.OVERSOLD_EXTREME
            result.rsi_signal = f"RSI严重超卖(6:{rsi6:.0f},12:{rsi12:.0f})，反弹概率大"
        elif rsi12 < 30:
            result.rsi_status = RSIStatus.OVERSOLD
            result.rsi_signal = f"RSI超卖(6:{rsi6:.0f},12:{rsi12:.0f})，关注反弹"
        else:
            result.rsi_status = RSIStatus.WEAK
            result.rsi_signal = f"RSI弱势(6:{rsi6:.0f},12:{rsi12:.0f})"

        recent_rsi6 = df[f'RSI_{self.RSI_SHORT}'].tail(10).values
        if len(recent_rsi6) >= 5:
            if all(r > 70 for r in recent_rsi6[-5:]):
                result.risk_factors.append("RSI6连续5天超买钝化，强势但随时可能回调")
            elif all(r < 30 for r in recent_rsi6[-5:]):
                result.signal_reasons.append("RSI6连续5天超卖钝化，极度弱势，反弹在即")

        recent = df.tail(15)
        price_high_idx = recent['close'].idxmax()
        price_low_idx = recent['close'].idxmin()
        rsi_high_idx = recent[f'RSI_{self.RSI_MID}'].idxmax()
        rsi_low_idx = recent[f'RSI_{self.RSI_MID}'].idxmin()

        if price_high_idx > rsi_high_idx and latest['close'] >= recent['close'].max() * 0.98:
            if latest[f'RSI_{self.RSI_MID}'] < recent.loc[rsi_high_idx, f'RSI_{self.RSI_MID}'] * 0.95:
                result.rsi_divergence = "顶背离"
                if result.macd_divergence != "顶背离":
                    result.risk_factors.append("RSI顶背离：价格新高但动量衰竭")

        if price_low_idx > rsi_low_idx and latest['close'] <= recent['close'].min() * 1.02:
            if latest[f'RSI_{self.RSI_MID}'] > recent.loc[rsi_low_idx, f'RSI_{self.RSI_MID}'] * 1.05:
                result.rsi_divergence = "底背离"
                if result.macd_divergence != "底背离":
                    result.signal_reasons.append("RSI底背离：价格新低但动量改善")

        if not result.rsi_divergence:
            result.rsi_divergence = "无"

    def _deep_analyze_support_resistance(self, df: pd.DataFrame, result: TrendAnalysisResult) -> None:
        latest = df.iloc[-1]
        price = latest['close']

        if len(df) >= 20:
            recent = df.tail(20)
            recent_high = float(recent['high'].max())
            recent_low = float(recent['low'].min())
            if recent_high > price * 1.005:
                result.resistance_levels.append(round(recent_high, 2))
            if recent_low < price * 0.995:
                result.support_levels.append(round(recent_low, 2))

        if len(df) >= 20:
            recent = df.tail(20)
            platform_upper = float(recent['low'].max())
            platform_lower = float(recent['high'].min())
            if platform_upper > platform_lower:
                overlap_pct = (platform_upper - platform_lower) / recent['close'].mean() * 100
                if overlap_pct < 5:
                    result.platform_zone = (round(platform_lower, 2), round(platform_upper, 2))
                    result.signal_reasons.append(
                        f"识别到近期平台({platform_lower:.2f}-{platform_upper:.2f})，突破/跌破有参考意义"
                    )

        for period, threshold in [(5, 0.015), (10, 0.02), (20, 0.025)]:
            ma_val = getattr(result, f'ma{period}')
            if ma_val <= 0:
                continue
            distance = abs(price - ma_val) / ma_val
            is_above = price >= ma_val
            if distance <= threshold and is_above:
                setattr(result, f'support_ma{period}', True)
                result.support_levels.append(ma_val)

            recent_10 = df.tail(10)
            touches = 0
            for _, row in recent_10.iterrows():
                low, high = row['low'], row['high']
                if low <= ma_val * (1 + threshold) and high >= ma_val * (1 - threshold):
                    touches += 1
            setattr(result, f'ma{period}_touch_count_10d', touches)
            if touches >= 3 and getattr(result, f'support_ma{period}'):
                result.signal_reasons.append(f"MA{period}近10天被测试{touches}次，支撑有效")

    def _basic_volume_reference(self, df: pd.DataFrame, result: TrendAnalysisResult) -> None:
        if len(df) < 5:
            return
        latest = df.iloc[-1]
        vol_5d = df['volume'].iloc[-6:-1].mean()
        vol_20d = df['volume'].iloc[-21:-1].mean()
        if vol_5d > 0:
            result.volume_ratio_5d = float(latest['volume']) / vol_5d
        if vol_20d > 0:
            result.volume_ratio_20d = float(latest['volume']) / vol_20d

        # 兼容：根据量比还原 volume_status / volume_trend
        price_change_pct = df['close'].pct_change().iloc[-1] * 100 if len(df) >= 2 else 0
        vr = result.volume_ratio_5d
        if vr >= 1.5:
            if price_change_pct > 0:
                result.volume_status = VolumeStatus.HEAVY_VOLUME_UP
                result.volume_trend = "放量上涨，多头力量强劲"
            else:
                result.volume_status = VolumeStatus.HEAVY_VOLUME_DOWN
                result.volume_trend = "放量下跌，注意风险"
        elif vr <= 0.7:
            if price_change_pct > 0:
                result.volume_status = VolumeStatus.SHRINK_VOLUME_UP
                result.volume_trend = "缩量上涨，上攻动能不足"
            else:
                result.volume_status = VolumeStatus.SHRINK_VOLUME_DOWN
                result.volume_trend = "缩量回调，洗盘特征明显（好）"
        else:
            result.volume_status = VolumeStatus.NORMAL
            result.volume_trend = "量能正常"

        if vr > 3:
            result.risk_factors.append(f"当日量能异常放大({vr:.1f}倍5日均量)，注意独立量能分析")
        elif vr < 0.3:
            result.signal_reasons.append(f"极度缩量({vr:.1f}倍5日均量)，抛压枯竭")

    def _generate_deep_signal(self, result: TrendAnalysisResult, ma_state: dict) -> None:
        score = 0
        reasons = list(result.signal_reasons)
        risks = list(result.risk_factors)

        base_th = getattr(get_config(), 'bias_threshold', 5.0)

        # 1. 趋势结构（25分）
        trend_scores = {
            TrendStatus.STRONG_BULL: 25, TrendStatus.BULL: 22, TrendStatus.WEAK_BULL: 15,
            TrendStatus.BOTTOMING: 12, TrendStatus.CONSOLIDATION: 8, TrendStatus.TOPPING: 5,
            TrendStatus.WEAK_BEAR: 3, TrendStatus.BEAR: 0, TrendStatus.STRONG_BEAR: 0,
        }
        trend_score = trend_scores.get(result.trend_status, 8)
        score += trend_score
        if result.trend_status in [TrendStatus.STRONG_BULL, TrendStatus.BULL]:
            reasons.append(f"趋势结构优秀：{result.ma_alignment}")
        elif result.trend_status in [TrendStatus.BEAR, TrendStatus.STRONG_BEAR]:
            risks.append(f"趋势结构恶劣：{result.ma_alignment}，严禁做多")

        # 2. 乖离率（25分）
        bias = result.bias_ma5
        if result.trend_status == TrendStatus.STRONG_BULL:
            eff_th = base_th * 1.6
        elif result.trend_status == TrendStatus.BULL:
            eff_th = base_th * 1.2
        else:
            eff_th = base_th * 0.9

        bias_score = 0
        if bias < -5:
            bias_score = 5
            risks.append(f"乖离率{bias:.1f}%，跌破均线过深")
        elif bias < -2:
            bias_score = 22
            reasons.append(f"乖离率{bias:.1f}%，回踩买点")
        elif -2 <= bias <= 2:
            bias_score = 25
            reasons.append(f"乖离率{bias:.1f}%，贴近均线，绝佳买点")
        elif 2 < bias <= eff_th * 0.6:
            bias_score = 18
            reasons.append(f"乖离率{bias:.1f}%，可小仓介入")
        elif eff_th * 0.6 < bias <= eff_th:
            bias_score = 10
            reasons.append(f"乖离率{bias:.1f}%，偏离偏大，等待回踩")
        else:
            bias_score = 2
            risks.append(f"乖离率{bias:.1f}%>{eff_th:.1f}%，偏离过大，严禁追高")
        score += bias_score

        # 3. MACD（20分）—— 背离分级
        macd_score = 0
        if result.macd_divergence == "二次顶背离":
            macd_score = 2
            risks.append("MACD二次顶背离，动能两次衰竭，强制降档")
        elif result.macd_divergence == "首次顶背离":
            if result.macd_status in [MACDStatus.BULLISH_ACCEL, MACDStatus.GOLDEN_CROSS_ZERO]:
                macd_score = 14
                reasons.append("MACD首次顶背离，但柱状图仍在扩张，可能为强势整理")
            else:
                macd_score = 10
                reasons.append("MACD首次顶背离，动能开始减弱，注意二次背离风险")
        elif result.macd_divergence == "底背离":
            macd_score = 18
            reasons.append("MACD底背离，反弹信号强烈")
        elif result.macd_status == MACDStatus.GOLDEN_CROSS_ZERO:
            macd_score = 20
            reasons.append("MACD零轴上金叉，趋势确认")
        elif result.macd_status == MACDStatus.GOLDEN_CROSS:
            macd_score = 16
            reasons.append("MACD金叉，动能向上")
        elif result.macd_status == MACDStatus.CROSSING_UP:
            macd_score = 14
            reasons.append("MACD上穿零轴，趋势转强")
        elif result.macd_status in [MACDStatus.BULLISH_ACCEL, MACDStatus.BULLISH_DECEL]:
            macd_score = 12 if result.macd_status == MACDStatus.BULLISH_ACCEL else 8
            reasons.append("MACD零轴上多头运行")
        elif result.macd_status in [MACDStatus.BEARISH_ACCEL, MACDStatus.BEARISH_DECEL]:
            macd_score = 2
            risks.append("MACD零轴下空头运行")
        else:
            macd_score = 5
        score += macd_score

        # 4. RSI（15分）
        rsi_score = 0
        if result.rsi_divergence == "顶背离":
            rsi_score = 2
            risks.append("RSI顶背离")
        elif result.rsi_divergence == "底背离":
            rsi_score = 14
            reasons.append("RSI底背离")
        elif result.rsi_status == RSIStatus.OVERSOLD_EXTREME:
            rsi_score = 13
            reasons.append("RSI严重超卖，反弹在即")
        elif result.rsi_status == RSIStatus.OVERSOLD:
            rsi_score = 12
            reasons.append("RSI超卖，关注反弹")
        elif result.rsi_status == RSIStatus.STRONG_BUY:
            rsi_score = 11
            reasons.append("RSI强势区间，动能充足")
        elif result.rsi_status == RSIStatus.NEUTRAL:
            rsi_score = 8
        elif result.rsi_status == RSIStatus.WEAK:
            rsi_score = 4
            risks.append("RSI弱势")
        elif result.rsi_status in [RSIStatus.OVERBOUGHT, RSIStatus.OVERBOUGHT_EXTREME]:
            rsi_score = 2
            risks.append(f"RSI超买({result.rsi_12:.0f})，短期回调风险")

        if "动量增强" in result.rsi_alignment:
            rsi_score += 2
            reasons.append("RSI三周期多头排列，动量增强")
        elif "动量衰减" in result.rsi_alignment:
            rsi_score -= 2
            risks.append("RSI三周期空头排列，动量衰减")
        rsi_score = max(0, min(15, rsi_score))
        score += rsi_score

        # 5. 支撑结构（15分）
        sr_score = 0
        if result.support_ma5 and result.ma5_touch_count_10d >= 2:
            sr_score += 8
            reasons.append("MA5支撑有效且经多次测试")
        elif result.support_ma5:
            sr_score += 5
            reasons.append("MA5支撑有效")
        if result.support_ma10 and result.ma10_touch_count_10d >= 2:
            sr_score += 5
            reasons.append("MA10支撑有效且经多次测试")
        elif result.support_ma10:
            sr_score += 3
        if result.support_ma20:
            sr_score += 2
        if result.platform_zone:
            sr_score += 2
        score += sr_score

        result.signal_score = score

        # ===== 背离降档规则（分级处理）=====
        if result.macd_divergence == "二次顶背离":
            if score >= 70:
                score = max(55, score - 20)
                risks.append("二次顶背离触发强制降档，禁止强烈买入")
            if result.buy_signal in [BuySignal.STRONG_BUY, BuySignal.BUY]:
                result.buy_signal = BuySignal.WATCH

        elif result.macd_divergence == "首次顶背离":
            if score >= 75 and result.macd_momentum != "加速扩张":
                score = max(60, score - 10)
                reasons.append("首次顶背离，结构优秀但需观察回踩确认")
                if result.buy_signal == BuySignal.STRONG_BUY:
                    result.buy_signal = BuySignal.WATCH

        elif result.macd_divergence == "顶背离" and result.macd_dif < 0:
            score = max(40, score - 15)
            risks.append("零轴下顶背离，反弹结束信号")
            if result.buy_signal in [BuySignal.BUY, BuySignal.STRONG_BUY]:
                result.buy_signal = BuySignal.WAIT

        if result.bias_ma5 > base_th * 1.2 and result.trend_status not in [TrendStatus.STRONG_BULL, TrendStatus.BULL]:
            if score >= 50:
                score = 45
                risks.append("非趋势市中乖离率过高，强制观望")

        result.signal_score = score
        result.signal_reasons = reasons
        result.risk_factors = risks

        # 最终信号
        if score >= 80 and result.trend_status in [TrendStatus.STRONG_BULL, TrendStatus.BULL]:
            result.buy_signal = BuySignal.STRONG_BUY
        elif score >= 65:
            if result.trend_status in [TrendStatus.STRONG_BULL, TrendStatus.BULL, TrendStatus.WEAK_BULL]:
                result.buy_signal = BuySignal.BUY
            else:
                result.buy_signal = BuySignal.WATCH
        elif score >= 50:
            result.buy_signal = BuySignal.WATCH if result.trend_status in [TrendStatus.BULL, TrendStatus.WEAK_BULL, TrendStatus.BOTTOMING] else BuySignal.HOLD
        elif score >= 35:
            result.buy_signal = BuySignal.WAIT
        elif result.trend_status in [TrendStatus.BEAR, TrendStatus.STRONG_BEAR]:
            result.buy_signal = BuySignal.STRONG_SELL
        elif result.macd_divergence == "二次顶背离":
            result.buy_signal = BuySignal.REDUCE
        else:
            result.buy_signal = BuySignal.SELL

    def _generate_signal(self, result: TrendAnalysisResult) -> None:
        """兼容方法：旧测试直接调用 _generate_signal(result)。"""
        ma_state = {
            'is_bull_arrange': result.trend_status in (TrendStatus.STRONG_BULL, TrendStatus.BULL),
            'is_bear_arrange': result.trend_status in (TrendStatus.STRONG_BEAR, TrendStatus.BEAR),
            'spread_5_20': 0.0,
            'spread_expanding': False,
            'cohesion': 0.0,
            'ma5_turning_up': False,
            'ma5_turning_down': False,
            's5': 0.0,
            's10': 0.0,
            's20': 0.0,
        }
        self._generate_deep_signal(result, ma_state)

    def format_analysis(self, result: TrendAnalysisResult) -> str:
        lines = [
            f"=== {result.code} 深度趋势分析 ===",
            f"",
            f"趋势结构: {result.trend_status.value}",
            f"   {result.ma_alignment}",
            f"   趋势强度: {result.trend_strength:.0f}/100",
            f"",
            f"均线与乖离率:",
            f"   现价: {result.current_price:.2f}",
            f"   MA5:  {result.ma5:.2f} (乖离 {result.bias_ma5:+.2f}%, {result.bias_trend})",
            f"   MA10: {result.ma10:.2f} (乖离 {result.bias_ma10:+.2f}%)",
            f"   MA20: {result.ma20:.2f} (乖离 {result.bias_ma20:+.2f}%)",
            f"   乖离率历史分位: {result.bias_historical_pct:.0f}%",
            f"",
            f"MACD指标: {result.macd_status.value}",
            f"   DIF: {result.macd_dif:.4f} | DEA: {result.macd_dea:.4f} | BAR: {result.macd_bar:.4f}",
            f"   动能: {result.macd_momentum} | 背离: {result.macd_divergence}",
            f"   信号: {result.macd_signal}",
            f"",
            f"RSI指标: {result.rsi_status.value}",
            f"   RSI(6/12/24): {result.rsi_6:.1f} / {result.rsi_12:.1f} / {result.rsi_24:.1f}",
            f"   排列: {result.rsi_alignment} | 背离: {result.rsi_divergence}",
            f"   信号: {result.rsi_signal}",
            f"",
            f"支撑阻力:",
            f"   MA5支撑: {'是' if result.support_ma5 else '否'} (近10天测试{result.ma5_touch_count_10d}次)",
            f"   MA10支撑: {'是' if result.support_ma10 else '否'} (近10天测试{result.ma10_touch_count_10d}次)",
            f"   支撑位: {', '.join(f'{s:.2f}' for s in result.support_levels) if result.support_levels else '无'}",
            f"   压力位: {', '.join(f'{r:.2f}' for r in result.resistance_levels) if result.resistance_levels else '无'}",
        ]
        if result.platform_zone:
            lines.append(f"   近期平台: {result.platform_zone[0]:.2f} - {result.platform_zone[1]:.2f}")

        lines.extend([
            f"",
            f"量能参考: 量比(5日)={result.volume_ratio_5d:.2f}, 量比(20日)={result.volume_ratio_20d:.2f}",
            f"",
            f"操作建议: {result.buy_signal.value}",
            f"   综合评分: {result.signal_score}/100",
        ])

        if result.signal_reasons:
            lines.append(f"")
            lines.append(f"看多理由:")
            for r in result.signal_reasons[:6]:
                lines.append(f"   {r}")

        if result.risk_factors:
            lines.append(f"")
            lines.append(f"风险因素:")
            for r in result.risk_factors[:6]:
                lines.append(f"   {r}")

        return "\n".join(lines)


def analyze_stock(df: pd.DataFrame, code: str) -> TrendAnalysisResult:
    """便捷函数：分析单只股票"""
    analyzer = StockTrendAnalyzer()
    return analyzer.analyze(df, code)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    dates = pd.date_range(start='2025-01-01', periods=90, freq='D')
    np.random.seed(42)

    base = 10.0
    prices = [base]
    for i in range(89):
        trend = 0.003 if i < 60 else -0.002
        noise = np.random.randn() * 0.015
        prices.append(prices[-1] * (1 + trend + noise))

    df = pd.DataFrame({
        'date': dates,
        'open': [p * (1 - np.random.uniform(0, 0.01)) for p in prices],
        'high': [p * (1 + np.random.uniform(0, 0.02)) for p in prices],
        'low': [p * (1 - np.random.uniform(0, 0.02)) for p in prices],
        'close': prices,
        'volume': [np.random.randint(1000000, 5000000) for _ in prices],
    })

    analyzer = StockTrendAnalyzer()
    result = analyzer.analyze(df, '000001')
    print(analyzer.format_analysis(result))
