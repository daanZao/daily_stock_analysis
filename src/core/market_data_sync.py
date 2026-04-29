# -*- coding: utf-8 -*-
"""
===================================
收盘市场数据同步模块
===================================

职责：
1. 在每日收盘作业（closing operations）阶段抓取并保存市场全景数据
2. 复用现有 DataFetcherManager 的接口，避免重复实现网络抓取
3. 失败时不阻断主分析流程，仅记录警告日志

数据覆盖：
- Phase 1（当前）：主要指数行情、板块涨跌榜
- Phase 2（预留）：涨跌停池、强势股、龙虎榜（需 akshare 专属接口）
"""

import logging
from datetime import date
from typing import Any, Dict, List, Optional

from src.config import Config
from src.repositories.market_data_repo import MarketDataRepository
from src.storage import DatabaseManager

logger = logging.getLogger(__name__)


class MarketDataSync:
    """
    收盘市场数据同步器

    在 ``run_full_analysis`` 的个股分析完成后调用，
    将当日市场全景数据持久化到 SQLite，供后续复盘、回测、Web 展示使用。
    """

    def __init__(
        self,
        fetcher_manager: Any,
        db_manager: Optional[DatabaseManager] = None,
        config: Optional[Config] = None,
    ):
        self.fetcher = fetcher_manager
        self.repo = MarketDataRepository(db_manager)
        self.config = config

    def sync_all(self, trade_date: Optional[date] = None) -> Dict[str, int]:
        """
        执行全量市场数据同步。

        Args:
            trade_date: 交易日期，默认最近交易日（自动跳过周末/节假日）

        Returns:
            各表写入条数字典，例如 {"indices": 5, "boards": 10}
        """
        if trade_date is None:
            from src.core.trading_calendar import get_effective_trading_date
            trade_date = get_effective_trading_date("cn")

        stats: Dict[str, int] = {}

        # 1. 主要指数
        try:
            stats["indices"] = self._sync_indices(trade_date)
        except Exception as e:
            logger.warning(f"[MarketDataSync] 指数同步失败（已忽略）: {e}")
            stats["indices"] = 0

        # 2. 板块排行
        try:
            stats["boards"] = self._sync_boards(trade_date)
        except Exception as e:
            logger.warning(f"[MarketDataSync] 板块同步失败（已忽略）: {e}")
            stats["boards"] = 0

        # 3. 涨跌停池（Phase 2：需 akshare 专属接口）
        try:
            stats["zt_pool"] = self._sync_zt_pool(trade_date)
        except Exception as e:
            logger.warning(f"[MarketDataSync] 涨跌停池同步失败（已忽略）: {e}")
            stats["zt_pool"] = 0

        # 4. 强势股（Phase 2）
        try:
            stats["strong_stocks"] = self._sync_strong_stocks(trade_date)
        except Exception as e:
            logger.warning(f"[MarketDataSync] 强势股同步失败（已忽略）: {e}")
            stats["strong_stocks"] = 0

        # 5. 龙虎榜（Phase 2）
        try:
            stats["lhb"] = self._sync_lhb(trade_date)
        except Exception as e:
            logger.warning(f"[MarketDataSync] 龙虎榜同步失败（已忽略）: {e}")
            stats["lhb"] = 0

        total = sum(v for v in stats.values() if v is not None)
        logger.info(
            f"[MarketDataSync] {trade_date} 同步完成: indices={stats.get('indices', 0)}, "
            f"boards={stats.get('boards', 0)}, zt_pool={stats.get('zt_pool', 0)}, "
            f"strong={stats.get('strong_stocks', 0)}, lhb={stats.get('lhb', 0)} (total={total})"
        )
        return stats

    # ============================================================
    # Phase 1：基于现有 DataFetcherManager 接口
    # ============================================================

    def _sync_indices(self, trade_date: date) -> int:
        """同步主要指数行情到 market_indices 表。"""
        if self.repo.has_indices(trade_date):
            logger.debug(f"[MarketDataSync] {trade_date} 指数数据已存在，跳过")
            return 0

        data = self.fetcher.get_main_indices(region="cn")
        if not data:
            logger.warning("[MarketDataSync] 未获取到指数行情")
            return 0

        records: List[Dict[str, Any]] = []
        for item in data:
            records.append(
                {
                    "trade_date": trade_date,
                    "index_code": item.get("code"),
                    "index_name": item.get("name"),
                    "latest_price": item.get("current"),
                    "change_amount": item.get("change"),
                    "change_percent": item.get("change_pct"),
                    "volume": item.get("volume"),
                    "amount": item.get("amount"),
                    "amplitude": item.get("amplitude"),
                    "high": item.get("high"),
                    "low": item.get("low"),
                    "open": item.get("open"),
                    "pre_close": item.get("prev_close"),
                }
            )

        return self.repo.save_indices(records)

    def _sync_boards(self, trade_date: date) -> int:
        """同步板块涨跌榜到 market_boards 表。"""
        if self.repo.has_boards(trade_date, source="em"):
            logger.debug(f"[MarketDataSync] {trade_date} 板块数据已存在，跳过")
            return 0

        top, bottom = self.fetcher.get_sector_rankings(n=20)
        if not top and not bottom:
            logger.warning("[MarketDataSync] 未获取到板块排行")
            return 0

        records: List[Dict[str, Any]] = []
        for item in top:
            records.append(
                {
                    "trade_date": trade_date,
                    "board_type": "industry",
                    "board_name": item.get("name"),
                    "change_percent": item.get("change_pct"),
                    "source": "em",
                }
            )
        for item in bottom:
            records.append(
                {
                    "trade_date": trade_date,
                    "board_type": "industry",
                    "board_name": item.get("name"),
                    "change_percent": item.get("change_pct"),
                    "source": "em",
                }
            )

        return self.repo.save_boards(records)

    # ============================================================
    # Phase 2：预留（需 akshare 专属接口或额外开发）
    # ============================================================

    def _sync_zt_pool(self, trade_date: date) -> int:
        """
        同步涨跌停/炸板池到 zt_pool 表。

        TODO: 需接入 akshare 的涨停股池接口（如 ak.stock_zt_pool_em）
        当前返回 0 作为占位。
        """
        # Placeholder for Phase 2 implementation
        return 0

    def _sync_strong_stocks(self, trade_date: date) -> int:
        """
        同步强势股到 strong_stocks 表。

        TODO: 需接入 akshare 的强势股接口（如 ak.stock_rank_lxsz_em）
        当前返回 0 作为占位。
        """
        # Placeholder for Phase 2 implementation
        return 0

    def _sync_lhb(self, trade_date: date) -> int:
        """
        同步龙虎榜数据到 lhb_* 表。

        TODO: 需接入 akshare 的龙虎榜接口（如 ak.stock_lhb_detail_em）
        当前返回 0 作为占位。
        """
        # Placeholder for Phase 2 implementation
        return 0
