"""
check_results.py
=================
Run this any time (locally, or by pulling the repo) to see how the forward
test is doing across the whole tracked symbol universe. Doesn't touch the
running system -- read-only.

Usage:
    python check_results.py
"""

import json
from pathlib import Path

import pandas as pd

from leverage_flush_radar import summarize

DATA_DIR = Path(__file__).parent / "data"
SYMBOL_LIST_FILE = DATA_DIR / "symbol_list.json"

print("=" * 70)
print("FORWARD TEST STATUS -- ALL SYMBOLS")
print("=" * 70)

if not DATA_DIR.exists():
    print("No data/ directory yet -- the workflow hasn't run successfully yet.")
    raise SystemExit(0)

if SYMBOL_LIST_FILE.exists():
    meta = json.loads(SYMBOL_LIST_FILE.read_text())
    print(f"Tracked universe: {len(meta['symbols'])} symbols "
          f"(list last refreshed {meta['fetched_at']})")
print()

symbol_dirs = sorted(d for d in DATA_DIR.iterdir() if d.is_dir())

rows = []
all_trades = []

for d in symbol_dirs:
    symbol = d.name
    history_f = d / "history.csv"
    position_f = d / "open_position.csv"
    trade_f = d / "trade_log.csv"

    n_snapshots = 0
    last_seen = None
    if history_f.exists():
        h = pd.read_csv(history_f, parse_dates=["timestamp"])
        n_snapshots = len(h)
        if n_snapshots > 0:
            last_seen = h["timestamp"].max()

    is_open = "yes" if position_f.exists() else "no"

    n_trades, win_rate, avg_ret, cum_ret = 0, float("nan"), float("nan"), 0.0
    if trade_f.exists():
        trades = pd.read_csv(trade_f, parse_dates=["entry_time", "exit_time"])
        trades["symbol"] = symbol
        all_trades.append(trades)
        stats = summarize(trades)
        n_trades = stats["n_trades"]
        win_rate = stats["win_rate"]
        avg_ret = stats["avg_net_return_pct"]
        cum_ret = trades["net_return_pct"].sum()

    rows.append({
        "symbol": symbol,
        "snapshots": n_snapshots,
        "last_seen": last_seen,
        "open_pos": is_open,
        "closed_trades": n_trades,
        "win_rate_pct": round(win_rate * 100, 1) if n_trades > 0 else None,
        "avg_net_ret_pct": round(avg_ret, 3) if n_trades > 0 else None,
        "cum_net_ret_pct": round(cum_ret, 3),
    })

summary_df = pd.DataFrame(rows)

print(f"Symbols with data: {len(summary_df)}")
print(f"Currently open positions: {(summary_df['open_pos'] == 'yes').sum()}")
print(f"Symbols with >=1 closed trade: {(summary_df['closed_trades'] > 0).sum()}")
print()

print("Per-symbol breakdown (sorted by cumulative net return):")
print(summary_df.sort_values("cum_net_ret_pct", ascending=False).to_string(index=False))
print()

print("=" * 70)
print("COMBINED SUMMARY (all symbols, all closed trades)")
print("=" * 70)

if all_trades:
    combined = pd.concat(all_trades, ignore_index=True)
    stats = summarize(combined)
    print(f"Total closed trades: {stats['n_trades']}")
    print(f"  Win rate:          {stats['win_rate']*100:.1f}%")
    print(f"  Avg net return:    {stats['avg_net_return_pct']:.3f}% per trade")
    print(f"  Max drawdown:      {stats['max_drawdown_pct']:.3f}% (cumulative return points, trade-sequence order)")
    print(f"  Cumulative return: {combined['net_return_pct'].sum():.3f}% (sum across all trades/symbols)")
    print()
    print("Most recent 15 trades across all symbols:")
    print(combined.sort_values("exit_time").tail(15)[
        ["symbol", "entry_time", "exit_time", "direction", "exit_reason", "net_return_pct"]
    ].to_string(index=False))
else:
    print("No trades have closed yet anywhere -- either still accumulating history,")
    print("or no funding/OI extreme has fired yet. That's normal; this signal is")
    print("meant to be rare per symbol, not constant -- with 100 symbols running,")
    print("expect trades to trickle in unevenly rather than on a fixed schedule.")

print("=" * 70)
