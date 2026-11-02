"""Transactional, bounded-memory account storage.

No service selects this backend implicitly. State, journal records and accounting
rollups commit together. Historical rows are immutable and read in bounded pages;
SQLite online backups include committed WAL contents.
"""
import json
import sqlite3
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def cents(value):
    return int((Decimal(str(value)) * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


class LedgerConflict(RuntimeError):
    pass


class SQLiteLedger:
    """Short-lived connections allow callers on different threads.

    Revision checking prevents a stale account from overwriting a newer commit.
    Callers must stop trading on any commit exception and reload before retrying.
    """
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS account (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    revision INTEGER NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS journal (
                    kind TEXT NOT NULL CHECK(kind IN ('trades','orders')),
                    seq INTEGER NOT NULL, date TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(kind,seq));
                CREATE INDEX IF NOT EXISTS journal_date ON journal(kind,date,seq);
                CREATE TABLE IF NOT EXISTS rollups (
                    scope TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(scope,key));
            ''')

    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=10)
        db.execute('PRAGMA synchronous=FULL')
        db.execute('PRAGMA cache_size=-2048')
        return db

    def load(self):
        with self.connect() as db:
            row = db.execute('SELECT revision,payload FROM account WHERE singleton=1').fetchone()
        return (row[0], json.loads(row[1])) if row else (0, None)

    @staticmethod
    def _rollup(db, scope, key, trade):
        row = db.execute('SELECT payload FROM rollups WHERE scope=? AND key=?', (scope,key)).fetchone()
        value = json.loads(row[0]) if row else dict(count=0, fees_cents=0, realized_cents=0,
            wins=0, losses=0, closed=0, win_cents=0, loss_cents=0,
            buy_cents=0, sell_cents=0)
        value['count'] += 1
        value['fees_cents'] += cents(trade.get('fees',0))
        side = trade.get('side')
        if side == 'BUY':
            value['buy_cents'] += cents(trade.get('amount',0))
        elif side == 'SELL':
            pnl = cents(trade.get('realized_pnl',0))
            value['sell_cents'] += cents(trade.get('amount',0))
            value['realized_cents'] += pnl
            value['closed'] += 1
            value['wins'] += pnl > 0
            value['losses'] += pnl < 0
            value['win_cents'] += max(pnl,0)
            value['loss_cents'] += max(-pnl,0)
        value['name'] = trade.get('name', trade.get('symbol',''))
        value['last_price'] = trade.get('price',0)
        db.execute('INSERT OR REPLACE INTO rollups VALUES (?,?,?)', (scope,key,encode(value)))

    def commit(self, state, *, expected_revision, trades=(), orders=(), order_updates=()):
        """Append iterables and checkpoint state in one FULL synchronous transaction.

        The account payload must exclude history. Input generators are consumed
        once inside the transaction; validation or I/O failures roll back all rows.
        """
        if 'trades' in state or 'orders' in state:
            raise ValueError('History must be passed separately from account state')
        payload = encode(state)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT revision FROM account WHERE singleton=1').fetchone()
            revision = row[0] if row else 0
            if revision != expected_revision:
                raise LedgerConflict(f'stale account revision: {expected_revision} != {revision}')
            for kind, records in (('trades',trades),('orders',orders)):
                seq = db.execute('SELECT COALESCE(MAX(seq),0) FROM journal WHERE kind=?',(kind,)).fetchone()[0]
                for record in records:
                    seq += 1
                    date = str(record.get('time',''))[:10]
                    db.execute('INSERT INTO journal VALUES (?,?,?,?)',(kind,seq,date,encode(record)))
                    if kind == 'trades':
                        for scope,key in (('total','all'),('symbol',record.get('symbol','')),('day',date)):
                            self._rollup(db,scope,key,record)
            for seq, record in order_updates:
                changed = db.execute("UPDATE journal SET payload=? WHERE kind='orders' AND seq=?",
                                     (encode(record),seq)).rowcount
                if changed != 1:
                    raise ValueError('Missing order to update')
            db.execute('INSERT OR REPLACE INTO account VALUES (1,?,?)',(revision+1,payload))
        return revision + 1

    def count(self, kind):
        self._kind(kind)
        with self.connect() as db:
            return db.execute('SELECT COALESCE(MAX(seq),0) FROM journal WHERE kind=?',(kind,)).fetchone()[0]

    @staticmethod
    def _kind(kind):
        if kind not in ('trades','orders'):
            raise ValueError('Unknown journal kind')

    def page(self, kind, *, before=None, limit=100, date=None):
        """Newest first, exclusive sequence cursor (stable while appending)."""
        self._kind(kind)
        if not isinstance(limit,int) or not 1 <= limit <= 1000:
            raise ValueError('limit must be in 1..1000')
        query = 'SELECT seq,payload FROM journal WHERE kind=?'
        args = [kind]
        if before is not None:
            query += ' AND seq<?'
            args.append(before)
        if date is not None:
            query += ' AND date=?'
            args.append(date)
        query += ' ORDER BY seq DESC LIMIT ?'
        args.append(limit)
        with self.connect() as db:
            rows = db.execute(query,args).fetchall()
        return [dict(sequence=seq, record=json.loads(payload)) for seq,payload in rows]

    def iterate(self, kind):
        """Chronological streaming export, fixed 256-record batches."""
        self._kind(kind)
        with self.connect() as db:
            cursor = db.execute('SELECT payload FROM journal WHERE kind=? ORDER BY seq',(kind,))
            while True:
                rows = cursor.fetchmany(256)
                if not rows:
                    break
                for row in rows:
                    yield json.loads(row[0])

    def aggregate(self, scope='total', key='all'):
        with self.connect() as db:
            row = db.execute('SELECT payload FROM rollups WHERE scope=? AND key=?',(scope,key)).fetchone()
        return json.loads(row[0]) if row else {}

    def aggregate_page(self, scope, *, after='', limit=100):
        if scope not in ('symbol','day') or not 1 <= limit <= 1000:
            raise ValueError('Invalid aggregate page')
        with self.connect() as db:
            rows = db.execute('SELECT key,payload FROM rollups WHERE scope=? AND key>? ORDER BY key LIMIT ?',
                              (scope,after,limit)).fetchall()
        return [(key,json.loads(payload)) for key,payload in rows]

    def backup(self, destination):
        """Use SQLite backup rather than copying a live DB and missing its WAL."""
        destination = Path(destination)
        if destination.exists():
            raise FileExistsError(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.connect() as source, sqlite3.connect(str(destination)) as target:
                source.backup(target, pages=128)
                if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('Backup failed integrity check')
        except Exception:
            destination.unlink(missing_ok=True)
            raise
