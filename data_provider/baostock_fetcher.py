# -*- coding: utf-8 -*-
"""
===================================
BaostockFetcher - 备用数据源 2 (Priority 3)
===================================

数据来源：证券宝（Baostock）
特点：免费、无需 Token、需要登录管理
优点：稳定、无配额限制

关键策略：
1. 管理 bs.login() 和 bs.logout() 生命周期
2. 使用上下文管理器防止连接泄露
3. 失败后指数退避重试
"""

import logging
import os
import re
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Optional, Generator, Dict, Any, List

import pandas as pd
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

from .base import BaseFetcher, DataFetchError, STANDARD_COLUMNS, is_bse_code, _is_hk_market
import os

logger = logging.getLogger(__name__)


def _is_us_code(stock_code: str) -> bool:
    """
    判断代码是否为美股
    
    美股代码规则：
    - 1-5个大写字母，如 'AAPL', 'TSLA'
    - 可能包含 '.'，如 'BRK.B'
    """
    code = stock_code.strip().upper()
    return bool(re.match(r'^[A-Z]{1,5}(\.[A-Z])?$', code))


class BaostockFetcher(BaseFetcher):
    """
    Baostock 数据源实现
    
    优先级：3
    数据来源：证券宝 Baostock API
    
    关键策略：
    - 使用上下文管理器管理连接生命周期
    - 每次请求都重新登录/登出，防止连接泄露
    - 失败后指数退避重试
    
    Baostock 特点：
    - 免费、无需注册
    - 需要显式登录/登出
    - 数据更新略有延迟（T+1）
    """
    
    name = "BaostockFetcher"
    priority = int(os.getenv("BAOSTOCK_PRIORITY", "3"))
    
    def __init__(self):
        """初始化 BaostockFetcher"""
        self._bs_module = None
    
    def _get_baostock(self):
        """
        延迟加载 baostock 模块
        
        只在首次使用时导入，避免未安装时报错
        """
        if self._bs_module is None:
            import baostock as bs
            self._bs_module = bs
        return self._bs_module
    
    @contextmanager
    def _baostock_session(self) -> Generator:
        """
        Baostock 连接上下文管理器
        
        确保：
        1. 进入上下文时自动登录
        2. 退出上下文时自动登出
        3. 异常时也能正确登出
        
        使用示例：
            with self._baostock_session():
                # 在这里执行数据查询
        """
        bs = self._get_baostock()
        login_result = None
        
        try:
            # 登录 Baostock
            login_result = bs.login()
            
            if login_result.error_code != '0':
                raise DataFetchError(f"Baostock 登录失败: {login_result.error_msg}")
            
            logger.debug("Baostock 登录成功")
            
            yield bs
            
        finally:
            # 确保登出，防止连接泄露
            try:
                logout_result = bs.logout()
                if logout_result.error_code == '0':
                    logger.debug("Baostock 登出成功")
                else:
                    logger.warning(f"Baostock 登出异常: {logout_result.error_msg}")
            except Exception as e:
                logger.warning(f"Baostock 登出时发生错误: {e}")
    
    def _convert_stock_code(self, stock_code: str) -> str:
        """
        转换股票代码为 Baostock 格式
        
        Baostock 要求的格式：
        - 沪市：sh.600519
        - 深市：sz.000001
        
        Args:
            stock_code: 原始代码，如 '600519', '000001'
            
        Returns:
            Baostock 格式代码，如 'sh.600519', 'sz.000001'
        """
        code = stock_code.strip()

        # HK stocks are not supported by Baostock
        if _is_hk_market(code):
            raise DataFetchError(f"BaostockFetcher 不支持港股 {code}，请使用 AkshareFetcher")

        # 已经包含前缀的情况
        if code.startswith(('sh.', 'sz.')):
            return code.lower()
        
        # 去除可能的后缀
        code = code.replace('.SH', '').replace('.SZ', '').replace('.sh', '').replace('.sz', '')
        
        # ETF: Shanghai ETF (51xx, 52xx, 56xx, 58xx) -> sh; Shenzhen ETF (15xx, 16xx, 18xx) -> sz
        if len(code) == 6:
            if code.startswith(('51', '52', '56', '58')):
                return f"sh.{code}"
            if code.startswith(('15', '16', '18')):
                return f"sz.{code}"

        # 根据代码前缀判断市场
        if code.startswith(('600', '601', '603', '688')):
            return f"sh.{code}"
        elif code.startswith(('000', '002', '300')):
            return f"sz.{code}"
        else:
            logger.warning(f"无法确定股票 {code} 的市场，默认使用深市")
            return f"sz.{code}"
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type((ConnectionError, TimeoutError)),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )
    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """
        从 Baostock 获取原始数据
        
        使用 query_history_k_data_plus() 获取日线数据
        
        流程：
        1. 检查是否为美股（不支持）
        2. 使用上下文管理器管理连接
        3. 转换股票代码格式
        4. 调用 API 查询数据
        5. 将结果转换为 DataFrame
        """
        # 美股不支持，抛出异常让 DataFetcherManager 切换到其他数据源
        if _is_us_code(stock_code):
            raise DataFetchError(f"BaostockFetcher 不支持美股 {stock_code}，请使用 AkshareFetcher 或 YfinanceFetcher")

        # 港股不支持，抛出异常让 DataFetcherManager 切换到其他数据源
        if _is_hk_market(stock_code):
            raise DataFetchError(f"BaostockFetcher 不支持港股 {stock_code}，请使用 AkshareFetcher")

        # 北交所不支持，抛出异常让 DataFetcherManager 切换到其他数据源
        if is_bse_code(stock_code):
            raise DataFetchError(
                f"BaostockFetcher 不支持北交所 {stock_code}，将自动切换其他数据源"
            )
        
        # 转换代码格式
        bs_code = self._convert_stock_code(stock_code)
        
        logger.debug(f"调用 Baostock query_history_k_data_plus({bs_code}, {start_date}, {end_date})")
        
        with self._baostock_session() as bs:
            try:
                # 查询日线数据
                # adjustflag: 1-后复权，2-前复权，3-不复权
                rs = bs.query_history_k_data_plus(
                    code=bs_code,
                    fields="date,open,high,low,close,volume,amount,pctChg",
                    start_date=start_date,
                    end_date=end_date,
                    frequency="d",  # 日线
                    adjustflag="2"  # 前复权
                )
                
                if rs.error_code != '0':
                    raise DataFetchError(f"Baostock 查询失败: {rs.error_msg}")
                
                # 转换为 DataFrame
                data_list = []
                while rs.next():
                    data_list.append(rs.get_row_data())
                
                if not data_list:
                    raise DataFetchError(f"Baostock 未查询到 {stock_code} 的数据")
                
                df = pd.DataFrame(data_list, columns=rs.fields)
                
                return df
                
            except Exception as e:
                if isinstance(e, DataFetchError):
                    raise
                raise DataFetchError(f"Baostock 获取数据失败: {e}") from e
    
    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        """
        标准化 Baostock 数据
        
        Baostock 返回的列名：
        date, open, high, low, close, volume, amount, pctChg
        
        需要映射到标准列名：
        date, open, high, low, close, volume, amount, pct_chg
        """
        df = df.copy()
        
        # 列名映射（只需要处理 pctChg）
        column_mapping = {
            'pctChg': 'pct_chg',
        }
        
        df = df.rename(columns=column_mapping)
        
        # 数值类型转换（Baostock 返回的都是字符串）
        numeric_cols = ['open', 'high', 'low', 'close', 'volume', 'amount', 'pct_chg']
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')
        
        # 添加股票代码列
        df['code'] = stock_code
        
        # 只保留需要的列
        keep_cols = ['code'] + STANDARD_COLUMNS
        existing_cols = [col for col in keep_cols if col in df.columns]
        df = df[existing_cols]
        
        return df

    def get_stock_name(self, stock_code: str) -> Optional[str]:
        """
        获取股票名称
        
        使用 Baostock 的 query_stock_basic 接口获取股票基本信息
        
        Args:
            stock_code: 股票代码
            
        Returns:
            股票名称，失败返回 None
        """
        # 检查缓存
        if hasattr(self, '_stock_name_cache') and stock_code in self._stock_name_cache:
            return self._stock_name_cache[stock_code]
        
        # 初始化缓存
        if not hasattr(self, '_stock_name_cache'):
            self._stock_name_cache = {}
        
        try:
            bs_code = self._convert_stock_code(stock_code)
            
            with self._baostock_session() as bs:
                # 查询股票基本信息
                rs = bs.query_stock_basic(code=bs_code)
                
                if rs.error_code == '0':
                    data_list = []
                    while rs.next():
                        data_list.append(rs.get_row_data())
                    
                    if data_list:
                        # Baostock 返回的字段：code, code_name, ipoDate, outDate, type, status
                        fields = rs.fields
                        name_idx = fields.index('code_name') if 'code_name' in fields else None
                        if name_idx is not None and len(data_list[0]) > name_idx:
                            name = data_list[0][name_idx]
                            self._stock_name_cache[stock_code] = name
                            logger.debug(f"Baostock 获取股票名称成功: {stock_code} -> {name}")
                            return name
                
        except Exception as e:
            logger.warning(f"Baostock 获取股票名称失败 {stock_code}: {e}")
        
        return None
    
    def get_stock_list(self) -> Optional[pd.DataFrame]:
        """
        获取股票列表

        使用 Baostock 的 query_stock_basic 接口获取全部股票列表

        Returns:
            包含 code, name 列的 DataFrame，失败返回 None
        """
        try:
            with self._baostock_session() as bs:
                # 查询所有股票基本信息
                rs = bs.query_stock_basic()

                if rs.error_code == '0':
                    data_list = []
                    while rs.next():
                        data_list.append(rs.get_row_data())

                    if data_list:
                        df = pd.DataFrame(data_list, columns=rs.fields)

                        # 转换代码格式（去除 sh. 或 sz. 前缀）
                        df['code'] = df['code'].apply(lambda x: x.split('.')[1] if '.' in x else x)
                        df = df.rename(columns={'code_name': 'name'})

                        # 更新缓存
                        if not hasattr(self, '_stock_name_cache'):
                            self._stock_name_cache = {}
                        for _, row in df.iterrows():
                            self._stock_name_cache[row['code']] = row['name']

                        logger.info(f"Baostock 获取股票列表成功: {len(df)} 条")
                        return df[['code', 'name']]

        except Exception as e:
            logger.warning(f"Baostock 获取股票列表失败: {e}")

        return None

    def get_all_securities(self, day: Optional[str] = None) -> Optional[pd.DataFrame]:
        """
        获取某交易日的全部证券列表（含股票、指数、基金等）

        使用 Baostock 的 query_all_stock 接口。
        返回的 DataFrame 包含以下列：
            - code:        证券代码（原始格式，如 sh.600000 / sz.000001）
            - code_name:   证券名称
            - ipo_date:    上市日期
            - out_date:    退市日期（未退市为空）
            - type:        证券类型（1=股票, 2=指数, 3=其它）
            - status:      上市状态（1=上市, 0=退市）

        Args:
            day: 查询日期，格式 "YYYY-MM-DD"，默认最近交易日

        Returns:
            全市场证券 DataFrame，失败返回 None
        """
        if day is None:
            # 默认取最近一个交易日（简单处理：如果今天是周末，取周五）
            today = date.today()
            weekday = today.weekday()
            if weekday >= 5:  # 周六或周日
                offset = weekday - 4
                day = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
            else:
                day = today.strftime("%Y-%m-%d")

        try:
            with self._baostock_session() as bs:
                rs = bs.query_all_stock(day=day)

                if rs.error_code != '0':
                    logger.warning(f"Baostock query_all_stock 失败: {rs.error_msg}")
                    return None

                data_list = []
                while rs.next():
                    data_list.append(rs.get_row_data())

                if not data_list:
                    logger.warning(f"Baostock query_all_stock({day}) 返回空数据")
                    return None

                df = pd.DataFrame(data_list, columns=rs.fields)

                # 统一列名（snake_case）
                rename_map = {
                    'code': 'code',
                    'code_name': 'code_name',
                    'ipoDate': 'ipo_date',
                    'outDate': 'out_date',
                    'type': 'type',
                    'status': 'status',
                }
                df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})

                # 添加可读映射
                type_map = {'1': 'stock', '2': 'index', '3': 'other'}
                status_map = {'1': 'listed', '0': 'delisted'}
                if 'type' in df.columns:
                    df['type_name'] = df['type'].astype(str).map(type_map).fillna('unknown')
                if 'status' in df.columns:
                    df['status_name'] = df['status'].astype(str).map(status_map).fillna('unknown')

                # 提取纯数字代码（方便下游使用）
                df['pure_code'] = df['code'].apply(lambda x: x.split('.')[1] if isinstance(x, str) and '.' in x else x)

                # 按类型分组计数日志
                if 'type_name' in df.columns:
                    counts = df['type_name'].value_counts().to_dict()
                    logger.info(f"Baostock 获取 {day} 全市场证券: {len(df)} 条, 分类={counts}")
                else:
                    logger.info(f"Baostock 获取 {day} 全市场证券: {len(df)} 条")

                return df

        except Exception as e:
            logger.warning(f"Baostock 获取全市场证券失败: {e}")
            return None

    def get_all_securities_csv(self, day: Optional[str] = None, output_path: Optional[str] = None) -> Optional[str]:
        """
        获取某日全市场证券并保存为 CSV

        Args:
            day: 查询日期，格式 "YYYY-MM-DD"，默认最近交易日
            output_path: CSV 保存路径，默认 ./data/all_securities_YYYY-MM-DD.csv

        Returns:
            保存的 CSV 文件路径，失败返回 None
        """
        df = self.get_all_securities(day=day)
        if df is None or df.empty:
            return None

        if day is None:
            day = date.today().strftime("%Y-%m-%d")

        if output_path is None:
            data_dir = os.path.join(os.getcwd(), "data")
            os.makedirs(data_dir, exist_ok=True)
            output_path = os.path.join(data_dir, f"all_securities_{day}.csv")

        try:
            df.to_csv(output_path, index=False, encoding='utf-8-sig')
            logger.info(f"全市场证券 CSV 已保存: {output_path} ({len(df)} 条)")
            return output_path
        except Exception as e:
            logger.warning(f"保存 CSV 失败: {e}")
            return None

    def _fetch_raw_minutely_data(
        self,
        stock_code: str,
        start_date: str,
        end_date: str,
        frequency: str = "60",
    ) -> pd.DataFrame:
        """
        从 Baostock 获取60分钟K线原始数据

        Args:
            stock_code: 股票代码
            start_date: 开始日期 YYYY-MM-DD
            end_date: 结束日期 YYYY-MM-DD
            frequency: 频率，默认 "60" 表示60分钟

        Returns:
            原始数据 DataFrame
        """
        if _is_us_code(stock_code):
            raise DataFetchError(f"BaostockFetcher 不支持美股 {stock_code}")
        if _is_hk_market(stock_code):
            raise DataFetchError(f"BaostockFetcher 不支持港股 {stock_code}")
        if is_bse_code(stock_code):
            raise DataFetchError(f"BaostockFetcher 不支持北交所 {stock_code}")

        bs_code = self._convert_stock_code(stock_code)
        logger.debug(
            f"调用 Baostock query_history_k_data_plus({bs_code}, {start_date}, {end_date}, freq={frequency})"
        )

        with self._baostock_session() as bs:
            rs = bs.query_history_k_data_plus(
                code=bs_code,
                fields="date,time,open,high,low,close,volume,amount",
                start_date=start_date,
                end_date=end_date,
                frequency=frequency,
                adjustflag="2",  # 前复权
            )

            if rs.error_code != '0':
                raise DataFetchError(f"Baostock 60分钟查询失败: {rs.error_msg}")

            data_list = []
            while rs.next():
                data_list.append(rs.get_row_data())

            if not data_list:
                raise DataFetchError(f"Baostock 未查询到 {stock_code} 的60分钟数据")

            df = pd.DataFrame(data_list, columns=rs.fields)
            return df

    def _normalize_minutely_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        """
        标准化60分钟K线数据

        Baostock time 格式: YYYYMMDDHHMMSSsss
        拆分为 date (YYYY-MM-DD) 和 time (HHMMSS)
        """
        df = df.copy()

        # Baostock 返回的都是字符串，需要转换
        numeric_cols = ['open', 'high', 'low', 'close', 'volume', 'amount']
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        # 解析 time 字段: YYYYMMDDHHMMSSsss -> date + time
        if 'time' in df.columns:
            df['time'] = df['time'].astype(str)
            # 取前8位作为日期，第9-14位作为时间
            df['date'] = df['time'].str[:8]
            df['time'] = df['time'].str[8:14]
            df['date'] = pd.to_datetime(df['date'], format='%Y%m%d').dt.strftime('%Y-%m-%d')

        # 计算涨跌幅 (若 Baostock 未返回)
        if 'pct_chg' not in df.columns:
            df['pct_chg'] = df['close'].pct_change() * 100

        df['code'] = stock_code

        keep_cols = ['code', 'date', 'time', 'open', 'high', 'low', 'close', 'volume', 'amount', 'pct_chg']
        existing = [c for c in keep_cols if c in df.columns]
        return df[existing].copy()

    def get_minutely_data(
        self,
        stock_code: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        days: int = 120,
        frequency: str = "60",
    ) -> pd.DataFrame:
        """
        获取60分钟K线数据（统一入口）

        Args:
            stock_code: 股票代码
            start_date: 开始日期（可选）
            end_date: 结束日期（可选，默认今天）
            days: 获取天数（当 start_date 未指定时使用）
            frequency: 频率，默认 "60"

        Returns:
            标准化的 DataFrame
        """
        if end_date is None:
            end_date = datetime.now().strftime('%Y-%m-%d')
        if start_date is None:
            start_dt = datetime.strptime(end_date, '%Y-%m-%d') - timedelta(days=days * 2)
            start_date = start_dt.strftime('%Y-%m-%d')

        logger.info(
            f"[{self.name}] 开始获取 {stock_code} 60分钟数据: 范围={start_date} ~ {end_date}"
        )

        raw_df = self._fetch_raw_minutely_data(stock_code, start_date, end_date, frequency)
        df = self._normalize_minutely_data(raw_df, stock_code)

        # 数据清洗
        df = df.dropna(subset=['close', 'volume'])
        df = df.sort_values(['date', 'time'], ascending=True).reset_index(drop=True)

        logger.info(f"[{self.name}] {stock_code} 60分钟数据获取成功: {len(df)} 条")
        return df

    def get_growth_data(
        self,
        stock_code: str,
        year: int,
        quarter: int,
    ) -> Optional[Dict[str, Any]]:
        """
        获取季频成长能力数据（query_growth_data）

        Args:
            stock_code: 股票代码
            year: 年份
            quarter: 季度 (1-4)

        Returns:
            字典或 None
        """
        try:
            bs_code = self._convert_stock_code(stock_code)
            with self._baostock_session() as bs:
                rs = bs.query_growth_data(code=bs_code, year=year, quarter=quarter)
                if rs.error_code != '0':
                    logger.warning(
                        f"Baostock query_growth_data 失败: {rs.error_msg}"
                    )
                    return None

                data_list = []
                while rs.next():
                    data_list.append(rs.get_row_data())

                if not data_list:
                    return None

                fields = rs.fields
                row = data_list[0]

                def _get(field: str) -> Optional[str]:
                    try:
                        idx = fields.index(field)
                        return row[idx] if idx < len(row) else None
                    except ValueError:
                        return None

                return {
                    'code': stock_code,
                    'year': year,
                    'quarter': quarter,
                    'pub_date': _get('pubDate'),
                    'stat_date': _get('statDate'),
                    'yoy_equity': _get('YOYEquity'),
                    'yoy_asset': _get('YOYAsset'),
                    'yoy_ni': _get('YOYNI'),
                    'yoy_eps_basic': _get('YOYEPSBasic'),
                    'yoy_pni': _get('YOYPNI'),
                    'raw': dict(zip(fields, row)),
                }
        except Exception as e:
            logger.warning(f"Baostock 获取成长数据失败 {stock_code}: {e}")
            return None

    def get_forecast_report(
        self,
        stock_code: str,
        start_date: str,
        end_date: str,
    ) -> List[Dict[str, Any]]:
        """
        获取季频公司业绩预告（query_forecast_report）

        Args:
            stock_code: 股票代码
            start_date: 开始日期 YYYY-MM-DD
            end_date: 结束日期 YYYY-MM-DD

        Returns:
            记录列表
        """
        try:
            bs_code = self._convert_stock_code(stock_code)
            with self._baostock_session() as bs:
                rs = bs.query_forecast_report(
                    code=bs_code,
                    start_date=start_date,
                    end_date=end_date,
                )
                if rs.error_code != '0':
                    logger.warning(
                        f"Baostock query_forecast_report 失败: {rs.error_msg}"
                    )
                    return []

                data_list = []
                while rs.next():
                    data_list.append(rs.get_row_data())

                if not data_list:
                    return []

                fields = rs.fields
                results = []
                for row in data_list:
                    def _get(field: str) -> Optional[str]:
                        try:
                            idx = fields.index(field)
                            return row[idx] if idx < len(row) else None
                        except ValueError:
                            return None

                    results.append({
                        'code': stock_code,
                        'forecast_date': _get('forecastDate'),
                        'report_date': _get('reportDate'),
                        'forecast_type': _get('forecastType'),
                        'forecast_abstract': _get('forecastAbstract'),
                        'chg_min': _get('forecastChgMin'),
                        'chg_max': _get('forecastChgMax'),
                        'net_profit_min': _get('forecastNetProfitMin'),
                        'net_profit_max': _get('forecastNetProfitMax'),
                        'raw': dict(zip(fields, row)),
                    })
                return results
        except Exception as e:
            logger.warning(f"Baostock 获取业绩预告失败 {stock_code}: {e}")
            return []


if __name__ == "__main__":
    # 测试代码
    logging.basicConfig(level=logging.DEBUG)
    
    fetcher = BaostockFetcher()
    
    try:
        # 测试历史数据
        df = fetcher.get_daily_data('600519')  # 茅台
        print(f"获取成功，共 {len(df)} 条数据")
        print(df.tail())
        
        # 测试股票名称
        name = fetcher.get_stock_name('600519')
        print(f"股票名称: {name}")
        
    except Exception as e:
        print(f"获取失败: {e}")
