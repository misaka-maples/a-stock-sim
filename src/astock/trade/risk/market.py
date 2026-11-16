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
        candidate_bars: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        market_breadth: Optional[float] = None
    ) -> Tuple[MarketRegime, Optional[float], RiskAuditResult]:
        """
        Determines market regime based on CSI 300 benchmark moving averages (MA120, MA60, MA20)
        and breadth confirmation.
        Strictly truncates bars up to as_of (zero lookahead bias).
        """
        audit_id = f"RA_MKT_{uuid.uuid4().hex[:10]}"
        now_iso = datetime.datetime.now().isoformat()

        # Strict lookahead protection: filter benchmark bars up to as_of
        filtered_bench = [b for b in bench_bars if str(b.get("date", ""))[:10] <= as_of] if bench_bars else []

        if not filtered_bench or len(filtered_bench) < 120:
            res = RiskAuditResult(
                audit_id=audit_id,
                timestamp=now_iso,
                account_id="global",
                layer=RiskLayer.MARKET,
                rule_name="BENCHMARK_SUFFICIENT_HISTORY",
                passed=False,
                severity=RiskSeverity.WARNING,
                action_taken="DEFAULT_RISK_OFF",
                message=f"Insufficient benchmark bars up to {as_of} (<120), falling back to RISK_OFF",
                details={"bars_count": len(filtered_bench), "as_of": as_of}
            )
            return MarketRegime.RISK_OFF, None, res

        c = float(filtered_bench[-1]["close"])
        ma120 = sum(float(b["close"]) for b in filtered_bench[-120:]) / 120.0
        ma60 = sum(float(b["close"]) for b in filtered_bench[-60:]) / 60.0
        ma20 = sum(float(b["close"]) for b in filtered_bench[-20:]) / 20.0

        # Calculate or extract market breadth (NEVER default to 1.0)
        breadth: Optional[float] = None
        if market_breadth is not None:
            breadth = float(market_breadth)
        elif candidate_bars:
            above_120 = 0
            total_cand = 0
            for sym, bars in candidate_bars.items():
                sym_sub = [b for b in bars if str(b.get("date", ""))[:10] <= as_of]
                if len(sym_sub) >= 120:
                    total_cand += 1
                    b_c = float(sym_sub[-1]["close"])
                    b_ma120 = sum(float(b["close"]) for b in sym_sub[-120:]) / 120.0
                    if b_c > b_ma120:
                        above_120 += 1
            if total_cand > 0:
                breadth = round(above_120 / total_cand, 4)

        # Classify regime based on Moving Averages & Breadth
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
            # Bullish golden cross (c > ma20 and ma20 > ma60 > ma120)
            if breadth is None:
                regime = MarketRegime.RISK_ON
                reason = "CSI300 golden cross, but market breadth data is missing; cannot confirm STRONG_RISK_ON"
            elif breadth < self.breadth_min:
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
                "as_of": as_of,
                "close": c,
                "ma20": round(ma20, 2),
                "ma60": round(ma60, 2),
                "ma120": round(ma120, 2),
                "breadth": breadth,
                "missing_breadth": breadth is None
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
        amount: float = 0.0,
        signal_asof: Optional[str] = None,
        current_asof: Optional[str] = None
    ) -> RiskAuditResult:
        """
        Evaluates strict multi-factor entry criteria during RISK_OFF:
        1. Signal as_of matches current trade date
        2. Price > MA60 and Price > MA120 (with zero lookahead bias)
        3. Positive 20-day and 60-day momentum (ret20 > 0 and ret60 > 0)
        4. Sector in Top 4 leading industries (missing top_sectors rejects entry)
        5. Sector momentum score >= 0
        6. Minimum turnover liquidity >= 30,000,000 RMB (missing amount rejects entry)
        """
        audit_id = f"RA_ENTRY_{uuid.uuid4().hex[:10]}"
        now_iso = datetime.datetime.now().isoformat()

        # 1. Date consistency check
        if signal_asof and current_asof and signal_asof != current_asof:
            return RiskAuditResult(
                audit_id=audit_id, timestamp=now_iso, account_id="global",
                layer=RiskLayer.MARKET, rule_name="RISK_OFF_ENTRY_GATE",
                passed=False, severity=RiskSeverity.BLOCK, action_taken="REJECT_ENTRY",
                message=f"Signal date {signal_asof} does not match current trade date {current_asof}",
                details={"symbol": symbol, "signal_asof": signal_asof, "current_asof": current_asof}
            )

        if price <= 0:
            return RiskAuditResult(
                audit_id=audit_id, timestamp=now_iso, account_id="global",
                layer=RiskLayer.MARKET, rule_name="RISK_OFF_ENTRY_GATE",
                passed=False, severity=RiskSeverity.BLOCK, action_taken="REJECT_ENTRY",
                message=f"Invalid stock price: {price}", details={"symbol": symbol}
            )

        # 2. Strict lookahead truncation
        cutoff_date = current_asof or signal_asof
        filtered_bars = [b for b in bars if str(b.get("date", ""))[:10] <= cutoff_date] if (bars and cutoff_date) else (bars or [])

        if not filtered_bars or len(filtered_bars) < 120:
            return RiskAuditResult(
                audit_id=audit_id, timestamp=now_iso, account_id="global",
                layer=RiskLayer.MARKET, rule_name="RISK_OFF_ENTRY_GATE",
                passed=False, severity=RiskSeverity.BLOCK, action_taken="REJECT_ENTRY",
                message="Insufficient history for MA120 calculation (<120 bars)",
                details={"symbol": symbol, "bars": len(filtered_bars) if filtered_bars else 0}
            )

        ma60 = sum(float(b["close"]) for b in filtered_bars[-60:]) / 60.0
        ma120 = sum(float(b["close"]) for b in filtered_bars[-120:]) / 120.0

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

        # 3. Top sectors check: missing top_sectors must NOT pass
        if not top_sectors or sector not in top_sectors:
            return RiskAuditResult(
                audit_id=audit_id, timestamp=now_iso, account_id="global",
                layer=RiskLayer.MARKET, rule_name="RISK_OFF_ENTRY_GATE",
                passed=False, severity=RiskSeverity.BLOCK, action_taken="REJECT_ENTRY",
                message=f"Sector [{sector}] not in valid Top leading sectors {top_sectors or []}",
                details={"symbol": symbol, "sector": sector, "top_sectors": top_sectors or []}
            )

        if sector_scores and sector in sector_scores and sector_scores[sector] < 0:
            return RiskAuditResult(
                audit_id=audit_id, timestamp=now_iso, account_id="global",
                layer=RiskLayer.MARKET, rule_name="RISK_OFF_ENTRY_GATE",
                passed=False, severity=RiskSeverity.BLOCK, action_taken="REJECT_ENTRY",
                message=f"Sector score ({sector_scores[sector]:.2f}) is negative",
                details={"symbol": symbol, "sector": sector}
            )

        # 4. Turnover amount check: missing or < 30M must NOT pass
        if amount < 30_000_000.0:
            return RiskAuditResult(
                audit_id=audit_id, timestamp=now_iso, account_id="global",
                layer=RiskLayer.MARKET, rule_name="RISK_OFF_ENTRY_GATE",
                passed=False, severity=RiskSeverity.BLOCK, action_taken="REJECT_ENTRY",
                message=f"Turnover ¥{amount:,.0f} < 30,000,000 RMB liquidity floor (or missing turnover)",
                details={"symbol": symbol, "amount": amount}
            )

        return RiskAuditResult(
            audit_id=audit_id, timestamp=now_iso, account_id="global",
            layer=RiskLayer.MARKET, rule_name="RISK_OFF_ENTRY_GATE",
            passed=True, severity=RiskSeverity.INFO, action_taken="ALLOW_ENTRY",
            message="Passed all RISK_OFF strict entry filters",
            details={"symbol": symbol, "sector": sector, "price": price, "amount": amount}
        )
