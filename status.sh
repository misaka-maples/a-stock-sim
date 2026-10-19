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

    q = st.get('data_quality', {})
    sh = q.get('source_health', {})
    rb = sh.get('request_budget', {})
    circuits = sh.get('circuits', {})

    print()
    print("--- [策略数据与历史更新情况] ---")
    st_status = q.get('status', 'not_loaded')
    st_map = {'ready': '🟢 日线已就绪', 'partial': '🟡 部分日线可用', 'loading': '🔄 日线准备中', 'failed': '🔴 日线不可用'}
    print(f"数据更新状态:   {st_map.get(st_status, st_status)} (决策基准日: {q.get('as_of', '待获取')})")
    print(f"历史数据覆盖:   {q.get('loaded', 0)}/{q.get('requested', 0)} 只标的 (不可用: {q.get('unavailable', 0)} 只)")
    cov = q.get('ranking_coverage')
    cov_str = f"{cov*100:.1f}%" if cov is not None else "--"
    rot_str = "🟢 满足轮动门槛(>=95%)" if q.get('rotation_ready') else "🔴 覆盖不足，暂停轮动"
    print(f"截面排名覆盖:   {cov_str} | 轮动条件: {rot_str}")
    hold_ok = q.get('holding_history_ready', False)
    missing_h = q.get('missing_holding_symbols', [])
    if hold_ok:
        pos_cnt = s.get('position_count', 0)
        hold_str = f"🟢 全部就绪 ({pos_cnt}/{pos_cnt} 已核验)"
    elif missing_h:
        miss_str = ', '.join(missing_h)
        hold_str = f"🔴 存在缺失: {miss_str}"
    else:
        hold_str = "🟡 待核验"
    bm_str = "🟢 正常" if q.get('benchmark_available') else "🔴 异常"
    print(f"持仓股票历史:   {hold_str} | 沪深300基准: {bm_str}")
    reasons = q.get('unavailable_reason_counts', {})
    if reasons:
        r_str = ', '.join(f"{k}: {v}只" for k, v in reasons.items())
        print(f"不可用分类:     {r_str}")
    up_mode = '盘中防冲击补数' if q.get('update_mode') == 'holding_and_missing_only' else (q.get('update_mode') or '--')
    print(f"调度策略模式:   {up_mode} | 下次重试倒计时: {q.get('next_failed_retry_seconds', '--')}s")

    print()
    print("--- [数据接口获取状态与配额] ---")
    for name, label in [('baostock', 'BaoStock (宝塔)'), ('eastmoney', 'EastMoney(东财)'), ('tencent', 'Tencent (腾讯)'), ('tushare', 'TuShare (备用)')]:
        b = rb.get(name, {})
        c = circuits.get(name, {})
        state = c.get('state', 'closed')
        state_icon = '🟢' if state == 'closed' else '🔴'
        used = b.get('used', 0)
        limit = b.get('daily_limit', '--')
        remaining = b.get('remaining', '--')
        fails = b.get('failures', 0)
        interval = b.get('interval', 1.0)
        extra = b.get('reason') or f"熔断器:{state}"
        print(f"  {state_icon} {label:<16}: 今日已用: {used:>6}/{str(limit):<5} (余{str(remaining):>5}) | 失败: {fails:>2} | 间隔: {interval:>4.1f}s | {extra}")
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

