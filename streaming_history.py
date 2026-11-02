"""One-symbol ingestion, disk-backed signals, and two-date replay for research."""
from collections import deque
from collections.abc import Sequence
import json
from pathlib import Path
import sqlite3
import tempfile

from strategy_rules import evaluate_momentum, holding_signal
from history_data import HistoricalDataFeed

class CacheOnlyFeed(HistoricalDataFeed):
    """Never consumes market provider quota or modifies live history caches."""
    def fetch_stock_kline(self, symbol_or_code, start_date, end_date=None, use_cache=True):
        start, end = self._dates(start_date, end_date)
        item = self._peek_cached(symbol_or_code, start, end)
        # Old fetched_at is fine for historical research; provenance/range isn't.
        metadata = dict(item['metadata'], cache_only=True)
        error = item.get('error')
        if not metadata.get('raw_prices_available'):
            error = error or 'raw_prices_missing'
        if error:
            # Exclude the entire unverified symbol. In particular, never turn
            # a cache-range error into a seemingly complete partial backtest.
            metadata['error'] = error
        with self._lock:
            self.metadata[symbol_or_code] = metadata
        return symbol_or_code, item['name'], [] if error else item['bars']
    def fetch_benchmark_kline(self, index_code='000300', start_date='2024-01-01', end_date=None, use_cache=True):
        start, end = self._dates(start_date, end_date)
        code = index_code.replace('sh', '').replace('sz', '')
        item = self._peek_cached('sh'+code, start, end, adjustment='none', cache_symbol='idx_'+code)
        if item.get('error') or not item['bars']:
            raise ValueError('cached_benchmark_unavailable: '+str(item.get('error')))
        return item['bars']

class DiskList(Sequence):
    def __init__(self, connection, name, max_rows=2_000_000):
        self.db, self.name, self.count, self.max_rows = connection, name, 0, max_rows
        self.db.execute(f'CREATE TABLE {name}(id INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
    def append(self, row):
        if self.count >= self.max_rows:
            raise ValueError('report_row_limit_exceeded')
        self.db.execute(f'INSERT INTO {self.name} VALUES (?,?)', (self.count, json.dumps(row, ensure_ascii=False, allow_nan=False)))
        self.count += 1
    def __len__(self):
        return self.count
    def __iter__(self):
        for row in self.db.execute(f'SELECT payload FROM {self.name} ORDER BY id'):
            yield json.loads(row[0])
    def __getitem__(self, index):
        if isinstance(index, slice):
            start, stop, step = index.indices(self.count)
            return [self[i] for i in range(start, stop, step)]
        if index < 0:
            index += self.count
        row = self.db.execute(f'SELECT payload FROM {self.name} WHERE id=?', (index,)).fetchone()
        if row is None:
            raise IndexError(index)
        return json.loads(row[0])

class DayIndex:
    def __init__(self, owner, symbol):
        self.owner, self.symbol = owner, symbol
    def get(self, date, default=None):
        record = self.owner.days.get(date, {}).get(self.symbol)
        return record[0] if record else default
    def __contains__(self, date):
        return self.symbol in self.owner.days.get(date, {})

class StreamingStocks:
    def __init__(self, engine, warmup_start, dates, signal_dates):
        self.temp = tempfile.TemporaryDirectory(prefix='backtest-stream-', dir=engine.work_dir)
        self.db = None
        try:
            self._prepare(engine, warmup_start, dates, signal_dates)
        except BaseException:
            self.close()
            raise

    def _prepare(self, engine, warmup_start, dates, signal_dates):
        self.db = sqlite3.connect(str(Path(self.temp.name)/'stream.sqlite'))
        self.db.execute('PRAGMA cache_size=-8192')
        self.db.execute('PRAGMA temp_store=FILE')
        self.db.execute('PRAGMA journal_mode=OFF')
        self.db.execute('CREATE TABLE bars(date TEXT, symbol TEXT, bar TEXT, signal TEXT, holding TEXT, PRIMARY KEY(date,symbol)) WITHOUT ROWID')
        self.days, self.info, self.coverage, self.warnings = {}, {}, {}, []
        self.missing_industry = 0
        if isinstance(engine.feed, CacheOnlyFeed):
            self.warnings.append('cache_only_research: no provider requests; missing or unverified stock caches are excluded; inspect data_coverage before interpreting results')
        needed = set(dates) | set(signal_dates)
        for n, symbol in enumerate(engine.symbols):
            engine.notify_progress('prepare', n, len(engine.symbols))
            feed = engine.feed
            if hasattr(feed, 'fetch_stock_kline'):
                _, name, bars = feed.fetch_stock_kline(symbol, warmup_start, engine.end_date)
                metadata = dict(getattr(feed, 'metadata', {}).get(symbol, {}))
            else:  # Existing deterministic test feeds expose only universe API.
                item = feed.fetch_universe_klines([symbol], start_date=warmup_start, end_date=engine.end_date).get(symbol, {})
                name, bars, metadata = item.get('name', symbol), item.get('bars', []), item.get('metadata', {})
            bars = sorted((dict(b) for b in bars if b.get('date', '') <= engine.end_date), key=lambda b:b['date'])
            seen, window = set(), deque(maxlen=126)
            for b in bars:
                date = b['date']
                if date in seen:
                    raise ValueError('Duplicate stock dates: '+symbol)
                seen.add(date)
                if not engine._raw_valid(b):
                    raise ValueError('raw_prices_missing: '+symbol+'; fail closed')
                window.append(b)
                if date in needed:
                    engine.notify_progress('prepare', n, len(engine.symbols), throttle=True)
                    signal = evaluate_momentum(window, date, engine.config) if date in signal_dates else None
                    hold = holding_signal(window, date) if date in signal_dates else None
                    self.db.execute('INSERT INTO bars VALUES (?,?,?,?,?)', (date, symbol, json.dumps(b), json.dumps(signal), json.dumps(hold)))
            self.info[symbol] = dict(name=name, by_date=DayIndex(self, symbol))
            pre_count = sum(b['date'] < dates[0] for b in bars)
            self.coverage[symbol] = dict(start=bars[0]['date'] if bars else None, end=bars[-1]['date'] if bars else None,
                count=len(bars), pre_start_bars=pre_count, period_count=sum(engine.start_date <= b['date'] <= engine.end_date for b in bars), metadata=metadata)
            self.missing_industry += not engine.industry_map.get(symbol) and not any(b.get('industry') or b.get('sector') for b in bars)
            if not bars:
                self.warnings.append('missing_stock_data: '+symbol)
            if pre_count < 125:
                self.warnings.append('insufficient_warmup: '+symbol)
            if any(d not in seen for d in dates):
                self.warnings.append('missing_bars_or_suspension: '+symbol)
            self.db.commit()
        engine.notify_progress('prepare', len(engine.symbols), len(engine.symbols))
    def __getitem__(self, symbol):
        return self.info[symbol]
    def prepare_day(self, date, previous):
        self.days = {key: self.days[key] for key in (date, previous) if key in self.days}
        for key in (date, previous):
            if key not in self.days:
                self.days[key] = {s: (json.loads(b), json.loads(sig), json.loads(hold)) for s,b,sig,hold in
                    self.db.execute('SELECT symbol,bar,signal,holding FROM bars WHERE date=?', (key,))}
    def signal(self, symbol, date, holding=False):
        row = self.days.get(date, {}).get(symbol)
        return row[2 if holding else 1] if row else None
    def close(self):
        try:
            if self.db is not None:
                self.db.close()
                self.db = None
        finally:
            self.temp.cleanup()
