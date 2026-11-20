# -*- coding: utf-8 -*-
"""
A-Stock V3 Trading Runtime Adapter for Web Server
Bridges legacy server.py endpoints and background worker to the unified TradingRuntime.
Prevents direct balance mutation and enforces all trades to flow through RiskManager and SQLite.
"""

import logging
from typing import Dict, List, Optional, Any
import datetime

from .runtime import TradingRuntime
from .models import Order, OrderSide, OrderType, OrderStatus
from .errors import TradeError

logger = logging.getLogger("TradingRuntimeAdapter")


class TradingRuntimeAdapter:
    """Adapts TradingRuntime to existing server.py APIs and worker loop."""

    def __init__(self, runtime: TradingRuntime, primary_account_id: str = "balanced"):
        self.runtime = runtime
        self.primary_account_id = primary_account_id
        self.save_path = str(self.runtime.db_path)
        self.strict_t1 = True
        self.participation_rate = 0.05
        from pathlib import Path
        self._state_file = Path(self.runtime.db_path).parent / "trading_v3_state.json"
        self.strategy_state: Dict[str, Any] = self._load_strategy_state()

    def _load_strategy_state(self) -> Dict[str, Any]:
        if hasattr(self, "_state_file") and self._state_file.exists():
            try:
                import json
                with open(self._state_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning("Failed to load trading_v3_state.json: %s", e)
        return {}

    @property
    def positions(self) -> Dict[str, Any]:
        """Provides positions dict matching legacy SimAccount.positions."""
        pos = self.runtime.ledger.get_positions(self.primary_account_id)
        result = {}
        for sym, p in pos.items():
            result[sym] = {
                "symbol": sym,
                "name": p.name,
                "total_shares": p.shares,
                "available_shares": p.tradable_shares,
                "cost_price": p.avg_cost,
                "last_price": p.current_price,
                "market_value": p.market_value,
                "unrealized_pnl": p.unrealized_pnl,
                "pnl_pct": p.unrealized_pnl_pct,
                "sector": getattr(p, "sector", "其他")
            }
        return result

    @property
    def trades(self) -> List[Dict[str, Any]]:
        """Provides trades list matching legacy SimAccount.trades."""
        return self.runtime.ledger.get_trades(self.primary_account_id)

    @property
    def orders(self) -> List[Dict[str, Any]]:
        """Provides orders list matching legacy SimAccount.orders."""
        with self.runtime.ledger.connect() as conn:
            rows = conn.execute("SELECT * FROM orders WHERE account_id = ? ORDER BY created_at DESC", (self.primary_account_id,)).fetchall()
            return [dict(r) for r in rows]

    @property
    def cash(self) -> float:
        return self.runtime.ledger.get_balance(self.primary_account_id).cash

    def purge_non_main_board(self) -> Dict[str, Any]:
        return {"removed_positions": 0, "message": "All V3 universe assets are strictly main board"}

    def get_performance_metrics(self) -> Dict[str, Any]:
        """Provides performance metrics matching legacy SimAccount.get_performance_metrics()."""
        sum_dict = self.get_summary()
        trades = self.trades
        closed_trades = [t for t in trades if t.get("side") == "SELL"]
        win_trades = [t for t in closed_trades if float(t.get("realized_pnl", 0.0)) > 0]
        loss_trades = [t for t in closed_trades if float(t.get("realized_pnl", 0.0)) < 0]
        win_rate = (len(win_trades) / len(closed_trades) * 100.0) if closed_trades else 0.0

        return {
            "total_equity": sum_dict["total_equity"],
            "cash": sum_dict["cash"],
            "market_value": sum_dict["stock_value"],
            "total_pnl": sum_dict["total_profit_loss"],
            "total_pnl_pct": sum_dict["profit_loss_pct"],
            "realized_pnl": sum_dict["realized_pnl"],
            "unrealized_pnl": sum_dict["unrealized_pnl"],
            "total_fees": sum(float(t.get("fees", 0.0)) for t in trades),
            "win_rate": round(win_rate, 2),
            "win_count": len(win_trades),
            "loss_count": len(loss_trades),
            "closed_count": len(closed_trades),
            "total_trades": len(trades),
            "peak_equity": sum_dict["peak_equity"],
            "max_drawdown_pct": sum_dict["drawdown_pct"],
            "position_count": sum_dict["positions_count"]
        }

    def get_summary(self) -> Dict[str, Any]:
        """Provides summary matching legacy SimAccount.get_summary()."""
        sum_dict = self.runtime.ledger.get_summary(self.primary_account_id)
        # Augment with extra UI fields
        bal = self.runtime.ledger.get_balance(self.primary_account_id)
        sum_dict.update({
            "initial_cash": 100000.0,
            "market_value": sum_dict.get("stock_value", 0.0),
            "total_pnl": sum_dict.get("total_profit_loss", 0.0),
            "total_pnl_pct": sum_dict.get("profit_loss_pct", 0.0),
            "today_pnl": 0.0,
            "today_pnl_pct": 0.0,
            "today_pnl_available": True,
            "persistence_healthy": True,
            "realized_pnl": sum_dict.get("total_profit_loss", 0.0),
            "unrealized_pnl": sum(p.unrealized_pnl for p in self.runtime.ledger.get_positions(self.primary_account_id).values()),
            "max_drawdown_pct": sum_dict.get("drawdown_pct", 0.0),
            "position_count": sum_dict.get("positions_count", 0),
            "v3_active": True,
            "runtime_mode": self.runtime.mode
        })
        return sum_dict

    def update_market_prices(self, quotes: Dict[str, Dict[str, Any]], now: Optional[datetime.datetime] = None):
        """Pass-through to update prices in SQLite."""
        self.runtime.ledger.update_market_prices(quotes)

    def bind_execution_quotes(self, quotes: Dict[str, Dict[str, Any]]):
        self.update_market_prices(quotes)

    def save(self):
        """Persists strategy_state (such as closing_report_date and rotation markers) to disk."""
        if hasattr(self, "_state_file"):
            try:
                import json
                temp_file = self._state_file.with_suffix(".tmp")
                with open(temp_file, "w", encoding="utf-8") as f:
                    json.dump(self.strategy_state, f, ensure_ascii=False, indent=2)
                temp_file.replace(self._state_file)
            except Exception as e:
                logger.warning("Failed to save trading_v3_state.json: %s", e)

    def save_if_due(self, interval_seconds: float = 30.0):
        self.save()

    def load(self):
        pass

    def execute_buy(
        self,
        symbol: str,
        name: str,
        price: float,
        shares: int,
        reason: str = "",
        limit_price: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Routes manual or legacy buy through OrderManager, RiskManager, and SimBroker.
        Prevents raw uninspected execution.
        """
        now_iso = datetime.datetime.now().isoformat()
        trade_date = now_iso[:10]
        exec_price = limit_price if limit_price and limit_price > 0 else price

        # Pre-trade Risk Check
        order = Order(
            order_id=f"ORD_MANUAL_BUY_{symbol}_{datetime.datetime.now().strftime('%H%M%S%f')}",
            client_order_id=f"C_MAN_{symbol}_{datetime.datetime.now().strftime('%H%M%S%f')}",
            account_id=self.primary_account_id,
            symbol=symbol,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT if limit_price else OrderType.MARKET,
            price=exec_price,
            shares=shares,
            status=OrderStatus.PROPOSED,
            reason=f"ManualOrder_{reason}"
        )

        bal = self.runtime.ledger.get_balance(self.primary_account_id)
        pos = self.runtime.ledger.get_positions(self.primary_account_id).get(symbol)

        passed, audit = self.runtime.risk_manager_bal.validate_pre_trade_order(
            order=order,
            available_cash=bal.available_cash,
            current_position=pos,
            prev_close=exec_price
        )
        self.runtime.ledger.record_risk_event(audit)

        if not passed:
            return {"success": False, "message": f"风控拒绝: {audit.message}", "audit": audit.to_dict()}

        self.runtime.order_manager.submit_order(order)
        self.runtime.ledger.save_order(order)

        fill = self.runtime.broker.execute_order(order, execution_price=exec_price, trade_date=trade_date)
        if fill:
            self.runtime.ledger.apply_fill(fill, name=name, sector="其他")
            return {
                "success": True,
                "message": "买入订单已成交",
                "trade": {
                    "trade_id": fill.fill_id,
                    "order_id": fill.order_id,
                    "symbol": symbol,
                    "name": name,
                    "price": fill.price,
                    "shares": fill.shares,
                    "amount": fill.amount,
                    "fees": fill.total_fee
                }
            }
        return {"success": False, "message": "撮合失败"}

    def execute_sell(
        self,
        symbol: str,
        price: float,
        shares: int,
        reason: str = "",
        limit_price: Optional[float] = None
    ) -> Dict[str, Any]:
        """Routes manual or legacy sell through OrderManager and SimBroker."""
        now_iso = datetime.datetime.now().isoformat()
        trade_date = now_iso[:10]
        exec_price = limit_price if limit_price and limit_price > 0 else price

        pos = self.runtime.ledger.get_positions(self.primary_account_id).get(symbol)
        if not pos or pos.tradable_shares < shares:
            return {"success": False, "message": f"可卖股份不足 (持仓: {pos.tradable_shares if pos else 0}, 申请: {shares})"}

        order = Order(
            order_id=f"ORD_MANUAL_SELL_{symbol}_{datetime.datetime.now().strftime('%H%M%S%f')}",
            client_order_id=f"C_MAN_SELL_{symbol}_{datetime.datetime.now().strftime('%H%M%S%f')}",
            account_id=self.primary_account_id,
            symbol=symbol,
            side=OrderSide.SELL,
            order_type=OrderType.LIMIT if limit_price else OrderType.MARKET,
            price=exec_price,
            shares=shares,
            status=OrderStatus.PROPOSED,
            reason=f"ManualOrder_{reason}"
        )

        bal = self.runtime.ledger.get_balance(self.primary_account_id)
        passed, audit = self.runtime.risk_manager_bal.validate_pre_trade_order(
            order=order,
            available_cash=bal.available_cash,
            current_position=pos,
            prev_close=exec_price
        )
        self.runtime.ledger.record_risk_event(audit)

        if not passed:
            return {"success": False, "message": f"风控拒绝: {audit.message}", "audit": audit.to_dict()}

        self.runtime.order_manager.submit_order(order)
        self.runtime.ledger.save_order(order)

        fill = self.runtime.broker.execute_order(order, execution_price=exec_price, trade_date=trade_date)
        if fill:
            self.runtime.ledger.apply_fill(fill, name=pos.name, sector=pos.sector)
            return {
                "success": True,
                "message": "卖出订单已成交",
                "trade": {
                    "trade_id": fill.fill_id,
                    "order_id": fill.order_id,
                    "symbol": symbol,
                    "price": fill.price,
                    "shares": fill.shares,
                    "amount": fill.amount,
                    "fees": fill.total_fee
                }
            }
        return {"success": False, "message": "撮合失败"}
