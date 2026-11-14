# -*- coding: utf-8 -*-
"""
A-Stock Offline Backtest Dataset Loader
Loads immutable historical data snapshots for 100% offline, reproducible backtesting.
Strictly isolates index benchmarks from stock caches.
"""

from typing import Dict, List, Optional, Any
from pathlib import Path
import json
import glob


class BacktestDataset:
    """Offline historical dataset container."""

    def __init__(self, cache_dir: Path):
        self.cache_dir = Path(cache_dir)
        self.benchmark_300: List[Dict[str, Any]] = []
        self.benchmark_500: List[Dict[str, Any]] = []
        self.benchmark_1000: List[Dict[str, Any]] = []
        self.industry_map: Dict[str, str] = {}
        self.stocks: Dict[str, Dict[str, Any]] = {}
        self.trading_days: List[str] = []

    def load(
        self,
        start_date: str = "2024-01-02",
        end_date: str = "2026-11-06",
        min_bars: int = 125
    ):
        """Loads offline benchmark indices, industry mappings, and stock histories."""
        # 1. Industry Map
        ind_path = self.cache_dir / "industry_map_eastmoney.json"
        if not ind_path.exists():
            ind_path = self.cache_dir.parent / "industry_map.json"
        if ind_path.exists():
            with open(ind_path, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.industry_map = d.get("industries", d)

        # 2. Benchmark Indices (isolated from stocks)
        def _load_idx(p: Path) -> List[Dict[str, Any]]:
            if not p.exists():
                return []
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            bars = d.get("bars", d)
            return sorted(bars, key=lambda x: x["date"])

        self.benchmark_300 = _load_idx(self.cache_dir / "idx_000300_none.json")
        self.benchmark_500 = _load_idx(self.cache_dir / "idx_000905_none.json")
        self.benchmark_1000 = _load_idx(self.cache_dir / "idx_000852_none.json")

        dates_300 = {b["date"] for b in self.benchmark_300}
        all_eval_days = sorted([d for d in dates_300 if start_date <= d <= end_date])
        self.trading_days = all_eval_days

        # 3. Stock Bars
        stock_files = sorted(glob.glob(str(self.cache_dir / "*_qfq.json")))
        for fpath in stock_files:
            sym = Path(fpath).name.replace("_qfq.json", "")
            code = sym[-6:]
            if not code.startswith(("600", "601", "603", "605", "000", "001", "002", "003", "300", "301", "688")):
                continue
            try:
                with open(fpath, "r", encoding="utf-8") as fp:
                    d = json.load(fp)
                bars = d.get("bars", d)
                if not bars or len(bars) < min_bars:
                    continue
                name = d.get("name", sym)
                if "ST" in name.upper() or "退" in name:
                    continue

                bars_sorted = sorted(bars, key=lambda x: x["date"])
                closes = [float(b["close"]) for b in bars_sorted]
                highs = [float(b["high"]) for b in bars_sorted]
                lows = [float(b["low"]) for b in bars_sorted]
                n = len(bars_sorted)

                atr = [0.0] * n
                for i in range(14, n):
                    tr_sum = sum(
                        max(highs[j] - lows[j], abs(highs[j] - closes[j-1]), abs(lows[j] - closes[j-1]))
                        for j in range(i-13, i+1)
                    )
                    atr[i] = tr_sum / 14.0

                self.stocks[sym] = {
                    "symbol": sym,
                    "name": name,
                    "industry": self.industry_map.get(sym, "其他"),
                    "bars": bars_sorted,
                    "date_to_idx": {b["date"]: i for i, b in enumerate(bars_sorted)},
                    "atr": atr
                }
            except Exception:
                continue
