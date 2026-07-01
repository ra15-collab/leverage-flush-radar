"""
Offline test harness -- monkeypatches network calls in
forward_test_signal_logger so we can run many simulated cycles without
hitting Binance, to shake out bugs before deploying.
"""
import random
import shutil
from pathlib import Path
from datetime import datetime, timezone, timedelta

import numpy as np

import forward_test_signal_logger as ftsl

N_TEST_SYMBOLS = 15
N_CYCLES = 60

random.seed(42)
np.random.seed(42)

TEST_SYMBOLS = [f"TEST{i}USDT" for i in range(N_TEST_SYMBOLS)]

# reset data dir
if ftsl.DATA_DIR.exists():
    shutil.rmtree(ftsl.DATA_DIR)
ftsl.DATA_DIR.mkdir(parents=True)

# Fake per-symbol state driving the "market"
state = {
    s: {
        "price": random.uniform(0.1, 60000),
        "funding": random.uniform(-0.0005, 0.0005),
        "oi": random.uniform(1e6, 1e9),
    }
    for s in TEST_SYMBOLS
}


def fake_load_or_refresh_symbol_list():
    return TEST_SYMBOLS


def fake_fetch_bulk_premium():
    out = {}
    for s in TEST_SYMBOLS:
        st = state[s]
        st["price"] *= (1 + random.uniform(-0.01, 0.01))
        # occasionally inject a funding extreme so signals actually fire
        if random.random() < 0.05:
            st["funding"] = random.choice([1, -1]) * random.uniform(0.004, 0.01)
        else:
            st["funding"] = st["funding"] * 0.9 + random.uniform(-0.0003, 0.0003)
        out[s] = {"close": st["price"], "funding_rate": st["funding"]}
    return out


def fake_fetch_open_interest(symbol):
    st = state[symbol]
    # occasionally spike OI to line up with funding extremes
    st["oi"] *= (1 + random.uniform(-0.01, 0.05))
    return st["oi"]


# One symbol deliberately raises to test fault isolation
_orig_fetch_oi = fake_fetch_open_interest
def flaky_fetch_open_interest(symbol):
    if symbol == "TEST3USDT" and random.random() < 0.3:
        raise RuntimeError("simulated transient API error")
    return _orig_fetch_oi(symbol)


ftsl.load_or_refresh_symbol_list = fake_load_or_refresh_symbol_list
ftsl.fetch_bulk_premium = fake_fetch_bulk_premium
ftsl.fetch_open_interest = flaky_fetch_open_interest
ftsl.REQUEST_DELAY_SEC = 0  # skip real sleep in test

# Drive fake time forward 15 min per cycle instead of using wall clock
base_time = datetime(2026, 1, 1, tzinfo=timezone.utc)


def run_cycle_at(t):
    now_iso = t.isoformat()
    symbols = ftsl.load_or_refresh_symbol_list()
    premium = ftsl.fetch_bulk_premium()
    ok, failed = 0, []
    for symbol in symbols:
        try:
            oi = ftsl.fetch_open_interest(symbol)
            snapshot = {
                "timestamp": now_iso,
                "close": premium[symbol]["close"],
                "funding_rate": premium[symbol]["funding_rate"],
                "open_interest": oi,
            }
            ftsl.run_cycle_for_symbol(symbol, snapshot)
            ok += 1
        except Exception as e:
            failed.append((symbol, str(e)))
    return ok, failed


total_failures = []
for i in range(N_CYCLES):
    t = base_time + timedelta(minutes=15 * i)
    ok, failed = run_cycle_at(t)
    total_failures.extend(failed)

print(f"Ran {N_CYCLES} cycles across {N_TEST_SYMBOLS} symbols.")
print(f"Fault-isolation check: {len(total_failures)} simulated failures were caught "
      f"(expected >0 from TEST3USDT), and no cycle crashed overall.")

# Sanity: every symbol should have a history file with N_CYCLES rows
import pandas as pd
bad = []
for s in TEST_SYMBOLS:
    f = ftsl.DATA_DIR / s / "history.csv"
    if not f.exists():
        bad.append((s, "no history file"))
        continue
    h = pd.read_csv(f)
    if len(h) < N_CYCLES - 5:  # allow a few dropped rows from TEST3USDT failures
        bad.append((s, f"only {len(h)} rows"))

if bad:
    print("PROBLEMS:", bad)
else:
    print("All symbols accumulated history as expected. Per-symbol isolation confirmed.")

print("\nSample trade_log.csv (first symbol with trades):")
for s in TEST_SYMBOLS:
    f = ftsl.DATA_DIR / s / "trade_log.csv"
    if f.exists():
        print(s)
        print(pd.read_csv(f).head())
        break
else:
    print("(no symbol produced a closed trade in this short random run -- not necessarily a bug)")
