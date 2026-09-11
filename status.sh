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
python3 -c "
import requests, json
try:
    s = requests.get('http://127.0.0.1:8000/api/summary', timeout=2).json()
    st = requests.get('http://127.0.0.1:8000/api/status', timeout=2).json()
    print(f'服务器时间:     {st.get(\"server_time\")}')
    print(f'市场交易状态:   {st.get(\"market_status\")}')
    print(f'全市场监控池:   {st.get(\"universe_count\", 0)} 只标的 (严格排除科创板)')
    print(f'共振起爆捕获:   {st.get(\"radar_count\", 0)} 只焦点标的')
    print(f'自动策略状态:   {\"已启用\" if st.get(\"strategy_active\") else \"已暂停\"}')
    print(f'总资产 (权益):  ¥{s.get(\"total_equity\", 0):,.2f}')
    print(f'可用现金:       ¥{s.get(\"cash\", 0):,.2f}')
    print(f'持仓总市值:     ¥{s.get(\"market_value\", 0):,.2f}')
    print(f'累计浮盈:       ¥{s.get(\"total_pnl\", 0):+,.2f} ({s.get(\"total_pnl_pct\", 0):+.2f}%)')
    print(f'当前持仓标的:   {s.get(\"position_count\", 0)} 只 | 成交总计: {s.get(\"trade_count\", 0)} 笔')
except Exception as e:
    print(f'无法获取接口数据: {e}')
" 2>/dev/null

echo ""
echo "--- [最新服务日志 (最后 10 行)] ---"
if [ -f "$LOG_FILE" ]; then
    tail -n 10 "$LOG_FILE"
else
    echo "暂无日志文件。"
fi
echo "=========================================================="

