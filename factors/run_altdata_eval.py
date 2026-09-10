"""The honest gauntlet for insider (SF2) and institutional (SF3) factors.

These data families are economically DISTINCT from price momentum (the one validated
factor), so a survivor is a candidate to DIVERSIFY momentum, not duplicate it. Every
factor faces the same unchanged bar:

  1. IC stage      — mean IC, IR, t-stat, decile spread + Sharpe, sign-match-to-prior
                     (bar: |mean IC| > 0.02 AND |t| > 2).
  2. WALK-FORWARD  — any IC-bar passer is re-tested across non-overlapping OOS windows
                     (positive windows, windows beating SPY, worst window, after-cost
                     Sharpe). An IC passer that fails here is "in-sample only — do not build".
  3. MOMENTUM CORR — for any survivor, correlation of its signal with momentum_12_1
                     (low = a genuine diversifier; high = redundant with momentum).

Offline from the local Sharadar cache (price + fundamentals + SF2 + SF3). Read-only.

    python factors/run_altdata_eval.py
"""

import dataclasses
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import factors.altdata  # noqa: E402,F401  (registers the insider/institutional factors)
from backtest.walkforward import make_windows  # noqa: E402
from data.sharadar_provider import (  # noqa: E402
    build_insider_panels,
    build_institutional_panels,
    load_institutional_holdings,
    load_insider_transactions,
)
from factors.altdata import ALTDATA_EXPECTED_SIGN, ALTDATA_FACTORS  # noqa: E402
from factors.base import all_factors, get  # noqa: E402
from factors.evaluate import _rebalance_dates, evaluate_factor  # noqa: E402
from factors.library import Momentum12_1  # noqa: E402
from factors.run_factor_eval import build_sharadar_factor_data  # noqa: E402
from research.combine_train import (  # noqa: E402
    IC_THRESHOLD,
    SPY_CACHE,
    TSTAT_THRESHOLD,
    _monthly_metrics,
    long_short_returns,
    spy_monthly_returns,
)

log = logging.getLogger("run_altdata_eval")


def _sign(x) -> int:
    return 0 if (x is None or abs(x) < 1e-12) else (1 if x > 0 else -1)


def _passes_ic_bar(s: dict) -> bool:
    ic, t = s["mean_ic"], s["t_stat"]
    return ic is not None and t is not None and abs(ic) > IC_THRESHOLD and abs(t) > TSTAT_THRESHOLD


def _f(v, pct=False):
    if v is None:
        return "n/a"
    return f"{v * 100:+.2f}%" if pct else f"{v:+.3f}"


# --- data assembly ------------------------------------------------------------------

def build_alt_factor_data():
    """Base PIT survivorship-free FactorData + merged insider (SF2) & institutional (SF3) panels."""
    base, eligible = build_sharadar_factor_data()
    tickers = list(base.close.columns)
    master = base.close.index
    shares = base.fundamentals["shares"]

    log.info("Loading SF2 insider transactions for %d names...", len(tickers))
    insider = build_insider_panels(load_insider_transactions(tickers), master)
    log.info("Loading SF3 institutional holdings for %d names (45-day filing lag applied)...", len(tickers))
    institutional = build_institutional_panels(load_institutional_holdings(tickers), master, shares)

    fundamentals = {**base.fundamentals, **insider, **institutional}
    return dataclasses.replace(base, fundamentals=fundamentals), eligible


def assert_altdata_safety(data, factor_name: str = "insider_buying") -> None:
    """Truncation test: the factor value at a cutoff is unchanged when future data is dropped."""
    factor = get(factor_name)()
    idx = data.close.index
    cutoff = idx[int(len(idx) * 0.7)]
    full = factor.compute(data).loc[cutoff]

    def _slice(p):
        return p.loc[:cutoff] if p is not None else None

    truncated = dataclasses.replace(
        data, open=_slice(data.open), high=_slice(data.high), low=_slice(data.low),
        close=_slice(data.close), volume=_slice(data.volume), market=_slice(data.market),
        fundamentals={k: _slice(v) for k, v in data.fundamentals.items()})
    trunc = factor.compute(truncated).loc[cutoff]
    diff = (full - trunc).abs().max()
    assert pd.isna(diff) or diff < 1e-9, f"LOOK-AHEAD LEAK in {factor_name}"
    log.info("Availability-date look-ahead guard verified on %s at %s.", factor_name, cutoff.date())


# --- gauntlet stages ----------------------------------------------------------------

def ic_scorecard(data, eligible) -> dict:
    """IC stage for the four alt-data factors; print and return the score dicts."""
    scores = {name: evaluate_factor(get(name)(), data, eligible=eligible) for name in ALTDATA_FACTORS}
    rows = []
    for name in ALTDATA_FACTORS:
        s = scores[name]
        exp = ALTDATA_EXPECTED_SIGN[name]
        rows.append({"factor": name, "cat": s["category"][:5], "periods": s["n_periods"],
                     "mean_IC": _f(s["mean_ic"]), "IR": _f(s["ir"]), "t_stat": _f(s["t_stat"]),
                     "decile_spread": _f(s["top_minus_bottom"], pct=True), "decile_Sharpe": _f(s["tmb_sharpe"]),
                     "exp_sign": "+" if exp > 0 else "-",
                     "sign_ok": "yes" if _sign(s["mean_ic"]) == exp else "no",
                     "IC_bar": "PASS" if _passes_ic_bar(s) else "no"})
    print("\n=== IC STAGE — insider & institutional factors (monthly, survivorship-free; "
          "bar |IC|>0.02 & |t|>2) ===")
    print(pd.DataFrame(rows).to_string(index=False))
    return scores


def walk_forward_factor(values: pd.DataFrame, close: pd.DataFrame, eligible: pd.DataFrame,
                        spy_close: pd.Series) -> dict:
    """Run a factor's decile long/short across non-overlapping OOS windows, after costs."""
    rebal = _rebalance_dates(close.index, "M")
    pairs_all = list(zip(rebal[:-1], rebal[1:]))
    rows = []
    for start, end in make_windows(rebal):
        ls = long_short_returns(values, close, eligible, start=start, end=end)
        if len(ls) < 6:
            continue
        m = _monthly_metrics(ls)
        spy = spy_monthly_returns([(c, n) for c, n in pairs_all if start <= n <= end], spy_close)
        spy = spy.reindex(ls.index).dropna()
        spy_m = _monthly_metrics(spy) if len(spy) >= 6 else None
        rows.append({"start": start, "end": end, "m": m, "spy": spy_m})
    full = _monthly_metrics(long_short_returns(values, close, eligible))
    return {"windows": rows, "full": full}


def momentum_correlation(values: pd.DataFrame, mom: pd.DataFrame, eligible: pd.DataFrame,
                         rebal: pd.DatetimeIndex) -> float:
    """Mean cross-sectional Spearman correlation of the factor with 12-1 momentum.

    Low |corr| => the factor ranks names differently from momentum (a real diversifier).
    """
    corrs = []
    for d in rebal:
        if d not in values.index or d not in mom.index:
            continue
        pair = pd.concat([values.loc[d], mom.loc[d]], axis=1, keys=["f", "m"]).dropna()
        if d in eligible.index:
            mask = eligible.loc[d].reindex(pair.index).fillna(False)
            pair = pair[mask.to_numpy(dtype=bool)]
        if len(pair) >= 10:
            corrs.append(pair["f"].corr(pair["m"], method="spearman"))
    return float(np.nanmean(corrs)) if corrs else float("nan")


def print_walk_forward(name: str, wf: dict, spy_available_only: bool = True) -> dict:
    """Print a factor's per-window walk-forward and return its consistency summary."""
    rows, pos, beat_spy, spy_n = [], 0, 0, 0
    for r in wf["windows"]:
        m, spy = r["m"], r["spy"]
        pos += int((m["sharpe"] or -9) > 0)
        if spy is not None:
            spy_n += 1
            beat_spy += int((m["sharpe"] or -9) > (spy["sharpe"] or -9))
        rows.append({"window": f"{r['start'].year}-{r['end'].year}",
                     "CAGR": _f(m["cagr"], pct=True), "Sharpe": _f(m["sharpe"]),
                     "maxDD": _f(m["max_drawdown"], pct=True),
                     "SPY_Sharpe": _f(spy["sharpe"]) if spy else "n/a",
                     "beat_SPY": "n/a" if spy is None else ("Y" if (m["sharpe"] or -9) > (spy["sharpe"] or -9) else "n")})
    n = len(rows)
    worst = min(wf["windows"], key=lambda r: r["m"]["sharpe"] if r["m"]["sharpe"] is not None else 9, default=None)
    print(f"\n--- WALK-FORWARD: {name} (non-overlapping OOS windows, after costs) ---")
    print(pd.DataFrame(rows).to_string(index=False))
    print(f"  {pos}/{n} windows positive Sharpe; beat SPY in {beat_spy}/{spy_n} SPY-covered windows; "
          f"full-history L/S Sharpe {_f(wf['full']['sharpe'])}.")
    if worst is not None:
        print(f"  WORST window: {worst['start'].year}-{worst['end'].year} Sharpe {_f(worst['m']['sharpe'])}, "
              f"maxDD {_f(worst['m']['max_drawdown'], pct=True)}.")
    consistent = n > 0 and pos * 2 >= n and (spy_n == 0 or beat_spy * 2 >= spy_n)
    return {"n": n, "positive": pos, "beat_spy": beat_spy, "spy_n": spy_n, "consistent": consistent}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("data.sharadar_provider").setLevel(logging.WARNING)

    print("=" * 92)
    print("INSIDER (SF2) & INSTITUTIONAL (SF3) FACTORS — honest gauntlet, momentum-diversifier hunt")
    print("=" * 92)
    data, eligible = build_alt_factor_data()
    assert_altdata_safety(data, "insider_buying")           # availability-date look-ahead guard
    assert_altdata_safety(data, "institutional_concentration")
    close = data.close
    rebal = _rebalance_dates(close.index, "M")
    spy_close = pd.read_parquet(SPY_CACHE)["Close"]
    mom = Momentum12_1().compute(data)

    scores = ic_scorecard(data, eligible)
    ic_passers = [n for n in ALTDATA_FACTORS if _passes_ic_bar(scores[n])]
    print(f"\nIC-bar passers (required before walk-forward): "
          f"{', '.join(ic_passers) if ic_passers else 'NONE'}.")

    shortlist, in_sample_only = [], []
    for name in ic_passers:
        values = get(name)().compute(data)
        summary = print_walk_forward(name, walk_forward_factor(values, close, eligible, spy_close))
        corr = momentum_correlation(values, mom, eligible, rebal)
        sign_ok = _sign(scores[name]["mean_ic"]) == ALTDATA_EXPECTED_SIGN[name]
        tag = "DISTINCT (low momentum corr)" if abs(corr) < 0.3 else "REDUNDANT with momentum"
        if summary["consistent"] and sign_ok:
            shortlist.append((name, corr, tag))
        else:
            in_sample_only.append((name, corr, summary))
        print(f"  Momentum corr: {corr:+.2f} -> {tag}. Walk-forward consistent: "
              f"{'YES' if summary['consistent'] else 'NO'}; correct sign: {'yes' if sign_ok else 'no'}.")

    print_summary(scores, ic_passers, shortlist, in_sample_only)
    return 0


def print_summary(scores, ic_passers, shortlist, in_sample_only) -> None:
    print("\n" + "=" * 92)
    print("SHORTLIST & VERDICT")
    print("=" * 92)
    print(f"Multiple testing: {len(ALTDATA_FACTORS)} factors tested; under the null ~1 in 22 (4.55%) "
          f"passes |t|>2 by chance. A real candidate must clear IC + correct sign + walk-forward "
          f"consistency, not one window.")
    print("\nSHORTLIST (passed ALL gates — IC bar + correct sign + walk-forward consistency):")
    if shortlist:
        for name, corr, tag in shortlist:
            print(f"  + {name:<32} momentum corr {corr:+.2f}  [{tag}]")
    else:
        print("  (none)")
    if in_sample_only:
        print("\nIN-SAMPLE ONLY — do NOT build (cleared IC but failed walk-forward / sign):")
        for name, corr, _ in in_sample_only:
            print(f"  ! {name:<32} momentum corr {corr:+.2f}")
    if not ic_passers:
        print("\nNo insider/institutional factor cleared the IC bar on this survivorship-free, "
              "point-in-time data — an honest negative (no leak, no survivorship inflation).")
    print("\nNote: the bar was NOT lowered; IC is monthly close-to-close predictive power; SF2 uses the "
          "Form-4 filing date and SF3 the 45-day 13F lag, so all values are point-in-time.")


if __name__ == "__main__":
    sys.exit(main())
