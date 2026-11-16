# src/astock/data/models.py
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional
import re


def normalize_symbol(symbol_or_code: str) -> str:
    """
    统一为小写带市场前缀代码:
    - 600519 -> sh600519
    - 000001 -> sz000001
    - 300750 -> sz300750
    - 688001 -> sh688001
    - 830000 -> bj830000
    - 000300 -> sh000300 (指数)
    """
    s = str(symbol_or_code).strip().lower().replace(".", "")
    if s.startswith(("sh", "sz", "bj")):
        return s
    if s in ("000300", "000016", "000905"):
        return f"sh{s}"
    if s == "000001":
        return "sz000001"
    if s.startswith(("60", "68")):
        return f"sh{s}"
    elif s.startswith(("00", "30", "399")):
        return f"sz{s}"
    elif s.startswith(("43", "83", "87", "92")):
        return f"bj{s}"
    return f"sz{s}"


def to_baostock_code(symbol_or_code: str) -> str:
    s = normalize_symbol(symbol_or_code)
    return f"{s[:2]}.{s[2:]}"


def to_clean_code(symbol_or_code: str) -> str:
    s = normalize_symbol(symbol_or_code)
    return s[2:]


@dataclass(frozen=True)
class DailyBar:
    symbol: str
    trade_date: str          # YYYY-MM-DD
    open: float              # 前复权开盘价
    high: float              # 前复权最高价
    low: float               # 前复权最低价
    close: float             # 前复权收盘价
    volume: float            # 统一为手 lots (100股)
    amount: float            # 成交额 (元)
    source: str              # 数据源: baostock / akshare / eastmoney / tencent
    adjust_type: str = "qfq" # qfq / hfq / none
    updated_at: str = ""     # ISO 8601 时间戳
    is_final: bool = True    # 是否为收盘核验后的正式历史日线
    raw_open: float = 0.0
    raw_high: float = 0.0
    raw_low: float = 0.0
    raw_close: float = 0.0
    raw_pre_close: Optional[float] = None
    adjustment_factor: float = 1.0
    turnover_rate: Optional[float] = None
    amplitude_pct: Optional[float] = None
    change_pct: Optional[float] = None
    is_suspended: bool = False
    is_st: Optional[bool] = None

    def __post_init__(self):
        if self.raw_open <= 0.0 and self.open > 0.0:
            object.__setattr__(self, "raw_open", self.open)
        if self.raw_high <= 0.0 and self.high > 0.0:
            object.__setattr__(self, "raw_high", self.high)
        if self.raw_low <= 0.0 and self.low > 0.0:
            object.__setattr__(self, "raw_low", self.low)
        if self.raw_close <= 0.0 and self.close > 0.0:
            object.__setattr__(self, "raw_close", self.close)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DailyBar":
        symbol = normalize_symbol(d.get("symbol", ""))
        trade_date = str(d.get("trade_date") or d.get("date") or "")
        open_val = float(d.get("open", 0.0))
        high_val = float(d.get("high", 0.0))
        low_val = float(d.get("low", 0.0))
        close_val = float(d.get("close", 0.0))
        vol_val = float(d.get("volume", 0.0))
        amt_val = float(d.get("amount", 0.0))
        source = str(d.get("source", "unknown"))
        adjust_type = str(d.get("adjust_type", "qfq"))
        updated_at = str(d.get("updated_at") or datetime.utcnow().isoformat())
        is_final = bool(d.get("is_final", True))

        raw_open = float(d.get("raw_open", open_val))
        raw_high = float(d.get("raw_high", high_val))
        raw_low = float(d.get("raw_low", low_val))
        raw_close = float(d.get("raw_close", close_val))
        raw_pre_close = float(d["raw_pre_close"]) if d.get("raw_pre_close") is not None else None
        adj_factor = float(d.get("adjustment_factor", 1.0))
        turnover = float(d["turnover_rate"]) if d.get("turnover_rate") is not None else None
        amp = float(d["amplitude_pct"]) if d.get("amplitude_pct") is not None else None
        chg = float(d["change_pct"]) if d.get("change_pct") is not None else None
        is_suspended = bool(d.get("is_suspended", False))
        is_st = d.get("is_st")
        is_st = bool(is_st) if is_st is not None else None

        return cls(
            symbol=symbol,
            trade_date=trade_date,
            open=open_val,
            high=high_val,
            low=low_val,
            close=close_val,
            volume=vol_val,
            amount=amt_val,
            source=source,
            adjust_type=adjust_type,
            updated_at=updated_at,
            is_final=is_final,
            raw_open=raw_open,
            raw_high=raw_high,
            raw_low=raw_low,
            raw_close=raw_close,
            raw_pre_close=raw_pre_close,
            adjustment_factor=adj_factor,
            turnover_rate=turnover,
            amplitude_pct=amp,
            change_pct=chg,
            is_suspended=is_suspended,
            is_st=is_st
        )

    def to_legacy_dict(self) -> Dict[str, Any]:
        """兼容已有模块使用的字典格式"""
        res = {
            "date": self.trade_date,
            "source": self.source,
            "open": self.open,
            "raw_open": self.raw_open,
            "high": self.high,
            "raw_high": self.raw_high,
            "low": self.low,
            "raw_low": self.raw_low,
            "close": self.close,
            "raw_close": self.raw_close,
            "volume": self.volume,
            "amount": self.amount,
            "adjustment_factor": self.adjustment_factor,
            "raw_pre_close": self.raw_pre_close,
            "is_suspended": self.is_suspended,
            "is_st": self.is_st,
            "is_final": self.is_final
        }
        if self.turnover_rate is not None:
            res["turnover_rate"] = self.turnover_rate
        if self.amplitude_pct is not None:
            res["amplitude_pct"] = self.amplitude_pct
        if self.change_pct is not None:
            res["change_pct"] = self.change_pct
        return res


@dataclass
class StockInfo:
    symbol: str
    name: str
    industry: str = ""
    area: str = ""
    market: str = ""
    list_date: str = ""
    status: str = "L"  # L 上市, D 退市, P 暂停
    is_st: bool = False
    updated_at: str = ""


@dataclass
class SyncProgress:
    symbol: str
    last_trade_date: str
    total_bars: int = 0
    first_trade_date: str = ""
    last_source: str = ""
    updated_at: str = ""
    failed_attempts: int = 0
    last_error: Optional[str] = None


@dataclass
class SyncTaskRecord:
    task_id: str
    task_type: str        # INIT / INCREMENTAL / MANUAL / RETRY
    target_date: str
    total_symbols: int
    completed_symbols: int
    failed_symbols: int
    status: str           # PENDING / RUNNING / COMPLETED / FAILED
    started_at: str
    finished_at: Optional[str] = None
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        from dataclasses import asdict
        return asdict(self)
