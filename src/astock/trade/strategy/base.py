# -*- coding: utf-8 -*-
"""
A-Stock Pure Strategy Interface
Strategies are pure signal and target position generators:
They accept market bars and current regime, and output Signals or TargetPortfolio.
They are strictly decoupled from order submission, broker interaction, and cash mutation.
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any
from ..models import Signal, TargetPortfolio, MarketRegime, Position, AccountBalance


class BaseStrategy(ABC):
    """Abstract pure quantitative strategy."""

    def __init__(self, name: str, account_id: str):
        self.name = name
        self.account_id = account_id

    @abstractmethod
    def generate_signals(
        self,
        as_of: str,
        market_data: Dict[str, Any],
        regime: MarketRegime
    ) -> List[Signal]:
        """Generates candidate signals sorted by rank/score."""
        pass

    @abstractmethod
    def plan_targets(
        self,
        as_of: str,
        regime: MarketRegime,
        signals: List[Signal],
        balance: AccountBalance,
        positions: Dict[str, Position],
        is_frozen: bool = False
    ) -> TargetPortfolio:
        """Translates candidate signals and portfolio state into TargetPortfolio."""
        pass
