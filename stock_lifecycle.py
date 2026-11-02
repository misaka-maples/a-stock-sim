"""Durable, explicitly confirmed delistings; historical price caches stay intact."""
import datetime as dt
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import tempfile

logger = logging.getLogger(__name__)


def _path(base_dir=None):
    base = Path(base_dir or os.environ.get('QUANT_MARKET_BASE_DIR', Path(__file__).resolve().parent))
    if base.is_file():
        base = base.parent
    if base.name == 'data':
        return base / 'delisted_stocks.json'
    return base / 'data' / 'delisted_stocks.json'


def _validated(row, today=None):
    if not isinstance(row, dict):
        return None
    code = str(row.get('code', '')).lower().replace('.', '')
    date = row.get('outDate', row.get('out_date', ''))
    if not re.fullmatch(r'(?:sh|sz|bj)\d{6}', code) or not isinstance(date, str):
        return None
    try:
        out_date = dt.date.fromisoformat(date)
    except ValueError:
        return None
    today = today or dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()
    if date != out_date.isoformat() or out_date > today:
        return None
    if str(row.get('status')) != '0' or str(row.get('type')) != '1':
        return None
    return dict(code=code, name=str(row.get('code_name', row.get('name', ''))),
                out_date=date, source='baostock', status='0', type='1',
                checked_at=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat())


def _read(path):
    try:
        records = json.loads(path.read_text(encoding='utf-8'))
        return records if isinstance(records, dict) else {}
    except (OSError, ValueError):
        return {}


def confirmed_delisted_symbols(base_dir=None):
    """Return only validated records; empty/error metadata never removes a stock."""
    path = _path(base_dir)
    return {valid['code'] for row in _read(path).values()
            if (valid := _validated(row)) is not None}


def filter_active_symbols(symbols, positions=None, base_dir=None):
    """Exclude confirmed delisted stocks from symbols, keeping any currently held."""
    delisted = confirmed_delisted_symbols(base_dir)
    if not delisted:
        return list(dict.fromkeys(symbols))
    held = set(positions or ())
    return [s for s in dict.fromkeys(symbols) if s not in delisted or s in held]


def record_delisted(rows, base_dir=None):
    """Merge provider query_stock_basic rows atomically, retaining prior evidence."""
    confirmed = {valid['code']: valid for row in rows if (valid := _validated(row)) is not None}
    if not confirmed:
        return set()
    path = _path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix('.lock'), 'a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        records = _read(path)
        records.update(confirmed)
        temp = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                             prefix='.delisted-', delete=False) as handle:
                temp = handle.name
                json.dump(records, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
            temp = None
        finally:
            if temp is not None:
                os.unlink(temp)
    return set(confirmed)


def sync_delisted_from_basic_file(file_path, base_dir=None):
    """Load stock basic dump from JSON file and record delisted stocks."""
    p = Path(file_path)
    if not p.exists():
        return set()
    try:
        rows = json.loads(p.read_text(encoding='utf-8'))
        if isinstance(rows, list):
            return record_delisted(rows, base_dir=base_dir)
    except Exception as exc:
        logger.warning("读取基础股票元数据失败: %s", exc)
    return set()


def sync_delisted_from_baostock(base_dir=None):
    """Query BaoStock stock basic metadata if available and record delisted stocks."""
    try:
        import baostock as bs
    except ImportError:
        return set()
    try:
        import contextlib
        import io
        from history_providers import _acquire_worker_lease, ProviderBusy
        try:
            lease = _acquire_worker_lease()
        except ProviderBusy:
            return set()
        with lease:
            with contextlib.redirect_stdout(io.StringIO()):
                login = bs.login()
            if getattr(login, "error_code", None) != "0":
                return set()
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    rs = bs.query_stock_basic()
                    rows = []
                    while rs.next():
                        rows.append(dict(zip(rs.fields, rs.get_row_data())))
                if getattr(rs, "error_code", None) == "0" and rows:
                    return record_delisted(rows, base_dir=base_dir)
            finally:
                with contextlib.redirect_stdout(io.StringIO()):
                    bs.logout()
    except Exception as exc:
        logger.warning("从BaoStock同步退市股票列表失败: %s", exc)
    return set()
