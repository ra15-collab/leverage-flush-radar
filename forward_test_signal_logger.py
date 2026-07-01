"""
forward_test_signal_logger.py
==============================
Runs ONE cycle across the tracked symbol universe (default: top 100 USDT-M
perpetuals by 24h volume): fetch live funding/price for all symbols in one
bulk call -> fetch open interest per symbol -> for each symbol, update its
own history, check open paper position for exit, check for new entry
signal, log.

Designed to be called repeatedly (every 15 min) by a scheduler (GitHub
Actions cron) rather than running as a long-lived process itself. State
lives entirely in per-symbol CSV files under data/<SYMBOL>/, so it's
stateless between invocations -- safe for ephemeral CI runners.

Layout (created on first run):
    data/symbol_list.json         -- cached top-100 list, re-ranked weekly
    data/<SYMBOL>/history.csv     -- every snapshot ever taken for that symbol
    data/<SYMBOL>/open_position.csv -- current open paper trade (0 or 1 rows)
    data/<SYMBOL>/trade_log.csv   -- closed paper trades, appended over time

One bad/delisted/rate-limited symbol must never take down the other 99 --
every per-symbol step in run_cycle() is wrapped in try/except.

No API key needed -- all Binance endpoints used here are public market data.

NOTE ON NETWORKING: GitHub Actions runners use US datacenter IP addresses,
and Binance geo-blocks all its endpoints (spot + futures) from US IPs,
returning HTTP 451 for every direct request. There is no alternate Binance
domain that avoids this -- the fix is to route requests through a public
proxy so they don't originate from a blocked region. That's handled in
_get_json() below, which every fetch function in this file goes through.
"""

import json
import time
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd

# Reuse the exact same signal/backtest logic as the offline backtester so
# forward-test results are directly comparable to backtest results.
from leverage_flush_radar import Config, generate_signals

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TOP_N_SYMBOLS = 100
SYMBOL_LIST_MAX_AGE = timedelta(days=7)   # re-rank the universe weekly
REQUEST_DELAY_SEC = 0.15                  # politeness delay between per-symbol OI calls

DATA_DIR = Path(__file__).parent / "data"
SYMBOL_LIST_FILE = DATA_DIR / "symbol_list.json"

BINANCE_BASE = "https://fapi.binance.com"

cfg = Config()


# ---------------------------------------------------------------------------
# 1. SYMBOL UNIVERSE (top N USDT-M perpetuals by 24h volume, cached weekly)
# ---------------------------------------------------------------------------

def _get_json(url: str, _retries: int = 2) -> dict | list:
    """Fetch JSON from `url`, routed through a public proxy to work around
    Binance's HTTP 451 geo-block of US-hosted CI runners.

    Tries a short list of free proxy services in order, since any single
    one can be flaky, rate-limited, or block non-browser requests on a
    given day. First one that returns valid JSON wins. Each proxy is
    retried a couple of times before moving to the next, since free proxies
    often fail transiently (timeouts, gateway errors) rather than being
    permanently broken.
    """
    encoded = urllib.parse.quote(url, safe="")
    proxy_templates = [
        f"https://api.allorigins.win/raw?url={encoded}",
        f"https://corsproxy.io/?url={encoded}",
        f"https://api.codetabs.com/v1/proxy?quest={url}",
        f"https://thingproxy.freeboard.io/fetch/{url}",
    ]

    errors = []
    for proxied_url in proxy_templates:
        for attempt in range(_retries):
            try:
                req = urllib.request.Request(
                    proxied_url,
                    headers={"User-Agent": "Mozilla/5.0 forward-test-script"},
                )
                with urllib.request.urlopen(req, timeout=25) as resp:
                    return json.loads(resp.read().decode())
            except Exception as e:
                errors.append(f"{proxied_url.split('?')[0]} (attempt {attempt + 1}): {e}")
                time.sleep(1)
                continue

    error_summary = "\n  ".join(errors)
    raise RuntimeError(f"All proxies failed for {url}:\n  {error_summary}")


def _rank_top_symbols(n: int) -> list[str]:
    """Pick the top-n USDT-margined perpetuals by 24h quote volume."""
    exchange_info = _get_json(f"{BINANCE_BASE}/fapi/v1/exchangeInfo")
    eligible = {
        s["symbol"]
        for s in exchange_info["symbols"]
        if s.get("contractType") == "PERPETUAL"
        and s.get("quoteAsset") == "USDT"
        and s.get("status") == "TRADING"
    }

    tickers = _get_json(f"{BINANCE_BASE}/fapi/v1/ticker/24hr")  # bulk, all symbols
    ranked = sorted(
        (t for t in tickers if t["symbol"] in eligible),
        key=lambda t: float(t.get("quoteVolume", 0.0)),
        reverse=True,
    )
    return [t["symbol"] for t in ranked[:n]]


def load_or_refresh_symbol_list() -> list[str]:
    """Cached top-N symbol list, re-ranked weekly so the tracked set stays
    liquid without jittering every 15-min cycle."""
    if SYMBOL_LIST_FILE.exists():
        cached = json.loads(SYMBOL_LIST_FILE.read_text())
        fetched_at = datetime.fromisoformat(cached["fetched_at"])
        if datetime.now(timezone.utc) - fetched_at < SYMBOL_LIST_MAX_AGE:
            return cached["symbols"]

    symbols = _rank_top_symbols(TOP_N_SYMBOLS)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SYMBOL_LIST_FILE.write_text(json.dumps({
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "symbols": symbols,
    }, indent=2))
    return symbols


# ---------------------------------------------------------------------------
# 2. FETCH LIVE DATA (public endpoints, no auth)
# ---------------------------------------------------------------------------

def fetch_bulk_premium() -> dict:
    """One call for ALL symbols' mark price + funding rate."""
    data = _get_json(f"{BINANCE_BASE}/fapi/v1/premiumIndex")  # no symbol param = all
    return {
        row["symbol"]: {
            "close": float(row["markPrice"]),
            "funding_rate": float(row["lastFundingRate"]),
        }
        for row in data
    }


def fetch_open_interest(symbol: str) -> float:
    """No bulk endpoint exists for open interest -- one call per symbol."""
    data = _get_json(f"{BINANCE_BASE}/fapi/v1/openInterest?symbol={symbol}")
    return float(data["openInterest"])


# ---------------------------------------------------------------------------
# 3. PER-SYMBOL STATE MANAGEMENT
# ---------------------------------------------------------------------------

def _symbol_dir(symbol: str) -> Path:
    d = DATA_DIR / symbol
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_or_init_history(symbol: str) -> pd.DataFrame:
    f = _symbol_dir(symbol) / "history.csv"
    if f.exists():
        return pd.read_csv(f, parse_dates=["timestamp"])
    return pd.DataFrame(columns=["timestamp", "close", "funding_rate", "open_interest"])


def load_open_position(symbol: str):
    f = _symbol_dir(symbol) / "open_position.csv"
    if f.exists():
        df = pd.read_csv(f, parse_dates=["entry_time"])
        if len(df) > 0:
            return df.iloc[0].to_dict()
    return None


def save_open_position(symbol: str, position: dict | None):
    f = _symbol_dir(symbol) / "open_position.csv"
    if position is None:
        if f.exists():
            f.unlink()
    else:
        pd.DataFrame([position]).to_csv(f, index=False)


def append_trade(symbol: str, trade: dict):
    f = _symbol_dir(symbol) / "trade_log.csv"
    row = pd.DataFrame([trade])
    if f.exists():
        row.to_csv(f, mode="a", header=False, index=False)
    else:
        row.to_csv(f, index=False)


# ---------------------------------------------------------------------------
# 4. ONE CYCLE, FOR ONE SYMBOL
# ---------------------------------------------------------------------------

def run_cycle_for_symbol(symbol: str, snapshot: dict):
    """snapshot = {timestamp, close, funding_rate, open_interest} already
    fetched by the caller. Pure state-update logic -- no network calls here,
    which is what makes this unit-testable with mocked data."""
    history = load_or_init_history(symbol)
    new_row = pd.DataFrame([snapshot])
    new_row["timestamp"] = pd.to_datetime(new_row["timestamp"])
    history = pd.concat([history, new_row], ignore_index=True)
    history["timestamp"] = pd.to_datetime(history["timestamp"])
    history = history.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)

    history_file = _symbol_dir(symbol) / "history.csv"

    # Need enough history for rolling windows to be meaningful. Below this,
    # just accumulate data -- don't trade on noise.
    min_rows = max(cfg.FUNDING_Z_WINDOW, cfg.VOL_REGIME_LOOKBACK) + 5
    if len(history) < min_rows:
        history.to_csv(history_file, index=False)
        return

    signals = generate_signals(history, cfg)
    latest = signals.iloc[-1]

    position = load_open_position(symbol)
    price = latest["close"]

    if position is not None:
        direction = position["direction"]
        hit_stop = (direction == 1 and price <= position["stop"]) or \
                   (direction == -1 and price >= position["stop"])
        hit_target = (direction == 1 and price >= position["target"]) or \
                     (direction == -1 and price <= position["target"])

        entry_time = pd.Timestamp(position["entry_time"])
        now = pd.Timestamp(latest["timestamp"])
        hours_held = (now - entry_time).total_seconds() / 3600
        # MAX_HOLD_BARS was defined in bar units for the backtester (hourly
        # bars there); here bars are ~15-min snapshots, so scale accordingly.
        time_exit = hours_held >= cfg.MAX_HOLD_BARS

        if hit_stop or hit_target or time_exit:
            raw_ret = direction * (price - position["entry_price"]) / position["entry_price"]
            net_ret = raw_ret - cfg.ROUND_TRIP_COST_PCT
            append_trade(symbol, {
                "entry_time": position["entry_time"],
                "exit_time": latest["timestamp"],
                "direction": "long" if direction == 1 else "short",
                "entry_price": position["entry_price"],
                "exit_price": price,
                "exit_reason": "stop" if hit_stop else ("target" if hit_target else "time"),
                "raw_return_pct": raw_ret * 100,
                "net_return_pct": net_ret * 100,
            })
            save_open_position(symbol, None)
            print(f"[{symbol}] Closed {('long' if direction==1 else 'short')} trade: {net_ret*100:.3f}% net")
            position = None

    if position is None and latest["signal"] != 0:
        direction = int(latest["signal"])
        atr = latest["atr"] if not np.isnan(latest["atr"]) else price * 0.005  # fallback ~0.5%
        new_position = {
            "entry_time": latest["timestamp"],
            "direction": direction,
            "entry_price": price,
            "stop": price - direction * cfg.STOP_LOSS_ATR_MULT * atr,
            "target": price + direction * cfg.TAKE_PROFIT_ATR_MULT * atr,
        }
        save_open_position(symbol, new_position)
        print(f"[{symbol}] Opened {('long' if direction==1 else 'short')} paper trade @ {price:.2f}")

    history.to_csv(history_file, index=False)


# ---------------------------------------------------------------------------
# 5. ONE CYCLE, ACROSS THE WHOLE UNIVERSE
# ---------------------------------------------------------------------------

def run_cycle():
    symbols = load_or_refresh_symbol_list()
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        premium = fetch_bulk_premium()
    except Exception as e:
        print(f"FATAL: bulk premium fetch failed, aborting this cycle: {e}")
        return

    ok, failed = 0, []
    for symbol in symbols:
        try:
            if symbol not in premium:
                raise KeyError("symbol missing from bulk premium response")
            oi = fetch_open_interest(symbol)
            snapshot = {
                "timestamp": now_iso,
                "close": premium[symbol]["close"],
                "funding_rate": premium[symbol]["funding_rate"],
                "open_interest": oi,
            }
            run_cycle_for_symbol(symbol, snapshot)
            ok += 1
        except Exception as e:
            failed.append((symbol, str(e)))
        time.sleep(REQUEST_DELAY_SEC)

    print(f"Cycle done: {ok}/{len(symbols)} symbols updated OK.")
    if failed:
        print(f"{len(failed)} symbol(s) failed this cycle (isolated, will retry next cycle):")
        for symbol, err in failed:
            print(f"  {symbol}: {err}")


if __name__ == "__main__":
    run_cycle()
