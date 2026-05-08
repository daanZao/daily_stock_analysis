# -*- coding: utf-8 -*-
"""
===================================
A股自选股智能分析系统 - 核心分析流水线
===================================

职责：
1. 管理整个分析流程
2. 协调数据获取、存储、搜索、分析、通知等模块
3. 实现并发控制和异常处理
4. 提供股票分析的核心功能
"""

import logging
import threading
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from typing import List, Dict, Any, Optional, Tuple, Callable

import numpy as np
import pandas as pd

from src.config import get_config, Config
from src.storage import get_db
from data_provider import DataFetcherManager
from data_provider.base import normalize_stock_code
from data_provider.realtime_types import ChipDistribution
from src.analyzer import (
    GeminiAnalyzer,
    AnalysisResult,
    _format_volume,
    fill_chip_structure_if_needed,
    fill_price_position_if_needed,
    format_analysis_prompt,
)
from src.data.stock_mapping import STOCK_NAME_MAP
from src.notification import NotificationService, NotificationChannel
from src.report_language import (
    get_unknown_text,
    infer_decision_type_from_advice,
    localize_confidence_level,
    localize_operation_advice,
    localize_trend_prediction,
    normalize_report_language,
)
from src.search_service import SearchService
from src.services.social_sentiment_service import SocialSentimentService
from src.enums import ReportType
from src.stock_analyzer import BuySignal, StockTrendAnalyzer, TrendAnalysisResult
from src.core.trading_calendar import (
    get_effective_trading_date,
    get_market_for_stock,
    get_market_now,
    is_market_open,
)
from data_provider.us_index_mapping import is_us_stock_code
from bot.models import BotMessage


logger = logging.getLogger(__name__)

# 防御性 guard：当实例绕过 __init__（如测试中 __new__）构造时，
# double-check 初始化 _single_stock_notify_lock 仍然线程安全。
_SINGLE_STOCK_NOTIFY_LOCK_INIT_GUARD = threading.Lock()


class StockAnalysisPipeline:
    """
    股票分析主流程调度器
    
    职责：
    1. 管理整个分析流程
    2. 协调数据获取、存储、搜索、分析、通知等模块
    3. 实现并发控制和异常处理
    """
    
    def __init__(
        self,
        config: Optional[Config] = None,
        max_workers: Optional[int] = None,
        source_message: Optional[BotMessage] = None,
        query_id: Optional[str] = None,
        query_source: Optional[str] = None,
        save_context_snapshot: Optional[bool] = None,
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ):
        """
        初始化调度器
        
        Args:
            config: 配置对象（可选，默认使用全局配置）
            max_workers: 最大并发线程数（可选，默认从配置读取）
        """
        self.config = config or get_config()
        self.max_workers = max_workers or self.config.max_workers
        self.source_message = source_message
        self.query_id = query_id
        self.query_source = self._resolve_query_source(query_source)
        self.save_context_snapshot = (
            self.config.save_context_snapshot if save_context_snapshot is None else save_context_snapshot
        )
        self.progress_callback = progress_callback
        
        # 初始化各模块
        self.db = get_db()
        self.fetcher_manager = DataFetcherManager()
        # 不再单独创建 akshare_fetcher，统一使用 fetcher_manager 获取增强数据
        self.trend_analyzer = StockTrendAnalyzer()  # 技术分析器
        self.analyzer = GeminiAnalyzer(config=self.config)
        self.notifier = NotificationService(source_message=source_message)
        self._single_stock_notify_lock = threading.Lock()
        
        # 初始化搜索服务（可选，初始化失败不应阻断主分析流程）
        try:
            self.search_service = SearchService(
                bocha_keys=self.config.bocha_api_keys,
                tavily_keys=self.config.tavily_api_keys,
                anspire_keys=self.config.anspire_api_keys,
                brave_keys=self.config.brave_api_keys,
                serpapi_keys=self.config.serpapi_keys,
                minimax_keys=self.config.minimax_api_keys,
                searxng_base_urls=self.config.searxng_base_urls,
                searxng_public_instances_enabled=self.config.searxng_public_instances_enabled,
                news_max_age_days=self.config.news_max_age_days,
                news_strategy_profile=getattr(self.config, "news_strategy_profile", "short"),
            )
        except Exception as exc:
            logger.warning("搜索服务初始化失败，将以无搜索模式运行: %s", exc, exc_info=True)
            self.search_service = None
        
        logger.info(f"调度器初始化完成，最大并发数: {self.max_workers}")
        logger.info("已启用技术分析引擎（均线/趋势/量价指标）")
        # 打印实时行情/筹码配置状态
        if self.config.enable_realtime_quote:
            logger.info(f"实时行情已启用 (优先级: {self.config.realtime_source_priority})")
        else:
            logger.info("实时行情已禁用，将使用历史收盘价")
        if self.config.enable_chip_distribution:
            logger.info("筹码分布分析已启用")
        else:
            logger.info("筹码分布分析已禁用")
        if self.search_service is None:
            logger.warning("搜索服务未启用（初始化失败或依赖缺失）")
        elif self.search_service.is_available:
            logger.info("搜索服务已启用")
        else:
            logger.warning("搜索服务未启用（未配置搜索能力）")

        # 初始化社交舆情服务（仅美股，可选）
        try:
            self.social_sentiment_service = SocialSentimentService(
                api_key=self.config.social_sentiment_api_key,
                api_url=self.config.social_sentiment_api_url,
            )
            if self.social_sentiment_service.is_available:
                logger.info("Social sentiment service enabled (Reddit/X/Polymarket, US stocks only)")
        except Exception as exc:
            logger.warning(
                "社交舆情服务初始化失败，将跳过舆情分析: %s",
                exc,
                exc_info=True,
            )
            self.social_sentiment_service = None

    def _emit_progress(self, progress: int, message: str) -> None:
        """Best-effort bridge from pipeline stages to task SSE progress."""
        callback = getattr(self, "progress_callback", None)
        if callback is None:
            return
        try:
            callback(progress, message)
        except Exception as exc:
            query_id = getattr(self, "query_id", None)
            logger.warning(
                "[pipeline] progress callback failed: %s (progress=%s, message=%r, query_id=%s)",
                exc,
                progress,
                message,
                query_id,
                extra={
                    "progress": progress,
                    "progress_message": message,
                    "query_id": query_id,
                },
            )

    def fetch_and_save_stock_data(
        self, 
        code: str,
        force_refresh: bool = False,
        current_time: Optional[datetime] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        获取并保存单只股票数据
        
        断点续传逻辑：
        1. 检查数据库是否已有最新可复用交易日数据
        2. 如果有且不强制刷新，则跳过网络请求
        3. 否则从数据源获取并保存
        
        Args:
            code: 股票代码
            force_refresh: 是否强制刷新（忽略本地缓存）
            current_time: 本轮运行冻结的参考时间，用于统一断点续传目标交易日判断
            
        Returns:
            Tuple[是否成功, 错误信息]
        """
        stock_name = code
        try:
            # 首先获取股票名称
            stock_name = self.fetcher_manager.get_stock_name(code, allow_realtime=False)

            target_date = self._resolve_resume_target_date(
                code, current_time=current_time
            )

            # 断点续传检查：如果最新可复用交易日的数据已存在，则跳过
            if not force_refresh and self.db.has_today_data(code, target_date):
                logger.info(
                    f"{stock_name}({code}) {target_date} 数据已存在，跳过获取（断点续传）"
                )
                return True, None

            # 从数据源获取数据
            logger.info(f"{stock_name}({code}) 开始从数据源获取数据...")
            df, source_name = self.fetcher_manager.get_daily_data(code, days=30)

            if df is None or df.empty:
                return False, "获取数据为空"

            # 保存到数据库
            saved_count = self.db.save_daily_data(df, code, source_name)
            logger.info(f"{stock_name}({code}) 数据保存成功（来源: {source_name}，新增 {saved_count} 条）")

            return True, None

        except Exception as e:
            error_msg = f"获取/保存数据失败: {str(e)}"
            logger.error(f"{stock_name}({code}) {error_msg}")
            return False, error_msg
    
    def analyze_stock(self, code: str, report_type: ReportType, query_id: str) -> Optional[AnalysisResult]:
        """
        分析单只股票（增强版：含量比、换手率、筹码分析、多维度情报）
        
        流程：
        1. 获取实时行情（量比、换手率）- 通过 DataFetcherManager 自动故障切换
        2. 获取筹码分布 - 通过 DataFetcherManager 带熔断保护
        3. 进行趋势分析（基于交易理念）
        4. 多维度情报搜索（最新消息+风险排查+业绩预期）
        5. 从数据库获取分析上下文
        6. 调用 AI 进行综合分析
        
        Args:
            query_id: 查询链路关联 id
            code: 股票代码
            report_type: 报告类型
            
        Returns:
            AnalysisResult 或 None（如果分析失败）
        """
        stock_name = code
        try:
            self._emit_progress(18, f"{code}：正在获取行情与筹码数据")
            # 获取股票名称（先走轻量名称路径，后续若 realtime_quote 有 name 再覆盖）
            stock_name = self.fetcher_manager.get_stock_name(code, allow_realtime=False)

            # Step 1: 获取实时行情（量比、换手率等）- 使用统一入口，自动故障切换
            realtime_quote = None
            try:
                if self.config.enable_realtime_quote:
                    realtime_quote = self.fetcher_manager.get_realtime_quote(code, log_final_failure=False)
                    if realtime_quote:
                        # 使用实时行情返回的真实股票名称
                        if realtime_quote.name:
                            stock_name = realtime_quote.name
                        # 兼容不同数据源的字段（有些数据源可能没有 volume_ratio）
                        volume_ratio = getattr(realtime_quote, 'volume_ratio', None)
                        turnover_rate = getattr(realtime_quote, 'turnover_rate', None)
                        logger.info(f"{stock_name}({code}) 实时行情: 价格={realtime_quote.price}, "
                                  f"量比={volume_ratio}, 换手率={turnover_rate}% "
                                  f"(来源: {realtime_quote.source.value if hasattr(realtime_quote, 'source') else 'unknown'})")
                    else:
                        logger.warning(f"{stock_name}({code}) 所有实时行情数据源均不可用，已降级为历史收盘价继续分析")
                else:
                    logger.info(f"{stock_name}({code}) 实时行情已禁用，使用历史收盘价继续分析")
            except Exception as e:
                logger.warning(f"{stock_name}({code}) 实时行情链路异常，已降级为历史收盘价继续分析: {e}")

            # 如果还是没有名称，使用代码作为名称
            if not stock_name:
                stock_name = f'股票{code}'

            # Step 2: 获取筹码分布 - 使用统一入口，带熔断保护
            chip_data = None
            try:
                chip_data = self.fetcher_manager.get_chip_distribution(code)
                if chip_data:
                    logger.info(f"{stock_name}({code}) 筹码分布: 获利比例={chip_data.profit_ratio:.1%}, "
                              f"90%集中度={chip_data.concentration_90:.2%}")
                else:
                    logger.debug(f"{stock_name}({code}) 筹码分布获取失败或已禁用")
            except Exception as e:
                logger.warning(f"{stock_name}({code}) 获取筹码分布失败: {e}")

            # If agent mode is explicitly enabled, or specific agent skills are configured, use the Agent analysis pipeline.
            # NOTE: use config.agent_mode (explicit opt-in) instead of
            # config.is_agent_available() so that users who only configured an
            # API Key for the traditional analysis path are not silently
            # switched to Agent mode (which is slower and more expensive).
            use_agent = getattr(self.config, 'agent_mode', False)
            if not use_agent:
                # Auto-enable agent mode when specific skills are configured (e.g., scheduled task with strategy)
                configured_skills = getattr(self.config, 'agent_skills', [])
                if configured_skills and configured_skills != ['all']:
                    use_agent = True
                    logger.info(f"{stock_name}({code}) Auto-enabled agent mode due to configured skills: {configured_skills}")

            self._emit_progress(32, f"{stock_name}：正在聚合基本面与趋势数据")

            # Step 2.5: 基本面能力聚合（统一入口，异常降级）
            # - 失败时返回 partial/failed，不影响既有技术面/新闻链路
            # - 关闭开关时仍返回 not_supported 结构
            fundamental_context = None
            try:
                fundamental_context = self.fetcher_manager.get_fundamental_context(
                    code,
                    budget_seconds=getattr(self.config, 'fundamental_stage_timeout_seconds', 1.5),
                )
            except Exception as e:
                logger.warning(f"{stock_name}({code}) 基本面聚合失败: {e}")
                fundamental_context = self.fetcher_manager.build_failed_fundamental_context(code, str(e))

            fundamental_context = self._attach_belong_boards_to_fundamental_context(
                code,
                fundamental_context,
            )

            # P0: write-only snapshot, fail-open, no read dependency on this table.
            try:
                self.db.save_fundamental_snapshot(
                    query_id=query_id,
                    code=code,
                    payload=fundamental_context,
                    source_chain=fundamental_context.get("source_chain", []),
                    coverage=fundamental_context.get("coverage", {}),
                )
            except Exception as e:
                logger.debug(f"{stock_name}({code}) 基本面快照写入失败: {e}")

            # Step 3: 趋势分析（基于交易理念）— 在 Agent 分支之前执行，供两条路径共用
            trend_result: Optional[TrendAnalysisResult] = None
            try:
                from src.services.history_loader import get_frozen_target_date
                _mkt = get_market_for_stock(normalize_stock_code(code))
                frozen = get_frozen_target_date()
                end_date = frozen if frozen else get_market_now(_mkt).date()
                start_date = end_date - timedelta(days=89)  # ~60 trading days for MA60
                historical_bars = self.db.get_data_range(code, start_date, end_date)
                if historical_bars:
                    df = pd.DataFrame([bar.to_dict() for bar in historical_bars])
                    # Issue #234: Augment with realtime for intraday MA calculation
                    if self.config.enable_realtime_quote and realtime_quote:
                        df = self._augment_historical_with_realtime(df, realtime_quote, code)
                    trend_result = self.trend_analyzer.analyze(df, code)
                    # 最小阻力校验：筹码分布确认趋势方向的阻力大小
                    _price = getattr(realtime_quote, 'price', None) if realtime_quote else None
                    self._validate_least_resistance(trend_result, chip_data, current_price=_price)
                    logger.info(f"{stock_name}({code}) 趋势分析: {trend_result.trend_status.value}, "
                              f"买入信号={trend_result.buy_signal.value}, 评分={trend_result.signal_score}")
            except Exception as e:
                logger.warning(f"{stock_name}({code}) 趋势分析失败: {e}", exc_info=True)

            if use_agent:
                logger.info(f"{stock_name}({code}) 启用 Agent 模式进行分析")
                self._emit_progress(58, f"{stock_name}：正在切换 Agent 分析链路")
                return self._analyze_with_agent(
                    code,
                    report_type,
                    query_id,
                    stock_name,
                    realtime_quote,
                    chip_data,
                    fundamental_context,
                    trend_result,
                )

            # Step 4: 多维度情报搜索（最新消息+风险排查+业绩预期）
            news_context = None
            self._emit_progress(46, f"{stock_name}：正在检索新闻与舆情")
            if self.search_service is not None and self.search_service.is_available:
                logger.info(f"{stock_name}({code}) 开始多维度情报搜索...")

                # 使用多维度搜索（最多5次搜索）
                intel_results = self.search_service.search_comprehensive_intel(
                    stock_code=code,
                    stock_name=stock_name,
                    max_searches=5
                )

                # 格式化情报报告
                if intel_results:
                    news_context = self.search_service.format_intel_report(intel_results, stock_name)
                    total_results = sum(
                        len(r.results) for r in intel_results.values() if r.success
                    )
                    logger.info(f"{stock_name}({code}) 情报搜索完成: 共 {total_results} 条结果")
                    logger.debug(f"{stock_name}({code}) 情报搜索结果:\n{news_context}")

                    # 保存新闻情报到数据库（用于后续复盘与查询）
                    try:
                        query_context = self._build_query_context(query_id=query_id)
                        for dim_name, response in intel_results.items():
                            if response and response.success and response.results:
                                self.db.save_news_intel(
                                    code=code,
                                    name=stock_name,
                                    dimension=dim_name,
                                    query=response.query,
                                    response=response,
                                    query_context=query_context
                                )
                    except Exception as e:
                        logger.warning(f"{stock_name}({code}) 保存新闻情报失败: {e}")
            else:
                logger.info(f"{stock_name}({code}) 搜索服务不可用，跳过情报搜索")

            # Step 4.5: Social sentiment intelligence (US stocks only)
            if self.social_sentiment_service is not None and self.social_sentiment_service.is_available and is_us_stock_code(code):
                try:
                    social_context = self.social_sentiment_service.get_social_context(code)
                    if social_context:
                        logger.info(f"{stock_name}({code}) Social sentiment data retrieved")
                        if news_context:
                            news_context = news_context + "\n\n" + social_context
                        else:
                            news_context = social_context
                except Exception as e:
                    logger.warning(f"{stock_name}({code}) Social sentiment fetch failed: {e}")

            # Step 5: 获取分析上下文（技术面数据）
            self._emit_progress(58, f"{stock_name}：正在整理分析上下文")
            context = self.db.get_analysis_context(code)

            if context is None:
                logger.warning(f"{stock_name}({code}) 无法获取历史行情数据，将仅基于新闻和实时行情分析")
                _mkt_date = get_market_now(
                    get_market_for_stock(normalize_stock_code(code))
                ).date()
                context = {
                    'code': code,
                    'stock_name': stock_name,
                    'date': _mkt_date.isoformat(),
                    'data_missing': True,
                    'today': {},
                    'yesterday': {}
                }
            
            # Step 6: 增强上下文数据（添加实时行情、筹码、趋势分析结果、股票名称）
            # 含 SEPA 数据质量门禁，最多 3 次重试
            enhanced_context, failure_result = self._enhance_context_with_sepa_quality_gate(
                context,
                realtime_quote,
                chip_data,
                trend_result,
                stock_name,
                fundamental_context,
            )
            if failure_result is not None:
                failure_result.query_id = query_id
                return failure_result

            # Step 7: 调用 AI 分析（传入增强的上下文和新闻）
            llm_progress_state = {"last_progress": 64}

            def _on_llm_stream(chars_received: int) -> None:
                dynamic_progress = min(92, 64 + min(chars_received // 80, 28))
                if dynamic_progress <= llm_progress_state["last_progress"]:
                    return
                llm_progress_state["last_progress"] = dynamic_progress
                self._emit_progress(
                    dynamic_progress,
                    f"{stock_name}：LLM 正在生成分析结果（已接收 {chars_received} 字符）",
                )

            self._emit_progress(64, f"{stock_name}：正在请求 LLM 生成报告")
            result = self.analyzer.analyze(
                enhanced_context,
                news_context=news_context,
                progress_callback=self._emit_progress,
                stream_progress_callback=_on_llm_stream,
            )

            # Step 7.5: 填充分析时的价格信息到 result
            if result:
                self._emit_progress(94, f"{stock_name}：正在校验并整理分析结果")
                result.query_id = query_id
                realtime_data = enhanced_context.get('realtime', {})
                result.current_price = realtime_data.get('price')
                result.change_pct = realtime_data.get('change_pct')

            # Step 7.6: chip_structure fallback (Issue #589)
            if result and chip_data:
                fill_chip_structure_if_needed(result, chip_data)

            # Step 7.7: price_position fallback
            if result:
                fill_price_position_if_needed(result, trend_result, realtime_quote)

            # Step 8: 保存分析历史记录
            if result and result.success:
                try:
                    self._emit_progress(97, f"{stock_name}：正在保存分析报告")
                    context_snapshot = self._build_context_snapshot(
                        enhanced_context=enhanced_context,
                        news_content=news_context,
                        realtime_quote=realtime_quote,
                        chip_data=chip_data
                    )
                    self.db.save_analysis_history(
                        result=result,
                        query_id=query_id,
                        report_type=report_type.value,
                        news_content=news_context,
                        context_snapshot=context_snapshot,
                        save_snapshot=self.save_context_snapshot
                    )
                except Exception as e:
                    logger.warning(f"{stock_name}({code}) 保存分析历史失败: {e}")

            return result

        except Exception as e:
            logger.error(f"{stock_name}({code}) 分析失败: {e}")
            logger.exception(f"{stock_name}({code}) 详细错误信息:")
            return None

    # ------------------------------------------------------------------
    # SEPA 数据质量门禁
    # ------------------------------------------------------------------

    _FUNDAMENTAL_KEY_FIELDS: Tuple[str, ...] = (
        "revenue",
        "net_profit_parent",
        "revenue_yoy",
        "net_profit_yoy",
        "roe",
        "gross_margin",
        "net_margin",
        "eps",
    )

    @staticmethod
    def _has_numeric_value(data: Any, fields: Tuple[str, ...]) -> bool:
        """Return True if at least one field in `data` has a valid numeric value (0 counts, None/NaN/empty does not)."""
        if not isinstance(data, dict):
            return False
        for f in fields:
            v = data.get(f)
            if v is None:
                continue
            if isinstance(v, (int, float)):
                if isinstance(v, float) and (np.isnan(v) or np.isinf(v)):
                    continue
                return True
            if isinstance(v, str):
                s = v.strip()
                if not s:
                    continue
                try:
                    float(s)
                    return True
                except (ValueError, TypeError):
                    continue
        return False

    def _validate_sepa_data_quality(
        self,
        enhanced_context: Dict[str, Any],
        fundamental_context: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, str]:
        """验证 SEPA 分析所需的核心数据是否完整。

        Returns:
            (is_valid, error_message)
        """
        code = enhanced_context.get('code', '')
        today = enhanced_context.get('today', {})
        sepa = enhanced_context.get('sepa_analysis', {})

        # 1. 检查历史日线基础字段
        required_fields = ['close', 'volume', 'high', 'low', 'open']
        missing = [f for f in required_fields if today.get(f) is None]
        if missing:
            return False, f"历史日线数据缺失字段: {', '.join(missing)}"

        # 2. 检查均线数据
        for ma in ['ma50', 'ma150', 'ma200']:
            val = sepa.get(ma)
            if val is None or val <= 0:
                return False, f"均线数据缺失: {ma}={val}"

        # 3. 检查季度业绩数据（最少 1 个季度，且关键字段有有效数值）
        financial_reports = enhanced_context.get('financial_reports', [])
        has_fr = False
        if financial_reports and len(financial_reports) >= 1:
            for report in financial_reports:
                if self._has_numeric_value(report, self._FUNDAMENTAL_KEY_FIELDS):
                    has_fr = True
                    break

        # 备选：检查 fundamental_context 中的 earnings
        has_fundamental_earnings = False
        if fundamental_context:
            earnings = fundamental_context.get('earnings')
            if earnings and isinstance(earnings, dict):
                # 优先检查与 prompt 注入路径一致的结构（data.financial_report 或 financial_report）
                for report in (
                    earnings.get('data', {}).get('financial_report', {}),
                    earnings.get('financial_report', {}),
                ):
                    if self._has_numeric_value(report, self._FUNDAMENTAL_KEY_FIELDS):
                        has_fundamental_earnings = True
                        break

                # 兜底：检查其他文本型字段（forecast_summary 等）
                if not has_fundamental_earnings:
                    for k, v in earnings.items():
                        if v is None:
                            continue
                        if isinstance(v, str) and v.strip():
                            has_fundamental_earnings = True
                            break

        if has_fr or has_fundamental_earnings:
            return True, ""

        if not has_fr and not has_fundamental_earnings:
            return (
                False,
                "季度业绩数据缺失: FinancialReport 表无有效数值且 fundamental_context.earnings 无有效数值"
            )
        if not has_fr:
            return False, "季度业绩数据缺失: FinancialReport 表无有效数值"
        return False, "季度业绩数据缺失: fundamental_context.earnings 无有效数值"

    def _invalidate_stock_history_cache(self, code: str) -> None:
        """清除股票历史日线 DB 缓存，强制下次从网络重新获取。"""
        try:
            with self.db.get_session() as session:
                from sqlalchemy import delete
                from src.storage import StockDaily
                result = session.execute(
                    delete(StockDaily).where(StockDaily.code == code)
                )
                session.commit()
                logger.info(
                    f"[{code}] 已清除 {result.rowcount} 条历史日线缓存，下次将从网络重新获取"
                )
        except Exception as e:
            logger.warning(f"[{code}] 清除历史缓存失败: {e}")

    def _validate_least_resistance(
        self,
        trend_result: TrendAnalysisResult,
        chip_data: Optional[Any],
        current_price: Optional[float] = None,
    ) -> None:
        """最小阻力方向校验 — 不评分，只过滤（当前仅覆盖做多方向）。

        当趋势、量能、RS 指向做多方向时，筹码分布确认"向上阻力确实小"才放行。
        否则写入 risk_factors 并降级信号。
        """
        if not chip_data or not trend_result:
            return

        # 统一从 ChipDistribution 或 dict 提取字段
        if hasattr(chip_data, "profit_ratio"):
            concentration_90 = float(getattr(chip_data, "concentration_90", 1.0) or 1.0)
            profit_ratio = float(getattr(chip_data, "profit_ratio", 0.5) or 0.5)
            avg_cost = float(getattr(chip_data, "avg_cost", 0.0) or 0.0)
        else:
            chip_dict = chip_data if isinstance(chip_data, dict) else {}
            concentration_90 = float(chip_dict.get("concentration_90", 1.0) or 1.0)
            profit_ratio = float(chip_dict.get("profit_ratio", 0.5) or 0.5)
            avg_cost = float(chip_dict.get("avg_cost", 0.0) or 0.0)

        # 向上阻力校验（做多时）
        if trend_result.buy_signal.value in ("强烈买入", "买入"):
            # 情况1：获利盘极高 + 筹码发散 → 到处都是抛压
            if profit_ratio > 0.85 and concentration_90 > 0.20:
                trend_result.risk_factors.append(
                    f"最小阻力：获利比例{profit_ratio:.0%}且筹码发散，上方抛压重重"
                )
                if trend_result.buy_signal.value == "强烈买入":
                    trend_result.buy_signal = BuySignal.BUY

            # 情况2：现价远离平均成本，下方支撑真空
            if avg_cost > 0 and current_price and current_price > avg_cost * 1.15:
                if concentration_90 > 0.25:
                    trend_result.risk_factors.append(
                        f"最小阻力：现价高于平均成本{(current_price / avg_cost - 1) * 100:.0f}%，"
                        f"下方支撑真空，回调无承接"
                    )

    def _fetch_minutely_analysis(self, code: str, days: int = 5) -> Dict[str, Any]:
        """获取60分钟K线并计算关键指标（RSI、MA20、K线形态、量趋势）。

        Returns:
            {"status": "ok" | "no_data", "records": [...], "rsi_14": float|None,
             "ma20": float|None, "latest_pattern": str, "volume_trend": str,
             "above_ma20": bool, "latest_close": float|None}
        """
        try:
            from src.agent.tools.data_tools import _handle_get_minutely_history
            raw = _handle_get_minutely_history(code, days=days)
            if raw.get("status") != "ok":
                return {"status": "no_data", "code": code}

            records = raw.get("records", [])
            if len(records) < 20:
                return {"status": "no_data", "code": code, "note": "数据不足20根"}

            df = pd.DataFrame(records)
            if "close" not in df.columns or df["close"].isna().all():
                return {"status": "no_data", "code": code, "note": "close字段缺失"}

            df["close"] = pd.to_numeric(df["close"], errors="coerce")
            df["open"] = pd.to_numeric(df.get("open", df["close"]), errors="coerce")
            df["high"] = pd.to_numeric(df.get("high", df["close"]), errors="coerce")
            df["low"] = pd.to_numeric(df.get("low", df["close"]), errors="coerce")
            df["volume"] = pd.to_numeric(df.get("volume", 0), errors="coerce")

            # RSI(14) — 复用 stock_analyzer 的手动计算逻辑
            delta = df["close"].diff()
            gain = delta.where(delta > 0, 0)
            loss = -delta.where(delta < 0, 0)
            avg_gain = gain.rolling(window=14, min_periods=14).mean()
            avg_loss = loss.rolling(window=14, min_periods=14).mean()
            rs = avg_gain / avg_loss.replace(0, np.nan)
            df["rsi_14"] = 100 - (100 / (1 + rs))

            # MA20
            df["ma20"] = df["close"].rolling(window=20, min_periods=20).mean()

            latest = df.iloc[-1]
            prev = df.iloc[-2] if len(df) >= 2 else latest

            # K线形态判断
            o, h, l, c = latest["open"], latest["high"], latest["low"], latest["close"]
            body = abs(c - o)
            rng = h - l if h != l else 1e-6
            pattern = "—"
            if rng > 0:
                if body / rng < 0.1:
                    pattern = "十字星"
                elif c > o and (o - l) > body * 2 and (h - c) < body * 0.5:
                    pattern = "锤子线"
                elif c < o and (h - o) > body * 2 and (c - l) < body * 0.5:
                    pattern = "流星线"
                elif c > o and prev["close"] < prev["open"] and o < prev["close"] and c > prev["open"]:
                    pattern = "阳吞没"
                elif c < o and prev["close"] > prev["open"] and o > prev["close"] and c < prev["open"]:
                    pattern = "阴吞没"

            # 量趋势：最近3根 vs 前3根
            vol_trend = "—"
            if len(df) >= 6:
                recent_vol = df["volume"].iloc[-3:].mean()
                prior_vol = df["volume"].iloc[-6:-3].mean()
                if prior_vol > 0:
                    v_ratio = recent_vol / prior_vol
                    if v_ratio > 1.3:
                        vol_trend = "量增"
                    elif v_ratio < 0.7:
                        vol_trend = "量缩"
                    else:
                        vol_trend = "持平"

            rsi_val = latest["rsi_14"]
            rsi_status = "—"
            if pd.notna(rsi_val):
                if rsi_val > 80:
                    rsi_status = "极度超买"
                elif rsi_val > 70:
                    rsi_status = "超买"
                elif rsi_val < 20:
                    rsi_status = "极度超卖"
                elif rsi_val < 30:
                    rsi_status = "超卖"
                else:
                    rsi_status = "正常"

            ma20_val = latest["ma20"]
            above_ma20 = bool(pd.notna(ma20_val) and c > ma20_val)

            return {
                "status": "ok",
                "code": code,
                "record_count": len(records),
                "rsi_14": round(float(rsi_val), 2) if pd.notna(rsi_val) else None,
                "rsi_status": rsi_status,
                "ma20": round(float(ma20_val), 3) if pd.notna(ma20_val) else None,
                "latest_pattern": pattern,
                "volume_trend": vol_trend,
                "above_ma20": above_ma20,
                "latest_close": round(float(c), 3) if pd.notna(c) else None,
                "records": records[-20:],  # 仅保留最近20根给下游
            }
        except Exception as exc:
            logger.warning("[%s] 60分钟数据获取/计算失败: %s", code, exc)
            return {"status": "no_data", "code": code, "note": str(exc)}

    def _enhance_context_with_sepa_quality_gate(
        self,
        context: Dict[str, Any],
        realtime_quote,
        chip_data: Optional[ChipDistribution],
        trend_result: Optional[TrendAnalysisResult],
        stock_name: str,
        fundamental_context: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Dict[str, Any], Optional[AnalysisResult]]:
        """增强上下文并执行 SEPA 数据质量门禁（含最多 3 次重试）。

        Returns:
            (enhanced_context, failure_result) — 如果 failure_result 不为 None，
            表示数据质量未通过，应直接返回该结果而不调用 LLM。
        """
        code = context.get('code', '')
        for attempt in range(1, 4):
            enhanced = self._enhance_context(
                context, realtime_quote, chip_data, trend_result, stock_name, fundamental_context
            )
            is_valid, error_msg = self._validate_sepa_data_quality(enhanced, fundamental_context)
            if is_valid:
                return enhanced, None

            if attempt < 3:
                logger.warning(
                    f"[{code}] SEPA 数据质量检查未通过（第 {attempt}/3 次）: {error_msg}，清除缓存重试..."
                )
                self._invalidate_stock_history_cache(code)
            else:
                logger.error(
                    f"[{code}] SEPA 数据质量检查未通过（第 3/3 次）: {error_msg}，放弃分析"
                )
                return enhanced, AnalysisResult(
                    code=code,
                    name=stock_name or code,
                    sentiment_score=0,
                    trend_prediction="数据不足",
                    operation_advice="观望",
                    success=False,
                    error_message=f"SEPA 数据质量未通过: {error_msg}",
                )
        # unreachable
        return enhanced, None  # type: ignore[return-value]

    def _enhance_context(
        self,
        context: Dict[str, Any],
        realtime_quote,
        chip_data: Optional[ChipDistribution],
        trend_result: Optional[TrendAnalysisResult],
        stock_name: str = "",
        fundamental_context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        增强分析上下文
        
        将实时行情、筹码分布、趋势分析结果、股票名称添加到上下文中
        
        Args:
            context: 原始上下文
            realtime_quote: 实时行情数据（UnifiedRealtimeQuote 或 None）
            chip_data: 筹码分布数据
            trend_result: 趋势分析结果
            stock_name: 股票名称
            
        Returns:
            增强后的上下文
        """
        enhanced = context.copy()
        enhanced["report_language"] = normalize_report_language(getattr(self.config, "report_language", "zh"))
        
        # 添加股票名称
        if stock_name:
            enhanced['stock_name'] = stock_name
        elif realtime_quote and getattr(realtime_quote, 'name', None):
            enhanced['stock_name'] = realtime_quote.name

        # 将运行时搜索窗口透传给 analyzer，避免与全局配置重新读取产生窗口不一致
        enhanced['news_window_days'] = getattr(self.search_service, "news_window_days", 3)
        
        # 添加实时行情（兼容不同数据源的字段差异）
        if realtime_quote:
            # 使用 getattr 安全获取字段，缺失字段返回 None 或默认值
            volume_ratio = getattr(realtime_quote, 'volume_ratio', None)
            enhanced['realtime'] = {
                'name': getattr(realtime_quote, 'name', ''),
                'price': getattr(realtime_quote, 'price', None),
                'change_pct': getattr(realtime_quote, 'change_pct', None),
                'volume_ratio': volume_ratio,
                'volume_ratio_desc': self._describe_volume_ratio(volume_ratio) if volume_ratio else '无数据',
                'turnover_rate': getattr(realtime_quote, 'turnover_rate', None),
                'pe_ratio': getattr(realtime_quote, 'pe_ratio', None),
                'pb_ratio': getattr(realtime_quote, 'pb_ratio', None),
                'total_mv': getattr(realtime_quote, 'total_mv', None),
                'circ_mv': getattr(realtime_quote, 'circ_mv', None),
                'change_60d': getattr(realtime_quote, 'change_60d', None),
                'source': getattr(realtime_quote, 'source', None),
            }
            # 移除 None 值以减少上下文大小
            enhanced['realtime'] = {k: v for k, v in enhanced['realtime'].items() if v is not None}
        
        # 添加筹码分布
        if chip_data:
            current_price = getattr(realtime_quote, 'price', 0) if realtime_quote else 0
            enhanced['chip'] = {
                'profit_ratio': chip_data.profit_ratio,
                'avg_cost': chip_data.avg_cost,
                'concentration_90': chip_data.concentration_90,
                'concentration_70': chip_data.concentration_70,
                'chip_status': chip_data.get_chip_status(current_price or 0),
            }
        
        # 添加趋势分析结果
        if trend_result:
            enhanced['trend_analysis'] = {
                'trend_status': trend_result.trend_status.value,
                'ma_alignment': trend_result.ma_alignment,
                'trend_strength': trend_result.trend_strength,
                'bias_ma5': trend_result.bias_ma5,
                'bias_ma10': trend_result.bias_ma10,
                'volume_status': trend_result.volume_status.value,
                'volume_trend': trend_result.volume_trend,
                'buy_signal': trend_result.buy_signal.value,
                'signal_score': trend_result.signal_score,
                'signal_reasons': trend_result.signal_reasons,
                'risk_factors': trend_result.risk_factors,
            }

        # --- SEPA 专用字段注入 ---
        code = context.get('code', '')
        today = enhanced.get('today', {})
        if code:
            # 1. MA50/150/200 从 today 读取（已预计算落表）
            sepa = {
                'ma50': today.get('ma50'),
                'ma150': today.get('ma150'),
                'ma200': today.get('ma200'),
            }
            # 2. 52周高低点、RS、涨停统计（调用已有工具 handler，失败不阻断）
            try:
                from src.agent.tools.data_tools import _handle_get_52w_range, _handle_get_relative_strength
                from src.agent.tools.analysis_tools import _handle_analyze_relative_strength
                w52 = _handle_get_52w_range(code)
                if w52.get('status') == 'ok':
                    sepa['high_52w'] = w52.get('high_52w')
                    sepa['low_52w'] = w52.get('low_52w')
                    sepa['pct_from_52w_high'] = w52.get('pct_from_52w_high')
                    sepa['pct_from_52w_low'] = w52.get('pct_from_52w_low')
                    sepa['within_25pct_of_high'] = w52.get('within_25pct_of_high')
                    sepa['above_130pct_of_low'] = w52.get('above_130pct_of_low')
                rs = _handle_get_relative_strength(code)
                if rs.get('status') == 'ok':
                    sepa['rs_stock_return_1y'] = rs.get('stock_return_1y_pct')
                    sepa['rs_index_return_1y'] = rs.get('index_return_1y_pct')
                    sepa['rs_ratio'] = rs.get('rs_ratio')
                    sepa['rs_rank_pct'] = rs.get('rs_rank_pct')
                    sepa['pass_sepa_rs_70'] = rs.get('pass_sepa_rs_70')
                rs_result = _handle_analyze_relative_strength(code, days=60)
                if rs_result.get('status') == 'ok':
                    sepa['limit_up_count'] = rs_result.get('limit_up_count')
                    sepa['limit_down_count'] = rs_result.get('limit_down_count')
                    sepa['failed_limit_up_count'] = rs_result.get('failed_limit_up_count')
                    sepa['max_consecutive_limit_up'] = rs_result.get('max_consecutive_limit_up')
                    sepa['momentum_grade'] = rs_result.get('momentum_grade')
                    sepa['grade_meaning'] = rs_result.get('grade_meaning')
                    sepa['rs_rating'] = rs_result.get('rs_rating')
                    sepa['sepa_score'] = rs_result.get('sepa_score')
                    sepa['above_ma20_ratio'] = rs_result.get('above_ma20_ratio')
                    sepa['trend_consistency'] = rs_result.get('trend_consistency')
                    sepa['total_return_pct'] = rs_result.get('total_return_pct')
                    sepa['max_drawdown_pct'] = rs_result.get('max_drawdown_pct')
            except Exception as exc:
                logger.warning("[%s] SEPA field injection failed: %s", code, exc)
            enhanced['sepa_analysis'] = sepa

        # Issue #234: Override today with realtime OHLC + trend MA for intraday analysis
        # Guard: trend_result.ma5 > 0 ensures MA calculation succeeded (data sufficient)
        if realtime_quote and trend_result and trend_result.ma5 > 0:
            price = getattr(realtime_quote, 'price', None)
            if price is not None and price > 0:
                yesterday_close = None
                if enhanced.get('yesterday') and isinstance(enhanced['yesterday'], dict):
                    yesterday_close = enhanced['yesterday'].get('close')
                orig_today = enhanced.get('today') or {}
                open_p = getattr(realtime_quote, 'open_price', None) or getattr(
                    realtime_quote, 'pre_close', None
                ) or yesterday_close or orig_today.get('open') or price
                high_p = getattr(realtime_quote, 'high', None) or price
                low_p = getattr(realtime_quote, 'low', None) or price
                vol = getattr(realtime_quote, 'volume', None)
                amt = getattr(realtime_quote, 'amount', None)
                pct = getattr(realtime_quote, 'change_pct', None)

                # Skip realtime override when data clearly reflects pre-market state:
                # volume is 0/None AND all prices are identical (no actual trading yet).
                is_premarket = (
                    (vol is None or vol == 0)
                    and open_p == price
                    and high_p == price
                    and low_p == price
                )
                if is_premarket:
                    logger.debug(
                        "[%s] Skipping realtime override: pre-market state detected "
                        "(price=%s, vol=%s)",
                        enhanced.get('code', ''),
                        price,
                        vol,
                    )
                else:
                    realtime_today = {
                        'close': price,
                        'open': open_p,
                        'high': high_p,
                        'low': low_p,
                        'ma5': trend_result.ma5,
                        'ma10': trend_result.ma10,
                        'ma20': trend_result.ma20,
                    }
                    if vol is not None:
                        realtime_today['volume'] = vol
                    if amt is not None:
                        realtime_today['amount'] = amt
                    if pct is not None:
                        realtime_today['pct_chg'] = pct
                    for k, v in orig_today.items():
                        if k not in realtime_today and v is not None:
                            realtime_today[k] = v
                    enhanced['today'] = realtime_today
                    enhanced['ma_status'] = self._compute_ma_status(
                        price, trend_result.ma5, trend_result.ma10, trend_result.ma20
                    )
                    enhanced['date'] = get_market_now(
                        get_market_for_stock(normalize_stock_code(enhanced.get('code', '')))
                    ).date().isoformat()
                    if yesterday_close is not None:
                        try:
                            yc = float(yesterday_close)
                            if yc > 0:
                                enhanced['price_change_ratio'] = round(
                                    (price - yc) / yc * 100, 2
                                )
                        except (TypeError, ValueError):
                            pass
                    if vol is not None and enhanced.get('yesterday'):
                        yest_vol = enhanced['yesterday'].get('volume') if isinstance(
                            enhanced['yesterday'], dict
                        ) else None
                        if yest_vol is not None:
                            try:
                                yv = float(yest_vol)
                                if yv > 0:
                                    enhanced['volume_change_ratio'] = round(
                                        float(vol) / yv, 2
                                    )
                            except (TypeError, ValueError):
                                pass

        # ETF/index flag for analyzer prompt (Fixes #274)
        enhanced['is_index_etf'] = SearchService.is_index_or_etf(
            context.get('code', ''), enhanced.get('stock_name', stock_name)
        )

        # P0: append unified fundamental block; keep as additional context only
        enhanced["fundamental_context"] = (
            fundamental_context
            if isinstance(fundamental_context, dict)
            else self.fetcher_manager.build_failed_fundamental_context(
                context.get("code", ""),
                "invalid fundamental context",
            )
        )

        # 60分钟数据注入（Classic 路径单轮增强用）
        try:
            code = context.get("code", "")
            if code:
                minutely = self._fetch_minutely_analysis(code, days=5)
                if minutely.get("status") == "ok":
                    enhanced["minutely_analysis"] = minutely
        except Exception as exc:
            logger.debug("[%s] 60分钟数据注入失败（不影响主流程）: %s", context.get("code", ""), exc)

        return enhanced

    def _attach_belong_boards_to_fundamental_context(
        self,
        code: str,
        fundamental_context: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Attach A-share board membership as a top-level supplemental field.

        Keep this as a shallow copy so cached fundamental contexts are not
        mutated in place after retrieval.
        """
        if isinstance(fundamental_context, dict):
            enriched_context = dict(fundamental_context)
        else:
            enriched_context = self.fetcher_manager.build_failed_fundamental_context(
                code,
                "invalid fundamental context",
            )

        existing_boards = enriched_context.get("belong_boards")
        if isinstance(existing_boards, list):
            enriched_context["belong_boards"] = list(existing_boards)
            return enriched_context

        boards_block = enriched_context.get("boards")
        boards_status = boards_block.get("status") if isinstance(boards_block, dict) else None
        coverage = enriched_context.get("coverage")
        boards_coverage = coverage.get("boards") if isinstance(coverage, dict) else None
        market = enriched_context.get("market")
        if not isinstance(market, str) or not market.strip():
            market = get_market_for_stock(normalize_stock_code(code))

        if (
            market != "cn"
            or boards_status == "not_supported"
            or boards_coverage == "not_supported"
        ):
            enriched_context["belong_boards"] = []
            return enriched_context

        boards: List[Dict[str, Any]] = []
        try:
            raw_boards = self.fetcher_manager.get_belong_boards(code)
            if isinstance(raw_boards, list):
                boards = raw_boards
        except Exception as e:
            logger.debug("%s attach belong_boards failed (fail-open): %s", code, e)

        enriched_context["belong_boards"] = boards
        return enriched_context

    def _ensure_agent_history(self, code: str, min_days: int = 300) -> None:
        """Ensure at least *min_days* of K-line history is in DB for agent tools."""
        from src.services.history_loader import get_frozen_target_date

        target = get_frozen_target_date()
        if target is None:
            target = self._resolve_resume_target_date(code)
        start = target - timedelta(days=int(min_days * 1.8))
        bars = self.db.get_data_range(code, start, target)
        if bars and len(bars) >= min(min_days, 200):
            logger.debug("[%s] Agent history: %d bars in DB, sufficient", code, len(bars))
            return
        try:
            df, source = self.fetcher_manager.get_daily_data(code, days=min_days)
            if df is not None and not df.empty:
                self.db.save_daily_data(df, code, source)
                logger.info("[%s] Prefetched %d rows of history for agent (source: %s)", code, len(df), source)
        except Exception as e:
            logger.warning("[%s] Agent history prefetch failed: %s", code, e)

    def _run_minutely_refinement(
        self,
        executor,
        code: str,
        stock_name: str,
        daily_result: AnalysisResult,
        minutely_data: Dict[str, Any],
        report_language: str,
    ) -> Optional[Dict[str, Any]]:
        """执行60分钟第二轮精修分析（Agent路径）。

        仅聚焦入场时机精修：60分钟VCP确认、RSI超买检查、MA20回踩机会。
        返回 refinement dict，供合并到 AnalysisResult。
        """
        try:
            # 提取日线关键结论用于第二轮上下文
            dashboard = daily_result.dashboard or {}
            core = dashboard.get("core_conclusion", {})
            data_p = dashboard.get("data_perspective", {})
            battle = dashboard.get("battle_plan", {})

            stage = "未知"
            if isinstance(data_p, dict):
                stage = data_p.get("stage", "未知")

            sepa_score = 0
            if isinstance(data_p, dict):
                sepa_score = data_p.get("sepa_score", 0)

            vcp = ""
            if isinstance(data_p, dict):
                vcp = data_p.get("vcp_structure", "")
                if isinstance(vcp, dict):
                    vcp = f"收缩{vcp.get('contractions', '?')}次, 枢轴点{vcp.get('pivot_price', 'N/A')}"

            entry_price = ""
            if isinstance(battle, dict):
                entry_price = battle.get("entry_price", "")
            stop_loss = ""
            if isinstance(battle, dict):
                stop_loss = battle.get("stop_loss", "")

            # 构建60分钟数据表格
            records = minutely_data.get("records", [])[-10:]  # 最近10根
            table_rows = ""
            for r in records:
                table_rows += (
                    f"| {r.get('date', '')} {r.get('time', '')} | "
                    f"{r.get('open', '-')} | {r.get('high', '-')} | "
                    f"{r.get('low', '-')} | {r.get('close', '-')} | "
                    f"{_format_volume(r.get('volume'))} |\n"
                )

            prompt = f"""# 60分钟入场时机精修（第二轮分析）

## 日线分析结论（第一轮）
- 阶段判定：{stage}
- SEPA评分：{sepa_score}/48
- VCP结构：{vcp}
- 建议入场价：{entry_price}
- 建议止损价：{stop_loss}
- 日线操作建议：{daily_result.operation_advice}

## 60分钟关键指标
| 指标 | 数值 | 信号 |
|------|------|------|
| RSI(14) | {minutely_data.get('rsi_14', 'N/A')} | {minutely_data.get('rsi_status', '—')} |
| MA20 | {minutely_data.get('ma20', 'N/A')} | {'价格在上方' if minutely_data.get('above_ma20') else '价格在下方/无数据'} |
| 量趋势 | {minutely_data.get('volume_trend', '—')} | — |
| 最新K线形态 | {minutely_data.get('latest_pattern', '—')} | — |

## 60分钟近期走势（最近10根）
| 时间 | 开盘 | 最高 | 最低 | 收盘 | 成交量 |
|------|------|------|------|------|--------|
{table_rows}

## 你的任务
基于日线结论和60分钟数据， STRICTLY 回答以下问题：
1. 60分钟是否呈现小型VCP或整理结构？（是/否/模糊）
2. 60分钟RSI是否>80（极度超买不宜追）？
3. 当前60分钟位置：突破中/回踩MA20/超买区/无结构
4. 精确入场建议：立即入场 / 等待60分钟回踩MA20 / 放弃
5. 若建议"等待"，触发条件是什么？（价格+时间）

输出严格JSON格式（不要Markdown、不要分析过程、不要表格）：
{{"minutely_structure": "...", "rsi_status": "...", "position": "...", "refined_entry": "...", "trigger_condition": "...", "confidence": "高/中/低"}}
"""

            ctx = {
                "stock_code": code,
                "stock_name": stock_name,
                "report_language": report_language,
                "formatted_data": prompt,
            }
            if report_language == "en":
                msg = f"Refine entry timing for {code} using 60-minute data."
            else:
                msg = f"对股票 {code} 进行60分钟入场时机精修。"

            logger.info("[%s] 启动60分钟第二轮精修分析", code)
            agent_result = executor.run_once(msg, context=ctx)

            # 尝试从 content 解析 JSON
            content = agent_result.content or ""
            import json
            # 先尝试直接解析
            try:
                refinement = json.loads(content)
                if isinstance(refinement, dict):
                    return refinement
            except json.JSONDecodeError:
                pass
            # 尝试提取 ```json 块
            import re
            m = re.search(r"```json\s*(.*?)\s*```", content, re.DOTALL)
            if m:
                try:
                    refinement = json.loads(m.group(1))
                    if isinstance(refinement, dict):
                        return refinement
                except json.JSONDecodeError:
                    pass
            # 兜底：尝试找到最外层 {...}（非贪婪，避免跨多个块误匹配）
            m = re.search(r"\{.*?\}", content, re.DOTALL)
            if m:
                try:
                    refinement = json.loads(m.group(0))
                    if isinstance(refinement, dict):
                        return refinement
                except json.JSONDecodeError:
                    pass

            logger.warning("[%s] 60分钟精修结果JSON解析失败，返回原始文本", code)
            return {"raw_text": content, "parse_error": True}

        except Exception as exc:
            logger.warning("[%s] 60分钟精修分析失败: %s", code, exc)
            return None

    def _analyze_with_agent(
        self,
        code: str,
        report_type: ReportType,
        query_id: str,
        stock_name: str,
        realtime_quote: Any,
        chip_data: Optional[ChipDistribution],
        fundamental_context: Optional[Dict[str, Any]] = None,
        trend_result: Optional[TrendAnalysisResult] = None,
    ) -> Optional[AnalysisResult]:
        """
        使用 Agent 模式分析单只股票。
        """
        try:
            from src.agent.factory import build_agent_executor
            report_language = normalize_report_language(getattr(self.config, "report_language", "zh"))

            # Build executor from shared factory (ToolRegistry and SkillManager prototype are cached)
            executor = build_agent_executor(self.config, getattr(self.config, 'agent_skills', None) or None)

            # --- Single-turn Agent path: prefetch all data, format prompt, call once ---

            # Issue #1066: ensure deep history is in DB BEFORE building context.
            # Must run before get_analysis_context so that MA50/150/200 are
            # populated in the latest row when the prompt is formatted.
            self._ensure_agent_history(code)

            # 1. 获取分析上下文（技术面数据）
            context = self.db.get_analysis_context(code)
            if context is None:
                logger.warning(f"[{code}] 无法获取历史行情数据，将仅基于新闻和实时行情分析")
                _mkt_date = get_market_now(
                    get_market_for_stock(normalize_stock_code(code))
                ).date()
                context = {
                    'code': code,
                    'stock_name': stock_name,
                    'date': _mkt_date.isoformat(),
                    'data_missing': True,
                    'today': {},
                    'yesterday': {}
                }

            # 2. 增强上下文数据（添加实时行情、筹码、趋势分析结果、SEPA 数据等）
            # 含 SEPA 数据质量门禁，最多 3 次重试
            enhanced_context, failure_result = self._enhance_context_with_sepa_quality_gate(
                context,
                realtime_quote,
                chip_data,
                trend_result,
                stock_name,
                fundamental_context,
            )
            if failure_result is not None:
                failure_result.query_id = query_id
                return failure_result

            # 3. 获取新闻情报（预取，避免 Agent 工具重复搜索）
            news_context = None
            if self.search_service is not None and self.search_service.is_available:
                try:
                    intel_results = self.search_service.search_comprehensive_intel(
                        stock_code=code,
                        stock_name=stock_name,
                        max_searches=5,
                    )
                    if intel_results:
                        news_context = self.search_service.format_intel_report(
                            intel_results, stock_name
                        )
                        total_results = sum(
                            len(r.results) for r in intel_results.values() if r.success
                        )
                        logger.info(f"[{code}] Agent 单次模式: 情报搜索完成，共 {total_results} 条结果")
                        # 保存新闻情报到数据库
                        try:
                            query_context = self._build_query_context(query_id=query_id)
                            for dim_name, response in intel_results.items():
                                if response and response.success and response.results:
                                    self.db.save_news_intel(
                                        code=code,
                                        name=stock_name,
                                        dimension=dim_name,
                                        query=response.query,
                                        response=response,
                                        query_context=query_context,
                                    )
                        except Exception as e:
                            logger.warning(f"[{code}] Agent 单次模式保存新闻情报失败: {e}")
                except Exception as e:
                    logger.warning(f"[{code}] Agent 单次模式新闻搜索失败: {e}")

            # 4. Social sentiment injection (US stocks only)
            if (
                self.social_sentiment_service is not None
                and self.social_sentiment_service.is_available
                and is_us_stock_code(code)
            ):
                try:
                    social_context = self.social_sentiment_service.get_social_context(code)
                    if social_context:
                        if news_context:
                            news_context = news_context + "\n\n" + social_context
                        else:
                            news_context = social_context
                        logger.info(f"[{code}] Agent 单次模式: social sentiment 已注入")
                except Exception as e:
                    logger.warning(f"[{code}] Agent 单次模式 social sentiment 获取失败: {e}")

            # 5. 格式化 prompt 数据
            formatted_data = format_analysis_prompt(
                context=enhanced_context,
                stock_name=stock_name,
                news_context=news_context,
                report_language=report_language,
            )

            # 6. 构建 initial_context for run_once
            initial_context = {
                "stock_code": code,
                "stock_name": stock_name,
                "report_type": report_type.value,
                "report_language": report_language,
                "formatted_data": formatted_data,
            }

            # 7. 单次 LLM 调用
            if report_language == "en":
                message = f"Analyze stock {code} ({stock_name}) and return the full decision dashboard JSON in English."
            else:
                message = f"请分析股票 {code} ({stock_name})，并生成决策仪表盘报告。"
            agent_result = executor.run_once(message, context=initial_context)

            # 转换为 AnalysisResult
            result = self._agent_result_to_analysis_result(
                agent_result,
                code,
                stock_name,
                report_type,
                query_id,
                trend_result=trend_result,
            )
            if result:
                result.query_id = query_id

            # Round 2: 60分钟精修（仅对买入/强烈买入信号启动）
            if result and result.success:
                try:
                    should_refinement = result.operation_advice in ("买入", "强烈买入", "BUY", "STRONG_BUY")
                    if not should_refinement and result.dashboard:
                        # 兼容英文/多种表达
                        core = result.dashboard.get("core_conclusion", {})
                        action = str(core.get("action", result.operation_advice)).lower()
                        should_refinement = action in ("买入", "强烈买入", "buy", "strong_buy", "strong buy")
                    if should_refinement:
                        minutely = self._fetch_minutely_analysis(code, days=5)
                        if minutely.get("status") == "ok":
                            refinement = self._run_minutely_refinement(
                                executor, code, stock_name, result, minutely, report_language
                            )
                            if refinement:
                                result.minutely_refinement = refinement
                                logger.info("[%s] 60分钟精修完成: %s", code, refinement.get("refined_entry", "N/A"))
                        else:
                            logger.info("[%s] 60分钟数据不可用，跳过精修", code)
                except Exception as exc:
                    logger.warning("[%s] 60分钟精修阶段异常（不影响主结果）: %s", code, exc)

            # Agent weak integrity: placeholder fill only, no LLM retry
            if result and getattr(self.config, "report_integrity_enabled", False):
                from src.analyzer import check_content_integrity, apply_placeholder_fill

                pass_integrity, missing = check_content_integrity(result)
                if not pass_integrity:
                    apply_placeholder_fill(result, missing)
                    logger.info(
                        "[LLM完整性] integrity_mode=agent_weak 必填字段缺失 %s，已占位补全",
                        missing,
                    )
            # chip_structure fallback (Issue #589), before save_analysis_history
            if result and chip_data:
                fill_chip_structure_if_needed(result, chip_data)

            # price_position fallback (same as non-agent path Step 7.7)
            if result:
                fill_price_position_if_needed(result, trend_result, realtime_quote)

            resolved_stock_name = result.name if result and result.name else stock_name

            # 保存分析历史记录
            if result and result.success:
                try:
                    initial_context["stock_name"] = resolved_stock_name
                    self.db.save_analysis_history(
                        result=result,
                        query_id=query_id,
                        report_type=report_type.value,
                        news_content=None,
                        context_snapshot=initial_context,
                        save_snapshot=self.save_context_snapshot
                    )
                except Exception as e:
                    logger.warning(f"[{code}] 保存 Agent 分析历史失败: {e}")

            return result

        except Exception as e:
            logger.error(f"[{code}] Agent 分析失败: {e}")
            logger.exception(f"[{code}] Agent 详细错误信息:")
            return None

    def _agent_result_to_analysis_result(
        self,
        agent_result,
        code: str,
        stock_name: str,
        report_type: ReportType,
        query_id: str,
        trend_result: Optional[TrendAnalysisResult] = None,
    ) -> AnalysisResult:
        """
        将 AgentResult 转换为 AnalysisResult。
        """
        report_language = normalize_report_language(getattr(self.config, "report_language", "zh"))
        result = AnalysisResult(
            code=code,
            name=stock_name,
            sentiment_score=50,
            trend_prediction="Unknown" if report_language == "en" else "未知",
            operation_advice="Watch" if report_language == "en" else "观望",
            confidence_level=localize_confidence_level("medium", report_language),
            report_language=report_language,
            success=agent_result.success,
            error_message=agent_result.error or None,
            data_sources=f"agent:{agent_result.provider}",
            model_used=agent_result.model or None,
        )

        if agent_result.success and agent_result.dashboard:
            dash = agent_result.dashboard
            ai_stock_name = str(dash.get("stock_name", "")).strip()
            if ai_stock_name and self._is_placeholder_stock_name(stock_name, code):
                result.name = ai_stock_name

            nested_dashboard = dash.get("dashboard") if isinstance(dash, dict) else None

            raw_score = self._agent_dashboard_value(
                dash,
                nested_dashboard,
                "sentiment_score",
                scalar=True,
            )
            if self._is_agent_field_missing(raw_score, scalar=True):
                fallback_score = self._trend_score_fallback(trend_result)
                if fallback_score is not None:
                    result.sentiment_score = fallback_score
                    self._mark_trend_fallback_source(result)
            else:
                result.sentiment_score = self._safe_int(raw_score, 50)

            raw_trend = self._agent_dashboard_value(
                dash,
                nested_dashboard,
                "trend_prediction",
                scalar=True,
                expect_text=True,
            )
            if self._is_agent_field_missing(raw_trend, scalar=True, expect_text=True):
                trend_label = self._trend_label_fallback(
                    trend_result,
                    report_language,
                )
                if trend_label:
                    result.trend_prediction = trend_label
                    self._mark_trend_fallback_source(result)
            else:
                result.trend_prediction = str(raw_trend)

            raw_advice = self._agent_dashboard_value(
                dash,
                nested_dashboard,
                "operation_advice",
                scalar=True,
                allow_dict=True,
                expect_text=True,
            )
            extracted_advice = ""
            if isinstance(raw_advice, dict):
                # LLM may return {"no_position": "...", "has_position": "..."}
                extracted_advice = self._extract_advice_text_from_dict(raw_advice)
                if extracted_advice:
                    result.operation_advice = localize_operation_advice(
                        extracted_advice,
                        report_language,
                    )
                else:
                    signal_label = self._trend_signal_fallback(
                        trend_result,
                        report_language,
                    )
                    if signal_label:
                        result.operation_advice = signal_label
                        self._mark_trend_fallback_source(result)
            elif not self._is_agent_field_missing(
                raw_advice,
                scalar=True,
                allow_dict=True,
                expect_text=True,
            ):
                result.operation_advice = str(raw_advice) if raw_advice else ("Watch" if report_language == "en" else "观望")
            else:
                signal_label = self._trend_signal_fallback(trend_result, report_language)
                if signal_label:
                    result.operation_advice = signal_label
                    self._mark_trend_fallback_source(result)
            from src.agent.protocols import normalize_decision_signal

            raw_decision = self._agent_dashboard_value(
                dash,
                nested_dashboard,
                "decision_type",
                scalar=True,
                expect_text=True,
            )
            if self._is_agent_field_missing(raw_decision, scalar=True, expect_text=True):
                trend_decision = self._trend_decision_fallback(trend_result)
                decision_from_advice = infer_decision_type_from_advice(
                    result.operation_advice,
                    default="",
                )
                if decision_from_advice:
                    result.decision_type = decision_from_advice
                    if (
                        self._is_agent_field_missing(
                            raw_advice,
                            scalar=True,
                            allow_dict=True,
                            expect_text=True,
                        )
                        and not extracted_advice
                        and trend_decision
                    ):
                        self._mark_trend_fallback_source(result)
                else:
                    result.decision_type = trend_decision or "hold"
                    if trend_decision:
                        self._mark_trend_fallback_source(result)
            else:
                result.decision_type = normalize_decision_signal(raw_decision)
            result.confidence_level = localize_confidence_level(
                self._agent_dashboard_value(dash, nested_dashboard, "confidence_level")
                or result.confidence_level,
                report_language,
            )
            raw_summary = self._agent_dashboard_value(
                dash,
                nested_dashboard,
                "analysis_summary",
                scalar=True,
                expect_text=True,
            )
            if not self._is_agent_field_missing(raw_summary, scalar=True, expect_text=True):
                result.analysis_summary = str(raw_summary)
            else:
                result.analysis_summary = self._summary_fallback_from_result(result, report_language)
            # The AI returns a top-level dict that contains a nested 'dashboard' sub-key
            # with core_conclusion / battle_plan / intelligence.  AnalysisResult's helper
            # methods (get_sniper_points, get_core_conclusion, etc.) expect that inner
            # structure, so we unwrap it here.
            result.dashboard = dash.get("dashboard") or dash
            # Backfill legacy flat fields from dashboard for downstream compatibility
            self._backfill_analysis_fields(result, dash)
        else:
            self._apply_trend_fallback(result, trend_result, report_language)
            if trend_result is not None:
                result.analysis_summary = (
                    result.analysis_summary
                    or self._summary_fallback_from_result(result, report_language)
                )
                self._backfill_agent_dashboard_fields(result, trend_result, report_language)
            if not result.error_message:
                result.error_message = "Agent failed to generate a valid decision dashboard" if report_language == "en" else "Agent 未能生成有效的决策仪表盘"

        return result

    @staticmethod
    def _backfill_analysis_fields(result: AnalysisResult, dash: Dict[str, Any]) -> None:
        """Backfill legacy flat fields from dashboard for downstream compatibility.

        Agent mode stores analysis text inside dashboard.data_perspective as strings,
        but downstream consumers (frontend / markdown generator) expect top-level
        flat fields. This method bridges the two formats without touching the UI.
        """
        inner = dash.get("dashboard") or dash
        if not isinstance(inner, dict):
            return

        dp = inner.get("data_perspective", {})
        if isinstance(dp, dict):
            # trend_analysis: combine trend_status + price_position
            if not result.trend_analysis:
                parts = []
                if dp.get("trend_status"):
                    parts.append(f"趋势状态：{dp['trend_status']}")
                if dp.get("price_position"):
                    parts.append(f"价格位置：{dp['price_position']}")
                result.trend_analysis = "\n".join(parts)
            # volume_analysis
            if not result.volume_analysis and dp.get("volume_analysis"):
                result.volume_analysis = dp["volume_analysis"]
            # technical_analysis: synthetic fallback
            if not result.technical_analysis:
                tech_parts = []
                if dp.get("trend_status"):
                    tech_parts.append(f"趋势：{dp['trend_status']}")
                if dp.get("volume_analysis"):
                    tech_parts.append(f"量能：{dp['volume_analysis']}")
                if dp.get("chip_structure"):
                    tech_parts.append(f"筹码：{dp['chip_structure']}")
                result.technical_analysis = "\n".join(tech_parts)

        intel = inner.get("intelligence", {})
        if isinstance(intel, dict):
            if not result.news_summary and intel.get("latest_news"):
                news = intel["latest_news"]
                if isinstance(news, list) and news:
                    def _fmt_news_item(n):
                        if isinstance(n, dict):
                            return f"- {n.get('title', '') or n.get('content', '')}"
                        if isinstance(n, str):
                            return f"- {n}"
                        return f"- {str(n)}"
                    result.news_summary = "\n".join(
                        _fmt_news_item(n) for n in news[:5]
                    )
                elif isinstance(news, str):
                    result.news_summary = news
            if not result.market_sentiment and intel.get("sentiment_summary"):
                result.market_sentiment = intel["sentiment_summary"]
            if not result.fundamental_analysis and intel.get("earnings_outlook"):
                result.fundamental_analysis = intel["earnings_outlook"]

        core = inner.get("core_conclusion", {})
        if isinstance(core, dict):
            if not result.short_term_outlook and core.get("time_sensitivity"):
                result.short_term_outlook = core["time_sensitivity"]
            if not result.buy_reason and core.get("one_sentence"):
                result.buy_reason = core["one_sentence"]

        battle = inner.get("battle_plan", {})
        if isinstance(battle, dict):
            if not result.risk_warning:
                checklist = battle.get("action_checklist", [])
                risks = [c for c in (checklist or []) if isinstance(c, str) and "❌" in c]
                if risks:
                    result.risk_warning = "\n".join(risks)

    @staticmethod
    def _apply_trend_fallback(
        result: AnalysisResult,
        trend_result: Optional[TrendAnalysisResult],
        report_language: str,
    ) -> None:
        if trend_result is None:
            result.sentiment_score = 50
            result.operation_advice = "Watch" if report_language == "en" else "观望"
            return

        score = getattr(trend_result, "signal_score", None)
        try:
            numeric_score = int(score)
        except (TypeError, ValueError):
            numeric_score = 50
        result.sentiment_score = numeric_score if numeric_score > 0 else 50

        trend_label = StockAnalysisPipeline._trend_label_fallback(trend_result, report_language)
        if trend_label:
            result.trend_prediction = trend_label

        buy_signal = getattr(trend_result, "buy_signal", None)
        signal_label = StockAnalysisPipeline._trend_signal_fallback(
            trend_result,
            report_language,
        )
        if signal_label:
            result.operation_advice = signal_label
        else:
            result.operation_advice = "Watch" if report_language == "en" else "观望"

        from src.agent.protocols import normalize_decision_signal

        signal_name = getattr(buy_signal, "name", "").lower()
        signal_to_decision = {
            "strong_buy": "buy",
            "buy": "buy",
            "hold": "hold",
            "wait": "hold",
            "sell": "sell",
            "strong_sell": "sell",
        }
        result.decision_type = signal_to_decision.get(signal_name, result.decision_type or "hold")
        result.decision_type = normalize_decision_signal(result.decision_type)
        result.data_sources = f"{result.data_sources},trend:fallback" if result.data_sources else "trend:fallback"

    @staticmethod
    def _is_placeholder_stock_name(name: str, code: str) -> bool:
        """Return True when the stock name is missing or placeholder-like."""
        if not name:
            return True
        normalized = str(name).strip()
        if not normalized:
            return True
        if normalized == code:
            return True
        if normalized.startswith("股票"):
            return True
        if "Unknown" in normalized:
            return True
        return False

    @staticmethod
    def _safe_int(value: Any, default: int = 50) -> int:
        """安全地将值转换为整数。"""
        if value is None:
            return default
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            import re
            match = re.search(r'-?\d+', value)
            if match:
                return int(match.group())
        return default
    
    def _describe_volume_ratio(self, volume_ratio: float) -> str:
        """
        量比描述
        
        量比 = 当前成交量 / 过去5日平均成交量
        """
        if volume_ratio < 0.5:
            return "极度萎缩"
        elif volume_ratio < 0.8:
            return "明显萎缩"
        elif volume_ratio < 1.2:
            return "正常"
        elif volume_ratio < 2.0:
            return "温和放量"
        elif volume_ratio < 3.0:
            return "明显放量"
        else:
            return "巨量"

    @staticmethod
    def _compute_ma_status(close: float, ma5: float, ma10: float, ma20: float) -> str:
        """
        Compute MA alignment status from price and MA values.
        Logic mirrors storage._analyze_ma_status (Issue #234).
        """
        close = close or 0
        ma5 = ma5 or 0
        ma10 = ma10 or 0
        ma20 = ma20 or 0
        if close > ma5 > ma10 > ma20 > 0:
            return "多头排列 📈"
        elif close < ma5 < ma10 < ma20 and ma20 > 0:
            return "空头排列 📉"
        elif close > ma5 and ma5 > ma10:
            return "短期向好 🔼"
        elif close < ma5 and ma5 < ma10:
            return "短期走弱 🔽"
        else:
            return "震荡整理 ↔️"

    def _augment_historical_with_realtime(
        self, df: pd.DataFrame, realtime_quote: Any, code: str
    ) -> pd.DataFrame:
        """
        Augment historical OHLCV with today's realtime quote for intraday MA calculation.
        Issue #234: Use realtime price instead of yesterday's close for technical indicators.
        """
        if df is None or df.empty or 'close' not in df.columns:
            return df
        if realtime_quote is None:
            return df
        price = getattr(realtime_quote, 'price', None)
        if price is None or not (isinstance(price, (int, float)) and price > 0):
            return df

        # Optional: skip augmentation on non-trading days (fail-open)
        enable_realtime_tech = getattr(
            self.config, 'enable_realtime_technical_indicators', True
        )
        if not enable_realtime_tech:
            return df
        market = get_market_for_stock(code)
        market_today = get_market_now(market).date()
        if market and not is_market_open(market, market_today):
            return df

        last_val = df['date'].max()
        last_date = (
            last_val.date() if hasattr(last_val, 'date') else
            (last_val if isinstance(last_val, date) else pd.Timestamp(last_val).date())
        )
        yesterday_close = float(df.iloc[-1]['close']) if len(df) > 0 else price
        open_p = getattr(realtime_quote, 'open_price', None) or getattr(
            realtime_quote, 'pre_close', None
        ) or yesterday_close
        high_p = getattr(realtime_quote, 'high', None) or price
        low_p = getattr(realtime_quote, 'low', None) or price
        vol = getattr(realtime_quote, 'volume', None) or 0
        amt = getattr(realtime_quote, 'amount', None)
        pct = getattr(realtime_quote, 'change_pct', None)

        if last_date >= market_today:
            # Update last row with realtime close (copy to avoid mutating caller's df)
            df = df.copy()
            idx = df.index[-1]
            df.loc[idx, 'close'] = price
            if open_p is not None:
                df.loc[idx, 'open'] = open_p
            if high_p is not None:
                df.loc[idx, 'high'] = high_p
            if low_p is not None:
                df.loc[idx, 'low'] = low_p
            if vol:
                df.loc[idx, 'volume'] = vol
            if amt is not None:
                df.loc[idx, 'amount'] = amt
            if pct is not None:
                df.loc[idx, 'pct_chg'] = pct
        else:
            # Append virtual today row
            new_row = {
                'code': code,
                'date': market_today,
                'open': open_p,
                'high': high_p,
                'low': low_p,
                'close': price,
                'volume': vol,
                'amount': amt if amt is not None else 0,
                'pct_chg': pct if pct is not None else 0,
            }
            new_df = pd.DataFrame([new_row])
            df = pd.concat([df, new_df], ignore_index=True)
        return df

    def _build_context_snapshot(
        self,
        enhanced_context: Dict[str, Any],
        news_content: Optional[str],
        realtime_quote: Any,
        chip_data: Optional[ChipDistribution]
    ) -> Dict[str, Any]:
        """
        构建分析上下文快照
        """
        return {
            "enhanced_context": enhanced_context,
            "news_content": news_content,
            "realtime_quote_raw": self._safe_to_dict(realtime_quote),
            "chip_distribution_raw": self._safe_to_dict(chip_data),
        }

    @staticmethod
    def _resolve_resume_target_date(
        code: str, current_time: Optional[datetime] = None
    ) -> date:
        """
        Resolve the trading date used by checkpoint/resume checks.
        """
        market = get_market_for_stock(normalize_stock_code(code))
        return get_effective_trading_date(market, current_time=current_time)

    @staticmethod
    def _safe_to_dict(value: Any) -> Optional[Dict[str, Any]]:
        """
        安全转换为字典
        """
        if value is None:
            return None
        if hasattr(value, "to_dict"):
            try:
                return value.to_dict()
            except Exception:
                return None
        if hasattr(value, "__dict__"):
            try:
                return dict(value.__dict__)
            except Exception:
                return None
        return None

    def _resolve_query_source(self, query_source: Optional[str]) -> str:
        """
        解析请求来源。

        优先级（从高到低）：
        1. 显式传入的 query_source：调用方明确指定时优先使用，便于覆盖推断结果或兼容未来 source_message 来自非 bot 的场景
        2. 存在 source_message 时推断为 "bot"：当前约定为机器人会话上下文
        3. 存在 query_id 时推断为 "web"：Web 触发的请求会带上 query_id
        4. 默认 "system"：定时任务或 CLI 等无上述上下文时

        Args:
            query_source: 调用方显式指定的来源，如 "bot" / "web" / "cli" / "system"

        Returns:
            归一化后的来源标识字符串，如 "bot" / "web" / "cli" / "system"
        """
        if query_source:
            return query_source
        if self.source_message:
            return "bot"
        if self.query_id:
            return "web"
        return "system"

    def _build_query_context(self, query_id: Optional[str] = None) -> Dict[str, str]:
        """
        生成用户查询关联信息
        """
        effective_query_id = query_id or self.query_id or ""

        context: Dict[str, str] = {
            "query_id": effective_query_id,
            "query_source": self.query_source or "",
        }

        if self.source_message:
            context.update({
                "requester_platform": self.source_message.platform or "",
                "requester_user_id": self.source_message.user_id or "",
                "requester_user_name": self.source_message.user_name or "",
                "requester_chat_id": self.source_message.chat_id or "",
                "requester_message_id": self.source_message.message_id or "",
                "requester_query": self.source_message.content or "",
            })

        return context
    
    def process_single_stock(
        self,
        code: str,
        skip_analysis: bool = False,
        single_stock_notify: bool = False,
        report_type: ReportType = ReportType.SIMPLE,
        analysis_query_id: Optional[str] = None,
        current_time: Optional[datetime] = None,
    ) -> Optional[AnalysisResult]:
        """
        处理单只股票的完整流程

        包括：
        1. 获取数据
        2. 保存数据
        3. AI 分析
        4. 单股推送（可选，#55）

        此方法会被线程池调用，需要处理好异常

        Args:
            analysis_query_id: 查询链路关联 id
            code: 股票代码
            skip_analysis: 是否跳过 AI 分析
            single_stock_notify: 是否启用单股推送模式（每分析完一只立即推送）
            report_type: 报告类型枚举（从配置读取，Issue #119）
            current_time: 本轮运行冻结的参考时间，用于统一断点续传目标交易日判断

        Returns:
            AnalysisResult 或 None
        """
        logger.info(f"========== 开始处理 {code} ==========")

        from src.services.history_loader import set_frozen_target_date, reset_frozen_target_date
        frozen_td = self._resolve_resume_target_date(code, current_time=current_time)
        token = set_frozen_target_date(frozen_td)
        try:
            self._emit_progress(12, f"{code}：正在准备分析任务")
            # Step 1: 获取并保存数据
            success, error = self.fetch_and_save_stock_data(
                code, current_time=current_time
            )
            
            if not success:
                logger.warning(f"[{code}] 数据获取失败: {error}")
                # 即使获取失败，也尝试用已有数据分析
            else:
                self._emit_progress(16, f"{code}：行情数据准备完成")
            
            # Step 2: AI 分析
            if skip_analysis:
                logger.info(f"[{code}] 跳过 AI 分析（dry-run 模式）")
                return None
            
            effective_query_id = analysis_query_id or self.query_id or uuid.uuid4().hex
            result = self.analyze_stock(code, report_type, query_id=effective_query_id)
            
            if result and result.success:
                logger.info(
                    f"[{code}] 分析完成: {result.operation_advice}, "
                    f"评分 {result.sentiment_score}"
                )
                
                # 单股推送模式（#55）：每分析完一只股票立即推送
                if single_stock_notify:
                    self._send_single_stock_notification(
                        result,
                        report_type=report_type,
                        fallback_code=code,
                    )
            elif result:
                logger.warning(
                    f"[{code}] 分析未成功: {result.error_message or '未知错误'}"
                )
            
            return result
            
        except Exception as e:
            # 捕获所有异常，确保单股失败不影响整体
            logger.exception(f"[{code}] 处理过程发生未知异常: {e}")
            return None
        finally:
            reset_frozen_target_date(token)
    
    def run(
        self,
        stock_codes: Optional[List[str]] = None,
        dry_run: bool = False,
        send_notification: bool = True,
        merge_notification: bool = False
    ) -> List[AnalysisResult]:
        """
        运行完整的分析流程

        流程：
        1. 获取待分析的股票列表
        2. 使用线程池并发处理
        3. 收集分析结果
        4. 发送通知

        Args:
            stock_codes: 股票代码列表（可选，默认使用配置中的自选股）
            dry_run: 是否仅获取数据不分析
            send_notification: 是否发送推送通知
            merge_notification: 是否合并推送（跳过本次推送，由 main 层合并个股+大盘后统一发送，Issue #190）

        Returns:
            分析结果列表
        """
        start_time = time.time()
        
        # 使用配置中的股票列表
        if stock_codes is None:
            self.config.refresh_stock_list()
            stock_codes = self.config.stock_list
        
        if not stock_codes:
            logger.error("未配置自选股列表，请在 .env 文件中设置 STOCK_LIST")
            return []
        
        logger.info(f"===== 开始分析 {len(stock_codes)} 只股票 =====")
        logger.info(f"股票列表: {', '.join(stock_codes)}")
        logger.info(f"并发数: {self.max_workers}, 模式: {'仅获取数据' if dry_run else '完整分析'}")

        # 冻结本轮运行的统一参考时间，避免跨市场收盘边界时同批股票使用不同目标交易日。
        resume_reference_time = datetime.now(timezone.utc)
        
        # === 批量预取实时行情（优化：避免每只股票都触发全量拉取）===
        # 只有股票数量 >= 5 时才进行预取，少量股票直接逐个查询更高效
        if len(stock_codes) >= 5:
            prefetch_count = self.fetcher_manager.prefetch_realtime_quotes(stock_codes)
            if prefetch_count > 0:
                logger.info(f"已启用批量预取架构：一次拉取全市场数据，{len(stock_codes)} 只股票共享缓存")

        # Issue #455: 预取股票名称，避免并发分析时显示「股票xxxxx」
        # dry_run 仅做数据拉取，不需要名称预取，避免额外网络开销
        if not dry_run:
            self.fetcher_manager.prefetch_stock_names(stock_codes, use_bulk=False)

        # 单股推送模式（#55）：从配置读取
        single_stock_notify = getattr(self.config, 'single_stock_notify', False)
        # Issue #119: 从配置读取报告类型
        report_type_str = getattr(self.config, 'report_type', 'simple').lower()
        if report_type_str == 'brief':
            report_type = ReportType.BRIEF
        elif report_type_str == 'full':
            report_type = ReportType.FULL
        else:
            report_type = ReportType.SIMPLE
        # Issue #128: 从配置读取分析间隔
        analysis_delay = getattr(self.config, 'analysis_delay', 0)

        if single_stock_notify:
            logger.info(
                "已启用单股推送模式：分析仍并发执行，通知改为在结果收集侧串行发送（报告类型: %s）",
                report_type_str,
            )
        
        results: List[AnalysisResult] = []
        
        # 使用线程池并发处理
        # 注意：max_workers 设置较低（默认3）以避免触发反爬
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # 提交任务
            future_to_code = {
                executor.submit(
                    self.process_single_stock,
                    code,
                    skip_analysis=dry_run,
                    single_stock_notify=False,
                    report_type=report_type,  # Issue #119: 传递报告类型
                    analysis_query_id=uuid.uuid4().hex,
                    current_time=resume_reference_time,
                ): code
                for code in stock_codes
            }
            
            # 收集结果
            for idx, future in enumerate(as_completed(future_to_code)):
                code = future_to_code[future]
                try:
                    result = future.result()
                    if result and result.success:
                        results.append(result)
                        if single_stock_notify and send_notification and not dry_run:
                            self._send_single_stock_notification(
                                result,
                                report_type=report_type,
                                fallback_code=code,
                            )
                    elif result and not result.success:
                        logger.warning(
                            f"[{code}] 分析结果标记为失败，不计入汇总: "
                            f"{result.error_message or '未知原因'}"
                        )

                    # Issue #128: 分析间隔 - 在个股分析和大盘分析之间添加延迟
                    if idx < len(stock_codes) - 1 and analysis_delay > 0:
                        # 注意：此 sleep 发生在“主线程收集 future 的循环”中，
                        # 并不会阻止线程池中的任务同时发起网络请求。
                        # 因此它对降低并发请求峰值的效果有限；真正的峰值主要由 max_workers 决定。
                        # 该行为目前保留（按需求不改逻辑）。
                        logger.debug(f"等待 {analysis_delay} 秒后继续下一只股票...")
                        time.sleep(analysis_delay)

                except Exception as e:
                    logger.error(f"[{code}] 任务执行失败: {e}")
        
        # 统计
        elapsed_time = time.time() - start_time
        
        # dry-run 模式下，数据获取成功即视为成功
        if dry_run:
            # 检查哪些股票的最新可复用交易日数据已存在
            success_count = sum(
                1
                for code in stock_codes
                if self.db.has_today_data(
                    code,
                    self._resolve_resume_target_date(
                        code, current_time=resume_reference_time
                    ),
                )
            )
            fail_count = len(stock_codes) - success_count
        else:
            success_count = len(results)
            fail_count = len(stock_codes) - success_count
        
        logger.info("===== 分析完成 =====")
        logger.info(f"成功: {success_count}, 失败: {fail_count}, 耗时: {elapsed_time:.2f} 秒")
        
        # 保存报告到本地文件（无论是否推送通知都保存）
        if results and not dry_run:
            self._save_local_report(results, report_type)

        # 发送通知（单股推送模式下跳过汇总推送，避免重复）
        if results and send_notification and not dry_run:
            if single_stock_notify:
                # 单股推送模式：只保存汇总报告，不再重复推送
                logger.info("单股推送模式：跳过汇总推送，仅保存报告到本地")
                self._send_notifications(results, report_type, skip_push=True)
            elif merge_notification:
                # 合并模式（Issue #190）：仅保存，不推送，由 main 层合并个股+大盘后统一发送
                logger.info("合并推送模式：跳过本次推送，将在个股+大盘复盘后统一发送")
                self._send_notifications(results, report_type, skip_push=True)
            else:
                self._send_notifications(results, report_type)
        
        return results

    def _send_single_stock_notification(
        self,
        result: AnalysisResult,
        report_type: ReportType = ReportType.SIMPLE,
        fallback_code: Optional[str] = None,
    ) -> None:
        """发送单股通知，供直接单股入口和批量串行推送共用。"""
        if not self.notifier.is_available():
            return

        stock_code = getattr(result, "code", None) or fallback_code or "unknown"
        notify_lock = getattr(self, "_single_stock_notify_lock", None)
        if notify_lock is None:
            with _SINGLE_STOCK_NOTIFY_LOCK_INIT_GUARD:
                notify_lock = getattr(self, "_single_stock_notify_lock", None)
                if notify_lock is None:
                    notify_lock = threading.Lock()
                    setattr(self, "_single_stock_notify_lock", notify_lock)

        with notify_lock:
            try:
                if report_type == ReportType.FULL:
                    report_content = self.notifier.generate_dashboard_report([result])
                    logger.info(f"[{stock_code}] 使用完整报告格式")
                elif report_type == ReportType.BRIEF:
                    report_content = self.notifier.generate_brief_report([result])
                    logger.info(f"[{stock_code}] 使用简洁报告格式")
                else:
                    report_content = self.notifier.generate_single_stock_report(result)
                    logger.info(f"[{stock_code}] 使用精简报告格式")

                if self.notifier.send(report_content, email_stock_codes=[stock_code]):
                    logger.info(f"[{stock_code}] 单股推送成功")
                else:
                    logger.warning(f"[{stock_code}] 单股推送失败")
            except Exception as e:
                logger.error(f"[{stock_code}] 单股推送异常: {e}")

    def _save_local_report(
        self,
        results: List[AnalysisResult],
        report_type: ReportType = ReportType.SIMPLE,
    ) -> None:
        """保存分析报告到本地文件（与通知推送解耦）"""
        try:
            report = self._generate_aggregate_report(results, report_type)
            filepath = self.notifier.save_report_to_file(report)
            logger.info(f"决策仪表盘日报已保存: {filepath}")
        except Exception as e:
            logger.error(f"保存本地报告失败: {e}")

    def _send_notifications(
        self,
        results: List[AnalysisResult],
        report_type: ReportType = ReportType.SIMPLE,
        skip_push: bool = False,
    ) -> None:
        """
        发送分析结果通知
        
        生成决策仪表盘格式的报告
        
        Args:
            results: 分析结果列表
            skip_push: 是否跳过推送（仅保存到本地，用于单股推送模式）
        """
        try:
            logger.info("生成决策仪表盘日报...")
            report = self._generate_aggregate_report(results, report_type)
            
            # 跳过推送（单股推送模式 / 合并模式：报告已由 _save_local_report 保存）
            if skip_push:
                return
            
            # 推送通知
            if self.notifier.is_available():
                channels = self.notifier.get_available_channels()
                context_success = self.notifier.send_to_context(report)

                # Issue #455: Markdown 转图片（与 notification.send 逻辑一致）
                from src.md2img import markdown_to_image

                channels_needing_image = {
                    ch for ch in channels
                    if ch.value in self.notifier._markdown_to_image_channels
                }
                non_wechat_channels_needing_image = {
                    ch for ch in channels_needing_image if ch != NotificationChannel.WECHAT
                }

                def _get_md2img_hint() -> str:
                    try:
                        engine = getattr(get_config(), "md2img_engine", "wkhtmltoimage")
                    except Exception:
                        engine = "wkhtmltoimage"
                    return (
                        "npm i -g markdown-to-file" if engine == "markdown-to-file"
                        else "wkhtmltopdf (apt install wkhtmltopdf / brew install wkhtmltopdf)"
                    )

                image_bytes = None
                if non_wechat_channels_needing_image:
                    image_bytes = markdown_to_image(
                        report, max_chars=self.notifier._markdown_to_image_max_chars
                    )
                    if image_bytes:
                        logger.info(
                            "Markdown 已转换为图片，将向 %s 发送图片",
                            [ch.value for ch in non_wechat_channels_needing_image],
                        )
                    else:
                        logger.warning(
                            "Markdown 转图片失败，将回退为文本发送。请检查 MARKDOWN_TO_IMAGE_CHANNELS 配置并安装 %s",
                            _get_md2img_hint(),
                        )

                # 企业微信：只发精简版（平台限制）
                wechat_success = False
                if NotificationChannel.WECHAT in channels:
                    if report_type == ReportType.BRIEF:
                        dashboard_content = self.notifier.generate_brief_report(results)
                    else:
                        dashboard_content = self.notifier.generate_wechat_dashboard(results)
                    logger.info(f"企业微信仪表盘长度: {len(dashboard_content)} 字符")
                    logger.debug(f"企业微信推送内容:\n{dashboard_content}")
                    wechat_image_bytes = None
                    if NotificationChannel.WECHAT in channels_needing_image:
                        wechat_image_bytes = markdown_to_image(
                            dashboard_content,
                            max_chars=self.notifier._markdown_to_image_max_chars,
                        )
                        if wechat_image_bytes is None:
                            logger.warning(
                                "企业微信 Markdown 转图片失败，将回退为文本发送。请检查 MARKDOWN_TO_IMAGE_CHANNELS 配置并安装 %s",
                                _get_md2img_hint(),
                            )
                    use_image = self.notifier._should_use_image_for_channel(
                        NotificationChannel.WECHAT, wechat_image_bytes
                    )
                    if use_image:
                        wechat_success = self.notifier._send_wechat_image(wechat_image_bytes)
                    else:
                        wechat_success = self.notifier.send_to_wechat(dashboard_content)

                # 其他渠道：发完整报告（避免自定义 Webhook 被 wechat 截断逻辑污染）
                non_wechat_success = False
                stock_email_groups = getattr(self.config, 'stock_email_groups', []) or []
                for channel in channels:
                    if channel == NotificationChannel.WECHAT:
                        continue
                    if channel == NotificationChannel.FEISHU:
                        non_wechat_success = self.notifier.send_to_feishu(report) or non_wechat_success
                    elif channel == NotificationChannel.TELEGRAM:
                        use_image = self.notifier._should_use_image_for_channel(
                            channel, image_bytes
                        )
                        if use_image:
                            result = self.notifier._send_telegram_photo(image_bytes)
                        else:
                            result = self.notifier.send_to_telegram(report)
                        non_wechat_success = result or non_wechat_success
                    elif channel == NotificationChannel.EMAIL:
                        if stock_email_groups:
                            code_to_emails: Dict[str, Optional[List[str]]] = {}
                            for r in results:
                                if r.code not in code_to_emails:
                                    canonical = normalize_stock_code(r.code)
                                    emails = []
                                    for stocks, emails_list in stock_email_groups:
                                        if canonical in stocks:
                                            emails.extend(emails_list)
                                    code_to_emails[r.code] = list(dict.fromkeys(emails)) if emails else None
                            emails_to_results: Dict[Optional[Tuple], List] = defaultdict(list)
                            for r in results:
                                recs = code_to_emails.get(r.code)
                                key = tuple(recs) if recs else None
                                emails_to_results[key].append(r)
                            for key, group_results in emails_to_results.items():
                                grp_report = self._generate_aggregate_report(group_results, report_type)
                                grp_image_bytes = None
                                if channel.value in self.notifier._markdown_to_image_channels:
                                    grp_image_bytes = markdown_to_image(
                                        grp_report,
                                        max_chars=self.notifier._markdown_to_image_max_chars,
                                    )
                                use_image = self.notifier._should_use_image_for_channel(
                                    channel, grp_image_bytes
                                )
                                receivers = list(key) if key is not None else None
                                if use_image:
                                    result = self.notifier._send_email_with_inline_image(
                                        grp_image_bytes, receivers=receivers
                                    )
                                else:
                                    result = self.notifier.send_to_email(
                                        grp_report, receivers=receivers
                                    )
                                non_wechat_success = result or non_wechat_success
                        else:
                            use_image = self.notifier._should_use_image_for_channel(
                                channel, image_bytes
                            )
                            if use_image:
                                result = self.notifier._send_email_with_inline_image(image_bytes)
                            else:
                                result = self.notifier.send_to_email(report)
                            non_wechat_success = result or non_wechat_success
                    elif channel == NotificationChannel.CUSTOM:
                        use_image = self.notifier._should_use_image_for_channel(
                            channel, image_bytes
                        )
                        if use_image:
                            result = self.notifier._send_custom_webhook_image(
                                image_bytes, fallback_content=report
                            )
                        else:
                            result = self.notifier.send_to_custom(report)
                        non_wechat_success = result or non_wechat_success
                    elif channel == NotificationChannel.PUSHPLUS:
                        non_wechat_success = self.notifier.send_to_pushplus(report) or non_wechat_success
                    elif channel == NotificationChannel.SERVERCHAN3:
                        non_wechat_success = self.notifier.send_to_serverchan3(report) or non_wechat_success
                    elif channel == NotificationChannel.DISCORD:
                        non_wechat_success = self.notifier.send_to_discord(report) or non_wechat_success
                    elif channel == NotificationChannel.PUSHOVER:
                        non_wechat_success = self.notifier.send_to_pushover(report) or non_wechat_success
                    elif channel == NotificationChannel.ASTRBOT:
                        non_wechat_success = self.notifier.send_to_astrbot(report) or non_wechat_success
                    elif channel == NotificationChannel.SLACK:
                        use_image = self.notifier._should_use_image_for_channel(
                            channel, image_bytes
                        )
                        if use_image and self.notifier._slack_bot_token and self.notifier._slack_channel_id:
                            result = self.notifier._send_slack_image(
                                image_bytes, fallback_content=report
                            )
                        else:
                            result = self.notifier.send_to_slack(report)
                        non_wechat_success = result or non_wechat_success
                    else:
                        logger.warning(f"未知通知渠道: {channel}")

                success = wechat_success or non_wechat_success or context_success
                if success:
                    logger.info("决策仪表盘推送成功")
                else:
                    logger.warning("决策仪表盘推送失败")
            else:
                logger.info("通知渠道未配置，跳过推送")
                
        except Exception as e:
            import traceback
            logger.error(f"发送通知失败: {e}\n{traceback.format_exc()}")

    def _generate_aggregate_report(
        self,
        results: List[AnalysisResult],
        report_type: ReportType,
    ) -> str:
        """Generate aggregate report with backward-compatible notifier fallback."""
        generator = getattr(self.notifier, "generate_aggregate_report", None)
        if callable(generator):
            return generator(results, report_type)
        if report_type == ReportType.BRIEF and hasattr(self.notifier, "generate_brief_report"):
            return self.notifier.generate_brief_report(results)
        return self.notifier.generate_dashboard_report(results)
