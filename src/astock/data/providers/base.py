# src/astock/data/providers/base.py
from abc import ABC, abstractmethod
from typing import Dict, List, Optional
from ..models import DailyBar, StockInfo


class BaseMarketDataProvider(ABC):
    """Abstract interface for external free market data providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique provider identifier (e.g., 'baostock', 'akshare', 'eastmoney')."""
        pass

    @abstractmethod
    def fetch_stock_daily(self, symbol: str, start_date: str, end_date: str,
                          adjust_type: str = "qfq") -> List[DailyBar]:
        """Fetch daily OHLCV bars for a single stock."""
        pass

    @abstractmethod
    def fetch_index_daily(self, index_code: str, start_date: str, end_date: str) -> List[DailyBar]:
        """Fetch daily OHLCV bars for a market index (e.g. '000300', '000001')."""
        pass

    @abstractmethod
    def fetch_stock_list(self) -> List[StockInfo]:
        """Fetch list of all tradable A-share stocks."""
        pass

    @abstractmethod
    def fetch_industry_map(self) -> Dict[str, str]:
        """Fetch symbol -> industry/sector classification mapping."""
        pass

    def is_healthy(self) -> bool:
        """Quick healthcheck for the provider."""
        return True
