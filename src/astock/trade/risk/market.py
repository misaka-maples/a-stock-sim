# -*- coding: utf-8 -*-
"""
A-Stock Market Risk Layer
Evaluates market regimes (RISK_OFF, NEUTRAL, RISK_ON, STRONG_RISK_ON),
breadth confirmation, and the strict RISK_OFF entry gate filter.
"""

from typing import Dict, List, Optional, Tuple, Any
from ..models import MarketRegime, RiskLayer, RiskSeverity, RiskAuditResult
import uuid
import datetime


class MarketRiskRule:
    """Manages macro market risk, regime detection, and entry gating."""

    def __init__(self, breadth_min: float = 0.50):
        self.breadth_min = breadth_min

    def determine_regime(
        self,
        bench_bars: List[Dict[str, Any]],
        as_of: str,
        candidate_bars: Optional[Dict[str, List[Dict[str, Any]]]] = None
    ) -> Tuple[MarketRegime, float, RiskAuditResult]:
        """
        Determines market regime based on CSI 300 benchmark moving averages (MA120, MA60, MA20)
        and breadth confirmation.
        """
        audit_id = f"RA_MKT_{uuid.uuid4().hex[:10]}"
        now_iso = datetime.datetime.now().isoformat()

        if not bench_bars or len(bench_bars) < 120:
            res = RiskAuditResult(
                audit_id=audit_id,
                timestamp=now_iso,
                account_id="global",
                layer=RiskLayer.MARKET,
                rule_name="BENCHMARK_SUFFICIENT_HISTORY",
                passed=False,
                severity=RiskSeverity.WARNING,
                action_taken="DEFAULT_RISK_OFF",
                message="Insufficient benchmark bars (<120), falling back to RISK_OFF",
                details={"bars_count": len(bench_bars) if bench_bars else 0}
            )
            return MarketRegime.RISK_OFF, 0.0, res

        # Align to as_of
        idx = next((i for i, b in enumerate(bench_bars) if b["date"] == as_of), -1)
        if idx < 119:
            idx = len(bench_bars) - 1

        c = float(bench_bars[idx]["close"])
        ma120 = sum(float(b["close"]) for b in bench_bars[idx-119:idx+1]) / 120.0
        ma60 = sum(float(b["close"]) for b in bench_bars[idx-59:idx+1]) / 60.0
        ma20 = sum(float(b["close"]) for b in bench_bars[idx-19:idx+1]) / 20.0

        breadth = 1.0
        if candidate_bars:
            above_120 = 0
            total_cand = 0
            for sym, bars in candidate_bars.items():
                if len(bars) >= 120:
                    total_cand += 1
                    b_c = float(bars[-1]["close"])
                    b_ma120 = sum(float(b["close"]) for b in bars[-120:]) / 120.0
                    if b_c > b_ma120:
                        above_120 += 1
            if total_cand > 0:
                breadth = above_120 / total_cand

        if c <= ma120:
            regime = MarketRegime.RISK_OFF
            reason = f"CSI300 close({c:.2f}) <= MA120({ma120:.2f})"
        elif c <= ma20:
            regime = MarketRegime.NEUTRAL
            reason = f"CSI300 close({c:.2f}) <= MA20({ma20:.2f}) but > MA120"
        elif ma20 <= ma60:
            regime = MarketRegime.RISK_ON
            reason = f"CSI300 MA20({ma20:.2f}) <= MA60({ma60:.2f})"
        else:
            if breadth < self.breadth_min:
                regime = MarketRegime.RISK_ON
                reason = f"CSI300 golden cross but breadth({breadth*100:.1f}%) < {self.breadth_min*100:.1f}%"
            else:
                regime = MarketRegime.STRONG_RISK_ON
                reason = f"CSI300 strong bullish + breadth({breadth*100:.1f}%) confirmed"

        audit = RiskAuditResult(
            audit_id=audit_id,
            timestamp=now_iso,
            account_id="global",
            layer=RiskLayer.MARKET,
            rule_name="MARKET_REGIME_CLASSIFICATION",
            passed=True,
            severity=RiskSeverity.INFO,
            action_taken=f"SET_REGIME_{regime.value}",
            message=reason,
            details={
                "close": c,
                "ma20": round(ma20, 2),
                "ma60": round(ma60, 2),
                "ma120": round(ma120, 2),
                "breadth": round(breadth, 4)
            }
        )
        return regime, breadth, audit

    def check_risk_off_entry_gate(
        self,
        symbol: str,
        price: float,
        bars: List[Dict[str, Any]],
        sector: str,
        top_sectors: List[str],
        sector_scores: Optional[Dict[str, float]] = None,
        ret20: float = 0.0,
        ret60: float = 0.0,
        amount: float = 0.0
    ) -> RiskAuditResult:
        """
        Evaluates strict multi-factor entry criteria during RISK_OFF:
        1. Price > MA60 and Price > MA120
        2. Positive 20-day and 60-day momentum (ret20 > 0 and ret60 > 0)
        3. Sector in Top 4 leading industries
        4. Sector momentum score >= 0
        5. Minimum turnover liquidity >= 30,000,000 RMB
        """
        audit_id = f"RA_ENTRY_{uuid.uuid4().hex[:10]}"
        now_iso = datetime.datetime.now().isoformat()

        if price <= 0:
            return RiskAuditResult(
                audit_id=audit_id, timestamp=now_iso, account_id="global",
                layer=RiskLayer.MARKET, rule_name="RISK_OFF_ENTRY_GATE",
                passed=False, severity=RiskSeverity.BLOCK, action_taken="REJECT_ENTRY",
                message=f"Invalid stock price: {price}", details={"symbol": symbol}
            )

        if not bars or len(bars) < 120:
            return RiskAuditResult(
                audit_id=audit_id, timestamp=now_iso, account_id="global",
                layer=RiskLayer.MARKET, rule_name="RISK_OFF_ENTRY_GATE",
                passed=False, severity=RiskSeverity.BLOCK, action_taken="REJECT_ENTRY",
                message="Insufficient history for MA120 calculation (<120 bars)",
                details={"symbol": symbol, "bars": len(bars) if bars else 0}
            )

        ma60 = sum(float(b["close"]) for b in bars[-60:]) / 60.0
        ma120 = sum(float(b["close"]) for b in bars[-120:]) / 120.0

        if price <= ma60:
            return RiskAuditResult(
                audit_id=audit_id, timestamp=now_iso, account_id="global",
                layer=RiskLayer.MARKET, rule_name="RISK_OFF_ENTRY_GATE",
                passed=False, severity=RiskSeverity.BLOCK, action_taken="REJECT_ENTRY",
                message=f"Price ¥{price:.2f} <= MA60(¥{ma60:.2f})",
                details={"symbol": symbol, "price": price, "ma60": ma60}
            )

        if price <= ma120:
            return RiskAuditResult(
                audit_id=audit_id, timestamp=now_iso, account_id="global",
                layer=RiskLayer.MARKET, rule_name="RISK_OFF_ENTRY_GATE",
                passed=False, severity=RiskSeverity.BLOCK, action_taken="REJECT_ENTRY",
                message=f"Price ¥{price:.2f} <= MA120(¥{ma120:.2f})",
                details={"symbol": symbol, "price": price, "ma120": ma120}
            )

        if ret20 <= 0 or ret60 <= 0:
            return RiskAuditResult(
                audit_id=audit_id, timestamp=now_iso, account_id="global",
                layer=RiskLayer.MARKET, rule_name="RISK_OFF_ENTRY_GATE",
                passed=False, severity=RiskSeverity.BLOCK, action_taken="REJECT_ENTRY",
                message=f"Momentum non-positive: ret20={ret20:.2f}%, ret60={ret60:.2f}%",
                details={"symbol": symbol, "ret20": ret20, "ret60": ret60}
            )

        if top_sectors and sector not in top_sectors:
            return RiskAuditResult(
                audit_id=audit_id, timestamp=now_iso, account_id="global",
                layer=RiskLayer.MARKET, rule_name="RISK_OFF_ENTRY_GATE",
                passed=False, severity=RiskSeverity.BLOCK, action_taken="REJECT_ENTRY",
                message=f"Sector [{sector}] not in Top 4 leading sectors {top_sectors}",
                details={"symbol": symbol, "sector": sector}
            )

        if sector_scores and sector in sector_scores and sector_scores[sector] < 0:
            return RiskAuditResult(
                audit_id=audit_id, timestamp=now_iso, account_id="global",
                layer=RiskLayer.MARKET, rule_name="RISK_OFF_ENTRY_GATE",
                passed=False, severity=RiskSeverity.BLOCK, action_taken="REJECT_ENTRY",
                message=f"Sector score ({sector_scores[sector]:.2f}) is negative",
                details={"symbol": symbol, "sector": sector}
            )

        if amount > 0 and amount < 30_000_000.0:
            return RiskAuditResult(
                audit_id=audit_id, timestamp=now_iso, account_id="global",
                layer=RiskLayer.MARKET, rule_name="RISK_OFF_ENTRY_GATE",
                passed=False, severity=RiskSeverity.BLOCK, action_taken="REJECT_ENTRY",
                message=f"Turnover ¥{amount:,.0f} < 30,000,000 RMB liquidity floor",
                details={"symbol": symbol, "amount": amount}
            )

        return RiskAuditResult(
            audit_id=audit_id, timestamp=now_iso, account_id="global",
            layer=RiskLayer.MARKET, rule_name="RISK_OFF_ENTRY_GATE",
            passed=True, severity=RiskSeverity.INFO, action_taken="ALLOW_ENTRY",
            message="Passed all RISK_OFF strict entry filters",
            details={"symbol": symbol, "sector": sector, "price": price}
        )
