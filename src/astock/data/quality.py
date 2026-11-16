# src/astock/data/quality.py
import math
from datetime import date
from typing import List, Dict, Any, Tuple, Optional
from .models import DailyBar, normalize_symbol


class DataValidationError(ValueError):
    """Raised when bar data fails quality or consistency validation."""
    def __init__(self, code: str, message: str = ""):
        self.code = code
        self.message = message or code
        super().__init__(self.message)


class DataQualityValidator:
    """Validates bar data against business and mathematical rules."""

    @classmethod
    def validate_single_bar(cls, bar: DailyBar) -> bool:
        """Helper that catches DataValidationError and returns boolean."""
        try:
            return cls.validate_bar(bar)
        except (DataValidationError, Exception):
            return False

    @staticmethod
    def validate_bar(bar: DailyBar) -> bool:
        """
        Validate single DailyBar. Raises DataValidationError on fatal flaws.
        """
        if not bar.trade_date:
            raise DataValidationError("empty_trade_date", "Bar has no trade date")
        try:
            date.fromisoformat(bar.trade_date)
        except (ValueError, TypeError):
            raise DataValidationError("invalid_trade_date", f"Invalid date: {bar.trade_date}")

        for field_name in ("open", "high", "low", "close", "raw_open", "raw_high", "raw_low", "raw_close"):
            val = getattr(bar, field_name)
            if not isinstance(val, (int, float)) or not math.isfinite(val):
                raise DataValidationError("non_finite_price", f"{field_name} is non-finite: {val}")
            if val <= 0 and not bar.is_suspended:
                raise DataValidationError("non_positive_price", f"{field_name} is <= 0: {val}")

        # Check OHLC boundary consistency
        # Allow tiny epsilon 1e-6 for floating point precision
        eps = 1e-5
        if bar.high + eps < max(bar.open, bar.close, bar.low):
            raise DataValidationError("invalid_high_price", f"High {bar.high} is less than open/close/low")
        if bar.low - eps > min(bar.open, bar.close, bar.high):
            raise DataValidationError("invalid_low_price", f"Low {bar.low} is greater than open/close/high")

        # Raw OHLC boundary consistency
        if bar.raw_high + eps < max(bar.raw_open, bar.raw_close, bar.raw_low):
            raise DataValidationError("invalid_raw_high_price", f"Raw high {bar.raw_high} is less than raw open/close/low")
        if bar.raw_low - eps > min(bar.raw_open, bar.raw_close, bar.raw_high):
            raise DataValidationError("invalid_raw_low_price", f"Raw low {bar.raw_low} is greater than raw open/close/high")

        if not math.isfinite(bar.volume) or bar.volume < 0:
            raise DataValidationError("negative_volume", f"Volume is negative: {bar.volume}")
        if not math.isfinite(bar.amount) or bar.amount < 0:
            raise DataValidationError("negative_amount", f"Amount is negative: {bar.amount}")

        if bar.adjustment_factor <= 0 or not math.isfinite(bar.adjustment_factor):
            raise DataValidationError("invalid_adjustment_factor", f"Adjustment factor is invalid: {bar.adjustment_factor}")

        return True

    @classmethod
    def clean_and_sort_bars(cls, bars: List[DailyBar]) -> List[DailyBar]:
        """
        Validates, deduplicates by trade_date, and sorts chronologically.
        If duplicates exist, the latest updated or final bar wins.
        """
        if not bars:
            return []

        by_date: Dict[str, DailyBar] = {}
        for bar in bars:
            try:
                cls.validate_bar(bar)
            except DataValidationError:
                continue

            dt = bar.trade_date
            if dt not in by_date:
                by_date[dt] = bar
            else:
                existing = by_date[dt]
                # If current is final and existing is not, replace
                if bar.is_final and not existing.is_final:
                    by_date[dt] = bar
                elif bar.updated_at >= existing.updated_at:
                    by_date[dt] = bar

        sorted_dates = sorted(by_date.keys())
        return [by_date[d] for d in sorted_dates]

    @staticmethod
    def check_continuity(dates: List[str], expected_calendar_dates: List[str]) -> List[str]:
        """Returns missing dates from the expected calendar."""
        date_set = set(dates)
        return [d for d in expected_calendar_dates if d not in date_set]
