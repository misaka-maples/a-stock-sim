"""Opt-in SQLite account prototype; deliberately not selected by the server.

The JSON snapshot/reset routes must be adapted before this class is deployed.
Use a separate database path and retain the original JSON as migration evidence.
"""
import copy
import datetime
import json
import time
from collections.abc import Sequence
from pathlib import Path

from sim_trader import SimAccount, AccountPersistenceError, BEIJING
from sqlite_ledger import SQLiteLedger, cents


class JournalSequence(Sequence):
    """Immutable disk prefix plus a small uncommitted transaction tail."""
    def __init__(self, ledger, kind):
        self.ledger, self.kind = ledger, kind
        self.committed = ledger.count(kind)
        self.pending = []

    def append(self, value):
        self.pending.append(value)

    def __len__(self):
        return self.committed + len(self.pending)

    def __getitem__(self, index):
        if isinstance(index, slice):
            start, stop, step = index.indices(len(self))
            return [self[i] for i in range(start,stop,step)]
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        if index >= self.committed:
            return self.pending[index-self.committed]
        with self.ledger.connect() as db:
            row = db.execute('SELECT payload FROM journal WHERE kind=? AND seq=?',
                             (self.kind,index+1)).fetchone()
        return json.loads(row[0])

    def __iter__(self):
        yield from self.ledger.iterate(self.kind)
        yield from self.pending

    def __deepcopy__(self, memo):
        result = object.__new__(type(self))
        result.ledger, result.kind, result.committed = self.ledger, self.kind, self.committed
        result.pending = copy.deepcopy(self.pending,memo)
        return result

    def accepted(self):
        self.committed += len(self.pending)
        self.pending.clear()


def add_rollup(value, trade):
    value['count'] = value.get('count',0) + 1
    value['fees_cents'] = value.get('fees_cents',0) + cents(trade.get('fees',0))
    side = trade.get('side')
    if side in ('BUY','SELL'):
        key = 'buy_cents' if side == 'BUY' else 'sell_cents'
        value[key] = value.get(key,0) + cents(trade.get('amount',0))
    if side == 'SELL':
        pnl = cents(trade.get('realized_pnl',0))
        for key,delta in [('realized_cents',pnl),('closed',1),('wins',int(pnl>0)),
                          ('losses',int(pnl<0)),('win_cents',max(pnl,0)),('loss_cents',max(-pnl,0))]:
            value[key] = value.get(key,0)+delta
    value['name'] = trade.get('name',trade.get('symbol',''))
    value['last_price'] = trade.get('price',0)
    return value


class SQLiteSimAccount(SimAccount):
    """Same matching/cost/risk engine; transactional incremental journal storage.

    Existing snapshot manager and reset routes are JSON-only: server integration
    is intentionally unavailable until those routes support database backups.
    """
    def __init__(self, *, database_path, legacy_json=None, **kwargs):
        self._ledger = SQLiteLedger(database_path)
        self._revision = 0
        self._order_updates = {}
        self._rejection_sequences = {}
        self._legacy_json = Path(legacy_json) if legacy_json else None
        if 'save_path' in kwargs:
            raise ValueError('Use database_path, not save_path')
        super().__init__(save_path=str(database_path), **kwargs)

    def load(self):
        self._revision, data = self._ledger.load()
        if data is None:
            if self._legacy_json is not None:
                # One-time migration: legacy validator repairs cost basis exactly
                # as JSON startup does. Original bytes are never modified.
                data = json.loads(self._legacy_json.read_text(encoding='utf-8'))
                self._apply_loaded_data(data)
            trades, orders = self.trades, self.orders
            self.trades = JournalSequence(self._ledger,'trades')
            self.orders = JournalSequence(self._ledger,'orders')
            self.trades.pending = trades
            self.orders.pending = orders
            self.save()
            return
        trades, orders = JournalSequence(self._ledger,'trades'), JournalSequence(self._ledger,'orders')
        self._apply_loaded_data(dict(data,trades=trades,orders=orders),migrate=False)
        self.accounting_history_complete = data.get('accounting_history_complete',False)

    def _state(self):
        return dict(schema_version=4, updated_at=(self._execution_now or datetime.datetime.now(BEIJING)).isoformat(),
            summary=self.get_summary(), positions=self.positions,
            equity_history=self.equity_history[-1000:], daily_equity_history=self.daily_equity_history,
            risk_history=self.risk_history, strategy_state=self.strategy_state,
            accounting_history_complete=self.accounting_history_complete,
            quote_consumption=[[*key,value] for key,value in self._quote_consumption.items()],
            quote_capacity=[[*key,value] for key,value in self._quote_capacity.items()],
            daily_consumption=[[*key,value] for key,value in self._daily_consumption.items()])

    def save(self):
        if not isinstance(self.trades,JournalSequence) or not isinstance(self.orders,JournalSequence):
            raise ValueError('Replacing SQLite journal sequences is unsupported')
        try:
            self._revision = self._ledger.commit(self._state(), expected_revision=self._revision,
                trades=self.trades.pending, orders=self.orders.pending,
                order_updates=self._order_updates.items())
        except Exception as exc:
            self._persistence_error = str(exc)
            raise AccountPersistenceError(str(exc),committed=False) from exc
        self._order_updates.clear()
        self.trades.accepted()
        self.orders.accepted()
        self._last_durable_save = time.monotonic()

    _transaction_fields = SimAccount._transaction_fields + ("_order_updates", "_rejection_sequences")

    def _record_rejection(self, side, symbol, name, price, shares, reason):
        now = (self._execution_now or datetime.datetime.now(BEIJING)).timestamp()
        self._last_rejections = {key:value for key,value in self._last_rejections.items()
                                 if now-value[0] < 60}
        self._rejection_sequences = {key:value for key,value in self._rejection_sequences.items()
                                     if key in self._last_rejections}
        key = (symbol,side,reason)
        existing = key in self._last_rejections
        result = super()._record_rejection(side,symbol,name,price,shares,reason)
        if existing:
            self._order_updates[self._rejection_sequences[key]] = result['order']
        else:
            self._rejection_sequences[key] = len(self.orders)
        return result

    def purge_non_main_board(self):
        raise ValueError('SQLite history is immutable; use an explicit audited migration')

    def _total(self):
        total = self._ledger.aggregate()
        for trade in self.trades.pending:
            add_rollup(total,trade)
        return total

    def _base_metrics(self):
        # A shallow view shares bounded account state but never mutates it.
        view = copy.copy(self)
        view.trades = []
        return SimAccount.get_performance_metrics(view)

    def get_summary(self):
        summary = self._base_metrics()['summary']
        total = self._total()
        wins,losses,closed = (total.get(k,0) for k in ('wins','losses','closed'))
        avg_win = round(total.get('win_cents',0)/100/wins,2) if wins else 0.0
        avg_loss = round(total.get('loss_cents',0)/100/losses,2) if losses else 0.0
        summary.update(total_fees=total.get('fees_cents',0)/100,
            realized_pnl=total.get('realized_cents',0)/100,
            win_count=wins,loss_count=losses,closed_count=closed,
            win_rate=round(wins/closed*100,2) if closed else 0.0,
            avg_win=avg_win,avg_loss=avg_loss,
            profit_loss_ratio=round(avg_win/avg_loss,2) if avg_loss else avg_win,
            total_trades=len(self.trades),trade_count=len(self.trades))
        today = (self._execution_now or datetime.datetime.now(BEIJING)).strftime('%Y-%m-%d')
        summary['today_pnl_available'] = not self.trades or any(
            pt.get('time','')[:10] < today for pt in self.equity_history+self.daily_equity_history)
        summary['risk_history_complete'] = self.risk_history.get('history_complete',not self.trades)
        return summary

    def get_performance_metrics(self):
        # Only bounded daily output; complete history remains queryable by cursor.
        result = self._base_metrics()
        result['summary'] = self.get_summary()
        symbols = {entry['symbol']:entry for entry in result['symbol_stats']}
        after = ''
        while True:
            page = self._ledger.aggregate_page('symbol',after=after)
            if not page:
                break
            for symbol,total in page:
                item = symbols.setdefault(symbol,dict(symbol=symbol,name=total.get('name',symbol),
                    status='已清仓',current_shares=0,cost_price=0.0,last_price=total.get('last_price',0),unrealized_pnl=0.0))
                item.update(buy_amount=total.get('buy_cents',0)/100,sell_amount=total.get('sell_cents',0)/100,
                    realized_pnl=total.get('realized_cents',0)/100,total_fees=total.get('fees_cents',0)/100,
                    trade_count=total.get('count',0))
                item['total_pnl'] = round(item['realized_pnl']+item['unrealized_pnl'],2)
                base = item['buy_amount'] or item['current_shares']*item['cost_price']
                item['pnl_pct'] = round(item['total_pnl']/base*100,2) if base else 0.0
            after = page[-1][0]
        result['symbol_stats'] = sorted(symbols.values(),key=lambda x:x['total_pnl'],reverse=True)
        daily = {}
        for point in self.equity_history:
            day = point.get('time','')[:10]
            if day:
                daily[day] = dict(date=day,end_equity=point.get('equity',result['summary']['total_equity']),
                                  trade_count=0,fees=0.0)
        after = ''
        while True:
            page = self._ledger.aggregate_page('day',after=after)
            if not page:
                break
            for day,total in page:
                entry = daily.setdefault(day,dict(date=day,end_equity=result['summary']['total_equity']))
                entry.update(trade_count=total['count'],fees=total['fees_cents']/100)
            after = page[-1][0]
        previous = self.initial_cash
        for day in sorted(daily):
            entry = daily[day]
            entry['start_equity'] = previous
            entry['daily_pnl'] = round(entry['end_equity']-previous,2)
            entry['daily_pnl_pct'] = round(entry['daily_pnl']/previous*100,2) if previous else 0.0
            previous = entry['end_equity']
        result['daily_stats'] = [daily[day] for day in sorted(daily,reverse=True)]
        return result

    def backup(self, path):
        self.save()
        self._ledger.backup(path)
