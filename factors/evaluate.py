"""Information-coefficient (IC) + quantile harness — does a factor predict returns?

For a factor over the universe and window, on a periodic rebalance, we measure:
  * IC — cross-sectional Spearman rank correlation between the factor on date t and
    the forward return over the next holding period. Report mean IC, IC std, IR
    (annualized mean/std), and the t-stat on mean IC.
  * Quantiles — sort names into deciles by the factor each rebalance, compute each
    decile's forward return and the long-top-minus-bottom spread (with its Sharpe).

THE LEAKAGE DISTINCTION (stated once, enforced below):
  * Factor VALUES on date t use only data <= t (the factor's compute() guarantees
    this; we read its row at t).
  * Forward RETURNS are labels and may use the future: close[t] -> close[t_next].
    Labels are allowed to look ahead; factor values are not.

This measures PREDICTIVE POWER (close-to-close), not tradable P&L net of frictions —
that is the strategy/engine layer's job. Read-only research.
"""

import logging

import numpy as np
import pandas as pd

from factors.base import Factor, FactorData

log = logging.getLogger(__name__)

MIN_NAMES_PER_DATE = 10  # need a real cross-section to rank
DECILES = 10


def _rebalance_dates(index: pd.DatetimeIndex, freq: str) -> pd.DatetimeIndex:
    """Last actual trading day of each period (e.g. month) in the calendar."""
    series = pd.Series(index, index=index)
    grouped = series.groupby(index.to_period(freq)).last()
    return pd.DatetimeIndex(grouped.values)


def _decile_spread(factor_row: pd.Series, forward: pd.Series) -> tuple[dict[int, float], float]:
    """Mean forward return per decile and the top-minus-bottom spread for one date."""
    try:
        labels = pd.qcut(factor_row, DECILES, labels=False, duplicates="drop")
    except (ValueError, IndexError):
        return {}, np.nan
    decile_returns = forward.groupby(labels).mean()
    if decile_returns.empty:
        return {}, np.nan
    top, bottom = decile_returns.index.max(), decile_returns.index.min()
    spread = float(decile_returns.loc[top] - decile_returns.loc[bottom])
    return {int(k): float(v) for k, v in decile_returns.items()}, spread


def evaluate_factor(factor: Factor, data: FactorData, *, freq: str = "M",
                    periods_per_year: int = 12, eligible: pd.DataFrame | None = None,
                    start: pd.Timestamp | None = None, end: pd.Timestamp | None = None) -> dict:
    """Compute IC, IR, t-stat, and decile spread for one factor.

    Rebalances on the last trading day of each period; the holding period is until
    the next rebalance. ``eligible`` is an optional date x symbol boolean mask (e.g. a
    liquidity screen) that restricts the cross-section to tradeable names AS OF the
    rebalance date — computed look-ahead safe by the caller (uses data <= t).

    ``start``/``end`` restrict only WHICH rebalance dates are scored (the holdout split),
    NOT the data fed to ``compute`` — so trailing-window factors keep their full history
    and stay look-ahead safe; we merely measure forward returns on the chosen segment.
    """
    values = factor.compute(data)        # date x symbol; value[t] uses data <= t
    # A division by a near-zero fundamental can yield +/-inf; inf is never a valid factor
    # value and would distort ranks/deciles, so treat it as missing.
    values = values.replace([np.inf, -np.inf], np.nan)
    close = data.close
    rebal = _rebalance_dates(close.index, freq)

    ics: list[float] = []
    spreads: list[float] = []
    decile_accumulator: dict[int, list[float]] = {d: [] for d in range(DECILES)}

    for current, nxt in zip(rebal[:-1], rebal[1:]):
        if start is not None and current < start:
            continue
        if end is not None and current > end:
            continue
        if current not in values.index or current not in close.index or nxt not in close.index:
            continue
        factor_row = values.loc[current]                      # factor as of t (<= t)
        forward = close.loc[nxt] / close.loc[current] - 1.0   # LABEL: future return t -> t_next
        paired = pd.concat([factor_row, forward], axis=1, keys=["f", "r"]).dropna()
        if eligible is not None and current in eligible.index:
            # Restrict to names tradeable as of t (liquidity screen, look-ahead safe).
            mask = eligible.loc[current].reindex(paired.index).fillna(False)
            paired = paired[mask.to_numpy(dtype=bool)]
        if len(paired) < MIN_NAMES_PER_DATE:
            continue

        ics.append(paired["f"].corr(paired["r"], method="spearman"))
        deciles, spread = _decile_spread(paired["f"], paired["r"])
        for decile, ret in deciles.items():
            if decile in decile_accumulator:
                decile_accumulator[decile].append(ret)
        if not np.isnan(spread):
            spreads.append(spread)

    return _summarize(factor, ics, spreads, decile_accumulator, periods_per_year)


def _summarize(factor: Factor, ics: list[float], spreads: list[float],
               decile_accumulator: dict[int, list[float]], periods_per_year: int) -> dict:
    """Aggregate per-rebalance IC/spread series into the factor scorecard."""
    ic = pd.Series(ics, dtype=float).dropna()
    n = len(ic)
    mean_ic = float(ic.mean()) if n else None
    std_ic = float(ic.std(ddof=1)) if n > 1 else None
    ir = (mean_ic / std_ic * np.sqrt(periods_per_year)) if (std_ic not in (None, 0.0)) else None
    t_stat = (mean_ic / (std_ic / np.sqrt(n))) if (std_ic not in (None, 0.0) and n > 1) else None

    spread_series = pd.Series(spreads, dtype=float).dropna()
    mean_spread = float(spread_series.mean()) if len(spread_series) else None
    spread_std = spread_series.std(ddof=1) if len(spread_series) > 1 else None
    tmb_sharpe = (mean_spread / spread_std * np.sqrt(periods_per_year)) \
        if (spread_std not in (None, 0.0)) else None

    avg_deciles = {d: (float(np.mean(v)) if v else None) for d, v in decile_accumulator.items()}

    return {
        "name": factor.name,
        "category": factor.category,
        "n_periods": n,
        "mean_ic": mean_ic,
        "std_ic": std_ic,
        "ir": ir,
        "t_stat": t_stat,
        "top_minus_bottom": mean_spread,
        "tmb_sharpe": tmb_sharpe,
        "avg_decile_returns": avg_deciles,
    }
