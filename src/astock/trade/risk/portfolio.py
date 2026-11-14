# -*- coding: utf-8 -*-
"""
A-Stock Portfolio Risk Layer
Enforces portfolio-level constraints:
- Drawdown circuit breaker (8% alert, 12% freeze)
- Base regime position caps
- Single-stock concentration limits (15% Balanced / 25% Aggressive)
- Industry concentration limits (30% Balanced / 40% Aggressive)
- Maximum position slots (3 Balanced / 5 Aggressive)
"""

from typing import Dict, List, Optional, Tuple, Any
from ..models import (
    MarketRegime, RiskLayer, RiskSeverity, RiskAuditResult, Position, AccountBalance
)
import uuid
import datetime


BALANCED_REGIME_CAPS = {
    MarketRegime.RISK_OFF: 0.30,
    MarketRegime.NEUTRAL: 0.45,
    MarketRegime.RISK_ON: 0.65,
    MarketRegime.STRONG_RISK_ON: 0.80,
}

AGGRESSIVE_REGIME_CAPS = {
    MarketRegime.RISK_OFF: 0.50,
    MarketRegime.NEUTRAL: 0.70,
    MarketRegime.RISK_ON: 0.90,
    MarketRegime.STRONG_RISK_ON: 1.00,
}


class PortfolioRiskRule:
    """Evaluates portfolio-level exposure, concentration, and drawdown risk."""

    def __init__(
        self,
        account_type: str = "balanced",
        alert_drawdown_pct: float = 8.0,
        freeze_drawdown_pct: float = 12.0
    ):
        self.account_type = account_type.lower()
        self.alert_drawdown_pct = alert_drawdown_pct
        self.freeze_drawdown_pct = freeze_drawdown_pct

        if self.account_type == "aggressive":
            self.max_stock_weight = 0.25
            self.max_sector_weight = 0.40
            self.max_positions = 5
            self.regime_caps = AGGRESSIVE_REGIME_CAPS
        else:
            self.max_stock_weight = 0.15
            self.max_sector_weight = 0.30
            self.max_positions = 3
            self.regime_caps = BALANCED_REGIME_CAPS

    def check_drawdown_circuit_breaker(
        self,
        balance: AccountBalance
    ) -> Tuple[bool, bool, RiskAuditResult]:
        """
        Monitors portfolio drawdown:
        - alert: dd >= 8%
        - freeze: dd >= 12% (halts new buying expansion)
        """
        audit_id = f"RA_DD_{uuid.uuid4().hex[:10]}"
        now_iso = datetime.datetime.now().isoformat()
        dd = balance.current_drawdown_pct

        is_alert = (dd >= self.alert_drawdown_pct)
        is_freeze = (dd >= self.freeze_drawdown_pct)

        severity = RiskSeverity.FREEZE if is_freeze else (RiskSeverity.WARNING if is_alert else RiskSeverity.INFO)
        action = "FREEZE_NEW_ENTRIES" if is_freeze else ("ALERT_NOTIFIED" if is_alert else "NORMAL")
        msg = f"Account {balance.account_id} drawdown {dd:.2f}% (Alert: {self.alert_drawdown_pct}%, Freeze: {self.freeze_drawdown_pct}%)"

        audit = RiskAuditResult(
            audit_id=audit_id,
            timestamp=now_iso,
            account_id=balance.account_id,
            layer=RiskLayer.PORTFOLIO,
            rule_name="DRAWDOWN_CIRCUIT_BREAKER",
            passed=not is_freeze,
            severity=severity,
            action_taken=action,
            message=msg,
            details={
                "drawdown_pct": dd,
                "peak_equity": balance.peak_equity,
                "total_equity": balance.total_equity,
                "is_freeze": is_freeze
            }
        )
        return is_alert, is_freeze, audit

    def get_effective_regime_cap(
        self,
        regime: MarketRegime,
        is_frozen: bool = False
    ) -> float:
        """Returns maximum allowed total equity weight."""
        if is_frozen:
            return 0.0  # Circuit breaker freeze active
        return self.regime_caps.get(regime, 0.30)

    def validate_allocation(
        self,
        symbol: str,
        target_weight: float,
        sector: str,
        current_positions: Dict[str, Position],
        total_equity: float,
        regime: MarketRegime,
        is_frozen: bool = False
    ) -> Tuple[float, RiskAuditResult]:
        """
        Calculates allowable target weight conforming to:
        1. Circuit breaker freeze
        2. Regime total cap
        3. Single stock weight cap
        4. Sector weight cap
        5. Position slot limit
        """
        audit_id = f"RA_ALLOC_{uuid.uuid4().hex[:10]}"
        now_iso = datetime.datetime.now().isoformat()

        if is_frozen:
            return 0.0, RiskAuditResult(
                audit_id=audit_id, timestamp=now_iso, account_id=self.account_type,
                layer=RiskLayer.PORTFOLIO, rule_name="PORTFOLIO_ALLOCATION_CHECK",
                passed=False, severity=RiskSeverity.FREEZE, action_taken="CAP_TO_ZERO",
                message="Portfolio drawdown freeze is active; new allocations capped at 0%",
                details={"symbol": symbol}
            )

        max_regime_cap = self.get_effective_regime_cap(regime, is_frozen)
        curr_stock_val = sum(p.market_value for p in current_positions.values())
        curr_stock_wt = curr_stock_val / total_equity if total_equity > 0 else 0.0
        remaining_regime_wt = max(0.0, max_regime_cap - curr_stock_wt)

        # Check slots
        if symbol not in current_positions and len(current_positions) >= self.max_positions:
            return 0.0, RiskAuditResult(
                audit_id=audit_id, timestamp=now_iso, account_id=self.account_type,
                layer=RiskLayer.PORTFOLIO, rule_name="MAX_POSITIONS_SLOT_CHECK",
                passed=False, severity=RiskSeverity.BLOCK, action_taken="CAP_TO_ZERO",
                message=f"Current positions ({len(current_positions)}) reached max limit ({self.max_positions})",
                details={"max_positions": self.max_positions}
            )

        # Sector weight
        curr_sec_val = sum(p.market_value for p in current_positions.values() if getattr(p, 'sector', '') == sector)
        curr_sec_wt = curr_sec_val / total_equity if total_equity > 0 else 0.0
        allowed_sec_wt = max(0.0, self.max_sector_weight - curr_sec_wt)

        final_wt = min(target_weight, self.max_stock_weight, remaining_regime_wt, allowed_sec_wt)
        final_wt = max(0.0, round(final_wt, 4))

        passed = (final_wt >= 0.02)  # Min 2% allocation threshold
        action = "ALLOCATED" if passed else "REJECTED_TOO_SMALL"

        audit = RiskAuditResult(
            audit_id=audit_id, timestamp=now_iso, account_id=self.account_type,
            layer=RiskLayer.PORTFOLIO, rule_name="PORTFOLIO_ALLOCATION_CHECK",
            passed=passed, severity=RiskSeverity.INFO if passed else RiskSeverity.WARNING,
            action_taken=action,
            message=f"Allocated final weight {final_wt*100:.2f}% (capped by single/sector/regime constraints)",
            details={
                "symbol": symbol,
                "requested_weight": target_weight,
                "final_weight": final_wt,
                "max_stock_weight": self.max_stock_weight,
                "allowed_sector_weight": allowed_sec_wt,
                "remaining_regime_weight": remaining_regime_wt
            }
        )
        return final_wt, audit
