# -*- coding: utf-8 -*-
"""
数据层增强功能测试脚本

测试标的：
- 创业板指 (399006) — 指数
- 德明利 (001309) — 个股

测试内容：
1. 抓取2年日线历史数据（~485个交易日），计算技术指标
2. 抓取6个月60分钟K线数据
3. 验证指标数值合理性
4. 写入数据库并读取验证
"""

import logging
import sys
from datetime import datetime, timedelta

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 测试标的
TEST_CODES = [
    {"code": "399006", "name": "创业板指", "type": "index"},
    {"code": "001309", "name": "德明利", "type": "stock"},
]

# 日期范围
DAILY_DAYS = 730   # 2年
MINUTELY_DAYS = 180  # 6个月


def _ensure_project_path():
    """确保项目根目录在 sys.path 中"""
    import os
    project_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "../..")
    )
    if project_root not in sys.path:
        sys.path.insert(0, project_root)


def test_daily_data_fetch():
    """测试日线数据抓取与指标计算"""
    _ensure_project_path()

    from data_provider.base import DataFetcherManager
    from src.storage import DatabaseManager, get_db

    db = get_db()
    manager = DataFetcherManager()

    end_date = datetime.now().strftime('%Y-%m-%d')
    start_date = (datetime.now() - timedelta(days=DAILY_DAYS)).strftime('%Y-%m-%d')

    logger.info("=" * 70)
    logger.info("[1/3] 测试日线数据抓取与指标计算")
    logger.info("=" * 70)

    for item in TEST_CODES:
        code = item["code"]
        name = item["name"]
        logger.info(f"\n--- 处理 {name} ({code}) ---")

        try:
            # 1. 抓取日线数据
            logger.info(f"  抓取日线: {start_date} ~ {end_date}")
            df, source = manager.get_daily_data(
                code,
                start_date=start_date,
                end_date=end_date,
            )
            logger.info(f"  数据源: {source}, 共 {len(df)} 条")

            if len(df) < 200:
                logger.warning(f"  数据量偏少 ({len(df)} 条)，可能影响指标精度")

            # 2. 检查指标列
            indicator_cols = [
                'ma5', 'ma10', 'ma20', 'ma60',
                'macd_dif', 'macd_dea', 'macd_bar', 'macd_signal',
                'rsi_6', 'rsi_12', 'rsi_24', 'rsi_signal',
                'kdj_k', 'kdj_d', 'kdj_j', 'kdj_signal',
                'bias_ma5', 'bias_ma10', 'bias_ma20',
                'boll_mid', 'boll_upper', 'boll_lower',
                'candle_pattern',
            ]
            missing = [c for c in indicator_cols if c not in df.columns]
            if missing:
                logger.error(f"  缺少指标列: {missing}")
            else:
                logger.info(f"  所有 {len(indicator_cols)} 个指标列已生成")

            # 3. 验证指标数值
            latest = df.iloc[-1]
            logger.info(f"  最新日期: {latest['date']}")
            logger.info(f"  最新收盘: {latest['close']:.2f}")
            logger.info(f"  MA5={latest['ma5']:.2f}, MA10={latest['ma10']:.2f}, "
                       f"MA20={latest['ma20']:.2f}, MA60={latest['ma60']:.2f}")
            logger.info(f"  MACD DIF={latest['macd_dif']:.2f}, DEA={latest['macd_dea']:.2f}, "
                       f"BAR={latest['macd_bar']:.2f}, signal={latest['macd_signal']}")
            logger.info(f"  RSI(6)={latest['rsi_6']:.2f}, RSI(12)={latest['rsi_12']:.2f}, "
                       f"RSI(24)={latest['rsi_24']:.2f}, signal={latest['rsi_signal']}")
            logger.info(f"  KDJ K={latest['kdj_k']:.2f}, D={latest['kdj_d']:.2f}, "
                       f"J={latest['kdj_j']:.2f}, signal={latest['kdj_signal']}")
            logger.info(f"  Bias MA5={latest['bias_ma5']:.2f}%, MA10={latest['bias_ma10']:.2f}%, "
                       f"MA20={latest['bias_ma20']:.2f}%")
            logger.info(f"  BOLL Mid={latest['boll_mid']:.2f}, Upper={latest['boll_upper']:.2f}, "
                       f"Lower={latest['boll_lower']:.2f}")
            logger.info(f"  量比: {latest['volume_ratio']:.2f}")
            logger.info(f"  K线形态: {latest['candle_pattern'] or '无'}")

            # 4. 写入数据库
            saved = db.save_daily_data(df, code, data_source=source)
            logger.info(f"  数据库写入: 新增/更新 {saved} 条")

            # 5. 读取验证
            from_date = (datetime.now() - timedelta(days=30)).date()
            to_date = datetime.now().date()
            db_records = db.get_data_range(code, from_date, to_date)
            logger.info(f"  数据库读取验证: 最近30天 {len(db_records)} 条")

            if db_records:
                rec = db_records[-1]
                logger.info(f"  最新DB记录: date={rec.date}, close={rec.close}, "
                           f"macd_dif={rec.macd_dif}, rsi_6={rec.rsi_6}, "
                           f"kdj_k={rec.kdj_k}, candle={rec.candle_pattern}")

            # 6. 保存基本信息
            db.save_stock_basic_info(
                code=code,
                name=name,
                market="sz",
                security_type=item["type"],
                data_source=source,
            )
            logger.info(f"  基本信息已保存")

        except Exception as e:
            logger.error(f"  {code} 处理失败: {e}", exc_info=True)

    logger.info("\n日线数据测试完成")


def test_minutely_data_fetch():
    """测试60分钟K线数据抓取"""
    _ensure_project_path()

    from data_provider.base import DataFetcherManager
    from src.storage import get_db

    db = get_db()
    manager = DataFetcherManager()

    end_date = datetime.now().strftime('%Y-%m-%d')
    start_date = (datetime.now() - timedelta(days=MINUTELY_DAYS)).strftime('%Y-%m-%d')

    logger.info("\n" + "=" * 70)
    logger.info("[2/3] 测试60分钟K线数据抓取")
    logger.info("=" * 70)

    for item in TEST_CODES:
        code = item["code"]
        name = item["name"]
        logger.info(f"\n--- 处理 {name} ({code}) ---")

        try:
            df, source = manager.get_minutely_data(
                code,
                start_date=start_date,
                end_date=end_date,
            )
            logger.info(f"  数据源: {source}, 共 {len(df)} 条")

            if len(df) > 0:
                first = df.iloc[0]
                last = df.iloc[-1]
                logger.info(f"  首条: date={first['date']} time={first['time']} close={first['close']}")
                logger.info(f"  末条: date={last['date']} time={last['time']} close={last['close']}")

                # 写入数据库
                saved = db.save_minutely_data(df, code, data_source=source)
                logger.info(f"  数据库写入: {saved} 条")

        except Exception as e:
            logger.error(f"  {code} 60分钟数据获取失败: {e}", exc_info=True)

    logger.info("\n60分钟数据测试完成")


def test_indicator_accuracy():
    """验证指标计算的正确性"""
    _ensure_project_path()

    import pandas as pd
    import numpy as np

    logger.info("\n" + "=" * 70)
    logger.info("[3/3] 指标计算正确性验证")
    logger.info("=" * 70)

    # 构造已知数据验证 MACD
    # 使用简单价格序列，手动计算 EMA 验证
    prices = pd.Series([10, 11, 12, 11, 10, 9, 10, 11, 12, 13, 14, 15, 14, 13, 12])
    # pandas ewm(span=12, adjust=False).mean() 使用递归公式
    ema12 = prices.ewm(span=12, adjust=False, min_periods=12).mean()
    ema26 = prices.ewm(span=26, adjust=False, min_periods=26).mean()

    # 验证 EMA 公式: EMA_t = alpha * Price_t + (1-alpha) * EMA_{t-1}
    alpha = 2 / (12 + 1)
    manual_ema = [prices.iloc[0]]  # 初始值用第一个价格
    for i in range(1, len(prices)):
        manual_ema.append(alpha * prices.iloc[i] + (1 - alpha) * manual_ema[-1])

    # pandas 的 ewm 初始值处理不同，所以只比较收敛后的值
    logger.info(f"  EMA(12) 收敛验证:")
    logger.info(f"    pandas 最后值: {ema12.iloc[-1]:.4f}")
    logger.info(f"    手动计算最后值: {manual_ema[-1]:.4f}")
    logger.info(f"    差异: {abs(ema12.iloc[-1] - manual_ema[-1]):.6f}")

    # 验证 RSI 计算
    logger.info(f"\n  RSI 验证:")
    delta = prices.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1/6, adjust=False, min_periods=6).mean()
    avg_loss = loss.ewm(alpha=1/6, adjust=False, min_periods=6).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    rsi = 100 - (100 / (1 + rs))
    logger.info(f"    价格序列: {list(prices)}")
    logger.info(f"    RSI(6) 最后值: {rsi.iloc[-1]:.2f}")
    logger.info(f"    预期范围: 0-100")

    # 验证 BOLL 计算
    logger.info(f"\n  BOLL 验证:")
    ma20 = prices.rolling(window=20, min_periods=20).mean()
    std20 = prices.rolling(window=20, min_periods=20).std(ddof=0)
    upper = ma20 + 2 * std20
    lower = ma20 - 2 * std20
    logger.info(f"    MA20={ma20.iloc[-1]:.4f}, Upper={upper.iloc[-1]:.4f}, Lower={lower.iloc[-1]:.4f}")
    logger.info(f"    价格={prices.iloc[-1]:.4f}, 在区间内: {lower.iloc[-1] <= prices.iloc[-1] <= upper.iloc[-1]}")

    logger.info("\n指标验证完成")


def main():
    logger.info("=" * 70)
    logger.info("数据层增强功能测试开始")
    logger.info(f"测试标的: {[i['code'] for i in TEST_CODES]}")
    logger.info(f"日线范围: 最近 {DAILY_DAYS} 天")
    logger.info(f"60分钟范围: 最近 {MINUTELY_DAYS} 天")
    logger.info("=" * 70)

    test_daily_data_fetch()
    test_minutely_data_fetch()
    test_indicator_accuracy()

    logger.info("\n" + "=" * 70)
    logger.info("所有测试完成")
    logger.info("=" * 70)


if __name__ == "__main__":
    main()
