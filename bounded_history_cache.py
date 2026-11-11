# -*- coding: utf-8 -*-
"""
有界磁盘热点历史行情缓存 (Bounded Disk-Backed LRU History Cache)
- 硬盘持久化为主: 全市场 3,185+ 只股票的历史 K 线持久化存储在磁盘 data/history_cache/
- 内存热点缓存为辅: 盘中仅缓存主账户持仓、Sandbox持仓、待交易候选及核心观察标的 (默认上限 150 只)
- LRU 淘汰机制: 超出容量时自动淘汰最久未访问的冷门标的历史K线
- 保护置顶 (Pinned Symbols): 主账户及 Sandbox 持仓与待撮合候选永久驻留内存，绝不被淘汰
- 统计监控: 跟踪记录缓存命中率 (hit rate)、命中次数、未命中次数与内存释放状态
"""

import collections
from collections.abc import MutableMapping
import json
import logging
from pathlib import Path
import threading
import gc
import ctypes
from typing import Dict, Any, List, Optional, Set, Iterator

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CACHE_DIR = BASE_DIR / "data" / "history_cache"


class BoundedHistoryCache(MutableMapping):
    def __init__(self,
                 feed=None,
                 max_capacity: int = 150,
                 cache_dir: Optional[Path] = None,
                 preload: Optional[Dict[str, Any]] = None):
        self.feed = feed
        self.max_capacity = max_capacity
        self.cache_dir = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
        self._hot_cache: collections.OrderedDict = collections.OrderedDict()
        self._known_symbols: Set[str] = set()
        self._pinned_symbols: Set[str] = set()
        self._hits = 0
        self._misses = 0
        self._as_of: Optional[str] = None
        self._lock = threading.RLock()

        # 扫描磁盘缓存目录 (毫秒级)
        self._disk_symbols: Set[str] = set()
        self.rescan_disk()

        if preload:
            for sym, bars in preload.items():
                self._known_symbols.add(sym)
                if len(self._hot_cache) < self.max_capacity:
                    self._hot_cache[sym] = bars

    def rescan_disk(self):
        with self._lock:
            try:
                # 仅当 feed 是具备磁盘缓存特性的真实 Feed 或 None 时才关联磁盘
                if (self.feed is None or hasattr(self.feed, "cache_dir") or hasattr(self.feed, "_peek_cached")) and self.cache_dir.exists():
                    self._disk_symbols = {
                        f.name.replace("_qfq.json", "")
                        for f in self.cache_dir.glob("*_qfq.json")
                    }
                else:
                    self._disk_symbols = set()
            except Exception as e:
                logger.warning(f"扫描磁盘缓存目录失败: {e}")

    def set_as_of(self, as_of: str):
        with self._lock:
            self._as_of = as_of

    def register_known_symbols(self, symbols):
        with self._lock:
            self._known_symbols.update(symbols)

    def pin(self, symbols):
        """置顶保护关键标的 (主账户持仓、Sandbox持仓、Top候选)，确保常驻内存"""
        sym_list = list(symbols)
        with self._lock:
            self._pinned_symbols.update(sym_list)
            if len(self._pinned_symbols) >= self.max_capacity:
                new_cap = max(self.max_capacity, len(self._pinned_symbols) + 50)
                logger.warning(
                    f"【缓存告警】置顶标的数量 ({len(self._pinned_symbols)}) 达到或超过容量上限 ({self.max_capacity})！"
                    f"启动动态保护扩容至 {new_cap}，严禁静默淘汰置顶历史行情。"
                )
                self.max_capacity = new_cap
            for s in sym_list:
                if s not in self._hot_cache and (s in self._known_symbols or self._disk_cache_exists(s)):
                    _ = self.get(s)

    def unpin(self, symbols):
        with self._lock:
            self._pinned_symbols.difference_update(symbols)

    def _disk_cache_exists(self, symbol: str) -> bool:
        clean_sym = symbol.replace(".", "_")
        return clean_sym in self._disk_symbols

    def has_bars(self, symbol: str) -> bool:
        with self._lock:
            return symbol in self._hot_cache or self._disk_cache_exists(symbol)

    def _load_from_disk(self, symbol: str) -> List[Dict[str, Any]]:
        # 1. 优先使用 feed._peek_cached
        if self.feed and hasattr(self.feed, "_peek_cached"):
            try:
                cached = self.feed._peek_cached(symbol, "2024-01-01", self._as_of or "2099-12-31")
                if cached and cached.get("bars"):
                    return cached["bars"]
            except Exception as e:
                logger.debug(f"feed._peek_cached failed for {symbol}: {e}")
        elif self.feed is not None and not hasattr(self.feed, "cache_dir"):
            # 单元测试 Stub/Mock Feed：不回退至生产磁盘目录
            return []

        # 2. 回退至直接读取磁盘 JSON
        clean_sym = symbol.replace(".", "_")
        if clean_sym in self._disk_symbols:
            path = self.cache_dir / f"{clean_sym}_qfq.json"
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    bars = data.get("bars", [])
                    if self._as_of:
                        bars = [b for b in bars if b.get("date", "") <= self._as_of]
                    return bars
                except Exception as e:
                    logger.warning(f"从磁盘读取 {symbol} 历史 K 线失败: {e}")
        return []

    def _evict_if_needed(self):
        while len(self._hot_cache) > self.max_capacity:
            evicted_key = None
            for key in self._hot_cache:
                if key not in self._pinned_symbols:
                    evicted_key = key
                    break
            if evicted_key is not None:
                self._hot_cache.pop(evicted_key)
            else:
                new_cap = len(self._hot_cache) + 50
                logger.warning(
                    f"【缓存保护】内存中所有标的均为保护置顶标的 ({len(self._hot_cache)} 只)，"
                    f"达到或超过容量上限 ({self.max_capacity})！阻止静默淘汰置顶行情，动态扩容至 {new_cap}。"
                )
                self.max_capacity = new_cap
                break

    def __getitem__(self, sym: str) -> List[Dict[str, Any]]:
        with self._lock:
            if sym in self._hot_cache:
                self._hits += 1
                self._hot_cache.move_to_end(sym)
                return self._hot_cache[sym]

            self._misses += 1
            bars = self._load_from_disk(sym)
            if bars:
                self._hot_cache[sym] = bars
                self._known_symbols.add(sym)
                self._hot_cache.move_to_end(sym)
                self._evict_if_needed()
                return bars
            elif sym in self._known_symbols:
                return []
            raise KeyError(sym)

    def __setitem__(self, sym: str, bars: List[Dict[str, Any]]):
        with self._lock:
            self._hot_cache[sym] = bars
            self._known_symbols.add(sym)
            self._hot_cache.move_to_end(sym)
            self._evict_if_needed()

    def __delitem__(self, sym: str):
        with self._lock:
            self._hot_cache.pop(sym, None)
            self._known_symbols.discard(sym)
            self._pinned_symbols.discard(sym)

    def __contains__(self, sym: Any) -> bool:
        with self._lock:
            sym_str = str(sym)
            return (sym_str in self._hot_cache or sym_str in self._known_symbols or self._disk_cache_exists(sym_str))

    def __len__(self) -> int:
        with self._lock:
            return len(self._known_symbols) if self._known_symbols else len(self._hot_cache)

    def __iter__(self) -> Iterator[str]:
        with self._lock:
            keys = list(self._known_symbols if self._known_symbols else self._hot_cache.keys())
        return iter(keys)

    def get(self, sym: str, default: Any = None) -> Any:
        try:
            return self[sym]
        except KeyError:
            return default

    def keys(self):
        with self._lock:
            return list(self._known_symbols if self._known_symbols else self._hot_cache.keys())

    def values(self):
        with self._lock:
            return [self.get(k, []) for k in self.keys()]

    def items(self):
        with self._lock:
            return [(k, self.get(k, [])) for k in self.keys()]

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            total_req = self._hits + self._misses
            hit_rate = round(self._hits / total_req * 100.0, 2) if total_req > 0 else 0.0
            return {
                "capacity": self.max_capacity,
                "hot_count": len(self._hot_cache),
                "known_count": len(self._known_symbols),
                "disk_count": len(self._disk_symbols),
                "pinned_count": len(self._pinned_symbols),
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate_pct": hit_rate
            }

    def trim(self):
        """主动释放未被置顶的非核心标的内存，调用 glibc malloc_trim 归还系统物理内存"""
        with self._lock:
            to_remove = [k for k in self._hot_cache if k not in self._pinned_symbols]
            for k in to_remove:
                self._hot_cache.pop(k, None)
            gc.collect()
            try:
                libc = ctypes.CDLL("libc.so.6")
                libc.malloc_trim(0)
            except Exception:
                pass
