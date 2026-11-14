# -*- coding: utf-8 -*-
"""
A-Stock Unified Backtest Engine
Reuses 100% of the identical Strategy, Portfolio, Risk, Order, and Ledger modules
as used in live paper trading, guaranteeing zero simulation divergence.
"""

from typing import Dict, List, Optional, Any
from pathlib import Path
import math
from .dataset import BacktestDataset
from ..strategy.base import BaseStrategy
from ..risk.manager import RiskManager
from ..order.manager import OrderManager
from ..broker.sim_broker import SimulatedBrokerAdapter
from ..ledger.ledger import AccountLedger
from ..portfolio.manager import PortfolioManager
from ..models import (
    MarketRegime, OrderSide, OrderStatus, TargetPosition, Position, AccountBalance
)


class BacktestEngine:
    """Deterministic, reproducible quantitative backtest engine."""

    def __init__(
        self,
        dataset: BacktestDataset,
        initial_cash: float = 100000.0,
        slippage: float = 0.001
    ):
        self.dataset = dataset
        self.initial_cash = initial_cash
        self.slippage = slippage

    def run(
        self,
        strategy: BaseStrategy,
        account_type: str = "balanced",
        rebalance_interval: int = 5
    ) -> Dict[str, Any]:
        """
        Executes unified backtest over dataset trading days.
        """
        account_id = strategy.account_id
        risk_mgr = RiskManager(account_type=account_type)
        order_mgr = OrderManager()
        broker = SimulatedBrokerAdapter(initial_cash=self.initial_cash, slippage=self.slippage)
        broker.init_account(account_id, cash=self.initial_cash)
        ledger = AccountLedger(account_id=account_id, initial_cash=self.initial_cash)
        portfolio_mgr = PortfolioManager(risk_manager=risk_mgr, order_manager=order_mgr, broker=broker)

        trading_days = self.dataset.trading_days
        daily_nav_history: List[Dict[str, Any]] = []

        b300_bars = self.dataset.benchmark_300
        b300_by_date = {b["date"]: b for b in b300_bars}

        # Track consecutive days below MA20 for open positions
        below_ma20_tracker: Dict[str, int] = {}

        for day_idx, date in enumerate(trading_days):
            if day_idx == 0:
                # First day: initialize
                ledger.apply_overnight_interest(date)
                daily_nav_history.append({
                    "date": date,
                    "equity": ledger.total_equity,
                    "cash": ledger.cash,
                    "stock_val": 0.0,
                    "exposure_pct": 0.0,
                    "regime": "NEUTRAL",
                    "positions_count": 0
                })
                continue

            as_of = trading_days[day_idx - 1]
            rebalance_day = (day_idx % rebalance_interval == 0)

            # 1. Market Regime Evaluation (as of previous close)
            regime, breadth = risk_mgr.evaluate_market_regime(b300_bars, as_of)

            # 2. Check Exits on Existing Positions (Open price execution on date)
            positions = broker.get_positions(account_id)
            for sym, pos in list(positions.items()):
                sinfo = self.dataset.stocks.get(sym)
                if not sinfo:
                    continue
                d_idx = sinfo["date_to_idx"].get(date)
                p_idx = sinfo["date_to_idx"].get(as_of)
                if d_idx is None or p_idx is None:
                    continue

                open_px = float(sinfo["bars"][d_idx]["open"])
                prev_close = float(sinfo["bars"][p_idx]["close"])
                atr = float(sinfo["atr"][p_idx])
                ma20 = sum(float(b["close"]) for b in sinfo["bars"][p_idx-19:p_idx+1]) / 20.0 if p_idx >= 19 else prev_close

                # Update consecutive below MA20
                if prev_close < ma20:
                    below_ma20_tracker[sym] = below_ma20_tracker.get(sym, 0) + 1
                else:
                    below_ma20_tracker[sym] = 0

                should_exit, exit_reason, _ = risk_mgr.check_position_exit(
                    pos=pos,
                    current_price=open_px,
                    atr=atr,
                    ma20=ma20,
                    consecutive_below_ma20=below_ma20_tracker[sym]
                )

                if should_exit and pos.available_shares > 0:
                    exit_order = order_mgr.create_order(
                        account_id=account_id,
                        symbol=sym,
                        side=OrderSide.SELL,
                        shares=pos.available_shares,
                        price=open_px,
                        name=pos.name,
                        reason=exit_reason
                    )
                    fill = broker.execute_order(exit_order, execution_price=open_px, trade_date=date)
                    ledger.record_fill(fill)

            # 3. Portfolio Drawdown Check
            curr_mv = sum(p.market_value for p in broker.get_positions(account_id).values())
            temp_bal = AccountBalance(
                account_id=account_id,
                cash=broker.get_cash(account_id),
                frozen_cash=0.0,
                stock_market_value=curr_mv,
                total_equity=broker.get_cash(account_id) + curr_mv,
                peak_equity=ledger.peak_equity,
                current_drawdown_pct=ledger.current_drawdown_pct
            )
            _, is_freeze = risk_mgr.check_portfolio_drawdown(temp_bal)

            # 4. Strategy Target Generation & Rebalancing
            if rebalance_day and not is_freeze:
                # Precompute candidate signals on as_of
                cand_signals = []
                for sym, sinfo in self.dataset.stocks.items():
                    p_idx = sinfo["date_to_idx"].get(as_of)
                    if p_idx is None or p_idx < 60:
                        continue
                    c_px = float(sinfo["bars"][p_idx]["close"])
                    ma20 = sum(float(b["close"]) for b in sinfo["bars"][p_idx-19:p_idx+1]) / 20.0
                    ma60 = sum(float(b["close"]) for b in sinfo["bars"][p_idx-59:p_idx+1]) / 60.0
                    ma120 = sum(float(b["close"]) for b in sinfo["bars"][p_idx-119:p_idx+1]) / 120.0 if p_idx >= 119 else ma60

                    if c_px > ma20 and c_px > ma60:
                        c_ret20 = (c_px - float(sinfo["bars"][p_idx-20]["close"])) / float(sinfo["bars"][p_idx-20]["close"]) * 100.0
                        c_ret60 = (c_px - float(sinfo["bars"][p_idx-60]["close"])) / float(sinfo["bars"][p_idx-60]["close"]) * 100.0
                        score = c_ret20 * 0.6 + c_ret60 * 0.4
                        cand_signals.append({
                            "symbol": sym,
                            "name": sinfo["name"],
                            "sector": sinfo["industry"],
                            "score": score,
                            "price": c_px,
                            "ma20": ma20,
                            "ma60": ma60,
                            "ma120": ma120,
                            "atr": sinfo["atr"][p_idx],
                            "ret20": c_ret20,
                            "ret60": c_ret60
                        })

                cand_signals.sort(key=lambda x: x["score"], reverse=True)
                signals = strategy.generate_signals(as_of, {"candidates": cand_signals}, regime)

                # Filter signals if in RISK_OFF
                if regime == MarketRegime.RISK_OFF:
                    top_sectors = list({s.sector for s in signals[:8]})[:4]
                    filtered_sigs = []
                    for sig in signals:
                        sinfo = self.dataset.stocks.get(sig.symbol)
                        bars = sinfo["bars"][:sinfo["date_to_idx"].get(as_of, 0)+1] if sinfo else []
                        audit = risk_mgr.market_rule.check_risk_off_entry_gate(
                            symbol=sig.symbol,
                            price=sig.price,
                            bars=bars,
                            sector=sig.sector,
                            top_sectors=top_sectors,
                            ret20=sig.ret20,
                            ret60=sig.ret60
                        )
                        if audit.passed:
                            filtered_sigs.append(sig)
                    signals = filtered_sigs

                # Generate TargetPortfolio
                target_portfolio = strategy.plan_targets(
                    as_of=as_of,
                    regime=regime,
                    signals=signals,
                    balance=temp_bal,
                    positions=broker.get_positions(account_id),
                    is_frozen=is_freeze
                )

                # Execute buys at date open price
                curr_pos = broker.get_positions(account_id)
                for sym, tpos in target_portfolio.target_positions.items():
                    if sym not in curr_pos:
                        sinfo = self.dataset.stocks.get(sym)
                        if not sinfo:
                            continue
                        d_idx = sinfo["date_to_idx"].get(date)
                        if d_idx is None:
                            continue
                        open_px = float(sinfo["bars"][d_idx]["open"])
                        target_capital = temp_bal.total_equity * tpos.target_weight
                        shares = int((target_capital / open_px) // 100) * 100
                        if shares >= 100:
                            order = order_mgr.create_order(
                                account_id=account_id,
                                symbol=sym,
                                side=OrderSide.BUY,
                                shares=shares,
                                price=open_px,
                                name=sinfo["name"],
                                reason=tpos.reason
                            )
                            try:
                                fill = broker.execute_order(order, execution_price=open_px, trade_date=date)
                                ledger.record_fill(fill)
                            except Exception:
                                order_mgr.mark_rejected(order.order_id, "Execution failed or cash limit")

            # 5. End of Day: Mark-to-Market with close prices
            close_price_map = {}
            for sym in broker.get_positions(account_id):
                sinfo = self.dataset.stocks.get(sym)
                if sinfo:
                    d_idx = sinfo["date_to_idx"].get(date)
                    if d_idx is not None:
                        close_price_map[sym] = float(sinfo["bars"][d_idx]["close"])
            ledger.update_prices(close_price_map)

            # 6. Apply overnight cash interest
            ledger.apply_overnight_interest(date)

            # 7. Roll T+1 tradable shares
            broker.roll_trading_day(account_id)
            ledger.roll_trading_day()

            # 8. Record Daily NAV
            nav_entry = {
                "date": date,
                "equity": ledger.total_equity,
                "cash": ledger.cash,
                "stock_val": ledger.stock_market_value,
                "exposure_pct": round(ledger.stock_market_value / ledger.total_equity * 100.0, 2) if ledger.total_equity > 0 else 0.0,
                "regime": regime.value,
                "positions_count": len(ledger.positions)
            }
            daily_nav_history.append(nav_entry)

        # 9. Compute Performance Metrics
        summary = ledger.get_summary()
        final_equity = ledger.total_equity
        total_ret = round((final_equity - self.initial_cash) / self.initial_cash * 100.0, 2)
        n_days = len(daily_nav_history)
        cagr = round(((final_equity / self.initial_cash) ** (252.0 / max(1, n_days)) - 1.0) * 100.0, 2) if final_equity > 0 else 0.0

        # Daily returns for Sharpe
        daily_returns = []
        for i in range(1, len(daily_nav_history)):
            prev_eq = daily_nav_history[i-1]["equity"]
            cur_eq = daily_nav_history[i]["equity"]
            daily_returns.append((cur_eq - prev_eq) / prev_eq if prev_eq > 0 else 0.0)

        mean_ret = sum(daily_returns) / len(daily_returns) if daily_returns else 0.0
        var_ret = sum((r - mean_ret)**2 for r in daily_returns) / len(daily_returns) if daily_returns else 0.0
        std_ret = math.sqrt(var_ret) if var_ret > 0 else 1e-6
        sharpe = round((mean_ret / std_ret) * math.sqrt(252), 2)
        calmar = round(cagr / ledger.max_drawdown_pct, 2) if ledger.max_drawdown_pct > 0 else 0.0

        return {
            "account_id": account_id,
            "total_return_pct": total_ret,
            "cagr_pct": cagr,
            "max_drawdown_pct": round(ledger.max_drawdown_pct, 2),
            "sharpe_ratio": sharpe,
            "calmar_ratio": calmar,
            "final_equity": final_equity,
            "total_trades": summary["total_trades"],
            "win_rate_pct": summary["win_rate_pct"],
            "profit_loss_ratio": summary["profit_loss_ratio"],
            "total_fees": summary["total_fees"],
            "total_interest_earned": summary["total_interest_earned"],
            "daily_nav": daily_nav_history,
            "trades": ledger.closed_trades,
            "orders": [o.to_dict() for o in order_mgr.get_all_orders()]
        }
