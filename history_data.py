#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
历史行情数据获取与本地缓存模块 (Historical Market Data Feed)

支持功能：
1. 腾讯 (Tencent) & 东方财富 (Eastmoney) 公共前复权日K线数据获取（OHLCV、成交额、振幅、涨跌幅）。
2. 基准指数（沪深300 000300、上证指数 000001）历史数据拉取与对齐。
3. 本地磁盘高速缓存 (data/history_cache/)，秒级重复加载，零额外重试负担。
4. 多线程并发批量下载与标准数据结构规整。
5. 内置精选代表性标的池（核心活跃池、沪深300代表池等）。
"""

import os
import sys
import json
import time
import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor

import requests

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "data" / "history_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# 预设标的池
PRESET_POOLS = {
    "core_active": [
        # 当前持仓与前期表现优异个股
        "sz300319",  # 麦捷科技 (电子/芯片)
        "sh600237",  # 铜峰电子 (电子元器件)
        "sh600105",  # 永鼎股份 (通信/光模块)
        "sz003035",  # 南网能源 (绿电/储能)
        "sh603578",  # 三星新材 (新材料)
        # 高流动性活跃成长/科技龙头
        "sz300750",  # 宁德时代 (电池/新能源)
        "sz002594",  # 比亚迪 (新能源车)
        "sz300059",  # 东方财富 (金融科技)
        "sz300308",  # 中际旭创 (光通信/CPO)
        "sh601127",  # 赛力斯 (智能汽车)
        "sh601138",  # 工业富联 (算力/AI服务器)
        "sz002475",  # 立讯精密 (消费电子/苹果链)
        "sh600519",  # 贵州茅台 (白酒/消费)
        "sz000001",  # 平安银行 (银行/大金融)
        "sh601318",  # 中国平安 (保险/金融)
        "sh601899",  # 紫金矿业 (有色金属/黄金)
        "sh600900",  # 长江电力 (红利/公用事业)
        "sz002230",  # 科大讯飞 (人工智能)
        "sz002460",  # 赣锋锂业 (锂电/能源金属)
        "sh600036",  # 招商银行 (银行)
    ],
    "csi300_sample": [
        "sh600519", "sh601318", "sz300750", "sz002594", "sh600036",
        "sh601899", "sh600900", "sz000858", "sh600276", "sh601138",
        "sz300059", "sz002475", "sh601012", "sh600309", "sh603259",
        "sz000001", "sh600030", "sh600887", "sz002352", "sh601888",
    ],
}


def normalize_code_for_eastmoney(symbol_or_code: str) -> Tuple[str, str]:
    """
    返回 (secid, clean_code)
    例如: 'sh600519' -> ('1.600519', '600519')
    """
    s = symbol_or_code.strip().lower()
    if s.startswith("sh"):
        code = s[2:]
        market = "1"
    elif s.startswith("sz"):
        code = s[2:]
        market = "0"
    elif s.startswith("bj"):
        code = s[2:]
        market = "0"
    else:
        code = s
        market = "1" if code.startswith(("60", "68")) else "0"
    return f"{market}.{code}", code


def normalize_symbol_for_tencent(symbol_or_code: str) -> str:
    """
    返回标准小写带前缀代码，例如 'sh600519'
    """
    s = symbol_or_code.strip().lower()
    if s.startswith(("sh", "sz", "bj")):
        return s
    if s.startswith(("60", "68")):
        return f"sh{s}"
    elif s.startswith(("00", "30", "399")):
        return f"sz{s}"
    elif s.startswith(("43", "83", "87", "92")):
        return f"bj{s}"
    return f"sz{s}"


class HistoricalDataFeed:
    """历史行情获取与本地缓存引擎"""

    def __init__(self, cache_dir: Optional[Path] = None):
        self.cache_dir = cache_dir or CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.trust_env = False  # 绕过系统本地代理，直连国内金融数据接口
        self.session.headers.update(HEADERS)

    def _get_cache_path(self, symbol: str, start_date: str, end_date: str) -> Path:
        clean_sym = symbol.replace(".", "_")
        return self.cache_dir / f"{clean_sym}_{start_date}_{end_date}.json"

    def fetch_benchmark_kline(
        self,
        index_code: str = "000300",
        start_date: str = "2024-01-01",
        end_date: str = "2026-09-16",
        use_cache: bool = True
    ) -> List[Dict[str, Any]]:
        """
        拉取基准指数历史日K线（默认沪深300 000300，可选上证指数 000001）
        """
        clean_code = index_code.lower().replace("sh", "").replace("sz", "")
        std_symbol = f"sh{clean_code}" if clean_code in ("000300", "000001", "000905") else f"sz{clean_code}"

        cache_file = self._get_cache_path(f"idx_{clean_code}", start_date, end_date)
        if use_cache and cache_file.exists():
            try:
                data = json.loads(cache_file.read_text(encoding="utf-8"))
                if data and isinstance(data, list):
                    filtered = [b for b in data if start_date <= b["date"] <= end_date]
                    if filtered:
                        return filtered
            except Exception:
                pass

        bars = []

        # 1. 优先使用腾讯前复权/指数接口 (稳定无阻断)
        try:
            tx_url = f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={std_symbol},day,,,640,qfq"
            r = self.session.get(tx_url, timeout=6)
            res = r.json()
            idx_data = res.get("data", {}).get(std_symbol, {})
            raw_klines = idx_data.get("day", idx_data.get("qfqday", []))
            prev_c = None
            for row in raw_klines:
                if len(row) >= 6:
                    dt = row[0]
                    op = float(row[1])
                    cl = float(row[2])
                    hi = float(row[3])
                    lo = float(row[4])
                    vol = float(row[5])
                    chg_pct = round(((cl / prev_c) - 1) * 100, 2) if prev_c else round(((cl / op) - 1) * 100, 2)
                    prev_c = cl

                    if start_date <= dt <= end_date:
                        bars.append({
                            "date": dt,
                            "open": op,
                            "close": cl,
                            "high": hi,
                            "low": lo,
                            "volume": vol,
                            "amount": vol * ((op + cl) / 2) * 100,
                            "amplitude_pct": round(((hi - lo) / lo) * 100, 2) if lo > 0 else 0.0,
                            "change_pct": chg_pct,
                            "change_amount": round(cl - op, 2),
                            "turnover_rate": 0.0
                        })
        except Exception as e:
            pass

        # 2. 备用：东方财富接口
        if not bars:
            try:
                secid = f"1.{clean_code}" if clean_code in ("000300", "000001", "000905") else f"0.{clean_code}"
                beg = start_date.replace("-", "")
                end = end_date.replace("-", "")
                url = (
                    f"http://push2his.eastmoney.com/api/qt/stock/kline/get?"
                    f"secid={secid}&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
                    f"&klt=101&fqt=1&beg={beg}&end={end}"
                )
                r = self.session.get(url, timeout=6)
                res = r.json()
                raw_klines = res.get("data", {}).get("klines", [])
                for line in raw_klines:
                    p = line.split(",")
                    if len(p) >= 7:
                        bars.append({
                            "date": p[0],
                            "open": float(p[1]),
                            "close": float(p[2]),
                            "high": float(p[3]),
                            "low": float(p[4]),
                            "volume": float(p[5]),
                            "amount": float(p[6]),
                            "amplitude_pct": float(p[7]) if len(p) > 7 else 0.0,
                            "change_pct": float(p[8]) if len(p) > 8 else 0.0,
                            "change_amount": float(p[9]) if len(p) > 9 else 0.0,
                            "turnover_rate": float(p[10]) if len(p) > 10 else 0.0,
                        })
            except Exception:
                pass

        if bars:
            try:
                cache_file.write_text(json.dumps(bars, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass

        return bars

    def fetch_stock_kline(
        self,
        symbol_or_code: str,
        start_date: str = "2024-01-01",
        end_date: str = "2026-09-16",
        use_cache: bool = True
    ) -> Tuple[str, str, List[Dict[str, Any]]]:
        """
        拉取单只股票前复权日K线
        返回: (symbol, name, [bars])
        """
        std_symbol = normalize_symbol_for_tencent(symbol_or_code)
        code = std_symbol[2:] if len(std_symbol) > 2 else std_symbol
        cache_file = self._get_cache_path(std_symbol, start_date, end_date)

        if use_cache and cache_file.exists():
            try:
                data = json.loads(cache_file.read_text(encoding="utf-8"))
                if "bars" in data and len(data["bars"]) > 0:
                    filtered = [b for b in data["bars"] if start_date <= b["date"] <= end_date]
                    if filtered:
                        return data.get("symbol", std_symbol), data.get("name", code), filtered
            except Exception:
                pass

        bars = []
        name = code

        # 1. 优先使用腾讯前复权接口 (高频直连稳定)
        try:
            tx_url = f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={std_symbol},day,,,640,qfq"
            tr = self.session.get(tx_url, timeout=6)
            t_json = tr.json()
            stock_data = t_json.get("data", {}).get(std_symbol, {})
            name = stock_data.get("qt", {}).get(std_symbol, [None, None, name])[1] or name
            qfqday = stock_data.get("qfqday", stock_data.get("day", []))

            prev_c = None
            for row in qfqday:
                if len(row) >= 6:
                    dt = row[0]
                    op = float(row[1])
                    cl = float(row[2])
                    hi = float(row[3])
                    lo = float(row[4])
                    vol = float(row[5])
                    chg_pct = round(((cl / prev_c) - 1) * 100, 2) if prev_c else round(((cl / op) - 1) * 100, 2)
                    prev_c = cl

                    if start_date <= dt <= end_date:
                        bars.append({
                            "date": dt,
                            "open": op,
                            "close": cl,
                            "high": hi,
                            "low": lo,
                            "volume": vol,
                            "amount": vol * ((op + cl) / 2) * 100,
                            "amplitude_pct": round(((hi - lo) / lo) * 100, 2) if lo > 0 else 0.0,
                            "change_pct": chg_pct,
                            "change_amount": round(cl - op, 2),
                            "turnover_rate": 0.0,
                        })
        except Exception:
            pass

        # 2. 备用：东方财富前复权接口 (补全成交额与换手率)
        if not bars:
            try:
                secid = f"1.{code}" if std_symbol.startswith("sh") else f"0.{code}"
                beg = start_date.replace("-", "")
                end = end_date.replace("-", "")
                url = (
                    f"http://push2his.eastmoney.com/api/qt/stock/kline/get?"
                    f"secid={secid}&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
                    f"&klt=101&fqt=1&beg={beg}&end={end}"
                )
                r = self.session.get(url, timeout=6)
                res = r.json()
                d = res.get("data")
                if d and "klines" in d:
                    name = d.get("name", name)
                    for line in d["klines"]:
                        p = line.split(",")
                        if len(p) >= 7:
                            bars.append({
                                "date": p[0],
                                "open": float(p[1]),
                                "close": float(p[2]),
                                "high": float(p[3]),
                                "low": float(p[4]),
                                "volume": float(p[5]),
                                "amount": float(p[6]),
                                "amplitude_pct": float(p[7]) if len(p) > 7 else 0.0,
                                "change_pct": float(p[8]) if len(p) > 8 else 0.0,
                                "change_amount": float(p[9]) if len(p) > 9 else 0.0,
                                "turnover_rate": float(p[10]) if len(p) > 10 else 0.0,
                            })
            except Exception:
                pass

        if bars:
            try:
                out = {"symbol": std_symbol, "name": name, "bars": bars}
                cache_file.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass

        return std_symbol, name, bars

    def fetch_universe_klines(
        self,
        symbols: List[str],
        start_date: str = "2024-01-01",
        end_date: str = "2026-09-16",
        max_workers: int = 10,
        use_cache: bool = True
    ) -> Dict[str, Dict[str, Any]]:
        """
        批量并发拉取多个标的历史日K线
        """
        results = {}

        def _fetch_one(s):
            return self.fetch_stock_kline(s, start_date=start_date, end_date=end_date, use_cache=use_cache)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            fetched = list(executor.map(_fetch_one, symbols))

        for sym, name, bars in fetched:
            if not bars:
                continue
            by_date = {b["date"]: b for b in bars}
            results[sym] = {
                "symbol": sym,
                "name": name,
                "bars": bars,
                "by_date": by_date
            }

        return results

    @staticmethod
    def get_preset_symbols(pool_type: str = "core_active") -> List[str]:
        """获取预设股票池代码列表"""
        return PRESET_POOLS.get(pool_type, PRESET_POOLS["core_active"])
