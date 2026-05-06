# -*- coding: utf-8 -*-
"""
Unit tests for StockTrendAnalyzer._generate_signal bias and strong-trend relief logic (Issue #296).
"""

import math
import unittest
from unittest.mock import patch, MagicMock

from src.stock_analyzer import (
    StockTrendAnalyzer,
    TrendAnalysisResult,
    TrendStatus,
    VolumeStatus,
    MACDStatus,
    RSIStatus,
)


def _make_result(
    code: str = "000001",
    trend_status: TrendStatus = TrendStatus.BULL,
    trend_strength: float = 50.0,
    bias_ma5: float = 0.0,
    volume_status: VolumeStatus = VolumeStatus.NORMAL,
    macd_status: MACDStatus = MACDStatus.BULLISH,
    rsi_status: RSIStatus = RSIStatus.NEUTRAL,
    support_ma5: bool = False,
    support_ma10: bool = False,
) -> TrendAnalysisResult:
    """Build TrendAnalysisResult with defaults for _generate_signal bias branch testing."""
    return TrendAnalysisResult(
        code=code,
        trend_status=trend_status,
        ma_alignment="",
        trend_strength=trend_strength,
        ma5=10.0,
        ma10=9.5,
        ma20=9.0,
        ma60=8.5,
        current_price=10.0,
        bias_ma5=bias_ma5,
        bias_ma10=0.0,
        bias_ma20=0.0,
        volume_status=volume_status,
        volume_ratio_5d=1.0,
        volume_trend="",
        support_ma5=support_ma5,
        support_ma10=support_ma10,
        macd_status=macd_status,
        rsi_status=rsi_status,
    )


class StockAnalyzerBiasTestCase(unittest.TestCase):
    """Tests for bias_ma5 and strong-trend relief in _generate_signal."""

    def setUp(self) -> None:
        self.analyzer = StockTrendAnalyzer()

    def _assert_contains(self, items: list, substring: str) -> None:
        """Assert at least one item contains the substring."""
        self.assertTrue(
            any(substring in s for s in items),
            msg=f"Expected substring '{substring}' in {items}",
        )

    def _assert_not_contains(self, items: list, substring: str) -> None:
        """Assert no item contains the substring."""
        self.assertFalse(
            any(substring in s for s in items),
            msg=f"Did not expect substring '{substring}' in {items}",
        )

    @patch("src.stock_analyzer.get_config")
    def test_bias_nan_defense(self, mock_get_config: MagicMock) -> None:
        """bias_ma5=NaN should be treated as 0.0 without exception."""
        mock_get_config.return_value.bias_threshold = 5.0
        result = _make_result(
            trend_status=TrendStatus.BULL,
            bias_ma5=float("nan"),
        )
        self.analyzer._generate_signal(result)
        self.assertIsInstance(result.signal_score, (int, float))
        self.assertFalse(math.isnan(result.signal_score))

    @patch("src.stock_analyzer.get_config")
    def test_bias_negative_pullback(self, mock_get_config: MagicMock) -> None:
        """bias=-2% should yield '绝佳买点' (within -2~2 band)."""
        mock_get_config.return_value.bias_threshold = 5.0
        result = _make_result(
            trend_status=TrendStatus.BULL,
            bias_ma5=-2.0,
        )
        self.analyzer._generate_signal(result)
        self._assert_contains(result.signal_reasons, "绝佳买点")

    @patch("src.stock_analyzer.get_config")
    def test_bias_close_to_ma5(self, mock_get_config: MagicMock) -> None:
        """bias=1.5% should yield '绝佳买点'."""
        mock_get_config.return_value.bias_threshold = 5.0
        result = _make_result(
            trend_status=TrendStatus.BULL,
            bias_ma5=1.5,
        )
        self.analyzer._generate_signal(result)
        self._assert_contains(result.signal_reasons, "绝佳买点")

    @patch("src.stock_analyzer.get_config")
    def test_bias_slightly_high(self, mock_get_config: MagicMock) -> None:
        """bias=4% (BULL eff_th=6.0) should yield '等待回踩'."""
        mock_get_config.return_value.bias_threshold = 5.0
        result = _make_result(
            trend_status=TrendStatus.BULL,
            bias_ma5=4.0,
        )
        self.analyzer._generate_signal(result)
        self._assert_contains(result.signal_reasons, "等待回踩")

    @patch("src.stock_analyzer.get_config")
    def test_strong_trend_relaxed_threshold(self, mock_get_config: MagicMock) -> None:
        """STRONG_BULL + trend_strength=75 + bias=6% -> '等待回踩' (effective=8.0%)."""
        mock_get_config.return_value.bias_threshold = 5.0
        result = _make_result(
            trend_status=TrendStatus.STRONG_BULL,
            trend_strength=75.0,
            bias_ma5=6.0,
        )
        self.analyzer._generate_signal(result)
        self._assert_contains(result.signal_reasons, "等待回踩")
        self._assert_not_contains(result.risk_factors, "严禁追高")

    @patch("src.stock_analyzer.get_config")
    def test_non_strong_trend_strict_threshold(self, mock_get_config: MagicMock) -> None:
        """BULL + bias=6% (eff_th=6.0) -> '等待回踩' (at upper edge of acceptable range)."""
        mock_get_config.return_value.bias_threshold = 5.0
        result = _make_result(
            trend_status=TrendStatus.BULL,
            bias_ma5=6.0,
        )
        self.analyzer._generate_signal(result)
        self._assert_contains(result.signal_reasons, "等待回踩")

    @patch("src.stock_analyzer.get_config")
    def test_strong_trend_exceed_effective(self, mock_get_config: MagicMock) -> None:
        """STRONG_BULL + trend_strength=80 + bias=10% -> '严禁追高!' (exceeds 7.5%)."""
        mock_get_config.return_value.bias_threshold = 5.0
        result = _make_result(
            trend_status=TrendStatus.STRONG_BULL,
            trend_strength=80.0,
            bias_ma5=10.0,
        )
        self.analyzer._generate_signal(result)
        self._assert_contains(result.risk_factors, "严禁追高")

    @patch("src.stock_analyzer.get_config")
    def test_boundary_at_base_threshold(self, mock_get_config: MagicMock) -> None:
        """bias=5.0% (BULL eff_th=6.0) -> '等待回踩' (5.0 falls in eff_th*0.6 < bias <= eff_th)."""
        mock_get_config.return_value.bias_threshold = 5.0
        result = _make_result(
            trend_status=TrendStatus.BULL,
            bias_ma5=5.0,
        )
        self.analyzer._generate_signal(result)
        # BULL eff_th = 5.0 * 1.2 = 6.0
        # 2 < 5.0 <= 3.6? False
        # 3.6 < 5.0 <= 6.0? True -> "等待回踩"
        self._assert_contains(result.signal_reasons, "等待回踩")
