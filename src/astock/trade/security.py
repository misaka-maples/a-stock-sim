# -*- coding: utf-8 -*-
"""
A-Stock Security Identification Model
Strictly distinguishes asset types (STOCK, INDEX, ETF) and markets (SH, SZ, BJ).
Prevents 6-digit asset collision (e.g., sz000905 stock vs sh000905 index).
"""

from enum import Enum
from typing import Optional
import re


class AssetType(str, Enum):
    STOCK = "stock"
    INDEX = "index"
    ETF = "etf"
    UNKNOWN = "unknown"


class Market(str, Enum):
    SH = "sh"
    SZ = "sz"
    BJ = "bj"
    UNKNOWN = "unknown"


class SecurityIdentifier:
    """Immutable, unambiguous security identity."""

    def __init__(
        self,
        symbol: str,
        code: str,
        market: Market,
        asset_type: AssetType,
        name: str = ""
    ):
        self.symbol = symbol.lower()
        self.code = code
        self.market = market
        self.asset_type = asset_type
        self.name = name

    @property
    def canonical_id(self) -> str:
        """Returns unique composite key, e.g. 'stock:sz000905' or 'index:sh000905'."""
        return f"{self.asset_type.value}:{self.market.value}{self.code}"

    def __repr__(self) -> str:
        return f"<SecurityIdentifier {self.canonical_id} name='{self.name}'>"

    def __eq__(self, other) -> bool:
        if not isinstance(other, SecurityIdentifier):
            return False
        return (self.canonical_id == other.canonical_id)

    def __hash__(self) -> int:
        return hash(self.canonical_id)

    @classmethod
    def parse(
        cls,
        symbol_or_code: str,
        asset_type: Optional[AssetType] = None,
        name: str = ""
    ) -> "SecurityIdentifier":
        """
        Parses symbol or code into a strict SecurityIdentifier.
        Handles:
        - "sh000300" / "idx_000300" -> Index CSI 300
        - "sh000905" / "idx_000905" -> Index CSI 500
        - "sz000905" -> Stock 厦门港务 (NOT CSI 500)
        - "sh000852" / "idx_000852" -> Index CSI 1000
        - "sz000852" -> Stock 石化机械 (NOT CSI 1000)
        - "sz159915" / "sh510300" -> ETF
        - "sh600519" / "sz000001" / "bj830946" -> Stock
        """
        raw = symbol_or_code.strip().lower()

        # Check explicit index prefixes
        if raw.startswith("idx_"):
            code = raw.replace("idx_", "")
            return cls(symbol=f"sh{code}", code=code, market=Market.SH, asset_type=AssetType.INDEX, name=name or "指数")

        m = re.match(r"^([a-z]{2})?(\d{6})$", raw)
        if not m:
            return cls(symbol=raw, code=raw, market=Market.UNKNOWN, asset_type=asset_type or AssetType.UNKNOWN, name=name)

        prefix, code = m.groups()

        # If asset_type is explicitly specified as index
        if asset_type == AssetType.INDEX:
            mkt = Market.SH if prefix == "sh" or code.startswith("000") else (Market.SZ if prefix == "sz" else Market.SH)
            return cls(symbol=f"{mkt.value}{code}", code=code, market=mkt, asset_type=AssetType.INDEX, name=name)

        # Detect by standard A-share conventions
        if prefix:
            mkt = Market(prefix)
        else:
            if code.startswith(("600", "601", "603", "605", "688", "510", "588")):
                mkt = Market.SH
            elif code.startswith(("000", "001", "002", "003", "300", "301", "159")):
                mkt = Market.SZ
            elif code.startswith(("8", "4", "9")):
                mkt = Market.BJ
            else:
                mkt = Market.UNKNOWN

        # Determine asset type if not passed
        if asset_type:
            determined_type = asset_type
        else:
            if code in ("000300", "000905", "000852", "000001") and mkt == Market.SH:
                determined_type = AssetType.INDEX
            elif code.startswith(("51", "58", "15")):
                determined_type = AssetType.ETF
            else:
                determined_type = AssetType.STOCK

        symbol = f"{mkt.value}{code}"
        return cls(symbol=symbol, code=code, market=mkt, asset_type=determined_type, name=name)
