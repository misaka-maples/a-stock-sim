"""Durable, per-user upstream admission control (one reservation per wire request).

All project processes must use the same root. The lock is held through the network
operation, so a successful admission also serializes requests across processes.
An SDK integration must wrap its packet send/receive boundary, including login,
logout and pagination, rather than wrapping a high-level stock download once.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time


SHANGHAI = timezone(timedelta(hours=8))


class RequestAdmissionError(RuntimeError):
    def __init__(self, code, provider, retry_after=0):
        self.code = code
        self.provider = provider
        self.retry_after = max(0., float(retry_after))
        super().__init__(f'{provider}:{code}')


class RequestBudgetExceeded(RequestAdmissionError):
    pass


class RequestQueueBusy(RequestAdmissionError):
    pass


class RequestBudgetUnavailable(RequestAdmissionError):
    pass


class MarketRequestBudget:
    def __init__(self, root=None, *, clock=time.time, monotonic=time.monotonic,
                 sleep=time.sleep, max_wait=15., policies=None,
                 block_untracked_baostock_day=True):
        self.root = Path(root or Path.home()/'.cache/openarmx-quant/request_budget')
        self.clock, self.monotonic, self.sleep = clock, monotonic, sleep
        self.max_wait = self._positive(max_wait)
        self.block_untracked_baostock_day = block_untracked_baostock_day
        self.policies = policies or {}

    @staticmethod
    def _positive(value):
        value = float(value)
        if not math.isfinite(value) or value <= 0:
            raise ValueError('expected positive finite value')
        return value

    def _policy(self, provider):
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,40}', provider):
            raise ValueError('invalid provider')
        values = self.policies.get(provider, {})
        hard_cap = 40000 if provider == 'baostock' else 10000
        cap = int(values.get('daily_limit', hard_cap))
        # Configuration can lower the safety budget, never exceed its ceiling.
        if cap < 1 or cap > hard_cap:
            raise ValueError('daily limit exceeds safe ceiling')
        interval = max(1., self._positive(values.get('min_interval', 1.)))
        return cap, interval

    def _day(self, now):
        return datetime.fromtimestamp(now, SHANGHAI).date().isoformat()

    def _tomorrow(self, now):
        today = datetime.fromtimestamp(now, SHANGHAI)
        return (today.replace(hour=0, minute=0, second=0, microsecond=0)
                + timedelta(days=1)).timestamp()

    @contextmanager
    def _lock(self, provider, deadline):
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            handle = os.open(self.root/f'{provider}.lock', os.O_CREAT|os.O_RDWR, 0o600)
        except OSError as exc:
            raise RequestBudgetUnavailable('state_unavailable', provider) from exc
        try:
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX|fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline-self.monotonic()
                    if remaining <= 0:
                        raise RequestQueueBusy('queue_busy', provider, .1)
                    self.sleep(min(.05, remaining))
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            os.close(handle)

    def _save(self, provider, state):
        path = self.root/f'{provider}.json'
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=f'.{provider}.', dir=self.root)
            with os.fdopen(fd, 'w') as stream:
                json.dump(state, stream, allow_nan=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except (OSError, ValueError) as exc:
            raise RequestBudgetUnavailable('state_unavailable', provider) from exc
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    def _load(self, provider, now, interval, *, persist=True):
        path = self.root/f'{provider}.json'
        existed = path.exists()
        try:
            state = json.loads(path.read_text()) if existed else None
            if state is not None:
                if (not isinstance(state, dict) or state.get('version') != 1
                        or not isinstance(state.get('day'), str)
                        or type(state.get('used')) is not int or state['used'] < 0
                        or type(state.get('failures')) is not int or state['failures'] < 0
                        or type(state.get('usage_known')) is not bool):
                    raise ValueError('invalid state')
                datetime.strptime(state['day'], '%Y-%m-%d')
                for field in ('next_allowed_at', 'blocked_until', 'interval'):
                    if not math.isfinite(float(state[field])) or float(state[field]) < 0:
                        raise ValueError('invalid state')
                if state['interval'] < 1.:
                    raise ValueError('unsafe interval')
                # A backward wall-clock jump cannot reset a day's reservations.
                if state['day'] > self._day(now):
                    raise ValueError('clock moved backwards')
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RequestBudgetUnavailable('invalid_state', provider) from exc
        if state is None or state['day'] != self._day(now):
            unknown = (not existed and provider == 'baostock'
                       and self.block_untracked_baostock_day)
            state = dict(version=1, day=self._day(now), used=0, failures=0,
                         usage_known=not unknown, interval=interval,
                         next_allowed_at=0.,
                         blocked_until=self._tomorrow(now) if unknown else 0.)
            if persist:
                self._save(provider, state)
        return state

    @contextmanager
    def request(self, provider):
        """Reserve durably before I/O; failed requests count too. Not reentrant.

        Raises RequestBudgetExceeded for daily limits/untracked first BaoStock day,
        RequestQueueBusy for lock/rate waits exceeding max_wait, or
        RequestBudgetUnavailable if admission cannot be persisted safely.
        """
        cap, interval = self._policy(provider)
        deadline = self.monotonic()+self.max_wait
        with self._lock(provider, deadline):
            while True:
                now = self.clock()
                state = self._load(provider, now, interval)
                state['interval'] = max(interval, state['interval'])
                if state['blocked_until'] > now or state['used'] >= cap:
                    code = 'untracked_prior_usage' if not state['usage_known'] else 'daily_limit'
                    raise RequestBudgetExceeded(code, provider, self._tomorrow(now)-now)
                delay = max(0., state['next_allowed_at']-now)
                if delay <= 0:
                    break
                if delay > deadline-self.monotonic():
                    raise RequestQueueBusy('rate_wait', provider, delay)
                self.sleep(delay)
            state['used'] += 1
            state['next_allowed_at'] = now+state['interval']
            self._save(provider, state)
            try:
                yield dict(day=state['day'], used=state['used'], daily_limit=cap)
            except RequestAdmissionError:
                # An inner local queue/quota refusal is not an upstream failure.
                # The durable reservation stays counted conservatively.
                raise
            except BaseException:
                state['failures'] = min(16, state['failures']+1)
                state['interval'] = min(60., max(interval, state['interval']*2))
                state['next_allowed_at'] = max(state['next_allowed_at'],
                                               self.clock()+state['interval'])
                self._save(provider, state)
                raise
            else:
                state['failures'] = max(0, state['failures']-1)
                state['interval'] = max(interval, state['interval']*.8)
                state['next_allowed_at'] = max(state['next_allowed_at'],
                                               self.clock()+state['interval'])
                self._save(provider, state)

    def initialize(self, provider):
        """Create/roll the ledger under a short lock without reserving a request.

        Call at the first real provider attempt (or explicit deployment setup),
        not on status polling. An unknown first BaoStock day is recorded as
        paused through midnight without consuming any request allowance.
        """
        _, interval = self._policy(provider)
        with self._lock(provider, self.monotonic()+self.max_wait):
            self._load(provider, self.clock(), interval)
        return self.status(provider)

    def status(self, provider):
        """Read the last atomic snapshot without locking, waiting or writing.

        In-flight admission may advance immediately after this read. Missing and
        next-day state are projected only; request() performs initialization under
        the lock. Status polling therefore cannot start the unknown-usage pause.
        """
        cap, interval = self._policy(provider)
        now = self.clock()
        state = self._load(provider, now, interval, persist=False)
        return dict(day=state['day'], used=state['used'], daily_limit=cap,
                    remaining=max(0, cap-state['used']) if state['usage_known'] else 0,
                    usage_known=state['usage_known'], interval=state['interval'],
                    failures=state['failures'],
                    reason=('untracked_prior_usage' if not state['usage_known'] else
                            'daily_limit' if state['used'] >= cap else
                            'rate_wait' if state['next_allowed_at'] > now else ''),
                    retry_after=max(0., state['blocked_until']-now,
                                    state['next_allowed_at']-now,
                                    self._tomorrow(now)-now if state['used'] >= cap else 0.))
