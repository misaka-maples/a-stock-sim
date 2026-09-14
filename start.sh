#!/usr/bin/env bash
# ==============================================================================
# A股模拟交易系统后台启动脚本 (常驻守护进程)
# ==============================================================================

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$DIR/server.pid"
LOG_FILE="$DIR/server.log"

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if ps -p "$PID" > /dev/null 2>&1; then
        echo "[!] 服务已在运行中 (PID: $PID)"
        echo "    Web 访问地址: http://127.0.0.1:8000"
        echo "    若需重启请先运行: ./stop.sh"
        exit 1
    else
        echo "[*] 清理已失效的 PID 文件..."
        rm -f "$PID_FILE"
    fi
fi

echo "[*] 正在启动 A股模拟交易系统服务..."
# 默认传递所有脚本传入参数（如 --test, --port 8080 等）
nohup python3 "$DIR/server.py" "$@" < /dev/null >> "$LOG_FILE" 2>&1 &
SERVER_PID=$!
disown "$SERVER_PID" 2>/dev/null || true
echo "$SERVER_PID" > "$PID_FILE"

# 等待 1.5 秒检查是否成功启动
sleep 1.5
if ps -p "$SERVER_PID" > /dev/null 2>&1; then
    SERVER_IP=$(ip -4 addr show scope global 2>/dev/null | grep inet | awk '{print $2}' | cut -d/ -f1 | head -n 1)
    echo "[✓] 服务已成功在后台启动！"
    echo "    进程 PID: $SERVER_PID"
    echo "    Web 控制台: http://${SERVER_IP:-127.0.0.1}:8000"
    echo "    本地访问:   http://127.0.0.1:8000"
    echo "    Swagger:    http://${SERVER_IP:-127.0.0.1}:8000/docs"
    echo "    日志文件: $LOG_FILE"
    echo "    查看运行状态: ./status.sh"
    echo "    停止服务: ./stop.sh"
else
    echo "[✗] 服务启动失败，请检查日志:"
    tail -n 20 "$LOG_FILE"
    rm -f "$PID_FILE"
    exit 1
fi
