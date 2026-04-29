# -*- coding: utf-8 -*-
"""
===================================
市场全景数据访问层
===================================

职责：
1. 封装 market_indices / market_boards / zt_pool / strong_stocks / lhb_* 表操作
2. 提供幂等写入（SQLite UPSERT）、缓存检查、查询接口
3. 支持数据回填场景下的缺失日期检测

源自 feat-0427-spiderdata2db，合并到主项目，适配 SQLite + SQLAlchemy。
"""

import logging
import math
from datetime import date, timedelta
from typing import Optional, List, Dict, Any, Set

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.storage import (
    DatabaseManager,
    LHBBasic,
    LHBStockDetail,
    LHBStockStatistic,
    LHBYybCapital,
    LHBYybMost,
    MarketBoard,
    MarketIndexData,
    StrongStock,
    ZTPoolStock,
)

logger = logging.getLogger(__name__)


class MarketDataRepository:
    """
    市场全景数据访问层

    覆盖：指数、板块、涨跌停池、强势股、龙虎榜基本信息+详情+统计+营业部排行
    """

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or DatabaseManager.get_instance()

    # ============================================================
    # 通用辅助
    # ============================================================

    @staticmethod
    def _model_to_dict(obj: Any) -> Dict[str, Any]:
        if hasattr(obj, "to_dict"):
            return obj.to_dict()
        return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}

    @staticmethod
    def _clean_record(record: Dict[str, Any]) -> Dict[str, Any]:
        """清理 DataFrame 导出的 NaN / 空值，兼容 SQLite 写入。"""
        cleaned: Dict[str, Any] = {}
        for k, v in record.items():
            if v is None:
                cleaned[k] = None
            elif isinstance(v, float):
                if math.isnan(v) or math.isinf(v):
                    cleaned[k] = None
                else:
                    cleaned[k] = round(v, 4)
            else:
                cleaned[k] = v
        return cleaned

    def _bulk_upsert(self, model_cls: Any, records: List[Dict[str, Any]], unique_cols: List[str]) -> int:
        """
        SQLite UPSERT：插入或更新。返回成功条数。

        Args:
            model_cls: SQLAlchemy ORM 类
            records: 字典列表
            unique_cols: 用于冲突检测的列名列表（对应表上的 UNIQUE 约束）
        """
        if not records:
            return 0

        cleaned = [self._clean_record(r) for r in records]
        # 过滤掉主键 id，让数据库自增
        for r in cleaned:
            r.pop("id", None)

        table = model_cls.__table__
        # 构造 update 字典：排除 unique_cols 和 id
        update_cols = [c.name for c in table.columns if c.name not in unique_cols and c.name != "id"]
        update_dict = {c: sqlite_insert(table).excluded[c] for c in update_cols}

        with self.db.get_session() as session:
            stmt = sqlite_insert(table).values(cleaned)
            stmt = stmt.on_conflict_do_update(index_elements=unique_cols, set_=update_dict)
            session.execute(stmt)
            session.commit()
            return len(cleaned)

    # ============================================================
    # 1. 市场指数
    # ============================================================

    def save_indices(self, indices: List[Dict[str, Any]]) -> int:
        return self._bulk_upsert(MarketIndexData, indices, ["trade_date", "index_code"])

    def has_indices(self, trade_date: date) -> bool:
        with self.db.get_session() as session:
            row = session.execute(
                select(MarketIndexData)
                .where(MarketIndexData.trade_date == trade_date)
                .limit(1)
            ).scalar_one_or_none()
            return row is not None

    def get_indices_by_date(self, trade_date: date) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            rows = session.execute(
                select(MarketIndexData).where(MarketIndexData.trade_date == trade_date)
            ).scalars().all()
            return [self._model_to_dict(r) for r in rows]

    def get_index_history(self, index_code: str, start_date: date, end_date: date) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            rows = session.execute(
                select(MarketIndexData)
                .where(
                    and_(
                        MarketIndexData.index_code == index_code,
                        MarketIndexData.trade_date >= start_date,
                        MarketIndexData.trade_date <= end_date,
                    )
                )
                .order_by(MarketIndexData.trade_date)
            ).scalars().all()
            return [self._model_to_dict(r) for r in rows]

    # ============================================================
    # 2. 板块数据
    # ============================================================

    def save_boards(self, boards: List[Dict[str, Any]]) -> int:
        return self._bulk_upsert(MarketBoard, boards, ["trade_date", "board_type", "board_name", "source"])

    def has_boards(self, trade_date: date, source: Optional[str] = None) -> bool:
        with self.db.get_session() as session:
            q = select(MarketBoard).where(MarketBoard.trade_date == trade_date)
            if source:
                q = q.where(MarketBoard.source == source)
            row = session.execute(q.limit(1)).scalar_one_or_none()
            return row is not None

    def get_boards_by_date(
        self, trade_date: date, board_type: Optional[str] = None, source: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            q = select(MarketBoard).where(MarketBoard.trade_date == trade_date)
            if board_type:
                q = q.where(MarketBoard.board_type == board_type)
            if source:
                q = q.where(MarketBoard.source == source)
            rows = session.execute(q).scalars().all()
            return [self._model_to_dict(r) for r in rows]

    # ============================================================
    # 3. 涨跌停/炸板池
    # ============================================================

    def save_zt_pool(self, stocks: List[Dict[str, Any]]) -> int:
        return self._bulk_upsert(ZTPoolStock, stocks, ["trade_date", "pool_type", "stock_code"])

    def has_zt_pool(self, trade_date: date, pool_type: Optional[str] = None) -> bool:
        with self.db.get_session() as session:
            q = select(ZTPoolStock).where(ZTPoolStock.trade_date == trade_date)
            if pool_type:
                q = q.where(ZTPoolStock.pool_type == pool_type)
            row = session.execute(q.limit(1)).scalar_one_or_none()
            return row is not None

    def get_zt_pool_by_date(
        self, trade_date: date, pool_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            q = select(ZTPoolStock).where(ZTPoolStock.trade_date == trade_date)
            if pool_type:
                q = q.where(ZTPoolStock.pool_type == pool_type)
            rows = session.execute(q).scalars().all()
            return [self._model_to_dict(r) for r in rows]

    # ============================================================
    # 4. 强势股
    # ============================================================

    def save_strong_stocks(self, stocks: List[Dict[str, Any]]) -> int:
        return self._bulk_upsert(StrongStock, stocks, ["trade_date", "stock_code"])

    def has_strong_stocks(self, trade_date: date) -> bool:
        with self.db.get_session() as session:
            row = session.execute(
                select(StrongStock)
                .where(StrongStock.trade_date == trade_date)
                .limit(1)
            ).scalar_one_or_none()
            return row is not None

    def get_strong_stocks_by_date(self, trade_date: date) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            rows = session.execute(
                select(StrongStock).where(StrongStock.trade_date == trade_date)
            ).scalars().all()
            return [self._model_to_dict(r) for r in rows]

    # ============================================================
    # 5. 龙虎榜 — 基本信息
    # ============================================================

    def save_lhb_basic(self, basics: List[Dict[str, Any]]) -> List[int]:
        """
        保存龙虎榜基本信息，返回插入/更新的主键 id 列表。
        注意：由于 unique 约束包含 lhb_reason，同一只股票同一天可能有多条记录。
        """
        if not basics:
            return []
        # 先单独写入，再按 trade_date + stock_code + lhb_reason 查回 id
        self._bulk_upsert(LHBBasic, basics, ["trade_date", "stock_code", "lhb_reason"])
        # 查回 id
        trade_date = basics[0].get("trade_date")
        codes = {b["stock_code"] for b in basics if b.get("stock_code")}
        with self.db.get_session() as session:
            rows = session.execute(
                select(LHBBasic.id)
                .where(
                    and_(
                        LHBBasic.trade_date == trade_date,
                        LHBBasic.stock_code.in_(list(codes)),
                    )
                )
            ).scalars().all()
            return list(rows)

    def has_lhb_basic(self, trade_date: date) -> bool:
        with self.db.get_session() as session:
            row = session.execute(
                select(LHBBasic).where(LHBBasic.trade_date == trade_date).limit(1)
            ).scalar_one_or_none()
            return row is not None

    def get_lhb_basic_by_date(self, trade_date: date) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            rows = session.execute(
                select(LHBBasic).where(LHBBasic.trade_date == trade_date)
            ).scalars().all()
            return [self._model_to_dict(r) for r in rows]

    def get_lhb_basic_ids(self, trade_date: date, stock_code: str) -> List[int]:
        """获取某股票某日的所有 lhb_basic 记录 id（可能因多条上榜原因有多条）。"""
        with self.db.get_session() as session:
            rows = session.execute(
                select(LHBBasic.id).where(
                    and_(LHBBasic.trade_date == trade_date, LHBBasic.stock_code == stock_code)
                )
            ).scalars().all()
            return list(rows)

    # ============================================================
    # 6. 龙虎榜 — 个股席位明细
    # ============================================================

    def save_lhb_details(self, details: List[Dict[str, Any]]) -> int:
        return self._bulk_upsert(LHBStockDetail, details, ["lhb_basic_id", "stock_code", "seat_name"])

    def get_lhb_details_by_basic_id(self, lhb_basic_id: int) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            rows = session.execute(
                select(LHBStockDetail).where(LHBStockDetail.lhb_basic_id == lhb_basic_id)
            ).scalars().all()
            return [self._model_to_dict(r) for r in rows]

    def get_lhb_details_by_date(self, trade_date: date) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            rows = session.execute(
                select(LHBStockDetail).where(LHBStockDetail.trade_date == trade_date)
            ).scalars().all()
            return [self._model_to_dict(r) for r in rows]

    # ============================================================
    # 7. 龙虎榜 — 个股上榜统计缓存
    # ============================================================

    def save_lhb_statistic(self, stats: List[Dict[str, Any]]) -> int:
        return self._bulk_upsert(LHBStockStatistic, stats, ["trade_date", "stock_code"])

    def has_lhb_statistic(self, trade_date: date) -> bool:
        with self.db.get_session() as session:
            row = session.execute(
                select(LHBStockStatistic)
                .where(LHBStockStatistic.trade_date == trade_date)
                .limit(1)
            ).scalar_one_or_none()
            return row is not None

    def get_lhb_statistic_by_stock(self, stock_code: str, limit: int = 10) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            rows = session.execute(
                select(LHBStockStatistic)
                .where(LHBStockStatistic.stock_code == stock_code)
                .order_by(LHBStockStatistic.trade_date.desc())
                .limit(limit)
            ).scalars().all()
            return [self._model_to_dict(r) for r in rows]

    def get_stock_statistic_last_update(self, stock_code: str) -> Optional[date]:
        """获取个股上榜统计的最后更新日期。"""
        with self.db.get_session() as session:
            row = session.execute(
                select(func.max(LHBStockStatistic.trade_date))
                .where(LHBStockStatistic.stock_code == stock_code)
            ).scalar_one_or_none()
            return row

    # ============================================================
    # 8. 营业部排行 — 上榜次数最多
    # ============================================================

    def save_lhb_yyb_most(self, ranks: List[Dict[str, Any]]) -> int:
        return self._bulk_upsert(LHBYybMost, ranks, ["fetch_date", "seat_name"])

    def has_lhb_yyb(self, fetch_date: date) -> bool:
        """两个营业部表一起检查。"""
        with self.db.get_session() as session:
            row1 = session.execute(
                select(LHBYybMost).where(LHBYybMost.fetch_date == fetch_date).limit(1)
            ).scalar_one_or_none()
            return row1 is not None

    def get_lhb_yyb_most_by_date(self, fetch_date: date) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            rows = session.execute(
                select(LHBYybMost).where(LHBYybMost.fetch_date == fetch_date)
            ).scalars().all()
            return [self._model_to_dict(r) for r in rows]

    # ============================================================
    # 9. 营业部排行 — 资金实力最强
    # ============================================================

    def save_lhb_yyb_capital(self, ranks: List[Dict[str, Any]]) -> int:
        return self._bulk_upsert(LHBYybCapital, ranks, ["fetch_date", "seat_name"])

    def get_lhb_yyb_capital_by_date(self, fetch_date: date) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            rows = session.execute(
                select(LHBYybCapital).where(LHBYybCapital.fetch_date == fetch_date)
            ).scalars().all()
            return [self._model_to_dict(r) for r in rows]

    # ============================================================
    # 10. 缺失日期检测（数据回填用）
    # ============================================================

    def get_missing_dates(
        self,
        model_cls: Any,
        date_col: str,
        start_date: date,
        end_date: date,
        trade_dates: Optional[List[date]] = None,
        extra_filter: Optional[Any] = None,
    ) -> List[date]:
        """
        获取指定模型在日期范围内缺失的交易日列表。

        Args:
            model_cls: ORM 类
            date_col: 日期列名（通常为 'trade_date' 或 'fetch_date'）
            start_date, end_date: 日期范围
            trade_dates: 预设的交易日列表（如有）。为 None 时反推已有数据日期。
            extra_filter: 额外的 SQLAlchemy where 条件

        Returns:
            缺失的日期列表，按从早到晚排序
        """
        col = getattr(model_cls, date_col)

        with self.db.get_session() as session:
            if trade_dates is not None:
                # 有预设交易日列表：检查哪些不在库中
                existing_q = select(col).where(
                    and_(col >= start_date, col <= end_date)
                )
                if extra_filter is not None:
                    existing_q = existing_q.where(extra_filter)
                existing = {
                    r for r in session.execute(existing_q).scalars().all() if r is not None
                }
                return sorted([d for d in trade_dates if start_date <= d <= end_date and d not in existing])
            else:
                # 无预设列表：只返回范围端点提示（需调用方提供 trade_dates）
                logger.warning(
                    "get_missing_dates: trade_dates 为 None，无法精确判断缺失日期，"
                    "请传入交易日列表或历史指数日线反推。"
                )
                return []

    def get_distinct_stock_codes(
        self,
        model_classes: List[Any],
        start_date: date,
        end_date: date,
    ) -> Set[str]:
        """
        从多个表中获取指定日期范围内去重后的股票代码列表。
        """
        codes: Set[str] = set()
        for cls in model_classes:
            if not hasattr(cls, "stock_code"):
                continue
            with self.db.get_session() as session:
                rows = session.execute(
                    select(cls.stock_code)
                    .where(
                        and_(
                            cls.trade_date >= start_date,
                            cls.trade_date <= end_date,
                        )
                    )
                    .distinct()
                ).scalars().all()
                codes.update(r for r in rows if r)
        return codes
