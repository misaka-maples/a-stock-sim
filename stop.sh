#!/usr/bin/env bash
# Stop only this project's server and allow its durable shutdown to complete.
set -eu
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 - "$DIR" <<'STOP_PY'
import os, signal, sys, time
from pathlib import Path
root = Path(sys.argv[1]).resolve()
pid_file = root / "server.pid"
def ours(pid):
    try:
        proc = Path("/proc") / str(pid)
        args = proc.joinpath("cmdline").read_bytes().split(b"\0")
        expected = str(root / "server.py").encode()
        if expected in args:
            return True
        return b"server.py" in args and proc.joinpath("cwd").resolve() == root
    except OSError:
        return False
if pid_file.exists():
    raw = pid_file.read_text().strip()
    if not raw.isdigit():
        raise SystemExit("PID文件无效，请检查；未停止任何进程")
    pid = int(raw)
    if Path(f"/proc/{pid}").exists() and not ours(pid):
        raise SystemExit("PID属于其他程序；未停止任何进程")
    pids = [pid] if ours(pid) else []
else:
    pids = [int(p.name) for p in Path("/proc").iterdir() if p.name.isdigit() and ours(int(p.name))]
for pid in pids:
    print(f"[*] 正在停止本项目服务 (PID: {pid})...", flush=True)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        continue
    deadline = time.monotonic() + 30
    while ours(pid) and time.monotonic() < deadline:
        time.sleep(.2)
    if ours(pid):
        raise SystemExit("服务仍在退出，未强制终止；请检查后台任务后重试")
pid_file.unlink(missing_ok=True)
print("[✓] 本项目服务已停止。" if pids else "[*] 本项目服务当前未运行。")
STOP_PY
