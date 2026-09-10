"""Vol-managed momentum — the validated, deployable crash-controlled momentum strategy.

This promotes the one variant that, on survivorship-free data OOS after costs, beat both
momentum-alone AND passive AND tamed the crash (see ml/momentum_variants.py): 12-1
momentum, ranked cross-sectionally, formed monthly as decile portfolios, then scaled to a
target volatility (Barroso & Santa-Clara 2015) so exposure shrinks before momentum's
crash-prone high-vol regimes.

It is a CROSS-SECTIONAL, MONTHLY, LEVERAGE-OVERLAY portfolio — a different paradigm from
the per-name daily breakout engine — so it is registered in the portfolio registry, not
the engine registry. Two variants:

  * long/short  — long top decile, short bottom decile (market-neutral-ish), vol-scaled.
  * long-only   — long top decile only, vol-scaled with the remainder in cash (for paper/
    live accounts that cannot short). Set ``config.VMM_LONG_ONLY`` or pass ``long_only``.

Every parameter lives in config (VMM_*). Nothing here was tuned to the result: the vol
TARGET is set from the TRAIN window only (never the test window), and all look-ahead
guards from the research helpers are preserved and re-flagged below.

Read-only research + paper sizing. No live orders.
"""

import numpy as np
import pandas as pd

import config
from factors.evaluate import DECILES, MIN_NAMES_PER_DATE, _rebalance_dates
from factors.library import Momentum12_1
from research.combine_train import RF_ANNUAL, long_short_returns
from strategies.registry import register_portfolio


def vol_target_weights(returns: pd.Series, train_end: pd.Timestamp | None,
                       window: int, cap: float) -> pd.Series:
    """Inverse-vol gross-exposure weights toward a constant target (Barroso-Santa-Clara).

    weight_t = target / realized_vol(returns up to t-1), capped at ``cap``.

    LOOK-AHEAD GUARDS: (1) ``realized.shift(1)`` makes the weight for month t use only
    returns realized BEFORE t; (2) the constant ``target`` is the MEDIAN trailing vol over
    the TRAIN window (strictly before ``train_end``), so the test stretch is scaled by a
    target fixed from the past — no future information enters.
    """
    realized = returns.rolling(window).std()
    train = realized[realized.index < train_end].dropna() if train_end is not None else realized.dropna()
    target = train.median() if len(train) else realized.median()
    return (target / realized.shift(1)).clip(upper=cap)


def long_only_returns(values: pd.DataFrame, close: pd.DataFrame, eligible: pd.DataFrame,
                      *, start=None, end=None, cost_bps_per_side: float) -> pd.Series:
    """Monthly NET return of an equal-weight LONG top-decile book (no short leg).

    LEAKAGE GUARD: the decile is formed from the factor at ``current`` (<= current); the
    return is the realized ``current -> nxt`` move (a label). Cost is charged on the long
    leg's actual one-way name turnover at ``cost_bps_per_side``.
    """
    rebal = _rebalance_dates(close.index, "M")
    per_side = cost_bps_per_side / 10_000.0
    dates, net, prev_top = [], [], set()
    for current, nxt in zip(rebal[:-1], rebal[1:]):
        if (start is not None and current < start) or (end is not None and current > end):
            continue
        if current not in values.index or current not in close.index or nxt not in close.index:
            continue
        forward = close.loc[nxt] / close.loc[current] - 1.0
        paired = pd.concat([values.loc[current], forward], axis=1, keys=["f", "r"]).dropna()
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
        ret = paired.loc[list(top), "r"].mean()
        turnover = 1.0 - len(top & prev_top) / len(top) if prev_top else 1.0
        net.append(float(ret - per_side * 2.0 * turnover))   # buy new + sell dropped
        dates.append(nxt)
        prev_top = top
    return pd.Series(net, index=pd.DatetimeIndex(dates), name="long_only")


@register_portfolio
class VolManagedMomentum:
    """Crash-controlled 12-1 momentum decile portfolio with a Barroso vol overlay."""

    name = "vol_managed_momentum"
    category = "price"
    point_in_time_provider = False   # momentum is price-only (runs on the cached universe)

    def __init__(self, long_only: bool | None = None, vol_window: int = config.VMM_VOL_WINDOW,
                 leverage_cap: float = config.VMM_LEVERAGE_CAP,
                 cost_bps_per_side: float = config.VMM_COST_BPS_PER_SIDE):
        self.long_only = config.VMM_LONG_ONLY if long_only is None else long_only
        self.vol_window = vol_window
        self.leverage_cap = leverage_cap
        self.cost_bps_per_side = cost_bps_per_side
        self.params = {"long_only": self.long_only, "vol_window": vol_window,
                       "leverage_cap": leverage_cap, "cost_bps_per_side": cost_bps_per_side}

    # --- signal -----------------------------------------------------------------
    def score(self, data) -> pd.DataFrame:
        """Cross-sectional 12-1 momentum panel (look-ahead safe via the Factor)."""
        return Momentum12_1().compute(data)

    # --- portfolio return stream ------------------------------------------------
    def portfolio_returns(self, data, eligible: pd.DataFrame, *, train_end=None,
                          start=None, end=None) -> pd.Series:
        """Vol-managed monthly net returns, optionally sliced to a test window [start, end].

        The raw return stream and the trailing-vol weights are built over FULL history so
        the weight at any month uses real prior data; ``start``/``end`` then slice only the
        OUTPUT. ``train_end`` (default ``start``) fixes the constant vol target using data
        strictly before it — so a walk-forward test window is scaled by a pre-window target.
        """
        train_end = train_end if train_end is not None else start
        scores = self.score(data)
        if self.long_only:
            raw = long_only_returns(scores, data.close, eligible, cost_bps_per_side=self.cost_bps_per_side)
            weights = vol_target_weights(raw, train_end, self.vol_window, self.leverage_cap)
            # Long-only: invest ``weight`` in the book, remainder in cash earning rf.
            managed = (weights * raw + (1.0 - weights) * (RF_ANNUAL / 12.0)).dropna()
        else:
            raw = long_short_returns(scores, data.close, eligible, cost_bps_per_side=self.cost_bps_per_side)
            weights = vol_target_weights(raw, train_end, self.vol_window, self.leverage_cap)
            # Dollar-neutral L/S: scaling applies to the spread; unused margin earns ~0.
            managed = (weights * raw).dropna()
        if start is not None:
            managed = managed[managed.index >= start]
        if end is not None:
            managed = managed[managed.index <= end]
        return managed

    # --- deployment: translate the top decile into a concrete paper book --------
    def top_decile(self, scores_row: pd.Series, eligible_row: pd.Series) -> list[str]:
        """The eligible top-decile names by 12-1 momentum on the rebalance date.

        LOOK-AHEAD GUARD: ``scores_row`` is the momentum factor at the rebalance date,
        which uses only past closes (shift(21)/shift(252)); eligibility is as-of the date.
        """
        elig = eligible_row.reindex(scores_row.index).fillna(False).to_numpy(dtype=bool)
        ranked = scores_row[elig].dropna()
        if len(ranked) < MIN_NAMES_PER_DATE:
            return []
        labels = pd.qcut(ranked, DECILES, labels=False, duplicates="drop")
        return list(ranked.index[labels == labels.max()])

    def paper_book(self, equity: float, scores_row: pd.Series, prices_row: pd.Series,
                   eligible_row: pd.Series, gross_weight: float = 1.0) -> dict[str, int]:
        """Integer-share EQUAL-WEIGHT long top-decile book, scaled to gross exposure.

        The validated strategy is an equal-weight decile, so we deploy it equal-weight:
        invest ``gross_weight`` of equity across the decile (the Barroso vol scaling, capped
        at ``leverage_cap``), each name capped at the EXISTING per-name portfolio cap
        ``config.MAX_POSITION_PCT``. (We intentionally do NOT use the 1%-risk
        ``size_position`` here — that sizer is for the few-name breakout book and would
        grossly over-allocate a ~90-name decile, i.e. a different strategy than the one
        that survived walk-forward.)
        """
        names = self.top_decile(scores_row, eligible_row)
        if not names:
            return {}
        gross = min(gross_weight, self.leverage_cap)
        per_name_weight = min(gross / len(names), config.MAX_POSITION_PCT)   # existing per-name cap
        book: dict[str, int] = {}
        for symbol in names:
            price = prices_row.get(symbol)
            if price is None or np.isnan(price) or price <= 0:
                continue
            shares = int(np.floor(per_name_weight * equity / price))
            if shares > 0:
                book[symbol] = shares
        return book

    def current_gross_weight(self, data, eligible: pd.DataFrame) -> float:
        """Latest Barroso vol-scaling gross weight to deploy now (long-only stream).

        LOOK-AHEAD GUARD: weights use ``realized.shift(1)`` so the current weight is set by
        volatility realized THROUGH THE PRIOR MONTH; ``train_end=None`` uses all history up
        to now (live deployment — everything is <= today, no future data).
        """
        raw = long_only_returns(self.score(data), data.close, eligible,
                                cost_bps_per_side=self.cost_bps_per_side)
        weights = vol_target_weights(raw, None, self.vol_window, self.leverage_cap).dropna()
        return float(weights.iloc[-1]) if len(weights) else 1.0

    def target_book(self, equity: float, data, eligible: pd.DataFrame) -> tuple[dict[str, int], float, pd.Timestamp]:
        """(target shares, gross_weight, as-of date) for the latest cached rebalance date."""
        scores, close = self.score(data), data.close
        as_of = close.index[-1]
        gross_weight = self.current_gross_weight(data, eligible)
        book = self.paper_book(equity, scores.loc[as_of], close.loc[as_of], eligible.loc[as_of], gross_weight)
        return book, gross_weight, as_of

    # --- capacity / turnover ----------------------------------------------------
    def annual_turnover(self, data, eligible: pd.DataFrame) -> float:
        """Average one-way decile turnover per rebalance, annualized (x12)."""
        scores, close = self.score(data), data.close
        rebal = _rebalance_dates(close.index, "M")
        turnovers, prev = [], set()
        for current in rebal:
            if current not in scores.index:
                continue
            row = scores.loc[current].dropna()
            if current in eligible.index:
                row = row[eligible.loc[current].reindex(row.index).fillna(False).to_numpy(dtype=bool)]
            if len(row) < MIN_NAMES_PER_DATE:
                continue
            try:
                labels = pd.qcut(row, DECILES, labels=False, duplicates="drop")
            except (ValueError, IndexError):
                continue
            top = set(row.index[labels == labels.max()])
            if prev:
                turnovers.append(1.0 - len(top & prev) / len(top))
            prev = top
        return float(np.mean(turnovers) * 12) if turnovers else float("nan")
