#!/usr/bin/env bash
# ==============================================================================
# A股模拟交易系统后台停止脚本
# ==============================================================================

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$DIR/server.pid"

if [ ! -f "$PID_FILE" ]; then
    # 尝试按进程名查找
    PIDS=$(pgrep -f "python3.*server.py")
    if [ -z "$PIDS" ]; then
        echo "[*] 服务当前未在运行。"
        exit 0
    fi
    echo "[*] 发现运行中的进程 PID: $PIDS"
    kill -15 $PIDS
    sleep 1
    echo "[✓] 已停止运行。"
    exit 0
fi

PID=$(cat "$PID_FILE")
if ps -p "$PID" > /dev/null 2>&1; then
    echo "[*] 正在停止 A股模拟交易系统服务 (PID: $PID)..."
    kill -15 "$PID"
    
    # 等待安全退出并保存持久化状态
    for i in {1..10}; do
        if ! ps -p "$PID" > /dev/null 2>&1; then
            break
        fi
        sleep 0.5
    done

    if ps -p "$PID" > /dev/null 2>&1; then
        echo "[!] 进程未响应 SIGTERM，强制关闭..."
        kill -9 "$PID"
    fi
    echo "[✓] 服务已安全停止，数据已保存至 sim_account.json。"
else
    echo "[*] 服务此前未运行。"
fi

rm -f "$PID_FILE"

