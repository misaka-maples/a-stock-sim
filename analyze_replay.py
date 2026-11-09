import collections
from paper_replay import run_paper_replay

res = run_paper_replay("2024-09-02", "2025-10-31")
log = res["replay_log"]
monthly = collections.defaultdict(list)
for d in log:
    m = d["date"][:7]
    monthly[m].append(d)

print(f"{'Month':<8} | {'Act Eq':<10} | {'Theo Eq':<10} | {'TE %':<8} | {'Turnover%':<10} | {'Trades':<6}")
print("-" * 65)
for m, days in sorted(monthly.items()):
    last = days[-1]
    turn = sum(d["actual_turnover_pct"] for d in days) / len(days)
    tr = sum(d["trades_count"] for d in days)
    act_eq = last["nav_actual"] * 100000.0
    theo_eq = last["nav_ab_theoretical"] * 100000.0
    te = last["tracking_error_pct"]
    print(f"{m:<8} | {act_eq:<10.1f} | {theo_eq:<10.1f} | {te:<+8.2f}% | {turn:<10.2f}% | {tr:<6}")

print("\n" + "=" * 65)
print("Regime Switch Events Summary:")
print("=" * 65)
for evt in res["regime_switches"][:10]:
    print(f"{evt['date']}: {evt['from_regime']} -> {evt['to_regime']} | Net Trade: ¥{evt['net_volume']:,.2f} | Turn: {evt['actual_turnover_pct']}% | TE: {evt['tracking_error_pct']:+.2f}%")

print(f"\nTotal Regime switches: {len(res['regime_switches'])}")
