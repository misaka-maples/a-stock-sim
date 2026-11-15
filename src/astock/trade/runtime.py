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
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

from .models import (
    MarketRegime, Order, TradeFill, Position, AccountBalance, OrderSide,
    OrderType, OrderStatus, TargetPortfolio, Signal, RiskAuditResult
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

logger = logging.getLogger("TradingRuntime")


class TradingRuntime:
    """Unified trading runtime executing dual accounts over SQLite ledger."""

    def __init__(
        self,
        db_path: str = "data/trading_v3.sqlite",
        mode: str = "SHADOW",  # "SHADOW" or "ACTIVE"
        initial_capital: float = 100000.0
    ):
        self.db_path = Path(db_path)
        self.mode = mode.upper()
        self.lock = threading.Lock()
        self.last_step_time: Optional[datetime.datetime] = None
        self.last_trade_date: Optional[str] = None

        # 1. Transactional SQLite Ledger
        self.ledger = SQLiteAccountLedger(str(self.db_path))
        self.ledger.init_account("balanced", "balanced", initial_capital)
        self.ledger.init_account("aggressive", "aggressive", initial_capital)

        # 2. Broker & Order Management
        self.broker = SimulatedBrokerAdapter(initial_cash=initial_capital, slippage=0.001)
        self.broker.init_account("balanced", initial_capital)
        self.broker.init_account("aggressive", initial_capital)
        self.order_manager = OrderManager()

        # 3. Four-layer Risk Managers (one for each account context)
        self.risk_manager_bal = RiskManager(account_type="balanced")
        self.risk_manager_agg = RiskManager(account_type="aggressive")

        # 4. Portfolio Managers
        self.pm_bal = PortfolioManager(self.risk_manager_bal, self.order_manager, self.broker)
        self.pm_agg = PortfolioManager(self.risk_manager_agg, self.order_manager, self.broker)

        # 5. Strategies
        self.strategy_bal = BalancedStrategy(account_id="balanced")
        self.strategy_agg = AggressiveStrategy(account_id="aggressive")

        # Runtime shadow state
        self.shadow_decisions: Dict[str, Any] = {
            "balanced": {},
            "aggressive": {},
            "timestamp": None
        }

        logger.info(f"TradingRuntime initialized in {self.mode} mode. SQLite DB: {self.db_path}")

    def set_mode(self, mode: str):
        """Switch between SHADOW and ACTIVE."""
        with self.lock:
            self.mode = mode.upper()
            logger.info(f"TradingRuntime mode switched to {self.mode}")

    def step(
        self,
        as_of: str,
        quotes: Dict[str, Dict[str, Any]],
        market_data: Optional[Dict[str, Any]] = None,
        now: Optional[datetime.datetime] = None
    ) -> Dict[str, Any]:
        """
        Executes one full synchronous trading step across both accounts:
        1. Lock check (prevents concurrent duplicate runs)
        2. Mark-to-market prices in ledger
        3. T+1 rollover if trade date changed
        4. Detect Market Regime
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
            market_data = market_data or {}

            # 1. Update Market Prices across positions in SQLite
            self.ledger.update_market_prices(quotes)

            # 2. Daily T+1 Rollover
            if self.last_trade_date != as_of:
                self.ledger.roll_t1(as_of)
                if hasattr(self.broker, "roll_trading_day"):
                    self.broker.roll_trading_day()
                self.last_trade_date = as_of

            # 3. Market Regime Classification
            csi300_bars = market_data.get("csi300_bars", [])
            regime, breadth, mkt_audit = self.risk_manager_bal.market_rule.determine_regime(csi300_bars, as_of)
            self.ledger.record_risk_event(mkt_audit)

            step_summary = {
                "as_of": as_of,
                "mode": self.mode,
                "regime": regime.value,
                "balanced": {},
                "aggressive": {}
            }

            # 4. Process Balanced Account
            res_bal = self._process_account_step(
                account_id="balanced",
                strategy=self.strategy_bal,
                pm=self.pm_bal,
                risk_mgr=self.risk_manager_bal,
                regime=regime,
                as_of=as_of,
                quotes=quotes,
                market_data=market_data
            )
            step_summary["balanced"] = res_bal

            # 5. Process Aggressive Account
            res_agg = self._process_account_step(
                account_id="aggressive",
                strategy=self.strategy_agg,
                pm=self.pm_agg,
                risk_mgr=self.risk_manager_agg,
                regime=regime,
                as_of=as_of,
                quotes=quotes,
                market_data=market_data
            )
            step_summary["aggressive"] = res_agg

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
        market_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Processes target planning, risk screening, and execution for one account."""
        bal = self.ledger.get_balance(account_id)
        positions = self.ledger.get_positions(account_id)

        # 1. Generate Signals
        signals = strategy.generate_signals(as_of, market_data, regime)

        # 2. Risk Off Entry Gate Check (Market Level)
        allowed_signals = []
        top_sectors = market_data.get("top_sectors", [])
        for s in signals:
            if regime == MarketRegime.RISK_OFF:
                bars = market_data.get("stock_bars", {}).get(s.symbol, [])
                audit = risk_mgr.market_rule.check_risk_off_entry_gate(
                    symbol=s.symbol,
                    price=s.price,
                    bars=bars,
                    sector=s.sector,
                    top_sectors=top_sectors,
                    ret20=s.ret20,
                    ret60=s.ret60,
                    amount=getattr(s, "amount", 50000000.0)
                )
                self.ledger.record_risk_event(audit)
                if not audit.passed:
                    continue
            allowed_signals.append(s)

        # 3. Check Drawdown Circuit Breaker
        is_alert, is_freeze, dd_audit = risk_mgr.portfolio_rule.check_drawdown_circuit_breaker(bal)
        self.ledger.record_risk_event(dd_audit)

        # 4. Position Risk Exits (-6% hard stop, trailing ATR, MA20 breakdown) executed FIRST
        if self.mode == "ACTIVE":
            for sym, pos in list(positions.items()):
                q = quotes.get(sym, {})
                price = float(q.get("price") or q.get("current") or q.get("close") or pos.last_price or 0.0)
                atr = float(q.get("atr") or 1.0)
                ma20 = float(q.get("ma20") or price)
                should_exit, exit_reason, audit = risk_mgr.position_rule.check_position_exit(pos, price, atr=atr, ma20=ma20)
                self.ledger.record_risk_event(audit)
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
                    self.order_manager.submit_order(exit_order)
                    self.ledger.save_order(exit_order)
                    fill = self.broker.execute_order(exit_order, execution_price=price, trade_date=as_of)
                    if fill:
                        self.ledger.apply_fill(fill, name=pos.name, sector=pos.sector)

            # Refresh positions and balances after exits
            positions = self.ledger.get_positions(account_id)
            bal = self.ledger.get_balance(account_id)

        # 5. Plan Target Portfolio (with updated post-exit positions and released cash)
        target_pf = strategy.plan_targets(
            as_of=as_of,
            regime=regime,
            signals=allowed_signals,
            balance=bal,
            positions=positions,
            is_frozen=is_freeze
        )

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
                "target_positions": len(target_pf.target_positions),
                "total_target_weight": target_pf.total_weight,
                "cash_weight": target_pf.cash_weight,
                "executed_orders": 0
            }

        # B. Portfolio Rebalancing Buys
        price_map = {sym: float(q.get("price") or q.get("current") or q.get("close") or 0.0) for sym, q in quotes.items()}
        stock_info_map = {sym: {"pre_close": float(q.get("pre_close") or q.get("price") or 0.0), "is_suspended": q.get("is_suspended", False), "is_st": q.get("is_st", False)} for sym, q in quotes.items()}

        orders = pm.execute_rebalance(
            target_portfolio=target_pf,
            price_map=price_map,
            stock_info_map=stock_info_map
        )

        executed_fills = []
        for o in orders:
            self.ledger.save_order(o)
            price = price_map.get(o.symbol, o.price)
            name = quotes.get(o.symbol, {}).get("name", o.symbol)
            sector = quotes.get(o.symbol, {}).get("sector", "其他")
            fill = self.broker.execute_order(o, execution_price=price, trade_date=as_of)
            if fill:
                self.ledger.apply_fill(fill, name=name, sector=sector)
                executed_fills.append(fill)

        return {
            "mode": "ACTIVE",
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
        bal_sum = self.ledger.get_summary("balanced")
        agg_sum = self.ledger.get_summary("aggressive")
        bal_pos = self.get_positions("balanced")
        agg_pos = self.get_positions("aggressive")

        bal_b = self.ledger.get_balance("balanced")
        agg_b = self.ledger.get_balance("aggressive")

        reg_bal = self.risk_manager_bal.portfolio_rule.regime_caps
        reg_agg = self.risk_manager_agg.portfolio_rule.regime_caps

        return {
            "success": True,
            "version": "v3.0",
            "runtime_mode": self.mode,
            "primary": {
                "account_id": "balanced",
                "strategy_name": "Product AB Balanced v3.0",
                "description": "中等风险、稳健增长。Regime动态仓位 (30%~80%)，单股15%，单行业30%，四级风控与SQLite原子记账",
                "total_equity": bal_sum["total_equity"],
                "cash": bal_sum["cash"],
                "stock_value": bal_sum["stock_value"],
                "drawdown_pct": bal_sum["drawdown_pct"],
                "positions_count": len(bal_pos),
                "max_positions": self.strategy_bal.max_positions,
                "is_frozen": bal_b.is_frozen,
                "regime_caps": {k.value: v for k, v in reg_bal.items()}
            },
            "secondary": {
                "account_id": "aggressive",
                "strategy_name": "Product B Aggressive v3.0",
                "description": "高风险、积极进攻。纯全市场行业动量，Regime仓位 (50%~100%)，单股25%，单行业40%，四级风控与SQLite原子记账",
                "total_equity": agg_sum["total_equity"],
                "cash": agg_sum["cash"],
                "stock_value": agg_sum["stock_value"],
                "drawdown_pct": agg_sum["drawdown_pct"],
                "positions_count": len(agg_pos),
                "max_positions": self.strategy_agg.max_positions,
                "is_frozen": agg_b.is_frozen,
                "regime_caps": {k.value: v for k, v in reg_agg.items()}
            }
        }
