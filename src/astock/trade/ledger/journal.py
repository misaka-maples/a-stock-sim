# -*- coding: utf-8 -*-
"""
A-Stock Double-Entry Account Ledger Journal
Records immutable cash and position journal entries.
"""

from dataclasses import dataclass, asdict
from typing import Optional
import datetime
import uuid


@dataclass(frozen=True)
class CashJournalEntry:
    entry_id: str
    account_id: str
    delta_cents: int
    balance_cents: int
    reason: str
    ref_id: Optional[str] = None
    created_at: str = ""

    @property
    def delta_rmb(self) -> float:
        return round(self.delta_cents / 100.0, 2)

    @property
    def balance_rmb(self) -> float:
        return round(self.balance_cents / 100.0, 2)

    def to_dict(self):
        d = asdict(self)
        d["delta_rmb"] = self.delta_rmb
        d["balance_rmb"] = self.balance_rmb
        return d


@dataclass(frozen=True)
class PositionJournalEntry:
    entry_id: str
    account_id: str
    symbol: str
    delta_shares: int
    balance_shares: int
    reason: str
    ref_id: Optional[str] = None
    created_at: str = ""

    def to_dict(self):
        return asdict(self)
