# Leverage Flush Radar — Autonomous Forward Test (100 symbols)

Runs itself, for free, on a schedule you don't have to babysit. Every 15
minutes it pulls live funding rate / open interest / price for the **top
100 USDT-margined perpetuals on Binance by 24h volume**, checks the signal
on each one independently, manages up to 100 paper positions in parallel,
and commits the results back to this repo. Come back in a month and run
`check_results.py`.

No API key needed (public market data only). No money at risk (paper
trades only — nothing ever touches a real exchange account).

## How it's different from the single-pair version

- **Symbol universe**: instead of a hardcoded `BTCUSDT`, the tracked set is
  the top 100 USDT-M perpetuals by 24h quote volume, re-ranked automatically
  once a week (cached in `data/symbol_list.json` so it doesn't jitter every
  cycle).
- **Bulk price fetch**: mark price + funding rate for *all* symbols come
  back in a single API call (`/fapi/v1/premiumIndex`, no `symbol` param).
- **Per-symbol open interest**: there's no bulk OI endpoint, so this is one
  call per tracked symbol, each on its own try/except — one bad, delisted,
  or rate-limited symbol never takes down the other 99.
- **Per-symbol state**: each symbol gets its own folder under `data/` with
  its own `history.csv`, `open_position.csv`, and `trade_log.csv`, so
  results are never mixed across symbols.

## Files

| File | What it does |
|---|---|
| `leverage_flush_radar.py` | Signal logic (funding z-score + OI rate-of-change + vol regime filter) and the offline backtester. Identical to the single-pair version — this file is symbol-agnostic. |
| `forward_test_signal_logger.py` | Runs **one cycle across the whole tracked universe**: refresh/load symbol list → bulk-fetch price+funding → per-symbol fetch OI → per-symbol update/close/open. Called repeatedly by the cron. |
| `.github/workflows/forward_test.yml` | The scheduler. Runs the cycle every 15 min and commits the updated `data/` folder. |
| `check_results.py` | Read-only status check — per-symbol breakdown plus a combined summary across every symbol's closed trades. |
| `_offline_test.py` | Offline harness that mocks all network calls and simulates many cycles across N fake symbols (including one that randomly fails) to verify fault isolation and state handling without hitting Binance. Not needed for deployment — just for validating changes before you push. |
| `data/symbol_list.json` | Cached top-100 symbol list (created automatically, re-ranked weekly). |
| `data/<SYMBOL>/history.csv` | Every snapshot ever taken for that symbol (created automatically). |
| `data/<SYMBOL>/open_position.csv` | That symbol's current open paper trade, if any (0 or 1 rows). |
| `data/<SYMBOL>/trade_log.csv` | That symbol's closed paper trades — **this is what accumulates the record.** |

## Setup (5 minutes, one time)

1. **Create a new GitHub repo** (public — private repos on the free tier
   have limited Actions minutes, public repos are unlimited for this kind
   of lightweight job).
2. **Upload all the files in this folder**, preserving the `.github/workflows/`
   folder structure exactly — GitHub only recognizes workflows in that
   specific path. You do **not** need to create the `data/` folder yourself —
   it's created automatically on the first run.
3. Go to your repo's **Actions** tab. GitHub may ask you to "enable
   workflows" the first time — click it.
4. That's it. It will fire automatically on its 15-minute schedule. You
   can also click **"Run workflow"** manually the first time to confirm
   it works instead of waiting.

## Checking on it

- **Quick check anytime:** pull the repo and run `python check_results.py`
  — it prints a per-symbol breakdown (snapshot count, open/closed status,
  win rate, cumulative return) plus a combined summary across every symbol.
- Individual symbol CSVs are also readable directly on GitHub if you want
  to check one pair specifically: `data/BTCUSDT/trade_log.csv`, etc.
- **A signal is meant to be rare per symbol**, not constant — with 100
  symbols running in parallel, expect trades to trickle in unevenly across
  different pairs rather than on a predictable schedule. Each symbol also
  needs ~24 hours of accumulated history (90+ snapshots) before it's even
  eligible to trade — that's normal ramp-up, not a bug.

## Validating changes before deploying

Run the offline test any time you touch the logger:

```bash
pip install pandas numpy
python _offline_test.py
```

It mocks out all Binance calls, simulates many 15-min cycles across a
handful of fake symbols (with one deliberately flaky one), and checks that
every symbol accumulates history independently and that the flaky symbol's
failures don't bleed into the others.
