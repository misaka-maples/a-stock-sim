#!/usr/bin/env bash
# ==============================================================================
# A股模拟交易系统状态检查脚本
# ==============================================================================

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$DIR/server.pid"
LOG_FILE="$DIR/server.log"

echo "=========================================================="
echo "          A股模拟交易系统运行状态巡检                    "
echo "=========================================================="

RUNNING=0
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if ps -p "$PID" > /dev/null 2>&1; then
        RUNNING=1
        echo "[状态]: 🟢 正在运行中 (PID: $PID)"
        ps -p "$PID" -o pid,user,%cpu,%mem,etime,cmd
    fi
fi

if [ $RUNNING -eq 0 ]; then
    echo "[状态]: 🔴 未运行"
    echo "使用 ./start.sh 可启动后台服务。"
    exit 0
fi

echo ""
echo "--- [系统核心信息] ---"
QUANT_TASK_DIR="$DIR" python3 - <<'STATUS_PY'
import json, os
from pathlib import Path
import requests
try:
    private = Path(os.environ["QUANT_TASK_DIR"]) / "data/private/api_credentials.json"
    token = json.loads(private.read_text())["api_token"]
    headers = {"Authorization": "Bearer " + token}
    def get(path):
        response = requests.get("http://127.0.0.1:8000" + path, headers=headers, timeout=5)
        response.raise_for_status()
        return response.json()
    s, st = get("/api/summary"), get("/api/status")
    print(f"服务器时间:     {st['server_time']}")
    print(f"市场交易状态:   {st['market_status']}")
    print(f"全市场监控池:   {st['universe_count']} 只标的")
    print(f"自动策略状态:   {'已启用' if st['strategy_active'] else '已暂停'}")
    print(f"后台健康状态:   {'正常' if st.get('worker_healthy') else '异常'}")
    print(f"总资产 (权益):  ¥{s['total_equity']:,.2f}")
    print(f"可用现金:       ¥{s['cash']:,.2f}")
    print(f"持仓总市值:     ¥{s['market_value']:,.2f}")
    print(f"累计收益:       ¥{s['total_pnl']:+,.2f} ({s['total_pnl_pct']:+.2f}%)")
    print(f"当前持仓标的:   {s['position_count']} 只 | 成交总计: {s['trade_count']} 笔")
except Exception as exc:
    print(f"无法获取接口数据（检查私有凭据及服务状态）: {type(exc).__name__}")
STATUS_PY

echo ""
echo "--- [最新服务日志 (最后 10 行)] ---"
if [ -f "$LOG_FILE" ]; then
    tail -n 10 "$LOG_FILE"
else
    echo "暂无日志文件。"
fi
echo "=========================================================="

