"""Linux resource admission and bounded application logging (single worker)."""
import ctypes
import gc
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import sys

GIB = 1024 ** 3


def positive_gib(name, default):
    value = float(os.environ.get(name, default))
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite GiB value")
    return int(value * GIB)


def memory_snapshot(proc=Path('/proc'), cgroup_root=Path('/sys/fs/cgroup')):
    result = dict(rss_bytes=None, available_bytes=None, cgroup_headroom_bytes=None)
    try:
        for line in (proc / 'self/status').read_text().splitlines():
            if line.startswith('VmRSS:'):
                result['rss_bytes'] = int(line.split()[1]) * 1024
        for line in (proc / 'meminfo').read_text().splitlines():
            if line.startswith('MemAvailable:'):
                result['available_bytes'] = int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    # Include ancestor constraints: a service may inherit a tighter slice limit.
    try:
        group = next(line.split(':', 2)[2] for line in (proc / 'self/cgroup').read_text().splitlines()
                     if line.startswith('0::'))
        node = cgroup_root / group.lstrip('/')
        while node == cgroup_root or cgroup_root in node.parents:
            try:
                limit = (node / 'memory.max').read_text().strip()
                if limit != 'max':
                    headroom = max(0, int(limit) - int((node / 'memory.current').read_text()))
                    previous = result['cgroup_headroom_bytes']
                    result['cgroup_headroom_bytes'] = headroom if previous is None else min(previous, headroom)
            except (OSError, ValueError):
                pass
            if node == cgroup_root:
                break
            node = node.parent
    except (OSError, ValueError, StopIteration):
        pass
    available = [n for n in (result['available_bytes'], result['cgroup_headroom_bytes']) if n is not None]
    result['effective_available_bytes'] = min(available) if available else None
    result['backtest_required_bytes'] = positive_gib('OPENARMX_BACKTEST_REQUIRED_GIB', 6)
    result['healthy_min_bytes'] = positive_gib('OPENARMX_MEMORY_HEALTH_MIN_GIB', .5)
    effective = result['effective_available_bytes']
    result['memory_healthy'] = effective is not None and effective >= result['healthy_min_bytes']
    result['backtest_allowed'] = effective is not None and effective >= result['backtest_required_bytes']
    return result


def release_unused_memory():
    """Best effort; malloc_trim is opt-in and never a correctness guarantee."""
    gc.collect()
    if sys.platform.startswith('linux') and os.environ.get('OPENARMX_MALLOC_TRIM', '0') == '1':
        try:
            trim = ctypes.CDLL(None).malloc_trim
            trim.argtypes = [ctypes.c_size_t]
            trim.restype = ctypes.c_int
            trim(0)
        except (AttributeError, OSError):
            logging.getLogger(__name__).debug('malloc_trim unavailable', exc_info=True)


def configure_logging(default_path):
    path = Path(os.environ.get('OPENARMX_LOG_FILE', str(default_path)))
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=50 * 1024 ** 2, backupCount=5, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s'))
    root = logging.getLogger()
    for previous in list(root.handlers):
        root.removeHandler(previous)
        previous.close()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for name in ('uvicorn', 'uvicorn.error'):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
