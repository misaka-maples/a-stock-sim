"""Durable single-job queue. The HTTP process never executes research workloads."""
import datetime as dt
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

from trade_calendar import BEIJING

BASE = Path(__file__).resolve().parent
JOB_ROOT = BASE/'data'/'backtest_jobs'

def protected_time(now=None):
    now = now or dt.datetime.now(BEIJING)
    if now.tzinfo is None:
        raise ValueError('scheduler requires timezone-aware time')
    now = now.astimezone(BEIJING)
    # All weekdays, including holidays/unknown calendars, are conservative.
    # Lunch is protected too. This never opens a window due to missing calendars.
    return now.weekday() < 5 and dt.time(8, 0) <= now.time().replace(tzinfo=None) < dt.time(17, 0)

class JobStore:
    def __init__(self, root=JOB_ROOT):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, status TEXT NOT NULL, created REAL, updated REAL, arguments TEXT, context TEXT, progress TEXT, error TEXT, cancel INTEGER DEFAULT 0)')
    def connect(self):
        db = sqlite3.connect(str(self.root/'queue.sqlite'), timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        return db
    def submit(self, arguments, context):
        if arguments.get('strategy_name') not in ('Momentum_Rotation', 'Momentum_Rotation_Pro'):
            raise ValueError('小内存模式只接受已验证的动量流式回测；旧实验引擎不可运行')
        start, end = dt.date.fromisoformat(arguments['start_date']), dt.date.fromisoformat(arguments['end_date'])
        if start > end or (end-start).days > 3660:
            raise ValueError('回测日期无效或超过10年上限')
        if not 1 <= len(arguments.get('symbols', [])) <= 6000:
            raise ValueError('回测股票池需在1至6000只之间')
        encoded = json.dumps(arguments, ensure_ascii=False, allow_nan=False)
        if len(encoded) > 2_000_000:
            raise ValueError('回测参数过大')
        self.prune()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running') LIMIT 1").fetchone():
                raise RuntimeError('已有回测任务排队或运行，请等待完成或取消')
            job_id = uuid.uuid4().hex
            now = time.time()
            db.execute('INSERT INTO jobs(id,status,created,updated,arguments,context,progress) VALUES (?,?,?,?,?,?,?)',
                (job_id,'queued',now,now,encoded,json.dumps(context,ensure_ascii=False), '{}'))
        return self.get(job_id)
    def get(self, job_id, private=False):
        if not re.fullmatch('[0-9a-f]{32}', job_id):
            raise KeyError(job_id)
        with self.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        data = dict(row)
        data['progress'] = json.loads(data['progress'] or '{}')
        if private:
            data['arguments'], data['context'] = json.loads(data['arguments']),json.loads(data['context'])
        else:
            data.pop('arguments');data.pop('context')
            data['protected_window'] = protected_time()
        return data
    def list(self):
        with self.connect() as db:
            rows = db.execute('SELECT id FROM jobs ORDER BY created DESC LIMIT 30').fetchall()
        return [self.get(row['id']) for row in rows]
    def update(self, job_id, **values):
        assert set(values) <= {'status','progress','error','cancel'}
        if 'progress' in values:
            values['progress'] = json.dumps(values['progress'],ensure_ascii=False)
        values['updated'] = time.time()
        with self.connect() as db:
            db.execute('UPDATE jobs SET '+','.join(k+'=?' for k in values)+' WHERE id=?', (*values.values(),job_id))
    def cancel(self, job_id):
        job = self.get(job_id)
        if job['status'] in ('queued','running'):
            self.update(job_id,cancel=1,status='cancelled' if job['status']=='queued' else 'running')
        return self.get(job_id)
    def recover(self):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status=CASE WHEN cancel=1 THEN 'cancelled' ELSE 'queued' END,error='worker_restarted',updated=? WHERE status='running'",(time.time(),))
    def claim(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT id FROM jobs WHERE status='queued' AND cancel=0 ORDER BY created LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET status='running',updated=?,error=NULL WHERE id=?",(time.time(),row['id']))
        return self.get(row['id'],private=True)
    def prune(self, keep=10):
        # Results are reproducible research artifacts; account history is separate.
        with self.connect() as db:
            old = db.execute("SELECT id FROM jobs WHERE status IN ('completed','failed','cancelled') ORDER BY created DESC LIMIT -1 OFFSET ?",(keep,)).fetchall()
            for row in old:
                (self.root/(row['id']+'.json')).unlink(missing_ok=True)
                (self.root/(row['id']+'.tmp')).unlink(missing_ok=True)
                db.execute('DELETE FROM jobs WHERE id=?',(row['id'],))

    def result_path(self, job_id):
        self.get(job_id)
        return self.root/(job_id+'.json')
