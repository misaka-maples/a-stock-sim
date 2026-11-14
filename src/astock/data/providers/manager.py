import logging
from typing import Any, Dict, List, Optional

from .base import BaseMarketDataProvider
from .baostock_provider import BaoStockProvider
from .sina_provider import SinaProvider
from .tencent_provider import TencentProvider
from .akshare_provider import AKShareProvider
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
            "baostock": BaoStockProvider(),
            "sina": SinaProvider(),
            "tencent": TencentProvider(),
            "akshare": AKShareProvider(),
        }

        # Rate limiter configurations per provider
        self.limiters: Dict[str, SmartRateLimiter] = {
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
                requests_per_second=10.0,
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
        self.daily_provider_chain = ["baostock", "sina", "tencent", "akshare"]

    def fetch_stock_daily(self, symbol: str, start_date: str, end_date: str,
                          adjust_type: str = "qfq") -> List[DailyBar]:
        """
        Executes query through the failover chain.
        Returns validated, sorted bars or empty list if no bars exist.
        """
        errors = []
        for name in self.daily_provider_chain:
            provider = self.providers.get(name)
            limiter = self.limiters.get(name)
            if not provider or not limiter:
                continue

            try:
                def _call():
                    return provider.fetch_stock_daily(symbol, start_date, end_date, adjust_type)

                raw_bars = limiter.execute(_call)
                if raw_bars:
                    cleaned = DataQualityValidator.clean_and_sort_bars(raw_bars)
                    if cleaned:
                        return cleaned
                elif raw_bars is not None and name == "baostock":
                    # If BaoStock successfully replied (no exception) with 0 bars for an SH/SZ stock,
                    # the stock was suspended, delisted, or unlisted in this window.
                    # Do not trigger cascading errors on backup providers unless it is a non-standard code.
                    if symbol.startswith(("sh", "sz")):
                        return []
            except (CircuitBreakerOpen, Exception) as exc:
                errors.append(f"{name}: {type(exc).__name__}({str(exc)})")
                continue

        if errors:
            logger.debug(f"Providers error for {symbol}: {', '.join(errors)}")
        return []

    def fetch_index_daily(self, index_code: str, start_date: str, end_date: str) -> List[DailyBar]:
        """Fetch index daily bars with failover."""
        errors = []
        for name in ("baostock", "sina", "tencent", "akshare"):
            provider = self.providers.get(name)
            limiter = self.limiters.get(name)
            if not provider or not limiter:
                continue

            try:
                def _call():
                    return provider.fetch_index_daily(index_code, start_date, end_date)

                raw_bars = limiter.execute(_call)
                if raw_bars:
                    cleaned = DataQualityValidator.clean_and_sort_bars(raw_bars)
                    if cleaned:
                        return cleaned
            except Exception as exc:
                errors.append(f"{name}: {type(exc).__name__}")
                continue

        logger.warning(f"All providers failed for index {index_code}: {', '.join(errors)}")
        return []

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
            metrics["healthy"] = (limiter.circuit.state != "OPEN")
            res[name] = metrics
        return res
