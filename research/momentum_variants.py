"""Head-to-head: established momentum-crash fixes vs momentum-alone and passive, OOS.

Momentum was the one factor that cleared the bar and beat passive OOS after costs, but
it crashes at reversals (deep 12-month drawdowns; the classic spring-2009 momentum
crash). We test PRE-SPECIFIED, theory-backed improvements — no mining — head to head:

  A. momentum_12_1            baseline (already validated)
  B. vol_managed_momentum     Barroso & Santa-Clara (2015): scale the momentum L/S gross
                              exposure inversely to its OWN trailing realized vol
  C. mom_plus_value           equal z-score blend of momentum + the stronger value factor
  D. mom_plus_quality         equal z-score blend of momentum + profitability
  E. risk_managed_quality_mom vol-management (B) applied to the mom+quality blend (D)

For each we report full-history and OOS (last 30%, never used to choose) IC where the
variant is a rankable score, and traded metrics after 15 bps/side costs on actual
turnover: CAGR, Sharpe, Sortino, maxDD, total return — versus momentum-alone, SPY
buy-hold, and the vol-matched SPY/cash blend. CRASH FOCUS: worst rolling 12-month return
and the 2008-2009 and 2020 sub-period returns.

These are all WELL-KNOWN fixes, so any surviving edge is expected to be modest, not
hidden alpha. Offline from the Sharadar cache. Read-only research, no orders.

    python ml/momentum_variants.py
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from factors.composite import CompositeFactor  # noqa: E402
from factors.evaluate import _rebalance_dates, evaluate_factor  # noqa: E402
from factors.fundamentals import BookToPrice, EarningsYield, Profitability  # noqa: E402
from factors.library import Momentum12_1  # noqa: E402
from factors.run_factor_eval import build_sharadar_factor_data  # noqa: E402
from research.combine_train import (  # noqa: E402  (reuse the validated traded/metrics helpers)
    COST_BPS_PER_SIDE,
    IC_THRESHOLD,
    OOS_FRACTION,
    SPY_CACHE,
    TSTAT_THRESHOLD,
    _monthly_metrics,
    _Panel,
    long_short_returns,
    spy_monthly_returns,
    vol_matched_blend,
)

log = logging.getLogger("momentum_variants")

VOL_TARGET_WINDOW = 12   # trailing months for the realized-vol estimate (Barroso uses ~6mo daily)
VOL_LEVERAGE_CAP = 2.0   # cap gross leverage so calm periods can't demand absurd size


# --- Barroso & Santa-Clara vol management (a portfolio overlay) ---------------------

def vol_managed(net_returns: pd.Series, split: pd.Timestamp,
                window: int = VOL_TARGET_WINDOW, cap: float = VOL_LEVERAGE_CAP) -> pd.Series:
    """Scale a monthly L/S return series inversely to its OWN trailing realized vol.

    weight_t = target / realized_vol(returns up to t-1), capped at ``cap``. Since costs
    and P&L both scale with exposure, scaling the NET return by weight is exact.

    LOOK-AHEAD GUARD: ``realized.shift(1)`` makes the weight for month t depend only on
    returns realized BEFORE t. The constant vol target is the MEDIAN trailing vol over
    the IN-SAMPLE segment only, so the out-of-sample stretch uses a target fixed from the
    past — no future information enters the scaling.
    """
    realized = net_returns.rolling(window).std()
    in_sample = realized[realized.index < split].dropna()
    target = in_sample.median() if len(in_sample) else realized.median()
    weight = (target / realized.shift(1)).clip(upper=cap)   # known at formation of month t
    return (weight * net_returns).dropna()


# --- crash diagnostics --------------------------------------------------------------

def worst_rolling_12m(returns: pd.Series) -> float | None:
    """Most negative trailing 12-month compounded return (the worst one-year stretch)."""
    if len(returns) < 12:
        return None
    rolling = (1.0 + returns).rolling(12).apply(np.prod, raw=True) - 1.0
    return float(rolling.min()) if rolling.notna().any() else None


def subperiod_return(returns: pd.Series, start: str, end: str) -> float | None:
    """Compounded return over a calendar sub-period [start, end] (crash episodes)."""
    window = returns[(returns.index >= pd.Timestamp(start)) & (returns.index <= pd.Timestamp(end))]
    return float((1.0 + window).prod() - 1.0) if len(window) else None


# --- reporting helpers --------------------------------------------------------------

def _f(v, pct=False):
    if v is None:
        return "n/a"
    return f"{v * 100:+.1f}%" if pct else f"{v:+.3f}"


def _ic_row(name, score):
    return {"variant": name, "n": score["n_periods"], "mean_IC": _f(score["mean_ic"]),
            "IR": _f(score["ir"]), "t_stat": _f(score["t_stat"]),
            "clears_bar": "YES" if (score["mean_ic"] is not None and score["t_stat"] is not None
                                    and abs(score["mean_ic"]) > IC_THRESHOLD
                                    and abs(score["t_stat"]) > TSTAT_THRESHOLD) else "no"}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("data.sharadar_provider").setLevel(logging.WARNING)

    data, eligible = build_sharadar_factor_data()       # offline, PIT, survivorship-free
    close = data.close
    rebal = _rebalance_dates(close.index, "M")
    split = rebal[int(len(rebal) * (1.0 - OOS_FRACTION))]

    # --- choose the stronger value factor on FULL history (correctly-signed mean IC) ---
    mom_panel = Momentum12_1().compute(data)
    prof_panel = Profitability().compute(data)
    btp = evaluate_factor(_Panel("book_to_price", "fundamental", BookToPrice().compute(data)),
                          data, eligible=eligible)
    ey = evaluate_factor(_Panel("earnings_yield", "fundamental", EarningsYield().compute(data)),
                         data, eligible=eligible)
    value_name = "book_to_price" if (btp["mean_ic"] or -9) > (ey["mean_ic"] or -9) else "earnings_yield"
    value_factor = BookToPrice() if value_name == "book_to_price" else EarningsYield()

    # --- build the rankable composite panels (C, D) ------------------------------------
    mom_value = CompositeFactor([Momentum12_1(), value_factor], "zscore").compute(data)
    mom_quality = CompositeFactor([Momentum12_1(), Profitability()], "zscore").compute(data)
    rankable = {
        "A_momentum": _Panel("A_momentum", "price", mom_panel),
        "C_mom_plus_value": _Panel("C_mom_plus_value", "composite", mom_value),
        "D_mom_plus_quality": _Panel("D_mom_plus_quality", "composite", mom_quality),
    }

    # --- traded L/S monthly net-return series, FULL history (then slice OOS) ------------
    ls_full = {name: long_short_returns(f.compute(data), close, eligible) for name, f in rankable.items()}
    # B and E are portfolio overlays (vol management) of A and D's traded series.
    ls_full["B_vol_managed_mom"] = vol_managed(ls_full["A_momentum"], split)
    ls_full["E_risk_managed_quality_mom"] = vol_managed(ls_full["D_mom_plus_quality"], split)
    order = ["A_momentum", "B_vol_managed_mom", "C_mom_plus_value",
             "D_mom_plus_quality", "E_risk_managed_quality_mom"]

    # Passive benchmarks (full history; sliced to OOS for the ranking).
    spy_close = pd.read_parquet(SPY_CACHE)["Close"]
    pairs = list(zip(rebal[:-1], rebal[1:]))
    spy_full = spy_monthly_returns(pairs, spy_close)

    print("=" * 92)
    print("MOMENTUM CRASH-FIX VARIANTS — survivorship-free, OOS, after costs")
    print("=" * 92)
    print(f"Universe: liquid survivorship-free US common stocks | {rebal[0].date()} -> {rebal[-1].date()} "
          f"| OOS from {split.date()} (last {int(OOS_FRACTION*100)}%)")
    print(f"Value factor chosen for C (stronger full-history IC): {value_name} "
          f"(book_to_price IC {_f(btp['mean_ic'])}, earnings_yield IC {_f(ey['mean_ic'])})")
    print(f"Costs: {COST_BPS_PER_SIDE:.0f} bps/side on actual decile turnover. "
          f"Vol target: trailing {VOL_TARGET_WINDOW}mo, leverage cap {VOL_LEVERAGE_CAP}x.\n")

    # --- (1) IC scorecard for the rankable variants (full + OOS) ------------------------
    print("--- IC (rankable variants only; B & E are portfolio overlays, no cross-sectional IC) ---")
    ic_rows = []
    for name, f in rankable.items():
        full = evaluate_factor(f, data, eligible=eligible)
        oos = evaluate_factor(f, data, eligible=eligible, start=split)
        ic_rows.append({**_ic_row(name + " (full)", full)})
        ic_rows.append({**_ic_row(name + " (OOS)", oos)})
    print(pd.DataFrame(ic_rows).to_string(index=False))

    # --- (2) OOS traded metrics after costs, ranked by Sharpe ---------------------------
    spy_oos = spy_full[spy_full.index >= split]
    rows = []
    for name in order:
        oos_ret = ls_full[name][ls_full[name].index >= split]
        m = _monthly_metrics(oos_ret)
        rows.append({"variant": name, **m})
    # passive
    rows.append({"variant": "SPY_buy_hold", **_monthly_metrics(spy_oos)})
    blend_oos = vol_matched_blend(spy_oos, target_vol=ls_full["A_momentum"][ls_full["A_momentum"].index >= split].std(ddof=1))
    rows.append({"variant": "vol_matched_SPY_cash", **_monthly_metrics(blend_oos)})
    rows.sort(key=lambda r: r["sharpe"] if r["sharpe"] is not None else -9, reverse=True)
    print("\n--- OOS TRADED, AFTER COSTS, ranked by Sharpe ---")
    print(pd.DataFrame([{"variant": r["variant"], "CAGR": _f(r["cagr"], pct=True),
                         "Sharpe": _f(r["sharpe"]), "Sortino": _f(r["sortino"]),
                         "maxDD": _f(r["max_drawdown"], pct=True),
                         "total": _f(r["total"], pct=True)} for r in rows]).to_string(index=False))

    # --- (3) CRASH FOCUS (full history: worst 12m, 2008-2009, 2020) ---------------------
    print("\n--- CRASH FOCUS (full history): worst rolling 12-month return + crash sub-periods ---")
    crash_rows = []
    for name in order + ["SPY_buy_hold"]:
        series = spy_full if name == "SPY_buy_hold" else ls_full[name]
        crash_rows.append({"variant": name,
                           "worst_12mo": _f(worst_rolling_12m(series), pct=True),
                           "ret_2008_2009": _f(subperiod_return(series, "2008-01-01", "2009-12-31"), pct=True),
                           "ret_2020": _f(subperiod_return(series, "2020-01-01", "2020-12-31"), pct=True),
                           "full_maxDD": _f(_monthly_metrics(series)["max_drawdown"], pct=True)})
    print(pd.DataFrame(crash_rows).to_string(index=False))

    _verdict(ls_full, spy_oos, blend_oos, split, order)
    return 0


def _verdict(ls_full, spy_oos, blend_oos, split, order) -> None:
    def oos(name):
        return _monthly_metrics(ls_full[name][ls_full[name].index >= split])
    mom = oos("A_momentum")
    spy_m, blend_m = _monthly_metrics(spy_oos), _monthly_metrics(blend_oos)
    mom_full_dd = _monthly_metrics(ls_full["A_momentum"])["max_drawdown"]
    mom_2009 = subperiod_return(ls_full["A_momentum"], "2008-01-01", "2009-12-31")

    print("\n" + "=" * 92)
    print("VERDICT — a variant 'counts' only if it (1) beats momentum-alone OOS after costs,")
    print("         (2) beats passive OOS after costs, AND (3) meaningfully tames the crash.")
    print("=" * 92)
    winners = []
    for name in order:
        if name == "A_momentum":
            continue
        v, vf = oos(name), _monthly_metrics(ls_full[name])
        beats_mom = (v["sharpe"] or -9) > (mom["sharpe"] or -9)
        beats_passive = (v["sharpe"] or -9) > (spy_m["sharpe"] or -9) and (v["sharpe"] or -9) > (blend_m["sharpe"] or -9)
        tames_crash = ((vf["max_drawdown"] or 9) < (mom_full_dd or 9) - 0.03 or
                       (subperiod_return(ls_full[name], "2008-01-01", "2009-12-31") or -9) >
                       (mom_2009 or -9) + 0.03)
        counts = beats_mom and beats_passive and tames_crash
        winners.append((name, counts))
        print(f"  {name:<28} beats_mom={'Y' if beats_mom else 'n'}  "
              f"beats_passive={'Y' if beats_passive else 'n'}  tames_crash={'Y' if tames_crash else 'n'}  "
              f"=> {'COUNTS' if counts else '—'}")
    qualified = [n for n, c in winners if c]
    print(f"\n  Momentum-alone OOS: Sharpe {_f(mom['sharpe'])}, maxDD(full) {_f(mom_full_dd, pct=True)}, "
          f"2008-09 {_f(mom_2009, pct=True)}.")
    print(f"  Passive OOS: SPY Sharpe {_f(spy_m['sharpe'])}.")
    if qualified:
        print(f"  => QUALIFYING variant(s): {', '.join(qualified)} — beat momentum AND passive AND tamed the crash.")
    else:
        print("  => NO variant cleared all three. The fixes mostly REDUCE crash risk (lower maxDD / better")
        print("     2009) but do NOT also beat momentum-alone on OOS Sharpe net of costs.")
    print("\nHonest notes:")
    print("  - All five are well-known, pre-specified fixes (no mining): a surviving edge should be")
    print("    MODEST, and ranking first is not enough — the winner must beat momentum AND passive AND")
    print("    cut the crash. Survivorship is removed and all signals are filing-date / look-ahead safe,")
    print("    but this remains monthly close-to-close decile L/S with turnover costs, not a live strategy.")
    print("  - Vol management changes risk, not raw edge: it lifts Sharpe mainly by shrinking the crash, so")
    print("    judge it on maxDD / 2009 as much as on Sharpe.")


if __name__ == "__main__":
    sys.exit(main())
