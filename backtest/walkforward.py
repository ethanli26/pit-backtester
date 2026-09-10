"""Walk-forward validation for vol-managed momentum — the real robustness test.

A single OOS split can get lucky. Here we roll through 1998-2026 in NON-OVERLAPPING test
windows and, for each, run the strategy out of sample with its vol target fixed from data
strictly BEFORE the window (the train side). We never choose anything on a test window;
the equal-weight/vol-target rules are fixed in advance. We then count how many windows
beat passive — the distribution matters more than any one window.

PASSIVE BENCHMARK: SPY (the free cache starts 2011, so pre-2011 windows show SPY "n/a");
for a full-history passive we also report the survivorship-free equal-weighted universe
index. The strategy is evaluated after costs throughout.

Offline from the Sharadar cache. Read-only research, no orders.

    python -m backtest.walkforward
"""

import logging

import numpy as np
import pandas as pd

import config
from backtest.universe import equal_weight_index
from research.combine_train import SPY_CACHE, _monthly_metrics, spy_monthly_returns, vol_matched_blend
from factors.evaluate import _rebalance_dates
from strategies.vol_managed_momentum import VolManagedMomentum

log = logging.getLogger(__name__)

WINDOW_YEARS = 3   # non-overlapping test-window length


def make_windows(rebal: pd.DatetimeIndex, window_years: int = WINDOW_YEARS) -> list[tuple]:
    """Non-overlapping [start, end) test windows of ``window_years`` across the calendar.

    The first window starts one window-length in, so window 1 already has prior data to
    set its vol target (no window is scaled by its own or future data).
    """
    first_year = rebal[0].year + window_years
    windows = []
    for year in range(first_year, rebal[-1].year + 1, window_years):
        start = pd.Timestamp(f"{year}-01-01")
        end = pd.Timestamp(f"{year + window_years}-01-01")
        if start <= rebal[-1]:
            windows.append((start, min(end, rebal[-1] + pd.Timedelta(days=1))))
    return windows


def _passive(returns_index: pd.DatetimeIndex, pairs: list[tuple], spy_close: pd.Series,
             ew_returns: pd.Series) -> tuple[pd.Series, pd.Series]:
    """SPY and equal-weight-market monthly returns aligned to a window's return index."""
    spy = spy_monthly_returns(pairs, spy_close).reindex(returns_index).dropna()
    ew = ew_returns.reindex(returns_index).dropna()
    return spy, ew


def run_walkforward(strategy: VolManagedMomentum, data, eligible: pd.DataFrame) -> dict:
    """Run the strategy across non-overlapping OOS windows; return per-window results."""
    close = data.close
    rebal = _rebalance_dates(close.index, "M")
    windows = make_windows(rebal)

    spy_close = pd.read_parquet(SPY_CACHE)["Close"]
    pairs_all = list(zip(rebal[:-1], rebal[1:]))
    # Equal-weight survivorship-free market return per rebalance (full-history passive).
    ew_index = equal_weight_index({s: pd.DataFrame({"Close": close[s]}) for s in close.columns})
    ew_returns = pd.Series(
        [ew_index.get(nxt, np.nan) / ew_index.get(cur, np.nan) - 1.0 for cur, nxt in pairs_all],
        index=pd.DatetimeIndex([nxt for _, nxt in pairs_all]))

    rows = []
    for start, end in windows:
        ret = strategy.portfolio_returns(data, eligible, train_end=start, start=start, end=end)
        if len(ret) < 6:
            continue
        m = _monthly_metrics(ret)
        pairs = [(c, n) for c, n in pairs_all if start <= n <= end]
        spy, ew = _passive(ret.index, pairs, spy_close, ew_returns)
        spy_m = _monthly_metrics(spy) if len(spy) >= 6 else None
        ew_m = _monthly_metrics(ew) if len(ew) >= 6 else None
        rows.append({"start": start, "end": end, "n": len(ret), "metrics": m,
                     "spy": spy_m, "ew": ew_m})
    return {"windows": rows, "n_windows": len(rows)}


def print_walkforward(result: dict) -> dict:
    """Print the per-window table + consistency counts; return a verdict summary dict."""
    rows = result["windows"]

    def _f(v, pct=False):
        return ("n/a" if v is None else (f"{v*100:+.1f}%" if pct else f"{v:+.2f}"))

    table = []
    beats_spy_sharpe = beats_spy_return = spy_windows = 0
    beats_ew_sharpe = ew_windows = 0
    for r in rows:
        m, spy, ew = r["metrics"], r["spy"], r["ew"]
        if spy is not None:
            spy_windows += 1
            beats_spy_sharpe += int((m["sharpe"] or -9) > (spy["sharpe"] or -9))
            beats_spy_return += int((m["cagr"] or -9) > (spy["cagr"] or -9))
        if ew is not None:
            ew_windows += 1
            beats_ew_sharpe += int((m["sharpe"] or -9) > (ew["sharpe"] or -9))
        table.append({
            "window": f"{r['start'].year}-{r['end'].year}", "months": r["n"],
            "CAGR": _f(m["cagr"], pct=True), "Sharpe": _f(m["sharpe"]),
            "Sortino": _f(m["sortino"]), "maxDD": _f(m["max_drawdown"], pct=True),
            "SPY_Sharpe": _f(spy["sharpe"] if spy else None),
            "beat_SPY": ("n/a" if spy is None else ("Y" if (m["sharpe"] or -9) > (spy["sharpe"] or -9) else "n")),
            "beat_EWmkt": ("n/a" if ew is None else ("Y" if (m["sharpe"] or -9) > (ew["sharpe"] or -9) else "n")),
        })
    print(pd.DataFrame(table).to_string(index=False))

    sharpes = [r["metrics"]["sharpe"] for r in rows if r["metrics"]["sharpe"] is not None]
    worst = min(rows, key=lambda r: r["metrics"]["sharpe"] if r["metrics"]["sharpe"] is not None else 9)
    print(f"\nWindows: {len(rows)} | Sharpe distribution: "
          f"min {min(sharpes):+.2f}, median {np.median(sharpes):+.2f}, max {max(sharpes):+.2f}; "
          f"{sum(s > 0 for s in sharpes)}/{len(sharpes)} positive.")
    print(f"Beat SPY (where SPY exists, {spy_windows} windows): "
          f"{beats_spy_sharpe}/{spy_windows} on Sharpe, {beats_spy_return}/{spy_windows} on return.")
    print(f"Beat equal-weight survivorship-free market ({ew_windows} windows): "
          f"{beats_ew_sharpe}/{ew_windows} on Sharpe.")
    print(f"WORST window: {worst['start'].year}-{worst['end'].year} -> "
          f"Sharpe {_f(worst['metrics']['sharpe'])}, maxDD {_f(worst['metrics']['max_drawdown'], pct=True)}, "
          f"CAGR {_f(worst['metrics']['cagr'], pct=True)}.")
    return {"n_windows": len(rows), "spy_windows": spy_windows, "beats_spy_sharpe": beats_spy_sharpe,
            "beats_spy_return": beats_spy_return, "ew_windows": ew_windows,
            "beats_ew_sharpe": beats_ew_sharpe, "positive": sum(s > 0 for s in sharpes),
            "worst": worst}


def capacity_report(strategy: VolManagedMomentum, data, eligible: pd.DataFrame) -> None:
    """Annual turnover + an honest, order-of-magnitude capacity ceiling."""
    turnover = strategy.annual_turnover(data, eligible)
    close, volume = data.close, data.volume
    # Per-name median daily dollar volume, then the median across the decile-eligible names.
    adv = (close * volume).median()
    median_adv = float(adv.median())
    n_names = int(eligible.iloc[-1].sum()) if len(eligible) else close.shape[1]
    decile_names = max(n_names // config.VMM_DECILES, 1)
    # Capacity ≈ names per decile x (participation x median ADV); thin names bind first.
    capacity = decile_names * config.MAX_ADV_PARTICIPATION * median_adv
    print("\n--- Capacity / turnover (honest order-of-magnitude) ---")
    print(f"  Annual one-way turnover (long leg): ~{turnover*100:.0f}% of the book per year "
          f"(monthly momentum decile turnover is high).")
    print(f"  Median name ADV in universe: ${median_adv:,.0f}/day; ~{decile_names} names per decile.")
    print(f"  At {config.MAX_ADV_PARTICIPATION*100:.0f}% ADV participation, rough capacity ceiling "
          f"~${capacity:,.0f} per side before costs balloon.")
    print("  Costs scale UP on smaller names (config slippage tiers: large "
          f"{config.SLIPPAGE_BPS_LARGE:.0f} / mid {config.SLIPPAGE_BPS_MID:.0f} / small "
          f"{config.SLIPPAGE_BPS_SMALL:.0f} bps/side); a real book skewed to thin names pays the")
    print("  small-tier rate, so the 15 bps/side here is optimistic at scale — capacity is modest.")
