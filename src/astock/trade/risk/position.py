# -*- coding: utf-8 -*-
"""
A-Stock Position Risk Layer
Monitors open positions for stop-loss, trailing ATR take-profit, and T+1 exit discipline.
"""

from typing import Dict, List, Optional, Tuple, Any
from ..models import (
    Position, RiskLayer, RiskSeverity, RiskAuditResult
)
import uuid
import datetime


class PositionRiskRule:
    """Evaluates individual position holding risk and exit signals."""

    def __init__(
        self,
        hard_stop_loss_pct: float = -6.0,
        atr_multiplier: float = 3.0,
        take_profit_floor_pct: float = 3.0
    ):
        self.hard_stop_loss_pct = hard_stop_loss_pct
        self.atr_multiplier = atr_multiplier
        self.take_profit_floor_pct = take_profit_floor_pct

    def check_position_exit(
        self,
        pos: Position,
        current_price: float,
        atr: float,
        ma20: float,
        consecutive_below_ma20: int = 0,
        policy: Optional[Dict[str, Any]] = None
    ) -> Tuple[bool, str, RiskAuditResult]:
        """
        Evaluates exit disciplines in order of priority:
        1. Hard Stop Loss: loss <= -6%
        2. Dynamic Trailing ATR Take-Profit: gain >= 3% floor and price <= highest - atr_mult*atr
        3. MA20 Breakdown: price < MA20 for 2 consecutive days
        """
        audit_id = f"RA_POS_{uuid.uuid4().hex[:10]}"
        now_iso = datetime.datetime.now().isoformat()

        if pos.available_shares <= 0:
            return False, "T_PLUS_1_LOCKED", RiskAuditResult(
                audit_id=audit_id, timestamp=now_iso, account_id="position",
                layer=RiskLayer.POSITION, rule_name="T_PLUS_1_CHECK",
                passed=False, severity=RiskSeverity.BLOCK, action_taken="HOLD_LOCKED",
                message=f"{pos.symbol} bought today, locked under T+1 rule",
                details={"symbol": pos.symbol, "shares": pos.shares, "available": 0}
            )

        if pos.cost_price <= 0:
            return False, "INVALID_COST", RiskAuditResult(
                audit_id=audit_id, timestamp=now_iso, account_id="position",
                layer=RiskLayer.POSITION, rule_name="COST_VALIDITY",
                passed=False, severity=RiskSeverity.WARNING, action_taken="HOLD",
                message="Cost price invalid", details={"symbol": pos.symbol}
            )

        pnl_pct = (current_price - pos.cost_price) / pos.cost_price * 100.0

        # 1. Hard Stop Loss
        hard_stop = float(policy['stop_loss_pct']) if policy else self.hard_stop_loss_pct
        if pnl_pct <= hard_stop:
            return True, "硬止损触发", RiskAuditResult(
                audit_id=audit_id, timestamp=now_iso, account_id="position",
                layer=RiskLayer.POSITION, rule_name="HARD_STOP_LOSS",
                passed=False, severity=RiskSeverity.BLOCK, action_taken="TRIGGER_SELL",
                message=f"{pos.symbol} pnl ({pnl_pct:.2f}%) <= hard stop line ({hard_stop:.2f}%)",
                details={"symbol": pos.symbol, "pnl_pct": pnl_pct, "cost": pos.cost_price, "price": current_price}
            )

        # 2. Dynamic Trailing ATR Take-Profit
        max_gain_pct = (pos.highest_price - pos.cost_price) / pos.cost_price * 100.0 if pos.cost_price > 0 else 0.0
        if policy:
            # Consume the existing Product AB profile, including its 20% peak
            # activation and account-specific ATR/floor/MA20 rules.
            stop = float(getattr(pos, 'trailing_stop_price', 0.0) or 0.0)
            active = max_gain_pct >= policy['trailing_stop_activation_pct']
            if policy['trailing_stop_enabled'] and active:
                candidate = stop
                if atr > 0:
                    candidate = pos.highest_price * (1 - policy['atr_trailing_multiplier'] * atr / current_price)
                if policy['profit_floor_enabled']:
                    candidate = max(candidate, pos.cost_price * (1 + policy['profit_floor_pct'] / 100))
                stop = max(stop, round(candidate, 2))
            details = {'symbol': pos.symbol, 'highest_price': pos.highest_price,
                       'atr': atr, 'pnl_pct': pnl_pct, 'trailing_stop_price': stop,
                       'activation_pct': policy['trailing_stop_activation_pct'],
                       'atr_multiplier': policy['atr_trailing_multiplier']}
            if stop > 0 and current_price <= stop:
                return True, '动态ATR止盈', RiskAuditResult(
                    audit_id=audit_id, timestamp=now_iso, account_id='position',
                    layer=RiskLayer.POSITION, rule_name='TRAILING_ATR_TAKE_PROFIT',
                    passed=False, severity=RiskSeverity.INFO, action_taken='TRIGGER_SELL',
                    message=f'{pos.symbol} price below persisted ATR protection line', details=details)
            buffer = 1 - policy['ma20_exit_buffer_pct'] / 100
            days = policy['ma20_exit_consecutive_days']
            ma_exit = (current_price < ma20 * buffer if days == 1 else
                       consecutive_below_ma20 >= days)
            if policy['realtime_ma20_exit'] and ma20 > 0 and ma_exit:
                return True, '跌破MA20退出', RiskAuditResult(
                    audit_id=audit_id, timestamp=now_iso, account_id='position',
                    layer=RiskLayer.POSITION, rule_name='MA20_BREAKDOWN',
                    passed=False, severity=RiskSeverity.WARNING, action_taken='TRIGGER_SELL',
                    message=f'{pos.symbol} configured MA20 exit condition met',
                    details={**details, 'ma20': ma20, 'required_days': days,
                             'consecutive_below_ma20': consecutive_below_ma20})
            return False, 'HOLD', RiskAuditResult(
                audit_id=audit_id, timestamp=now_iso, account_id='position',
                layer=RiskLayer.POSITION, rule_name='POSITION_HEALTHY', passed=True,
                severity=RiskSeverity.INFO, action_taken='HOLD',
                message=f'{pos.symbol} within configured position risk limits', details=details)
        if max_gain_pct >= self.take_profit_floor_pct and atr > 0:
            trailing_stop_price = pos.highest_price - (self.atr_multiplier * atr)
            if current_price <= trailing_stop_price:
                return True, "动态ATR止盈", RiskAuditResult(
                    audit_id=audit_id, timestamp=now_iso, account_id="position",
                    layer=RiskLayer.POSITION, rule_name="TRAILING_ATR_TAKE_PROFIT",
                    passed=False, severity=RiskSeverity.INFO, action_taken="TRIGGER_SELL",
                    message=f"{pos.symbol} price ¥{current_price:.2f} fell below trailing stop ¥{trailing_stop_price:.2f}",
                    details={
                        "symbol": pos.symbol, "highest_price": pos.highest_price,
                        "atr": atr, "trailing_stop": round(trailing_stop_price, 2), "pnl_pct": pnl_pct
                    }
                )

        # 3. MA20 Breakdown
        if ma20 > 0 and current_price < ma20 and consecutive_below_ma20 >= 2:
            return True, "跌破MA20退出", RiskAuditResult(
                audit_id=audit_id, timestamp=now_iso, account_id="position",
                layer=RiskLayer.POSITION, rule_name="MA20_BREAKDOWN",
                passed=False, severity=RiskSeverity.WARNING, action_taken="TRIGGER_SELL",
                message=f"{pos.symbol} price ¥{current_price:.2f} < MA20(¥{ma20:.2f}) for 2 days",
                details={"symbol": pos.symbol, "price": current_price, "ma20": ma20}
            )

        # Normal hold
        return False, "HOLD", RiskAuditResult(
            audit_id=audit_id, timestamp=now_iso, account_id="position",
            layer=RiskLayer.POSITION, rule_name="POSITION_HEALTHY",
            passed=True, severity=RiskSeverity.INFO, action_taken="HOLD",
            message=f"{pos.symbol} within risk tolerance (pnl: {pnl_pct:+.2f}%)",
            details={"symbol": pos.symbol, "pnl_pct": pnl_pct}
        )
