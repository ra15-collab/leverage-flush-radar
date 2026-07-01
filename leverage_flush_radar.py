"""
leverage_flush_radar.py
========================
"Leverage Flush Radar" signal: fades extreme, fast-building leverage
(funding-rate extremity + rising open interest) but only in a vol regime
where mean-reversion after a flush is plausible.

This module is intentionally dependency-light (pandas + numpy only) and
is imported by BOTH:
  - the offline vectorized backtester (this file's own backtest())
  - forward_test_signal_logger.py, so live paper-trading uses the exact
    same signal logic that was backtested. Keeping them in sync is the
    whole point -- never duplicate this logic in the logger.

Signal convention: +1 = long, -1 = short, 0 = no signal.
Long  = funding very negative (shorts overleveraged) + OI building fast
         + non-trending vol regime  -> fade the short squeeze.
Short = funding very positive (longs overleveraged) + OI building fast
         + non-trending vol regime  -> fade the long squeeze.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class Config:
    # --- Funding z-score ---
    FUNDING_Z_WINDOW: int = 90        # bars of history used to compute mean/std of funding
    FUNDING_Z_ENTRY: float = 2.0      # |z| above this counts as "extreme"

    # --- OI rate-of-change ---
    OI_ROC_WINDOW: int = 8            # bars over which OI % change is measured
    OI_ROC_ENTRY: float = 0.02        # OI must have grown >= 2% over that window

    # --- Volatility regime filter ---
    VOL_REGIME_LOOKBACK: int = 48     # bars used for the realized-vol rolling window
    VOL_REGIME_MEDIAN_WINDOW: int = 200  # longer window defining "typical" vol
    VOL_REGIME_LOW: float = 0.5       # vol_ratio below this = dead market, skip
    VOL_REGIME_HIGH: float = 1.8      # vol_ratio above this = trending/breakout, skip
    # tradeable band is (VOL_REGIME_LOW, VOL_REGIME_HIGH) -- i.e. normal vol,
    # where a leverage flush is more likely to mean-revert than to keep trending.

    # --- ATR for stops/targets ---
    ATR_WINDOW: int = 14
    STOP_LOSS_ATR_MULT: float = 1.5
    TAKE_PROFIT_ATR_MULT: float = 2.5

    # --- Trade management ---
    MAX_HOLD_BARS: int = 24           # in "bar" units -- hourly bars for the
                                       # backtester; forward_test_signal_logger
                                       # treats this as hours held (see its note).
    ROUND_TRIP_COST_PCT: float = 0.0008  # ~8bps round trip (fees + slippage), tune per venue


# ---------------------------------------------------------------------------
# Signal generation
# ---------------------------------------------------------------------------

def _true_range(df: pd.DataFrame) -> pd.Series:
    """ATR proxy from close-only data (no high/low in our public feeds):
    use the absolute bar-to-bar close change as a simple range proxy."""
    return df["close"].diff().abs()


def generate_signals(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """
    Input df must have columns: timestamp, close, funding_rate, open_interest
    (sorted ascending by timestamp). Returns a copy with added columns:
    funding_z, oi_roc, vol_ratio, atr, signal.
    """
    out = df.copy().reset_index(drop=True)

    # Funding z-score vs its own recent history
    roll_mean = out["funding_rate"].rolling(cfg.FUNDING_Z_WINDOW, min_periods=cfg.FUNDING_Z_WINDOW // 2).mean()
    roll_std = out["funding_rate"].rolling(cfg.FUNDING_Z_WINDOW, min_periods=cfg.FUNDING_Z_WINDOW // 2).std()
    out["funding_z"] = (out["funding_rate"] - roll_mean) / roll_std.replace(0, np.nan)

    # OI rate of change over a short window
    out["oi_roc"] = out["open_interest"].pct_change(cfg.OI_ROC_WINDOW)

    # Volatility regime: short realized vol vs a longer "typical" median
    ret = out["close"].pct_change()
    short_vol = ret.rolling(cfg.VOL_REGIME_LOOKBACK, min_periods=cfg.VOL_REGIME_LOOKBACK // 2).std()
    long_median_vol = short_vol.rolling(cfg.VOL_REGIME_MEDIAN_WINDOW,
                                         min_periods=cfg.VOL_REGIME_MEDIAN_WINDOW // 4).median()
    out["vol_ratio"] = short_vol / long_median_vol.replace(0, np.nan)

    # ATR proxy for stop/target sizing
    tr = _true_range(out)
    out["atr"] = tr.rolling(cfg.ATR_WINDOW, min_periods=max(2, cfg.ATR_WINDOW // 2)).mean()

    tradeable_vol = out["vol_ratio"].between(cfg.VOL_REGIME_LOW, cfg.VOL_REGIME_HIGH)
    oi_building = out["oi_roc"] >= cfg.OI_ROC_ENTRY

    long_cond = (out["funding_z"] <= -cfg.FUNDING_Z_ENTRY) & oi_building & tradeable_vol
    short_cond = (out["funding_z"] >= cfg.FUNDING_Z_ENTRY) & oi_building & tradeable_vol

    out["signal"] = 0
    out.loc[long_cond, "signal"] = 1
    out.loc[short_cond, "signal"] = -1

    return out


# ---------------------------------------------------------------------------
# Offline vectorized-ish backtester (one trade at a time, no overlap)
# ---------------------------------------------------------------------------

def backtest(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """
    Runs a simple one-trade-at-a-time backtest over signals generated from df.
    Returns a DataFrame of closed trades with entry/exit info and returns.
    """
    signals = generate_signals(df, cfg)
    trades = []
    position = None  # dict: direction, entry_idx, entry_price, stop, target

    for i in range(len(signals)):
        row = signals.iloc[i]
        price = row["close"]

        if position is not None:
            direction = position["direction"]
            hit_stop = (direction == 1 and price <= position["stop"]) or \
                       (direction == -1 and price >= position["stop"])
            hit_target = (direction == 1 and price >= position["target"]) or \
                         (direction == -1 and price <= position["target"])
            bars_held = i - position["entry_idx"]
            time_exit = bars_held >= cfg.MAX_HOLD_BARS

            if hit_stop or hit_target or time_exit:
                raw_ret = direction * (price - position["entry_price"]) / position["entry_price"]
                net_ret = raw_ret - cfg.ROUND_TRIP_COST_PCT
                trades.append({
                    "entry_time": signals.iloc[position["entry_idx"]]["timestamp"],
                    "exit_time": row["timestamp"],
                    "direction": "long" if direction == 1 else "short",
                    "entry_price": position["entry_price"],
                    "exit_price": price,
                    "bars_held": bars_held,
                    "exit_reason": "stop" if hit_stop else ("target" if hit_target else "time"),
                    "raw_return_pct": raw_ret * 100,
                    "net_return_pct": net_ret * 100,
                })
                position = None

        if position is None and row["signal"] != 0 and not np.isnan(row.get("atr", np.nan)):
            direction = int(row["signal"])
            atr = row["atr"]
            position = {
                "direction": direction,
                "entry_idx": i,
                "entry_price": price,
                "stop": price - direction * cfg.STOP_LOSS_ATR_MULT * atr,
                "target": price + direction * cfg.TAKE_PROFIT_ATR_MULT * atr,
            }

    return pd.DataFrame(trades)


def summarize(trades: pd.DataFrame) -> dict:
    """Win rate, average return, max drawdown (on cumulative net return path)."""
    if len(trades) == 0:
        return {"n_trades": 0, "win_rate": np.nan, "avg_net_return_pct": np.nan, "max_drawdown_pct": np.nan}

    wins = (trades["net_return_pct"] > 0).sum()
    win_rate = wins / len(trades)
    avg_ret = trades["net_return_pct"].mean()

    cum = trades["net_return_pct"].cumsum()
    running_max = cum.cummax()
    drawdown = cum - running_max
    max_dd = drawdown.min()

    return {
        "n_trades": len(trades),
        "win_rate": round(win_rate, 4),
        "avg_net_return_pct": round(avg_ret, 4),
        "max_drawdown_pct": round(max_dd, 4),
    }
