# -*- coding: utf-8 -*-
"""
A-Stock Unified Trading Runtime
Coordinates DataService -> MarketSnapshot -> StrategyEngine -> TargetPortfolio
-> PortfolioManager -> RiskManager -> OrderManager -> SimBrokerAdapter
-> TradeFill -> AccountLedger (SQLite ACID) -> Web API / Monitoring.
Supports Shadow Mode, Dual Accounts (Balanced & Aggressive), and rollback.
"""

import threading
import logging
import datetime
import json
import uuid
import math
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

from .models import (
    MarketRegime, Order, TradeFill, Position, AccountBalance, OrderSide,
    OrderType, OrderStatus, TargetPortfolio, Signal, RiskAuditResult,
    RiskLayer, RiskSeverity
)
from .security import SecurityIdentifier, AssetType
from .ledger.sqlite_ledger import SQLiteAccountLedger
from .order.manager import OrderManager
from .broker.sim_broker import SimulatedBrokerAdapter
from .risk.manager import RiskManager
from .risk.market import MarketRiskRule
from .risk.portfolio import PortfolioRiskRule
from .risk.position import PositionRiskRule
from .risk.order import OrderRiskRule
from .portfolio.manager import PortfolioManager
from .strategy.balanced import BalancedStrategy
from .strategy.aggressive import AggressiveStrategy
from .config import get_db_path, get_audit_path
from .market_inputs import nontrading_on_execution

logger = logging.getLogger("TradingRuntime")


def _bar_day(bar):
    return str(bar.get("date", bar.get("trade_date", "")))[:10]


def _signal_entry_rejection(signal, signal_day, market_data):
    """Check actual per-stock evidence in every regime, before allocating cash."""
    snapshot = market_data.get("snapshot")
    lineage = getattr(snapshot, "source_lineage", {}) or {}
    excluded = market_data.get("excluded_symbols", lineage.get("excluded_symbols", [])) or []
    if isinstance(excluded, dict):
        excluded = set(excluded)
    else:
        excluded = {row.get("symbol") if isinstance(row, dict) else row for row in excluded}
    if signal.symbol in excluded:
        return "symbol_quarantined_in_snapshot"
    nontrading = (lineage.get("nontrading_statuses") or {}).get(signal.symbol)
    status = nontrading.get("status") if isinstance(nontrading, dict) else nontrading
    if str(getattr(status, "value", status) or "").upper() in ("SUSPENDED", "DELISTED"):
        return "symbol_verified_nontrading"
    if signal.signal_asof != signal_day:
        return "signal_date_mismatch"
    history = market_data.get("stock_bars") or market_data.get("candidate_bars") or {}
    dated = [bar for bar in (history.get(signal.symbol, []) or []) if _bar_day(bar) <= signal_day]
    if not dated:
        return "symbol_history_missing"
    latest = max(dated, key=_bar_day)
    if _bar_day(latest) != signal_day:
        return "symbol_history_stale"
    if latest.get("is_final") is False:
        return "symbol_daily_bar_unsettled"
    if latest.get("is_suspended") is True:
        return "symbol_suspended"
    volume = latest.get("volume")
    try:
        if volume is not None and (not math.isfinite(float(volume)) or float(volume) <= 0):
            return "symbol_has_no_traded_volume"
    except (TypeError, ValueError, OverflowError):
        return "symbol_invalid_volume"
    st = latest.get("is_st")
    name = str(latest.get("name") or signal.name or "").upper()
    # Preserve the existing dated-flag/name policy; unknown is not invented False.
    if st is True or (st is not False and "ST" in name) or "退" in name or name.startswith("PT"):
        return "symbol_risk_warning"
    return None


def _live_exit_price(quote, execution_day, now=None):
    """Never turn a missing or explicitly stale mark into a market sell."""
    value = quote.get("price") or quote.get("current") or quote.get("close")
    try:
        price = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if isinstance(value, bool) or not math.isfinite(price) or price <= 0:
        return None
    if (quote.get("fresh") is False or quote.get("is_stale") is True
            or quote.get("is_suspended") is True):
        return None
    if 'open' in quote and 'volume_lots' in quote:
        try:
            if float(quote['open']) <= 0 and float(quote['volume_lots']) <= 0:
                return None
        except (ValueError, TypeError, OverflowError):
            return None
    stamp = quote.get("timestamp") or quote.get("time") or quote.get("quote_time")
    if not stamp:
        # Server-produced freshness is already checked against a timestamp.
        return price if quote.get("fresh") is True else None
    try:
        from trade_calendar import BEIJING
        stamp = str(stamp)
        observed = (datetime.datetime.strptime(stamp, "%Y%m%d%H%M%S") if len(stamp) == 14 and stamp.isdigit()
                    else datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")))
        observed = observed.replace(tzinfo=BEIJING) if observed.tzinfo is None else observed.astimezone(BEIJING)
        if observed.date().isoformat() != execution_day:
            return None
        if now is not None:
            current = now.replace(tzinfo=BEIJING) if now.tzinfo is None else now.astimezone(BEIJING)
            if current.date().isoformat() == execution_day and not 0 <= (current - observed).total_seconds() <= 120:
                return None
    except (ValueError, TypeError, OverflowError):
        return None
    return price


def _entry_quote_rejection(signal, quote, policy, execution_day, now):
    price = _live_exit_price(quote, execution_day, now)
    if price is None:
        return 'entry_live_quote_missing_or_stale'
    name = str(quote.get('name') or signal.name or '').upper()
    if quote.get('is_st') is True or 'ST' in name or '退' in name:
        return 'entry_live_risk_warning'
    try:
        if ('open' in quote and 'volume_lots' in quote and
                float(quote['open']) <= 0 and float(quote['volume_lots']) <= 0):
            return 'entry_no_live_trading'
        from strategy_rules import entry_guard_reason
        from types import SimpleNamespace
        adjusted = float(signal.features.get('signal_price_adjusted') or 0)
        factor = adjusted / signal.price if signal.price > 0 else 0
        return entry_guard_reason(
            {'raw_signal_price': signal.price, 'adjustment_factor': factor, 'ma20': signal.ma20},
            SimpleNamespace(**policy),
            quote.get('raw_pre_close') or quote.get('pre_close'), quote.get('open'), price)
    except (ValueError, TypeError, ZeroDivisionError, OverflowError):
        return 'entry_unverified_prices'


class TradingRuntime:
    """Unified trading runtime executing dual accounts over SQLite ledger."""

    def __init__(
        self,
        db_path: Optional[str] = None,
        mode: str = "SHADOW",  # "SHADOW" or "ACTIVE"
        initial_capital: float = 100000.0,
        data_scheduler: Optional[Any] = None
    ):
        self.db_path = Path(db_path) if db_path is not None else get_db_path()
        self.mode = mode.upper()
        self.lock = threading.Lock()
        self.last_step_time: Optional[datetime.datetime] = None
        self.last_trade_date: Optional[str] = None
        self.data_scheduler = data_scheduler
        self.last_sync_completed_time: Optional[str] = None
        self.execution_policies = {}

        # 1. Transactional SQLite Ledger (authoritative state of record)
        self.ledger = SQLiteAccountLedger(str(self.db_path))
        self.ledger.init_account("balanced", "balanced", initial_capital)
        self.ledger.init_account("aggressive", "aggressive", initial_capital)

        # 2. Broker & Order Management
        # Connect Broker directly to SQLite ledger so there is NEVER a dual memory state
        self.broker = SimulatedBrokerAdapter(ledger=self.ledger, slippage=0.001)
        self.order_manager = OrderManager()
        # Restore active and past orders from SQLite ledger
        self.order_manager.restore_from_ledger(self.ledger)

        # 3. Four-layer Risk Managers (one for each account context)
        self.risk_manager_bal = RiskManager(account_type="balanced")
        self.risk_manager_agg = RiskManager(account_type="aggressive")

        # 4. Portfolio Managers (reads unified cash & positions from SQLite ledger)
        self.pm_bal = PortfolioManager(self.risk_manager_bal, self.order_manager, self.broker, ledger=self.ledger)
        self.pm_agg = PortfolioManager(self.risk_manager_agg, self.order_manager, self.broker, ledger=self.ledger)

        # 5. Strategies
        self.strategy_bal = BalancedStrategy(account_id="balanced")
        self.strategy_agg = AggressiveStrategy(account_id="aggressive")

        # Runtime shadow state
        self.shadow_decisions: Dict[str, Any] = {
            "balanced": {},
            "aggressive": {},
            "timestamp": None
        }
        self.last_regime: Optional[MarketRegime] = None

        logger.info(f"TradingRuntime initialized in {self.mode} mode. SQLite DB: {self.db_path}")

    def set_mode(self, mode: str):
        """Switch between SHADOW and ACTIVE."""
        with self.lock:
            self.mode = mode.upper()
            logger.info(f"TradingRuntime mode switched to {self.mode}")

    def configure_execution_policies(self, policies):
        """Wire the saved account profiles without changing their parameters."""
        for account_id, manager in [('balanced', self.risk_manager_bal),
                                    ('aggressive', self.risk_manager_agg)]:
            policy = policies[account_id]
            manager.portfolio_rule.alert_drawdown_pct = policy['max_drawdown_warn_pct']
            manager.portfolio_rule.freeze_drawdown_pct = policy['max_drawdown_freeze_pct']
            manager.position_rule.hard_stop_loss_pct = policy['stop_loss_pct']
            manager.position_rule.atr_multiplier = policy['atr_trailing_multiplier']
        self.execution_policies = policies

    def step(
        self,
        as_of: str,
        quotes: Optional[Dict[str, Dict[str, Any]]] = None,
        market_data: Optional[Dict[str, Any]] = None,
        now: Optional[datetime.datetime] = None,
        signal_asof_date: Optional[str] = None,
        snapshot: Optional[Any] = None,
        data_scheduler_result: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Executes one full synchronous trading step across both accounts:
        1. Lock check (prevents concurrent duplicate runs)
        2. Mark-to-market prices in ledger
        3. T+1 rollover if trade date changed
        4. Detect Market Regime with real market breadth & zero lookahead bias
        5. Run Balanced Strategy -> Plan -> Risk -> Order -> Execute
        6. Run Aggressive Strategy -> Plan -> Risk -> Order -> Execute
        7. Audit Trail and Record Strategy Runs
        """
        # Non-blocking lock to guarantee no concurrent double execution
        acquired = self.lock.acquire(blocking=False)
        if not acquired:
            logger.warning("Trading step skipped: another step is currently executing")
            return {"status": "SKIPPED_CONCURRENT"}

        try:
            now = now or datetime.datetime.now()
            self.last_step_time = now
            market_data = dict(market_data or {})
            quotes = quotes or {}
            policies = market_data.get('execution_policy') or self.execution_policies
            if policies:
                self.configure_execution_policies(policies)
                market_data['execution_policy'] = policies
            if snapshot is None:
                snapshot = market_data.get("snapshot")
            if data_scheduler_result is None:
                data_scheduler_result = market_data.get("data_scheduler_result") or market_data.get("scheduler_result")

            # 1. Update Market Prices across positions in SQLite
            self.ledger.update_market_prices(quotes)

            # 2. Daily T+1 Rollover (based on execution trade date)
            if self.last_trade_date != as_of:
                self.ledger.roll_t1(as_of)
                if hasattr(self.broker, "roll_trading_day"):
                    self.broker.roll_trading_day()
                self.last_trade_date = as_of

            # 3. Market Regime Classification (consuming pre-verified MarketSnapshot or calculating via MarketRiskRule)
            snapshot = snapshot or market_data.get("snapshot")
            if snapshot is not None:
                market_data["snapshot"] = snapshot
            csi300_bars = market_data.get("csi300_bars", [])
            cand_bars = market_data.get("stock_bars") or market_data.get("candidate_bars")
            m_breadth = market_data.get("breadth", market_data.get("market_breadth"))
            cov_ratio = market_data.get("coverage_ratio")
            if cov_ratio is None:
                cov_ratio = (getattr(snapshot, "source_lineage", {}) or {}).get("coverage_ratio")

            # Determine signal_asof vs execution_date
            signal_asof = (
                signal_asof_date or
                getattr(snapshot, "signal_asof_date", None) or
                market_data.get("signal_asof_date") or
                as_of
            )

            if snapshot and hasattr(snapshot, "market_regime") and snapshot.market_regime:
                # Direct consumption of authoritative MarketSnapshot from DataService
                regime = snapshot.market_regime
                breadth = getattr(snapshot, "market_breadth", None)
                snap_bench = getattr(snapshot, "benchmark_bars", None)
                if snap_bench and not csi300_bars:
                    csi300_bars = [
                        {"date": getattr(b, "date", getattr(b, "trade_date", "")), "close": b.close}
                        for b in snap_bench
                    ]
                    # Both accounts must validate the same authoritative bars
                    # used for the shared market-risk audit.
                    market_data["csi300_bars"] = csi300_bars
                _, _, mkt_audit = self.risk_manager_bal.market_rule.determine_regime(
                    bench_bars=csi300_bars,
                    as_of=signal_asof,
                    candidate_bars=cand_bars,
                    market_breadth=breadth,
                    coverage_ratio=cov_ratio
                )
            else:
                regime, breadth, mkt_audit = self.risk_manager_bal.market_rule.determine_regime(
                    bench_bars=csi300_bars,
                    as_of=signal_asof,
                    candidate_bars=cand_bars,
                    market_breadth=m_breadth,
                    coverage_ratio=cov_ratio
                )
            self.ledger.record_risk_event(mkt_audit)
            self.last_regime = regime
            self.last_trade_date = as_of

            step_summary = {
                "as_of": as_of,
                "execution_date": as_of,
                "signal_asof_date": signal_asof,
                "mode": self.mode,
                "regime": regime.value,
                "breadth": breadth,
                "market_risk_audit": mkt_audit.to_dict(),
                "balanced": {},
                "aggressive": {}
            }

            # 4. Execute Step for Balanced Strategy
            res_bal = self._process_account_step(
                account_id="balanced",
                strategy=self.strategy_bal,
                pm=self.pm_bal,
                risk_mgr=self.risk_manager_bal,
                regime=regime,
                breadth=breadth,
                as_of=as_of,
                quotes=quotes,
                market_data=market_data,
                market_risk_audit=mkt_audit,
                signal_asof_date=signal_asof,
                now=now
            )
            step_summary["balanced"] = res_bal

            # 5. Execute Step for Aggressive Strategy
            res_agg = self._process_account_step(
                account_id="aggressive",
                strategy=self.strategy_agg,
                pm=self.pm_agg,
                risk_mgr=self.risk_manager_agg,
                regime=regime,
                breadth=breadth,
                as_of=as_of,
                quotes=quotes,
                market_data=market_data,
                market_risk_audit=mkt_audit,
                signal_asof_date=signal_asof,
                now=now
            )
            step_summary["aggressive"] = res_agg
            self.last_step_summary = step_summary

            if self.mode == "ACTIVE":
                try:
                    from .daily_audit_recorder import DailyAuditRecorder
                    rec_path = get_audit_path()
                    b_exec = res_bal.get("executed_orders", 0)
                    a_exec = res_agg.get("executed_orders", 0)
                    b_rej = res_bal.get("rejected_orders", [])
                    a_rej = res_agg.get("rejected_orders", [])

                    # Verified sync completed time
                    verified_sync = (
                        market_data.get("sync_completed_at") or
                        (data_scheduler_result.get("completed_at") if data_scheduler_result else None) or
                        self.last_sync_completed_time or
                        ""
                    )

                    DailyAuditRecorder(log_path=rec_path).capture_from_runtime(
                        runtime=self,
                        trade_date=as_of,
                        market_regime=regime.value,
                        sync_completed_time=verified_sync,
                        orders_generated=b_exec + len(b_rej) + a_exec + len(a_rej),
                        orders_executed=b_exec + a_exec,
                        orders_rejected=b_rej + a_rej,
                        error_summary="None",
                        data_scheduler_result=data_scheduler_result
                    )
                except Exception as _e:
                    logger.warning(f"DailyAuditRecorder step capture error: {_e}")

            return step_summary

        finally:
            self.lock.release()

    def _process_account_step(
        self,
        account_id: str,
        strategy: Any,
        pm: PortfolioManager,
        risk_mgr: RiskManager,
        regime: MarketRegime,
        as_of: str,
        quotes: Dict[str, Dict[str, Any]],
        market_data: Dict[str, Any],
        breadth: Optional[float] = None,
        market_risk_audit: Optional[RiskAuditResult] = None,
        signal_asof_date: Optional[str] = None,
        now: Optional[datetime.datetime] = None
    ) -> Dict[str, Any]:
        """Processes target planning, risk screening, and execution for one account."""
        bal = self.ledger.get_balance(account_id)
        positions = self.ledger.get_positions(account_id)

        def resolve_holding_industries(current):
            mapping = market_data.get('industry_map') or {}
            for symbol, position in current.items():
                if position.sector in ('', '其他', '未知行业') and mapping.get(symbol):
                    position.sector = mapping[symbol]
            return current

        positions = resolve_holding_industries(positions)

        sig_date = signal_asof_date or as_of
        policy = (market_data.get('execution_policy') or {}).get(account_id)

        # 1. Generate Signals (using signal_asof_date)
        signals = strategy.generate_signals(sig_date, market_data, regime)

        # 2. Check essential market data availability and market risk audit
        csi_bars = market_data.get("csi300_bars", [])
        filtered_bench_bars = [b for b in csi_bars if str(b.get("date", b.get("trade_date", "")))[:10] <= sig_date] if csi_bars else []
        missing_market_data = (not filtered_bench_bars or len(filtered_bench_bars) < 120
                               or _bar_day(filtered_bench_bars[-1]) != sig_date)
        try:
            from trade_calendar import get_previous_trading_day
            if sig_date not in (as_of, get_previous_trading_day(as_of).isoformat()):
                missing_market_data = True
        except (ValueError, TypeError):
            missing_market_data = True

        snapshot = market_data.get("snapshot")
        snapshot_quality = getattr(snapshot, "quality", None)
        snapshot_quality = getattr(snapshot_quality, "value", snapshot_quality)
        if snapshot_quality is not None and snapshot_quality != "VALID":
            missing_market_data = True
        if (getattr(snapshot, "source_lineage", {}) or {}).get("trading_ready") is False:
            missing_market_data = True

        # Market risk audit failure (missing breadth on golden cross, expired benchmark, coverage < 60%)
        mkt_audit_failed = False
        if market_risk_audit and not market_risk_audit.passed:
            mkt_audit_failed = True
            missing_market_data = True

        if missing_market_data and not mkt_audit_failed:
            mkt_audit_res = RiskAuditResult(
                audit_id=f"RA_MD_{uuid.uuid4().hex[:10]}",
                timestamp=datetime.datetime.now().isoformat(),
                account_id=account_id,
                layer=RiskLayer.MARKET,
                rule_name="ESSENTIAL_MARKET_DATA_INTEGRITY",
                passed=False,
                severity=RiskSeverity.WARNING,
                action_taken="BLOCK_NEW_BUYS",
                message=f"Essential benchmark/snapshot evidence is missing, stale or unaccepted for {sig_date}; blocking new buys.",
                details={"as_of": sig_date, "bench_bars_count": len(filtered_bench_bars),
                         "snapshot_quality": snapshot_quality}
            )
            self.ledger.record_risk_event(mkt_audit_res)

        # 3. Risk Off Entry Gate Check (Market Level)
        allowed_signals = []
        top_sectors = market_data.get("top_sectors", [])
        for s in signals:
            rejection = _signal_entry_rejection(s, sig_date, market_data)
            if rejection:
                self.ledger.record_risk_event(RiskAuditResult(
                    audit_id=f"RA_STOCK_{uuid.uuid4().hex[:10]}",
                    timestamp=datetime.datetime.now().isoformat(), account_id=account_id,
                    layer=RiskLayer.MARKET, rule_name="SYMBOL_ENTRY_DATA_INTEGRITY",
                    passed=False, severity=RiskSeverity.BLOCK, action_taken="REJECT_SYMBOL_ENTRY",
                    message=rejection, details={"symbol": s.symbol, "signal_asof": s.signal_asof,
                                                 "required_signal_date": sig_date}))
                continue
            if policy and self.mode == 'ACTIVE':
                rejection = _entry_quote_rejection(s, quotes.get(s.symbol, {}), policy, as_of, now)
                if rejection:
                    self.ledger.record_risk_event(RiskAuditResult(
                        audit_id=f'RA_ENTRY_QUOTE_{uuid.uuid4().hex[:10]}',
                        timestamp=datetime.datetime.now().isoformat(), account_id=account_id,
                        layer=RiskLayer.ORDER, rule_name='ENTRY_LIVE_QUOTE_INTEGRITY',
                        passed=False, severity=RiskSeverity.BLOCK, action_taken='REJECT_SYMBOL_ENTRY',
                        message=rejection, details={'symbol': s.symbol, 'execution_date': as_of}))
                    continue
            if regime == MarketRegime.RISK_OFF:
                history = market_data.get("stock_bars") or market_data.get("candidate_bars") or {}
                bars = history.get(s.symbol, [])
                real_amt = getattr(s, "amount", 0.0)
                if real_amt <= 0:
                    q = quotes.get(s.symbol, {})
                    real_amt = float(q.get("amount", q.get("turnover", 0.0)))

                audit = risk_mgr.market_rule.check_risk_off_entry_gate(
                    symbol=s.symbol,
                    price=s.features.get("signal_price_adjusted", s.price),
                    bars=bars,
                    sector=s.sector,
                    top_sectors=top_sectors,
                    sector_scores=market_data.get("sector_scores"),
                    ret20=s.ret20,
                    ret60=s.ret60,
                    amount=real_amt,
                    signal_asof=getattr(s, "signal_asof", sig_date),
                    current_asof=as_of
                )
                self.ledger.record_risk_event(audit)
                if not audit.passed:
                    continue
            allowed_signals.append(s)

        # 4. Check Drawdown Circuit Breaker
        is_alert, is_freeze, dd_audit = risk_mgr.portfolio_rule.check_drawdown_circuit_breaker(bal)
        self.ledger.record_risk_event(dd_audit)

        # If essential data missing, market audit failed, or circuit breaker active: freeze new buy allocations
        is_freeze = is_freeze or missing_market_data or mkt_audit_failed

        # 5. Position Risk Exits (-6% hard stop, trailing ATR, MA20 breakdown) executed FIRST
        if self.mode == "ACTIVE":
            for sym, pos in list(positions.items()):
                lineage = getattr(market_data.get("snapshot"), "source_lineage", {}) or {}
                nontrading = (lineage.get("nontrading_statuses") or {}).get(sym)
                status = nontrading.get("status") if isinstance(nontrading, dict) else nontrading
                status = str(getattr(status, "value", status) or "").upper()
                if nontrading_on_execution(nontrading, as_of):
                    self.ledger.record_risk_event(RiskAuditResult(
                        audit_id=f"RA_EXIT_STATUS_{uuid.uuid4().hex[:10]}",
                        timestamp=datetime.datetime.now().isoformat(), account_id=account_id,
                        layer=RiskLayer.POSITION, rule_name="POSITION_NONTRADING_STATUS",
                        passed=False, severity=RiskSeverity.WARNING, action_taken="HOLD_NONTRADABLE_SYMBOL",
                        message="Verified nontrading status; do not sell on a residual quote",
                        details={"symbol": sym, "status": status}))
                    continue
                q = quotes.get(sym, {})
                price = _live_exit_price(q, as_of, now)
                if price is None:
                    self.ledger.record_risk_event(RiskAuditResult(
                        audit_id=f"RA_EXIT_DATA_{uuid.uuid4().hex[:10]}",
                        timestamp=datetime.datetime.now().isoformat(), account_id=account_id,
                        layer=RiskLayer.POSITION, rule_name="POSITION_QUOTE_FRESHNESS",
                        passed=False, severity=RiskSeverity.WARNING, action_taken="HOLD_UNVERIFIED_PRICE",
                        message="Missing/stale live quote; no market sell based on cached position mark",
                        details={"symbol": sym, "execution_date": as_of}))
                    continue
                supplied = (market_data.get('position_indicators') or {}).get(sym, {})
                if supplied.get('signal_asof') != sig_date:
                    supplied = {}
                def indicator(key):
                    try:
                        value = float(supplied.get(key, q.get(key)) or 0.0)
                        return value if math.isfinite(value) and value > 0 else 0.0
                    except (TypeError, ValueError, OverflowError):
                        return 0.0
                atr, ma20 = indicator("atr"), indicator("ma20")
                if policy and policy['ma20_exit_consecutive_days'] == 1 and supplied.get('ma20_previous19_sum'):
                    ma20 = (supplied['ma20_previous19_sum'] + price) / 20
                if not atr or not ma20:
                    self.ledger.record_risk_event(RiskAuditResult(
                        audit_id=f"RA_EXIT_IND_{uuid.uuid4().hex[:10]}",
                        timestamp=datetime.datetime.now().isoformat(), account_id=account_id,
                        layer=RiskLayer.POSITION, rule_name="POSITION_INDICATOR_AVAILABILITY",
                        passed=False, severity=RiskSeverity.WARNING, action_taken="HARD_STOP_REMAINS_ACTIVE",
                        message="Unavailable ATR/MA20-dependent exit rules disabled; hard stop remains active",
                        details={"symbol": sym, "atr_available": bool(atr), "ma20_available": bool(ma20)}))
                should_exit, exit_reason, audit = risk_mgr.position_rule.check_position_exit(
                    pos, price, atr=atr, ma20=ma20,
                    consecutive_below_ma20=int(supplied.get('consecutive_below_ma20') or 0),
                    policy=policy)
                self.ledger.record_risk_event(audit)
                if audit.details.get('trailing_stop_price', 0) > 0:
                    self.ledger.raise_trailing_stop(account_id, sym, audit.details['trailing_stop_price'])
                if should_exit and pos.tradable_shares > 0:
                    exit_order = Order(
                        order_id=f"ORD_EXIT_{sym}_{as_of}_{account_id[:3]}",
                        client_order_id=f"C_EXIT_{sym}_{as_of}_{account_id}",
                        account_id=account_id,
                        symbol=sym,
                        side=OrderSide.SELL,
                        order_type=OrderType.MARKET,
                        price=price,
                        shares=pos.tradable_shares,
                        status=OrderStatus.PROPOSED,
                        reason=f"RiskExit_{exit_reason}"
                    )
                    passed, order_audit = risk_mgr.validate_pre_trade_order(
                        exit_order, available_cash=bal.available_cash, current_position=pos,
                        prev_close=q.get('raw_pre_close') or q.get('pre_close'),
                        is_suspended=q.get('is_suspended', False), is_st=q.get('is_st', False))
                    self.ledger.record_risk_event(order_audit)
                    if not passed:
                        continue
                    self.order_manager.submit_order(exit_order)
                    self.ledger.save_order(exit_order)
                    fill = self.broker.execute_order(exit_order, execution_price=price, trade_date=as_of)
                    if fill:
                        if not getattr(self.broker, "ledger", None):
                            self.ledger.apply_fill(fill, name=pos.name, sector=pos.sector)

            # Refresh positions and balances after exits
            positions = resolve_holding_industries(self.ledger.get_positions(account_id))
            bal = self.ledger.get_balance(account_id)

        # 6. Plan Target Portfolio (with updated post-exit positions and released cash)
        target_pf = strategy.plan_targets(
            as_of=as_of,
            regime=regime,
            signals=allowed_signals,
            balance=bal,
            positions=positions,
            is_frozen=is_freeze
        )
        cycle_details = {}
        if policy and not is_freeze:
            from .execution_cycles import restrict_entry_cycle
            target_pf, cycle_details = restrict_entry_cycle(
                self.ledger, account_id, sig_date, as_of, target_pf, positions, policy,
                persist=self.mode == 'ACTIVE')

        # Save strategy run snapshot to SQLite
        run_id = f"RUN_{account_id.upper()}_{as_of}_{datetime.datetime.now().strftime('%H%M%S')}"
        self.ledger.record_strategy_run(
            run_id=run_id,
            account_id=account_id,
            strategy_name=strategy.name,
            as_of_date=as_of,
            regime=regime.value,
            target_portfolio=target_pf.to_dict(),
            status="COMPLETED"
        )

        # If in SHADOW mode: Record decision without submitting live simulation fills
        if self.mode == "SHADOW":
            self.shadow_decisions[account_id] = {
                "as_of": as_of,
                "regime": regime.value,
                "target_portfolio": target_pf.to_dict(),
                "positions_count": len(positions),
                "is_freeze": is_freeze,
                "total_equity": bal.total_equity
            }
            return {
                "mode": "SHADOW",
                "entry_risk_freeze": is_freeze,
                "missing_market_data": missing_market_data,
                "allowed_signal_count": len(allowed_signals),
                "execution_cycle": cycle_details,
                "target_positions": len(target_pf.target_positions),
                "total_target_weight": target_pf.total_weight,
                "cash_weight": target_pf.cash_weight,
                "executed_orders": 0
            }

        # B. Portfolio Rebalancing Buys
        price_map = {sym: float(q.get("price") or q.get("current") or q.get("close") or 0.0)
                     for sym, q in quotes.items()
                     if not policy or _live_exit_price(q, as_of, now) is not None}
        stock_info_map = {sym: {"pre_close": float(q.get("pre_close") or q.get("price") or 0.0), "is_suspended": q.get("is_suspended", False), "is_st": q.get("is_st", False)} for sym, q in quotes.items()}

        orders = pm.execute_rebalance(
            target_portfolio=target_pf,
            price_map=price_map,
            stock_info_map=stock_info_map
        )

        executed_fills = []
        for o in orders:
            signal = next((s for s in signals if s.symbol == o.symbol), None)
            o.name = quotes.get(o.symbol, {}).get('name') or (signal.name if signal else o.name)
            o.sector = signal.sector if signal else getattr(positions.get(o.symbol), 'sector', '其他')
            self.ledger.save_order(o)
            price = price_map.get(o.symbol, o.price)
            name = quotes.get(o.symbol, {}).get("name", o.symbol)
            sector = quotes.get(o.symbol, {}).get("sector", "其他")
            fill = self.broker.execute_order(o, execution_price=price, trade_date=as_of)
            if fill:
                if not getattr(self.broker, "ledger", None):
                    self.ledger.apply_fill(fill, name=name, sector=sector)
                executed_fills.append(fill)

        return {
            "mode": "ACTIVE",
            "entry_risk_freeze": is_freeze,
            "missing_market_data": missing_market_data,
            "allowed_signal_count": len(allowed_signals),
            "execution_cycle": cycle_details,
            "target_positions": len(target_pf.target_positions),
            "total_target_weight": target_pf.total_weight,
            "executed_orders": len(orders),
            "executed_fills": len(executed_fills)
        }

    # =========================================================================
    # Web API / UI / Legacy Compatibility Methods
    # =========================================================================

    def get_account_summary(self, account_id: str = "balanced") -> Dict[str, Any]:
        """Provides Web API compatible account summary."""
        return self.ledger.get_summary(account_id)

    def get_positions(self, account_id: str = "balanced") -> Dict[str, Dict[str, Any]]:
        """Provides Web API compatible positions map."""
        pos = self.ledger.get_positions(account_id)
        return {sym: p.to_dict() for sym, p in pos.items()}

    def get_trades(self, account_id: str = "balanced", limit: int = 100) -> List[Dict[str, Any]]:
        """Provides Web API compatible trades list."""
        return self.ledger.get_trades(account_id, limit=limit)

    def get_dual_accounts_summary(self) -> Dict[str, Any]:
        """Provides 18-dimension dual account summary for /api/strategy/dual_accounts."""
        from src.astock.trade.summary_builder import build_dual_accounts_summary
        return build_dual_accounts_summary(self)
