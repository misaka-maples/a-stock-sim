import logging
from typing import Any, Dict, List, Optional

from .base import BaseMarketDataProvider
from .baostock_provider import BaoStockProvider
from .sina_provider import SinaProvider
from .tencent_provider import TencentProvider
from .akshare_provider import AKShareProvider
from .eastmoney_provider import EastMoneyProvider
from ..models import normalize_symbol
from trade_calendar import is_trading_day, get_previous_trading_day
from datetime import date
import math
import threading
from ..models import DailyBar, StockInfo
from ..quality import DataQualityValidator
from ..rate_limiter import SmartRateLimiter, RateLimitConfig, CircuitBreakerOpen

logger = logging.getLogger(__name__)


class ProviderManager:
    """
    Orchestrates multiple free A-share market data providers with:
    - Independent rate limiting and concurrency control per provider
    - Automatic exponential backoff retry with jitter
    - Three-state Circuit Breaker
    - Seamless bounded failover between providers
    - Data quality verification on every returned bar sequence
    """

    def __init__(self):
        self.providers: Dict[str, BaseMarketDataProvider] = {
            "eastmoney": EastMoneyProvider(),
            "baostock": BaoStockProvider(),
            "sina": SinaProvider(),
            "tencent": TencentProvider(),
            "akshare": AKShareProvider(),
        }

        # Rate limiter configurations per provider
        self.limiters: Dict[str, SmartRateLimiter] = {
            "eastmoney": SmartRateLimiter("eastmoney", RateLimitConfig(
                requests_per_second=2.0,max_concurrency=2,retry_times=1,
                failure_threshold=3,circuit_cooldown_seconds=60.0)),
            "baostock": SmartRateLimiter("baostock", RateLimitConfig(
                enabled=True,
                requests_per_second=10.0,
                max_concurrency=1,  # BaoStock SDK socket is single-connection friendly
                retry_times=2,
                backoff_factor=1.5,
                failure_threshold=5,
                circuit_cooldown_seconds=30.0
            )),
            "sina": SmartRateLimiter("sina", RateLimitConfig(
                enabled=True,
                requests_per_second=10.0,
                max_concurrency=4,
                retry_times=2,
                backoff_factor=1.5,
                failure_threshold=5,
                circuit_cooldown_seconds=30.0
            )),
            "tencent": SmartRateLimiter("tencent", RateLimitConfig(
                enabled=True,
                requests_per_second=2.0,
                max_concurrency=4,
                retry_times=2,
                backoff_factor=1.5,
                failure_threshold=5,
                circuit_cooldown_seconds=30.0
            )),
            "akshare": SmartRateLimiter("akshare", RateLimitConfig(
                enabled=True,
                requests_per_second=2.0,
                max_concurrency=1,
                retry_times=1,
                backoff_factor=2.0,
                failure_threshold=3,
                circuit_cooldown_seconds=60.0
            )),
        }

        # Priority chain for daily stock bars
        self.daily_provider_chain = ["eastmoney", "tencent", "baostock", "akshare", "sina"]
        self._data_lock = threading.Lock()
        self._data_metrics = {}

    @staticmethod
    def _target_date(end_date):
        if not end_date:
            from history_data import last_completed_trading_day
            return last_completed_trading_day()
        return end_date if is_trading_day(end_date) else get_previous_trading_day(end_date).isoformat()

    def _record_data_result(self, name, result, latest=None):
        with self._data_lock:
            stats = self._data_metrics.setdefault(name, {
                "usable_responses": 0, "unusable_responses": 0,
                "last_result": "not_tested", "last_usable_trade_date": None})
            stats["usable_responses" if result == "usable" else "unusable_responses"] += 1
            stats["last_result"] = result
            if result == "usable": stats["last_usable_trade_date"] = latest

    supports_storage_admission = True

    def _fetch_validated(self, symbol, start, end, adjust_type, index=False, accept=None):
        expected = self._target_date(end)
        symbol = normalize_symbol(symbol)
        for name in self.daily_provider_chain:
            provider, limiter = self.providers.get(name), self.limiters.get(name)
            if not provider or not limiter: continue
            if adjust_type not in getattr(provider, "supported_adjustments", {"none", "qfq", "hfq"}):
                self._record_data_result(name, "unsupported_adjustment")
                continue
            try:
                raw = limiter.execute(lambda: provider.fetch_index_daily(symbol, start, end)
                    if index else provider.fetch_stock_daily(symbol, start, end, adjust_type))
                bars = DataQualityValidator.clean_and_sort_bars(raw or [])
                bars = [b for b in bars if (not start or b.trade_date >= start)
                        and b.trade_date <= expected]
                if not bars:
                    self._record_data_result(name, "empty_or_invalid")
                    continue
                if bars[-1].trade_date != expected or not bars[-1].is_final:
                    self._record_data_result(name, "stale_or_unsettled")
                    continue
                valid = all(b.symbol == symbol and b.adjust_type == adjust_type
                    and math.isfinite(b.volume) and math.isfinite(b.amount)
                    and math.isclose(b.close / b.raw_close, b.adjustment_factor, rel_tol=.001)
                    and (index or b.volume == 0 or b.amount > 0) for b in bars)
                if not valid:
                    self._record_data_result(name, "invalid_price_basis_or_amount")
                    continue
                # A target-day payload is insufficient when its incremental
                # price basis cannot be joined to the verified local history.
                # Rejection must try the next independent provider as well.
                if accept is not None and accept(bars) is False:
                    self._record_data_result(name, "storage_admission_rejected")
                    continue
                self._record_data_result(name, "usable", bars[-1].trade_date)
                return bars
            except Exception as exc:
                self._record_data_result(name, type(exc).__name__)
                logger.debug("Provider %s unavailable for %s: %s", name, symbol, type(exc).__name__)
        return []

    def fetch_stock_daily(self, symbol: str, start_date: str, end_date: str,
                          adjust_type: str = "qfq") -> List[DailyBar]:
        """
        Executes query through the failover chain.
        Returns validated, sorted bars or empty list if no bars exist.
        """
        return self._fetch_validated(symbol, start_date, end_date, adjust_type)

    def fetch_stock_daily_admitted(self, symbol, start_date, end_date, accept, adjust_type="qfq"):
        return self._fetch_validated(symbol, start_date, end_date, adjust_type, accept=accept)

    def fetch_index_daily(self, index_code: str, start_date: str, end_date: str) -> List[DailyBar]:
        """Require a settled target-date benchmark, with bounded failover."""
        return self._fetch_validated(index_code, start_date, end_date, "none", index=True)

    def fetch_stock_list(self) -> List[StockInfo]:
        """Fetch universe stock list."""
        for name in ("akshare", "baostock"):
            provider = self.providers.get(name)
            limiter = self.limiters.get(name)
            if not provider or not limiter:
                continue
            try:
                stocks = limiter.execute(provider.fetch_stock_list)
                if stocks:
                    return stocks
            except Exception:
                continue
        return []

    def fetch_industry_map(self) -> Dict[str, str]:
        """Fetch industry mapping."""
        provider = self.providers.get("baostock")
        limiter = self.limiters.get("baostock")
        if provider and limiter:
            try:
                return limiter.execute(provider.fetch_industry_map)
            except Exception:
                pass
        return {}

    def get_provider_status(self) -> Dict[str, Any]:
        """Returns health and metrics dictionary for API reporting."""
        res = {}
        for name, limiter in self.limiters.items():
            metrics = limiter.metrics.to_dict()
            metrics["circuit_state"] = limiter.circuit.state
            with self._data_lock:
                data_metrics = dict(self._data_metrics.get(name, {"last_result": "not_tested"}))
            metrics.update(data_metrics)
            metrics["healthy"] = ((limiter.circuit.state != "OPEN") and
                                  data_metrics["last_result"] == "usable")
            metrics["upstream"] = "eastmoney" if name in ("eastmoney", "akshare") else name
            res[name] = metrics
        return res
