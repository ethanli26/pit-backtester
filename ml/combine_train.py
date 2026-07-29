"""Does combining momentum_12_1 (price) + profitability (fundamental) beat each alone?

These are the two individually-significant, correctly-signed, economically DISTINCT
survivors of the survivorship-free scorecard. Price and fundamental signals are nearly
uncorrelated, so an equal-weight (untuned) blend is the legitimate diversification test.

The bar, set IN ADVANCE — the composite must:
  1. beat BOTH standalone factors' IC (full history), and
  2. clear the IC bar (|mean IC| > 0.02, |t| > 2) OUT OF SAMPLE (last ~30%, never used
     to choose anything — equal weight fits nothing, so OOS just confirms stability), and
  3. beat passive (SPY buy-hold and the vol-matched SPY/cash blend) after realistic costs.

Everything runs offline from the local Sharadar archive. Read-only research, no orders.

    python ml/combine_train.py
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import config  # noqa: E402,F401  (loads .env)
from backtest import risk_metrics  # noqa: E402
from factors.base import Factor, FactorData  # noqa: E402
from factors.composite import CompositeFactor  # noqa: E402
from factors.evaluate import (  # noqa: E402
    DECILES,
    MIN_NAMES_PER_DATE,
    _rebalance_dates,
    evaluate_factor,
)
from factors.fundamentals import Profitability  # noqa: E402
from factors.library import Momentum12_1  # noqa: E402
from factors.run_factor_eval import build_sharadar_factor_data  # noqa: E402

log = logging.getLogger("combine_train")

IC_THRESHOLD = 0.02
TSTAT_THRESHOLD = 2.0
OOS_FRACTION = 0.30                 # last 30% of rebalances are the holdout
RF_ANNUAL = 0.04
COST_BPS_PER_SIDE = config.SLIPPAGE_BPS_MID   # reuse the repo's mid-tier slippage (15 bps/side)
SPY_CACHE = Path(__file__).resolve().parent.parent / "backtest" / "cache" / "ohlcv" / "SPY.parquet"


class _Panel(Factor):
    """Wrap a precomputed date x symbol panel so evaluate_factor can reuse it (no recompute)."""

    def __init__(self, name: str, category: str, panel: pd.DataFrame):
        self.name, self.category, self._panel = name, category, panel

    def compute(self, data: FactorData) -> pd.DataFrame:
        return self._panel


# --- monthly performance metrics (the harness's risk_metrics assume daily) ----------

def _monthly_metrics(returns: pd.Series, rf_annual: float = RF_ANNUAL) -> dict:
    """CAGR / Sharpe / Sortino / maxDD / total return from a MONTHLY net-return series."""
    returns = returns.dropna()
    if len(returns) < 2:
        return {"cagr": None, "sharpe": None, "sortino": None, "max_drawdown": None, "total": None}
    equity = (1.0 + returns).cumprod()
    excess = returns - rf_annual / 12.0
    std = returns.std(ddof=1)
    downside = excess.clip(upper=0.0)
    dd = np.sqrt((downside ** 2).mean())
    mdd, _, _ = risk_metrics.max_drawdown_detail(equity)
    return {
        "cagr": risk_metrics.cagr(equity),                                   # calendar-year, freq-agnostic
        "sharpe": float(excess.mean() / std * np.sqrt(12)) if std else None,
        "sortino": float(excess.mean() / dd * np.sqrt(12)) if dd else None,
        "max_drawdown": mdd,
        "total": float(equity.iloc[-1] - 1.0),
    }


# --- long-short decile portfolio with turnover-based costs --------------------------

def long_short_returns(values: pd.DataFrame, close: pd.DataFrame, eligible: pd.DataFrame,
                       *, start=None, end=None, cost_bps_per_side: float = COST_BPS_PER_SIDE) -> pd.Series:
    """Monthly NET return of a long-top-decile / short-bottom-decile portfolio.

    LEAKAGE GUARD: the decile is formed from the factor at ``current`` (<= current); the
    return is the realized ``current -> nxt`` move (a label). Costs are charged on the
    actual name turnover of each leg (one-way fraction replaced) at ``cost_bps_per_side``.
    """
    rebal = _rebalance_dates(close.index, "M")
    per_side = cost_bps_per_side / 10_000.0
    dates, net = [], []
    prev_top, prev_bottom = set(), set()
    for current, nxt in zip(rebal[:-1], rebal[1:]):
        if (start is not None and current < start) or (end is not None and current > end):
            continue
        if current not in values.index or current not in close.index or nxt not in close.index:
            continue
        row = values.loc[current]
        forward = close.loc[nxt] / close.loc[current] - 1.0
        paired = pd.concat([row, forward], axis=1, keys=["f", "r"]).dropna()
        if current in eligible.index:
            mask = eligible.loc[current].reindex(paired.index).fillna(False)
            paired = paired[mask.to_numpy(dtype=bool)]
        if len(paired) < MIN_NAMES_PER_DATE:
            continue
        try:
            labels = pd.qcut(paired["f"], DECILES, labels=False, duplicates="drop")
        except (ValueError, IndexError):
            continue
        top = set(paired.index[labels == labels.max()])
        bottom = set(paired.index[labels == labels.min()])
        gross = paired.loc[list(top), "r"].mean() - paired.loc[list(bottom), "r"].mean()
        # One-way turnover per leg = fraction of names replaced since last rebalance.
        turn_top = 1.0 - len(top & prev_top) / len(top) if prev_top else 1.0
        turn_bottom = 1.0 - len(bottom & prev_bottom) / len(bottom) if prev_bottom else 1.0
        cost = per_side * 2.0 * (turn_top + turn_bottom)   # buy+sell on each leg
        dates.append(nxt)
        net.append(float(gross - cost))
        prev_top, prev_bottom = top, bottom
    return pd.Series(net, index=pd.DatetimeIndex(dates), name="ls_net")


def spy_monthly_returns(rebal_pairs: list[tuple], spy_close: pd.Series) -> pd.Series:
    """SPY buy-hold return over each (current -> nxt) rebalance interval, indexed by nxt."""
    # Normalize to tz-naive calendar dates (the SPY parquet and Sharadar calendars differ).
    spy = pd.Series(spy_close.to_numpy(),
                    index=pd.DatetimeIndex(pd.to_datetime(spy_close.index).normalize()))
    spy = spy[~spy.index.duplicated(keep="last")].sort_index()
    rebal_dates = pd.DatetimeIndex(sorted({d for pair in rebal_pairs for d in pair})).normalize()
    level = spy.reindex(spy.index.union(rebal_dates)).ffill().reindex(rebal_dates)  # as-of each date
    rows = [(nxt, level[nxt.normalize()] / level[current.normalize()] - 1.0)
            for current, nxt in rebal_pairs
            if not np.isnan(level.get(current.normalize(), np.nan))
            and not np.isnan(level.get(nxt.normalize(), np.nan))]
    return pd.Series([r for _, r in rows], index=pd.DatetimeIndex([d for d, _ in rows]), name="spy")


def vol_matched_blend(spy_returns: pd.Series, target_vol: float, rf_annual: float = RF_ANNUAL) -> pd.Series:
    """SPY/cash blend scaled so its vol matches ``target_vol`` (cash earns rf)."""
    spy_vol = spy_returns.std(ddof=1)
    weight = 0.0 if spy_vol == 0 else min(1.0, max(0.0, target_vol / spy_vol))
    return weight * spy_returns + (1.0 - weight) * (rf_annual / 12.0)


# --- reporting ---------------------------------------------------------------------

def _fmt(v, pct=False):
    if v is None:
        return "n/a"
    return f"{v * 100:+.2f}%" if pct else f"{v:+.3f}"


def _scorecard_row(name: str, score: dict) -> dict:
    return {"factor": name, "n": score["n_periods"], "mean_IC": _fmt(score["mean_ic"]),
            "IR": _fmt(score["ir"]), "t_stat": _fmt(score["t_stat"]),
            "decile_spread": _fmt(score["top_minus_bottom"], pct=True),
            "TMB_Sharpe": _fmt(score["tmb_sharpe"])}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("data.sharadar_provider").setLevel(logging.WARNING)

    data, eligible = build_sharadar_factor_data()      # offline, point-in-time, survivorship-free
    close = data.close

    # Precompute the four factor panels ONCE (cheap, avoids recompute across segments).
    mom = Momentum12_1().compute(data)
    prof = Profitability().compute(data)
    comp_z = CompositeFactor([Momentum12_1(), Profitability()], "zscore").compute(data)
    comp_r = CompositeFactor([Momentum12_1(), Profitability()], "rank").compute(data)
    factors = {
        "momentum_12_1": _Panel("momentum_12_1", "price", mom),
        "profitability": _Panel("profitability", "fundamental", prof),
        "composite_zscore": _Panel("composite_zscore", "composite", comp_z),
        "composite_rank": _Panel("composite_rank", "composite", comp_r),
    }

    # OOS split: last OOS_FRACTION of monthly rebalances is the holdout.
    rebal = _rebalance_dates(close.index, "M")
    split = rebal[int(len(rebal) * (1.0 - OOS_FRACTION))]
    print("=" * 84)
    print("COMPOSITE TEST: momentum_12_1 (price) + profitability (fundamental), equal-weight")
    print("=" * 84)
    print(f"Universe: liquid survivorship-free US common stocks | rebalances {rebal[0].date()} "
          f"-> {rebal[-1].date()} | OOS split at {split.date()} (last {int(OOS_FRACTION*100)}%)")
    print(f"Costs (traded check): {COST_BPS_PER_SIDE:.0f} bps/side on actual decile turnover. "
          f"Equal weight is UNTUNED by design.\n")

    # --- (1) Full-history scorecard --------------------------------------------------
    full = {name: evaluate_factor(f, data, eligible=eligible) for name, f in factors.items()}
    print("--- FULL HISTORY (the composite must beat BOTH standalone ICs) ---")
    print(pd.DataFrame([_scorecard_row(n, full[n]) for n in factors]).to_string(index=False))

    mom_ic, prof_ic = full["momentum_12_1"]["mean_ic"], full["profitability"]["mean_ic"]
    best_alone = max(mom_ic, prof_ic)
    beats_both = {m: full[m]["mean_ic"] > best_alone for m in ("composite_zscore", "composite_rank")}
    print(f"\nBeats BOTH standalone ICs (> {best_alone:+.4f}): "
          + ", ".join(f"{m}={'YES' if v else 'no'}" for m, v in beats_both.items()))

    # --- (2) In-sample vs out-of-sample ---------------------------------------------
    print("\n--- IN-SAMPLE (first 70%) vs OUT-OF-SAMPLE (last 30%, never used to choose) ---")
    segments, seg_rows = {}, []
    for name, f in factors.items():
        is_s = evaluate_factor(f, data, eligible=eligible, end=split)
        oos = evaluate_factor(f, data, eligible=eligible, start=split)
        segments[name] = {"is": is_s, "oos": oos}
        seg_rows.append({"factor": name,
                         "IS_IC": _fmt(is_s["mean_ic"]), "IS_t": _fmt(is_s["t_stat"]),
                         "IS_Sharpe": _fmt(is_s["tmb_sharpe"]),
                         "OOS_IC": _fmt(oos["mean_ic"]), "OOS_t": _fmt(oos["t_stat"]),
                         "OOS_Sharpe": _fmt(oos["tmb_sharpe"]),
                         "OOS_clears_bar": "YES" if (oos["mean_ic"] is not None and oos["t_stat"] is not None
                                                     and abs(oos["mean_ic"]) > IC_THRESHOLD
                                                     and abs(oos["t_stat"]) > TSTAT_THRESHOLD) else "no"})
    print(pd.DataFrame(seg_rows).to_string(index=False))
    oos_ic = {n: segments[n]["oos"]["mean_ic"] for n in factors}
    beats_both_oos = oos_ic["composite_zscore"] > max(oos_ic["momentum_12_1"], oos_ic["profitability"])
    print(f"\nOOS: composite_zscore beats BOTH standalone OOS ICs "
          f"(> {max(oos_ic['momentum_12_1'], oos_ic['profitability']):+.4f}): "
          f"{'YES' if beats_both_oos else 'no'}")

    # --- (4) Traded OOS check after costs vs passive --------------------------------
    print("\n--- TRADED, OUT-OF-SAMPLE, AFTER COSTS (long top decile / short bottom decile) ---")
    ls = {name: long_short_returns(factors[name].compute(data), close, eligible, start=split)
          for name in ("composite_zscore", "momentum_12_1", "profitability")}
    comp_returns = ls["composite_zscore"]

    spy_close = pd.read_parquet(SPY_CACHE)["Close"]
    pairs = [(c, n) for c, n in zip(rebal[:-1], rebal[1:]) if c >= split]
    spy_ret = spy_monthly_returns(pairs, spy_close).reindex(comp_returns.index).dropna()
    blend_ret = vol_matched_blend(spy_ret, target_vol=comp_returns.std(ddof=1))

    perf = {
        "composite_zscore L/S": _monthly_metrics(comp_returns),
        "momentum_12_1 L/S": _monthly_metrics(ls["momentum_12_1"]),
        "profitability L/S": _monthly_metrics(ls["profitability"]),
        "SPY buy-hold": _monthly_metrics(spy_ret),
        "vol-matched SPY/cash": _monthly_metrics(blend_ret),
    }
    rows = [{"portfolio": k, "CAGR": _fmt(v["cagr"], pct=True), "Sharpe": _fmt(v["sharpe"]),
             "Sortino": _fmt(v["sortino"]), "maxDD": _fmt(v["max_drawdown"], pct=True),
             "total_return": _fmt(v["total"], pct=True)} for k, v in perf.items()]
    print(pd.DataFrame(rows).to_string(index=False))
    blend_w = min(1.0, comp_returns.std(ddof=1) / spy_ret.std(ddof=1)) if spy_ret.std(ddof=1) else 0.0
    if blend_w >= 1.0:
        print("  (vol-matched blend pins to 100% SPY: the dollar-neutral L/S is higher-vol than SPY, "
              "so matching its vol would require LEVERING SPY, which a cash blend cannot.)")

    _verdict(beats_both, beats_both_oos, segments, perf)
    return 0


def _verdict(beats_both_full, beats_both_oos, segments, perf) -> None:
    oos = segments["composite_zscore"]["oos"]
    oos_clears = (oos["mean_ic"] is not None and oos["t_stat"] is not None
                  and abs(oos["mean_ic"]) > IC_THRESHOLD and abs(oos["t_stat"]) > TSTAT_THRESHOLD)
    comp_sharpe = perf["composite_zscore L/S"]["sharpe"] or -9
    beats_passive = (comp_sharpe > (perf["SPY buy-hold"]["sharpe"] or -9)
                     and comp_sharpe > (perf["vol-matched SPY/cash"]["sharpe"] or -9))
    print("\n" + "=" * 84)
    print("VERDICT (bar set in advance: beat both alone + clear IC bar OOS + beat passive net)")
    print("=" * 84)
    print(f"  1. composite beats BOTH standalone ICs  (full: {'yes' if beats_both_full['composite_zscore'] else 'no'}, "
          f"OOS: {'yes' if beats_both_oos else 'no'}):  {'PASS' if beats_both_oos else 'FAIL'}")
    print(f"  2. composite clears the IC bar (|t|>2) OUT OF SAMPLE:   {'PASS' if oos_clears else 'FAIL'} "
          f"(OOS t={oos['t_stat']:+.2f}, IC={oos['mean_ic']:+.4f})")
    print(f"  3. composite L/S beats SPY & vol-matched blend (net):  {'PASS' if beats_passive else 'FAIL'} "
          f"(Sharpe {comp_sharpe:+.2f} vs SPY {perf['SPY buy-hold']['sharpe']:+.2f})")
    all_pass = beats_both_oos and oos_clears and beats_passive
    print(f"\n  => {'ALL THREE PASS — the combination is real, not overfit.' if all_pass else 'NOT all three pass.'}")
    print("\nHonest notes:")
    print("  - Equal weight is DELIBERATELY untuned (no weights fit to results); OOS is a pure")
    print("    stability check, not a fitted holdout.")
    print("  - Survivorship bias is removed (delisted names included) and all factor values are")
    print("    filing-date / look-ahead safe — but this is still close-to-close MONTHLY IC plus a")
    print("    stylized decile L/S with turnover costs, NOT a live, capacity-aware strategy.")


if __name__ == "__main__":
    sys.exit(main())
