"""
data_fetcher.py — K线数据统一获取模块 (V6.1)

优先级链：
  ① neodata 全量数据（最全面，ETF/指数/个股均可）
  ② baostock 历史K线（第二顺位，~0.03s/次，无反爬限制）
  ③ AKShare 实时行情（兜底，仅 fund_etf_spot_em 可用）

baostock 代码格式：
  沪市(6XXXXX/51XXXX/58XXXX): sh.XXXXXX
  深市(0XXXXX/3XXXXX/159XXX): sz.XXXXXX
  上证指数:                     sh.000001

调用方式：
  from trading_engine.data_fetcher import fetch_klines, fetch_weekly
  candles = fetch_klines('512880', '证券ETF国泰', period='daily')
  weekly_df = fetch_weekly('512880', '证券ETF国泰')
"""

import sys, json, subprocess, re, threading
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Tuple
import pandas as pd
from trading_engine.models import Candle

# ── 全局配置 ──
_VENV_PY = r'C:\Users\Admin\.workbuddy\binaries\python\envs\default\Scripts\python.exe'
_ND_ROOT = 'D:/WorkBuddy/resources/app.asar.unpacked/resources/builtin-skills/neodata-financial-search'
_BEIJING_TZ = timezone(timedelta(hours=8))
_TODAY_STR = datetime.now(_BEIJING_TZ).strftime('%Y-%m-%d')
_BS_START = '2015-01-01'  # baostock 统一起始日期


def _to_bs_code(code: str) -> Optional[str]:
    """将内部代码转为 baostock 格式 (sh.XXXXXX / sz.XXXXXX)"""
    if code == '000001.SH':
        return 'sh.000001'
    if code.startswith(('0', '3', '159')):
        return f'sz.{code}'
    if code.startswith(('6', '5', '58')):
        return f'sh.{code}'
    return None


# ── neodata ──

_ND_LOCK = threading.Lock()  # neodata API 有限流，全局串行化防止并发触发限流


def _neodata_query(query_str: str, retries: int = 3) -> str:
    """执行 neodata 查询，返回原始 JSON 字符串（全局锁串行化 + 偶发失败重试）"""
    import time as _time
    import random as _random
    last_out = ''
    with _ND_LOCK:  # 串行化，防并发限流（8线程并发会触发限流导致空返回）
        for attempt in range(retries):
            try:
                r = subprocess.run(
                    [_VENV_PY, f'{_ND_ROOT}/scripts/query.py', '--query', query_str],
                    capture_output=True, text=True, timeout=30, cwd=_ND_ROOT,
                )
                out = r.stdout or ''
                # 有效返回: 非空 且 包含JSON成功标记 且 非鉴权错误
                if out.strip() and 'TOKEN_' not in out:
                    return out
                last_out = out
            except subprocess.TimeoutExpired:
                last_out = ''
            # 退避重试: 0.8~1.6s 随机延迟，避免连续请求触发限流
            if attempt < retries - 1:
                _time.sleep(0.8 + _random.random() * 0.8)
    return last_out


def _parse_neodata_table(raw_json: str, min_rows: int = 5) -> List[Candle]:
    """解析 neodata 返回的表格格式 → Candle[]（升序排列）"""
    cands = []
    try:
        data = json.loads(raw_json)
    except:
        return cands
    for item in data.get('data', {}).get('apiData', {}).get('apiRecall', []):
        if '数据详情' not in item.get('content', ''):
            continue
        for line in item['content'].split('\n'):
            cols = [x.strip() for x in line.split('|')]
            # 第10列(cols[9])为日期，格式 YYYY-MM-DD
            if len(cols) < 10 or not re.match(r'\d{4}-\d{2}-\d{2}', cols[9]):
                continue
            try:
                cands.append(Candle(
                    date=cols[9],
                    open=float(cols[1]),
                    close=float(cols[2]),
                    high=float(cols[3]),
                    low=float(cols[4]),
                    volume=float(cols[5].replace(',', '') or '0'),
                ))
            except:
                pass
        break  # 只取第一个数据详情块
    return cands


# ── baostock ──

import threading  # noqa: F401 (已在顶部导入)
_BS_LOCK = threading.Lock()  # baostock全局socket非线程安全，串行化访问


def _from_baostock(bs_code: str, period: str = 'daily', max_candles: int = 300) -> Optional[List[Candle]]:
    """从 baostock 获取历史K线（第二顺位，无反爬）
    
    Returns:
        Candle[] 升序排列
    """
    try:
        import baostock as bs
        with _BS_LOCK:  # 串行化，防止并发login/logout踩踏socket
            lg = bs.login()
            if lg.error_code != '0':
                return None
            freq = 'd' if period == 'daily' else 'w'
            rs = bs.query_history_k_data_plus(
                bs_code,
                'date,open,high,low,close,volume,amount',
                start_date=_BS_START, end_date=_TODAY_STR,
                frequency=freq, adjustflag='2',  # 前复权
            )
            df = rs.get_data()
            bs.logout()
        if df is None or len(df) < 5:
            return None
        # 过滤有效行(baostock空值返回空字符串)
        df = df[df['close'].str.strip() != ''].copy()
        if len(df) < 5:
            return None
        df = df.tail(max_candles)
        return [
            Candle(
                date=str(r['date'])[:10],
                open=float(r['open']),
                close=float(r['close']),
                high=float(r['high']),
                low=float(r['low']),
                volume=float(r['volume']) if r['volume'].strip() else 0,
            )
            for _, r in df.iterrows()
        ]
    except:
        return None


# ── 腾讯前复权K线（主数据源，解决除权失真，支持日K/周K）──

def _from_tencent_etf(code: str, period: str = 'daily') -> Optional[List[Candle]]:
    """从腾讯获取ETF前复权日K/周K线（解决除权失真）

    腾讯接口支持前复权(qfq)，能正确处理科创芯片等除权个股；直接支持周K。
    period: 'daily'-日K, 'weekly'-周K；约60根
    成交量单位=手 → ×100 转股
    """
    try:
        import requests, json as _json
        if code == '000001.SH':
            symbol = 'sh000001'
        elif code.startswith(('159', '16')):
            symbol = f'sz{code}'
        else:
            symbol = f'sh{code}'
        period_key = 'day' if period == 'daily' else 'week'
        url = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
        params = {'param': f'{symbol},{period_key},,,{60},qfq'}
        r = requests.get(url, params=params, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        kdata = data.get('data', {}).get(symbol, {})
        # 前复权 key: qfqday/qfqweek/day/week
        rows = None
        for key in [f'qfq{period_key}', period_key]:
            if key in kdata and kdata[key]:
                rows = kdata[key]
                break
        if not rows:
            for key, val in kdata.items():
                if val and isinstance(val, list):
                    rows = val
                    break
        if not rows or len(rows) < 20:
            return None
        cands = []
        for row in rows:
            try:
                # 腾讯行: [日期, 开, 收, 高, 低, 成交量(手), ...]
                cands.append(Candle(
                    date=str(row[0]),
                    open=float(row[1]),
                    close=float(row[2]),
                    high=float(row[3]),
                    low=float(row[4]),
                    volume=float(row[5]) * 100 if len(row) > 5 and row[5] else 0,  # 手→股
                ))
            except (IndexError, ValueError):
                continue
        cands.sort(key=lambda c: c.date)
        return cands if len(cands) >= 20 else None
    except Exception:
        return None


# ── sina 历史K线（主数据源，稳定无反爬，250根）──

def _from_sina_etf(code: str, period: str = 'daily') -> Optional[List[Candle]]:
    """从 sina 获取ETF日K线（稳定，不受东财/baostock/neodata影响）

    sina 接口支持沪/深/科创板 ETF + 上证指数，成交量单位股。
    period: 'daily' 只支持日K；'weekly' 由日K合成（dow_engine.daily_to_weekly）
    """
    try:
        import requests, re, json as _json
        if code == '000001.SH':
            symbol = 'sh000001'
        elif code.startswith(('159', '16')):
            symbol = f'sz{code}'
        else:
            symbol = f'sh{code}'
        url = 'https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20d=/CN_MarketDataService.getKLineData'
        params = {'symbol': symbol, 'scale': 240, 'ma': 'no', 'datalen': 1023}  # 上限1023≈4年历史，回测/兜底用足
        r = requests.get(url, params=params, timeout=10,
            headers={'Referer': 'https://finance.sina.com.cn'})
        if r.status_code != 200:
            return None
        m = re.search(r'\[.*\]', r.text, re.DOTALL)
        if not m:
            return None
        data = _json.loads(m.group(0))
        if len(data) < 20:
            return None
        cands = []
        for row in data:
            try:
                cands.append(Candle(
                    date=str(row['day']),
                    open=float(row['open']),
                    close=float(row['close']),
                    high=float(row['high']),
                    low=float(row['low']),
                    volume=float(row['volume']),
                ))
            except (KeyError, ValueError):
                continue
        cands.sort(key=lambda c: c.date)
        return cands if len(cands) >= 20 else None
    except Exception:
        return None


# ── AKShare（兜底，仅实时）──

def _from_akshare_etf(code: str, period: str = 'daily') -> Optional[List[Candle]]:
    """从 AKShare 获取ETF日/周K线（已基本被封，仅作兜底）"""
    try:
        import akshare as ak
        pd_map = {'daily': 'daily', 'weekly': 'weekly'}
        df = ak.fund_etf_hist_em(
            symbol=code,
            period=pd_map.get(period, 'daily'),
            start_date='20251001',
            end_date=_TODAY_STR,
            adjust='qfq',
        )
        if df is None or len(df) < 5:
            return None
        df = df.rename(columns={
            '日期': 'date', '开盘': 'open', '收盘': 'close',
            '最高': 'high', '最低': 'low', '成交量': 'volume',
        })
        df = df[['date', 'open', 'high', 'low', 'close', 'volume']]
        df = df.sort_values('date').reset_index(drop=True)
        return [
            Candle(
                date=str(r['date'])[:10],
                open=float(r['open']),
                close=float(r['close']),
                high=float(r['high']),
                low=float(r['low']),
                volume=float(r['volume']),
            )
            for _, r in df.iterrows()
        ]
    except:
        return None


def _from_akshare_index(period: str = 'daily') -> Optional[List[Candle]]:
    """从 AKShare 获取上证指数日K线（兜底）"""
    try:
        import akshare as ak
        df = ak.stock_zh_index_daily(symbol='sh000001')
        if df is None or len(df) < 20:
            return None
        recent = df.tail(150 if period == 'daily' else 200)
        return [
            Candle(
                date=str(r['date'])[:10],
                open=float(r['open']),
                close=float(r['close']),
                high=float(r['high']),
                low=float(r['low']),
                volume=float(r['volume']),
            )
            for _, r in recent.iterrows()
        ]
    except:
        return None


def _from_akshare_stock(code: str) -> Optional[List[Candle]]:
    """从 AKShare 获取个股日K线（兜底）"""
    try:
        import akshare as ak
        if not code.startswith(('sh', 'sz')):
            code = f'sz{code}' if code.startswith(('0', '3')) else f'sh{code}'
        df = ak.stock_zh_a_daily(symbol=code, adjust='qfq')
        if df is None or len(df) < 20:
            return None
        df = df.sort_values('date').reset_index(drop=True)
        return [
            Candle(
                date=str(r['date'])[:10],
                open=float(r['open']),
                close=float(r['close']),
                high=float(r['high']),
                low=float(r['low']),
                volume=float(r['volume']),
            )
            for _, r in df.iterrows()
        ]
    except:
        return None


def _real_time_from_sina(code: str, name: str) -> Optional[Candle]:
    """从 sina 获取实时行情（支持上证指数/ETF/个股，完整OHLC+成交量）"""
    try:
        import requests
        if code == '000001.SH':
            url_code = 'sh000001'
        elif code.startswith(('159', '16')):
            url_code = f'sz{code}'  # 深交所ETF
        else:
            url_code = f'sh{code}'  # 上交所ETF(5/58开头)/个股(6开头)
        r = requests.get(
            f'https://hq.sinajs.cn/list={url_code}',
            timeout=10,
            headers={'Referer': 'https://finance.sina.com.cn'},
        )
        if r.status_code != 200 or 'hq_str' not in r.text:
            return None
        payload = r.text.split('"')[1]
        parts = payload.split(',')
        if len(parts) < 32:
            return None
        today = parts[30]
        if today != _TODAY_STR:
            return None
        # sina 字段: [0]名称 [1]今开 [2]昨收 [3]现价 [4]最高 [5]最低 [8]成交量 [9]成交额 [30]日期
        # 单位：ETF/个股 parts[8] 已是股；上证指数 parts[8] 是"手"需 ×100
        raw_vol = max(0, float(parts[8].replace(',', '') or 0)) if len(parts) > 8 and parts[8].strip() else 0
        vol = raw_vol * 100 if code == '000001.SH' else raw_vol
        return Candle(
            date=today,
            open=float(parts[1]),
            close=float(parts[3]),
            high=float(parts[4]),
            low=float(parts[5]),
            volume=vol,
        )
    except:
        return None


def _real_time_from_akshare_index() -> Optional[Candle]:
    """从 AKShare spot_em 获取上证指数实时价（sina失败时的备选）"""
    try:
        import akshare as ak
        spot_df = ak.stock_zh_index_spot_em(symbol='上证系列指数')
        sh = spot_df[spot_df['代码'] == '000001']
        if len(sh) == 0:
            return None
        row = sh.iloc[0]
        vol = float(row.get('成交量', 0))
        return Candle(
            date=_TODAY_STR,
            open=float(row['今开']),
            close=float(row['最新价']),
            high=float(row['最高']),
            low=float(row['最低']),
            volume=vol if vol > 0 else float(row.get('成交额', 0)),
        )
    except:
        return None


def _real_time_from_akshare_spot(code: str) -> Optional[Candle]:
    """从 AKShare fund_etf_spot_em 获取ETF实时行情（东方财富盘中快照）
    
    历史K线接口被封后，这是唯一能拿ETF当日实时价/涨跌幅的通道。
    """
    try:
        import akshare as ak
        df_spot = ak.fund_etf_spot_em()
        row = df_spot[df_spot['代码'].astype(str) == code]
        if row.empty:
            return None
        r = row.iloc[0]
        price = float(r['最新价'])
        if price <= 0 or pd.isna(price):
            return None
        # 用最新价+昨收推算今开近似值（spot不提供OHLC，涨跌幅换算昨收）
        chg_pct = float(r['涨跌幅']) if pd.notna(r['涨跌幅']) else 0.0
        prev_close = price / (1 + chg_pct / 100) if chg_pct != 0 else price
        vol = float(r['成交量']) if pd.notna(r['成交量']) else 0.0
        # ⚠️ 东方财富ETF成交量单位=手(1手=100份)，而neodata/baostock的量单位=份
        # 不换算会导致VPA量价/量比显示小100倍（如医药10.88亿份显示成0.1亿）
        vol = vol * 100.0
        return Candle(
            date=_TODAY_STR,
            open=round(prev_close, 4),   # 近似：开盘≈昨收（盘中无法取真实开盘价）
            close=round(price, 4),
            high=round(price, 4),
            low=round(price, 4),
            volume=vol,
        )
    except:
        return None


def _merge_real_time(cands: List[Candle], rt: Candle) -> List[Candle]:
    """将实时行情合并到K线列表中（替换最后一条或追加）"""
    if not cands:
        return [rt] if rt else []
    if not rt:
        return cands
    if cands[-1].date == rt.date:
        vol = rt.volume or cands[-1].volume
        cands[-1] = Candle(
            date=rt.date, open=rt.open, close=rt.close,
            high=rt.high, low=rt.low, volume=vol,
        )
    else:
        vol = rt.volume or cands[-1].volume
        cands.append(Candle(
            date=rt.date, open=rt.open, close=rt.close,
            high=rt.high, low=rt.low, volume=vol,
        ))
    return cands


# ── 对外接口 ──

def _drop_non_trading_days(cands: List[Candle]) -> List[Candle]:
    """过滤非交易日假K线。

    数据源（腾讯/neodata）在周末/节假日会复制上一交易日的K线、
    把日期标成周六/周日（如 8/1 周六返回 C=昨收 V=昨量 的假K线），
    导致涨跌幅恒为 0%。A股/港股ETF 交易日不可能是周末 → 直接剔除。
    """
    if not cands:
        return cands
    out = []
    for c in cands:
        try:
            if pd.to_datetime(c.date).weekday() >= 5:
                continue  # 周六/周日非交易日
        except Exception:
            pass
        out.append(c)
    return out


def fetch_klines(code: str, name: str, period: str = 'daily') -> List[Candle]:
    """
    统一K线获取接口（升级：neodata → baostock → AKShare兜底）

    Args:
        code: 标的代码（如 '512880', '000001.SH', '002230'）
        name: 标的名称（如 '证券ETF国泰'）
        period: 'daily' 或 'weekly'

    Returns:
        Candle[] 升序排列
    """
    cands: List[Candle] = []

    # ── Step 1: 腾讯前复权K线（主，解决除权失真）──
    cands = _from_tencent_etf(code, period=period) or []

    # ── Step 1.5: 腾讯失败 → sina 250根日K（稳定无反爬）──
    if len(cands) < 20:
        cands = _from_sina_etf(code, period=period) or []

    # ── Step 2: 仍不足 → neodata 全量（查询词变体重试）──
    if len(cands) < 20:
        # ⚠️ 查询词不能带"日行情"：neodata 会路由成实时快照(无数据详情块)
        import time as _t, random as _rnd
        _Q_VARIANTS = [
            f'{code} {name} 历史K线',
            f'{code} {name} 历史行情',
            f'{code} {name} K线',
            f'{code} {name} 历史K线 日行情',  # 兜底：万一哪天该词恢复
        ]
        for attempt in range(3):
            for q in _Q_VARIANTS:
                raw = _neodata_query(q)
                cands = _parse_neodata_table(raw)
                if len(cands) >= 20:
                    break
            if len(cands) >= 20:
                break
            if attempt < 2:
                _t.sleep(0.5 + _rnd.random() * 0.5)

    # ── Step 3: 仍不足 → baostock（黑名单中大概率失败）──
    if len(cands) < 20:
        bs_code = _to_bs_code(code)
        if bs_code:
            cands = _from_baostock(bs_code, period=period) or []

    # ── Step 4: 仍不足 → AKShare 兜底（历史K线已被封大概率失败）──
    if len(cands) < 20:
        if code == '000001.SH':
            cands = _from_akshare_index(period=period) or []
        elif code.startswith(('0', '3', '6')) and len(code) == 6:
            cands = _from_akshare_stock(code) or []
        else:
            cands = _from_akshare_etf(code, period=period) or []

    # ── 补充实时价（仅daily模式，日间拼接今天K线）──
    if cands and period == 'daily':
        rt = None
        if code == '000001.SH':
            rt = _real_time_from_sina(code, name)
            if not rt:
                rt = _real_time_from_akshare_index()
        else:
            # 优先级: neodata当日(3位精度最准) → sina实时 → baostock → AKShare spot
            # ⚠️ neodata当日K线精度3位小数(0.377)，腾讯当日只有2位(0.38) → neodata优先
            raw_rt = _neodata_query(f'{code} {name} 历史K线')  # 不带"日行情"，避免路由成快照
            neodata_rt = _parse_neodata_table(raw_rt)
            if neodata_rt and neodata_rt[-1].date == _TODAY_STR:
                rt = neodata_rt[-1]
            if not rt:
                rt = _real_time_from_sina(code, name)
            if not rt:
                bs_code = _to_bs_code(code)
                if bs_code:
                    bs_today = _from_baostock(bs_code, period='daily', max_candles=5)
                    if bs_today and bs_today[-1].date == _TODAY_STR:
                        rt = bs_today[-1]
            # AKShare fund_etf_spot_em 盘中实时快照（最后兜底，注意成交量单位=手已×100）
            if not rt:
                rt = _real_time_from_akshare_spot(code)
        if rt:
            cands = _merge_real_time(cands, rt)

    # ── 最后: 剔除非交易日假K线（周末复制K线，避免涨跌幅恒为0）──
    cands = _drop_non_trading_days(cands)
    return cands


def fetch_weekly(code: str, name: str) -> Optional[pd.DataFrame]:
    """
    获取周K线数据，用于主趋势分析。

    优先级：baostock(主, ~0.03s/次) → neodata(兜底)

    Returns:
        DataFrame with columns: date, open, high, low, close, volume
        升序排列；失败返回 None
    """
    df = None

    # ── Step 0: 腾讯前复权周K（主，解决除权失真，直接周K，完全信任腾讯数据）──
    t_cands = _from_tencent_etf(code, period='weekly')
    if t_cands and len(t_cands) >= 8:
        df = pd.DataFrame({
            'date': pd.to_datetime([c.date for c in t_cands]),
            'open': [c.open for c in t_cands],
            'high': [c.high for c in t_cands],
            'low': [c.low for c in t_cands],
            'close': [c.close for c in t_cands],
            'volume': [c.volume for c in t_cands],
        }).sort_values('date').reset_index(drop=True)
        # 注: 周K不做当日拼接/修正——本周2位小数的误差对主趋势(ADX/SMA周线)影响可忽略，
        #     且省去neodata查询，保持周K纯净 = 腾讯原生数据

    # ── Step 1: baostock（高效稳定，黑名单期间会失败）──
    if df is None:
        bs_code = _to_bs_code(code)
        if bs_code:
            cands = _from_baostock(bs_code, period='weekly', max_candles=600)
            if cands and len(cands) >= 8:
                df = pd.DataFrame({
                    'date': pd.to_datetime([c.date for c in cands]),
                    'open': [c.open for c in cands],
                    'high': [c.high for c in cands],
                    'low': [c.low for c in cands],
                    'close': [c.close for c in cands],
                    'volume': [c.volume for c in cands],
                }).sort_values('date').reset_index(drop=True)

    # ── Step 2: sina 250根日K → 聚合周K（主兜底，约53周）──
    if df is None:
        try:
            from trading_engine.dow_engine import daily_to_weekly
        except ImportError:
            daily_to_weekly = None
        if daily_to_weekly is not None:
            s_cands = _from_sina_etf(code, period='daily')
            if s_cands and len(s_cands) >= 60:  # 至少60个交易日 → 12周
                df_daily = pd.DataFrame({
                    'open': [c.open for c in s_cands],
                    'high': [c.high for c in s_cands],
                    'low': [c.low for c in s_cands],
                    'close': [c.close for c in s_cands],
                    'volume': [c.volume for c in s_cands],
                }, index=pd.to_datetime([c.date for c in s_cands]))
                weekly = daily_to_weekly(df_daily.sort_index())
                if weekly is not None and len(weekly) >= 8:
                    df = weekly.reset_index()
                    df.columns = ['date', 'open', 'high', 'low', 'close', 'volume']
                    df['date'] = pd.to_datetime(df['date'])

    # ── Step 3: neodata 兜底（50根日K → 11周，最后手段）──
    if df is None:
        # 查询词不带"日行情"（会路由成快照）；neodata 偶发返回"成功但空" → 变体重试
        import time as _t2, random as _rnd2
        _WQ = [
            f'{code} {name} 历史K线',
            f'{code} {name} 历史行情',
            f'{code} {name} K线',
        ]
        cands = []
        for _attempt in range(2):
            for q in _WQ:
                raw = _neodata_query(q)
                cands = _parse_neodata_table(raw)
                if len(cands) >= 30:
                    break
            if len(cands) >= 30:
                break
            if _attempt < 1:
                _t2.sleep(0.5 + _rnd2.random() * 0.5)
        if len(cands) >= 30:  # 至少30个交易日 → 6周
            try:
                from trading_engine.dow_engine import daily_to_weekly
            except ImportError:
                daily_to_weekly = None
            if daily_to_weekly is not None:
                df_daily = pd.DataFrame({
                    'open': [c.open for c in cands],
                    'high': [c.high for c in cands],
                    'low': [c.low for c in cands],
                    'close': [c.close for c in cands],
                    'volume': [c.volume for c in cands],
                }, index=pd.to_datetime([c.date for c in cands]))
                weekly = daily_to_weekly(df_daily.sort_index())
                if weekly is not None and len(weekly) >= 8:
                    df = weekly.reset_index()
                    df.columns = ['date', 'open', 'high', 'low', 'close', 'volume']
                    df['date'] = pd.to_datetime(df['date'])

    # ── 最后: 剔除周K中的周末假数据（腾讯周K date 可能是周日）──
    if df is not None and len(df) > 0:
        df = df[df['date'].dt.weekday < 5].reset_index(drop=True)

    return df
