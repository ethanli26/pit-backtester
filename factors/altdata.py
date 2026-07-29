"""Insider (SF2) and institutional (SF3) factors — candidate momentum DIVERSIFIERS.

Momentum is the one validated factor in this codebase; it is a PRICE signal. Insider and
institutional ownership are economically DISTINCT information sources (what corporate
insiders and 13F institutions are actually doing), so a survivor here would diversify
momentum rather than duplicate it. These data families are cached but untested — this
runs them through the same honest gauntlet (IC, then walk-forward, then a momentum-
correlation check) without lowering the bar.

POINT-IN-TIME: each factor reads forward-filled panels built in
``data/sharadar_provider.py`` keyed to the data's PUBLIC availability date — the Form 4
``filingdate`` for insiders, and ``calendardate + 45 days`` (the 13F deadline) for
institutions. So a value on date ``t`` uses only information public on or before ``t``.

All four carry a POSITIVE expected IC sign (see each class). Read-only research, no orders.
"""

import numpy as np
import pandas as pd

from factors.base import Factor, FactorData, register


def _panel(data: FactorData, field: str) -> pd.DataFrame | None:
    """Return a fundamentals panel aligned to the close calendar, or None if absent.

    Aligning to ``data.close.index`` keeps any element-wise combine index-matched, and
    therefore look-ahead safe (each panel value at ``t`` is availability-dated <= t).
    """
    if data.fundamentals is None or field not in data.fundamentals:
        return None
    return data.fundamentals[field].reindex(data.close.index)


def _nan_like(data: FactorData) -> pd.DataFrame:
    """An all-NaN date x symbol frame — what a factor yields when its data is absent."""
    return pd.DataFrame(np.nan, index=data.close.index, columns=data.close.columns)


@register
class InsiderBuying(Factor):
    """Net open-market insider buying over a trailing window, scaled by market cap.

    Source: Lakonishok & Lee (2001), "Are Insider Trades Informative?", RFS; Jeng,
    Metrick & Zeckhauser (2003), "Estimating the Returns to Insider Trading", REStat —
    open-market insider PURCHASES predict positive abnormal returns (sales are far less
    informative, being driven by diversification/liquidity). Rationale: insiders trade on
    superior information about their own firm. Expected IC: POSITIVE.

    LOOK-AHEAD GUARD: ``insider_net_buy_value`` is a trailing sum keyed to the Form 4
    FILING date (public date), divided here by the filing-dated shares x day-t close.
    """

    name = "insider_buying"
    category = "insider"
    requires = ("insider_net_buy_value", "shares")
    point_in_time_provider = True

    def compute(self, data: FactorData) -> pd.DataFrame:
        net = _panel(data, "insider_net_buy_value")
        shares = _panel(data, "shares")
        if net is None or shares is None:
            return _nan_like(data)
        market_cap = (shares * data.close).where(lambda x: x > 0)
        return net / market_cap


@register
class InsiderBuyIntensity(Factor):
    """Number of DISTINCT insiders making open-market purchases in a trailing window.

    Source: Lakonishok & Lee (2001) on consensus/cluster buying; Cohen, Malloy & Pomorski
    (2012), "Decoding Inside Information", JF — clusters of independent insider buyers are
    a stronger, less noisy signal than a single buyer. Rationale: agreement among several
    insiders is harder to explain by idiosyncratic liquidity needs. Expected IC: POSITIVE.

    LOOK-AHEAD GUARD: distinct-buyer counts use Form-4 FILING dates within (t-window, t].
    """

    name = "insider_buy_intensity"
    category = "insider"
    requires = ("insider_distinct_buyers",)
    point_in_time_provider = True

    def compute(self, data: FactorData) -> pd.DataFrame:
        buyers = _panel(data, "insider_distinct_buyers")
        return buyers if buyers is not None else _nan_like(data)


@register
class InstitutionalOwnershipChange(Factor):
    """Quarter-over-quarter change in institutional ownership % (13F).

    Source: Gompers & Metrick (2001), "Institutional Investors and Equity Prices", QJE
    (institutional demand) and Nofsinger & Sias (1999), "Herding and Feedback Trading", JF.
    Rationale/SIGN DEBATE: rising institutional ownership can mean informed accumulation
    (POSITIVE), but it can also mark a crowded/over-owned name with little marginal buyer
    left (a contrarian NEGATIVE). We encode the change naturally and set the prior POSITIVE
    (accumulation), but the gauntlet REPORTS what the data actually says — a significant
    negative IC would be the crowding effect, surfaced by the sign check.

    LOOK-AHEAD GUARD: the change is computed on the 13F availability grid (calendardate +
    45-day deadline) and forward-filled, so quarter Q only enters AFTER its filing date.
    """

    name = "institutional_ownership_change"
    category = "institutional"
    requires = ("inst_own_pct_chg",)
    point_in_time_provider = True

    def compute(self, data: FactorData) -> pd.DataFrame:
        chg = _panel(data, "inst_own_pct_chg")
        return chg if chg is not None else _nan_like(data)


@register
class InstitutionalConcentration(Factor):
    """Change in BREADTH of institutional ownership (distinct 13F holders), QoQ.

    Source: Chen, Hong & Stein (2002), "Breadth of Ownership and Stock Returns", JFE —
    rising breadth (more institutions taking a position) predicts POSITIVE returns, while
    falling breadth (institutions exiting / short-sale constraints binding) predicts
    negative returns. Breadth is the INVERSE of concentration, so a higher value =
    less-concentrated, broader ownership. Expected IC: POSITIVE.

    LOOK-AHEAD GUARD: breadth change is on the 45-day-lagged 13F availability grid,
    forward-filled — no quarter is used before its filing deadline.
    """

    name = "institutional_concentration"
    category = "institutional"
    requires = ("inst_breadth_chg",)
    point_in_time_provider = True

    def compute(self, data: FactorData) -> pd.DataFrame:
        breadth_chg = _panel(data, "inst_breadth_chg")
        return breadth_chg if breadth_chg is not None else _nan_like(data)


# Literature-expected IC signs (positive IC = thesis holds) for the gauntlet's sign check.
ALTDATA_EXPECTED_SIGN = {
    "insider_buying": +1,
    "insider_buy_intensity": +1,
    "institutional_ownership_change": +1,   # accumulation prior; crowding could flip it
    "institutional_concentration": +1,      # CHS breadth: rising breadth -> positive
}
ALTDATA_FACTORS = list(ALTDATA_EXPECTED_SIGN)
