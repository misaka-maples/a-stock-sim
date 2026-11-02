"""Low-priority, cache-only research runner, supervised outside the web service."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

from backtest_jobs import JobStore, JOB_ROOT, BASE, protected_time

STOP = False

def stopping(*_):
    global STOP
    STOP = True

def live_ready():
    """Health is mandatory; data refresh and main service always take priority."""
    try:
        private = json.loads((BASE/'data/private/api_credentials.json').read_text())
        req = urllib.request.Request('http://127.0.0.1:8000/api/status', headers={'Authorization':'Bearer '+private['api_token']})
        with urllib.request.urlopen(req,timeout=2) as response:
            status=json.load(response)
        return (bool(status.get('worker_healthy')) and not status.get('ignore_market_hours', False)
                and not status.get('is_trading_time', False) and not status.get('closing_report_pending', False)
                and not status.get('data_quality',{}).get('refreshing',False))
    except Exception:
        return False

def host_headroom():
    try:
        value = next(int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:'))
        return value >= 900*1024**2
    except Exception:
        return False

def memory_emergency():
    try:
        available = next(int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:'))
        return available < 350*1024**2
    except Exception:
        return True


def write_result(path, result):
    from streaming_history import DiskList
    tmp=path.with_suffix('.tmp')
    try:
        with tmp.open('w') as stream:
            stream.write('{')
            for n,(key,value) in enumerate(result.items()):
                if n: stream.write(',')
                stream.write(json.dumps(key)+':')
                if isinstance(value,DiskList):
                    stream.write('[')
                    for index,row in enumerate(value):
                        if index: stream.write(',')
                        json.dump(row,stream,ensure_ascii=False,allow_nan=False)
                        if index % 100 == 0 and stream.tell() > 256*1024**2:
                            raise ValueError('回测结果超过256MiB上限，请缩短区间或缩小股票池')
                    stream.write(']')
                else:
                    json.dump(value,stream,ensure_ascii=False,allow_nan=False)
            stream.write('}')
            stream.flush();os.fsync(stream.fileno())
        os.replace(tmp,path)
    finally:
        tmp.unlink(missing_ok=True)

def execute(store, job):
    from momentum_backtest import MomentumBacktestEngine
    from streaming_history import CacheOnlyFeed
    started=time.monotonic()
    def progress(phase,done,total):
        if STOP or protected_time() or store.get(job['id'])['cancel']:
            raise InterruptedError('protected_window_or_cancelled')
        store.update(job['id'],progress={'phase':phase,'done':done,'total':total,'elapsed_seconds':round(time.monotonic()-started,1)})
    # Compute child cannot accidentally call any market/network provider.
    import socket
    def deny_network(*args, **kwargs):
        raise RuntimeError('backtest_network_forbidden')
    socket.socket.connect = deny_network
    engine=MomentumBacktestEngine(**job['arguments'],feed=CacheOnlyFeed(),streaming=True,
            spill_results=True,work_dir=store.root,progress=progress)
    try:
        result=engine.run()
        progress('writing',0,1)
        result['request_context']=job['context']
        import resource
        result['runtime']={'elapsed_seconds':round(time.monotonic()-started,3),'peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,'mode':'cache_only_streaming'}
        write_result(store.result_path(job['id']),{'success':True,'data':result})
    finally:
        engine.close()

def supervise(store):
    lock=(store.root/'worker.lock').open('a')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    store.recover()
    child=None
    try:
        while not STOP:
            if protected_time() or not host_headroom() or not live_ready():
                time.sleep(5);continue
            if shutil.disk_usage(store.root).free < 2*1024**3:
                time.sleep(30);continue
            job=store.claim()
            if not job:
                time.sleep(5);continue
            child=subprocess.Popen([sys.executable,__file__,'--job',job['id'],'--root',str(store.root)],start_new_session=True)
            reason=None;last_health=0;ready=True
            while child.poll() is None:
                if time.monotonic()-last_health >= 5:
                    ready=live_ready();last_health=time.monotonic()
                if STOP or protected_time() or not ready or memory_emergency() or store.get(job['id'])['cancel']:
                    reason='cancelled' if store.get(job['id'])['cancel'] else 'deferred_for_live_strategy'
                    os.killpg(child.pid,signal.SIGTERM)
                    try:child.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid,signal.SIGKILL);child.wait()
                    break
                time.sleep(.25)
            if reason:
                store.result_path(job['id']).unlink(missing_ok=True)
                store.update(job['id'],status='cancelled' if reason=='cancelled' else 'queued',error=reason)
            elif child.returncode == 0 and store.result_path(job['id']).exists():
                store.update(job['id'],status='completed',progress={'phase':'complete'},error=None)
            else:
                store.update(job['id'],status='failed',error=f'worker_exit_{child.returncode}; inspect worker log')
            # Temporary research databases are disposable; never touch live caches.
            for path in store.root.glob('backtest-stream-*'):
                if path.is_dir() and not path.is_symlink():shutil.rmtree(path)
            child=None
    finally:
        if child and child.poll() is None:
            os.killpg(child.pid,signal.SIGKILL);child.wait()
        lock.close()

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--job');parser.add_argument('--root',type=Path,default=JOB_ROOT)
    args=parser.parse_args()
    signal.signal(signal.SIGTERM,stopping);signal.signal(signal.SIGINT,stopping)
    os.umask(0o077)
    store=JobStore(args.root)
    if args.job:
        if protected_time():
            raise SystemExit("protected_window")
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (512*1024**2,512*1024**2))
        execute(store,store.get(args.job,private=True))
    else:
        supervise(store)

if __name__=='__main__':main()
