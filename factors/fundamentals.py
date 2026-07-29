"""The US-effective fundamental factor library: profitability, value, growth, quality.

A US-vs-China factor study (Orient Securities report 22) found that in US equities the
FUNDAMENTAL factors — profitability, value, and earnings growth — are the most
effective, in contrast to the price/reversal factors that dominate Chinese A-shares.
Those factors could never be tested in this harness before because they require
point-in-time fundamentals; with the Sharadar provider they finally can.

Each factor reads ``FactorData.fundamentals`` — ``{field: date x symbol}`` panels that
are forward-filled from FILING dates (see data/sharadar_provider.build_fundamental_panels),
so the value on date ``t`` uses only statements filed on or before ``t``. Combined with
the day-``t`` close, each factor is point-in-time safe. ``point_in_time_provider = True``
marks them so the free-data harness defers them.

Each factor is encoded in its NATURAL economic direction; its literature-expected IC
SIGN (recorded in the harness) may be positive (quality/value/growth) or negative
(accruals, asset growth, leverage). A passer with the WRONG sign is flagged as a likely
artifact, not a signal.

Read-only research: no orders.
"""

import pandas as pd

from factors.base import Factor, FactorData, register


def _require_fundamentals(data: FactorData, fields: tuple[str, ...]) -> dict[str, pd.DataFrame]:
    """Return the needed fundamental panels, or raise if running without PIT data.

    Aligns each panel to the close calendar so an element-wise combine with price is
    index-matched (and therefore look-ahead safe: panel[t] is filing-dated <= t).
    """
    if data.fundamentals is None:
        raise NotImplementedError(
            "Fundamental factors need point-in-time data. Run on the 'sharadar_broad' "
            "universe with a Sharadar API key (NASDAQ_DATA_LINK_API_KEY); the free data "
            "path cannot honor filing-dated fundamentals.")
    missing = [f for f in fields if f not in data.fundamentals]
    if missing:
        raise KeyError(f"FactorData.fundamentals missing required panels: {missing}")
    return {f: data.fundamentals[f].reindex(data.close.index) for f in fields}


def _market_cap(f: dict[str, pd.DataFrame], close: pd.DataFrame) -> pd.DataFrame:
    """Filing-dated shares x day-t close, masked to positive (avoids div-by-zero)."""
    # LOOK-AHEAD GUARD: shares is filing-dated (<= t); close is the day-t price.
    return (f["shares"] * close).where(lambda x: x > 0)


def _zscore_rows(panel: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional (per-date) z-score of a panel.

    LOOK-AHEAD GUARD: standardization uses only that row's cross-section (same date t),
    so it introduces no time leakage.
    """
    mean = panel.mean(axis=1)
    std = panel.std(axis=1, ddof=0)
    return panel.sub(mean, axis=0).div(std.where(std > 0), axis=0)


@register
class Profitability(Factor):
    """Gross profitability: trailing-12m gross profit / total assets.

    Source: Novy-Marx (2013), "The Other Side of Value: The Gross Profitability
    Premium", JFE 108(1). Rationale: more profitable firms earn higher returns; gross
    profit is the cleanest (least-manipulated) profitability line. Expected IC: POSITIVE.
    """

    name = "profitability"
    category = "fundamental"
    requires = ("gross_profit_ttm", "total_assets")
    point_in_time_provider = True

    def compute(self, data: FactorData) -> pd.DataFrame:
        f = _require_fundamentals(data, self.requires)
        # LOOK-AHEAD GUARD: both panels are filing-dated (datekey <= t); ratio is element-
        # wise, so value[t] uses only statements filed on or before t.
        assets = f["total_assets"].where(f["total_assets"] > 0)
        return f["gross_profit_ttm"] / assets


@register
class EarningsYield(Factor):
    """Earnings yield (E/P): trailing-12m net income / market cap.

    Source: Basu (1977), "Investment Performance of Common Stocks in Relation to Their
    Price-Earnings Ratios", JF 32(3). Rationale: cheap (high E/P) stocks outperform.
    Expected IC: POSITIVE. Market cap = filing-dated shares x day-t close.
    """

    name = "earnings_yield"
    category = "fundamental"
    requires = ("net_income_ttm", "shares")
    point_in_time_provider = True

    def compute(self, data: FactorData) -> pd.DataFrame:
        f = _require_fundamentals(data, self.requires)
        # LOOK-AHEAD GUARD: net income & shares are filing-dated (<= t); close is day-t.
        market_cap = (f["shares"] * data.close).where(lambda x: x > 0)
        return f["net_income_ttm"] / market_cap


@register
class BookToPrice(Factor):
    """Book-to-price (value): book equity / market cap.

    Source: the value factor of Fama & French (1992, 1993) (HML uses book-to-market).
    Rationale: high book-to-market ("value") stocks earn higher returns. Expected IC:
    POSITIVE. Market cap = filing-dated shares x day-t close.
    """

    name = "book_to_price"
    category = "fundamental"
    requires = ("book_equity", "shares")
    point_in_time_provider = True

    def compute(self, data: FactorData) -> pd.DataFrame:
        f = _require_fundamentals(data, self.requires)
        # LOOK-AHEAD GUARD: book equity & shares are filing-dated (<= t); close is day-t.
        market_cap = (f["shares"] * data.close).where(lambda x: x > 0)
        return f["book_equity"] / market_cap


@register
class EarningsGrowth(Factor):
    """Earnings growth: year-over-year growth in quarterly net income.

    Source: the growth factor flagged as US-effective in Orient Securities report 22;
    report 5 found net-profit YoY growth had a backtest Sharpe of ~1.82 in its set.
    Rationale: accelerating earnings predict higher forward returns. Expected IC:
    POSITIVE. Growth compares a quarter to the same quarter a year earlier (4 filings
    back), both filed on or before t.
    """

    name = "earnings_growth"
    category = "fundamental"
    requires = ("net_income_yoy",)
    point_in_time_provider = True

    def compute(self, data: FactorData) -> pd.DataFrame:
        f = _require_fundamentals(data, self.requires)
        # LOOK-AHEAD GUARD: the YoY series is computed at filing frequency then forward-
        # filled from datekey, so value[t] uses only filings with datekey <= t.
        return f["net_income_yoy"]


@register
class OperatingProfitability(Factor):
    """Operating profitability: trailing-12m operating income / book equity.

    Source: Fama & French (2015), "A Five-Factor Asset Pricing Model", JFE (the RMW
    profitability factor). Rationale: operationally profitable firms earn higher returns.
    Expected IC: POSITIVE.
    """

    name = "operating_profitability"
    category = "fundamental"
    requires = ("operating_income_ttm", "book_equity")
    point_in_time_provider = True

    def compute(self, data: FactorData) -> pd.DataFrame:
        f = _require_fundamentals(data, self.requires)
        # LOOK-AHEAD GUARD: both panels are filing-dated (<= t); ratio is element-wise.
        equity = f["book_equity"].where(f["book_equity"] > 0)
        return f["operating_income_ttm"] / equity


@register
class GrossMargin(Factor):
    """Gross margin: trailing-12m gross profit / revenue.

    Source: Novy-Marx (2013) profitability work; gross margin as a quality proxy.
    Rationale: higher-margin firms are higher quality and earn higher returns. Expected
    IC: POSITIVE.
    """

    name = "gross_margin"
    category = "fundamental"
    requires = ("gross_profit_ttm", "revenue_ttm")
    point_in_time_provider = True

    def compute(self, data: FactorData) -> pd.DataFrame:
        f = _require_fundamentals(data, self.requires)
        revenue = f["revenue_ttm"].where(f["revenue_ttm"] > 0)
        return f["gross_profit_ttm"] / revenue


@register
class FCFYield(Factor):
    """Free-cash-flow yield: trailing-12m free cash flow / market cap.

    Source: the cash-flow value literature (e.g. Lakonishok, Shleifer & Vishny 1994 use
    cash-flow-to-price). Rationale: cheap on hard-to-fake cash flow outperforms. Expected
    IC: POSITIVE.
    """

    name = "fcf_yield"
    category = "fundamental"
    requires = ("free_cash_flow_ttm", "shares")
    point_in_time_provider = True

    def compute(self, data: FactorData) -> pd.DataFrame:
        f = _require_fundamentals(data, self.requires)
        return f["free_cash_flow_ttm"] / _market_cap(f, data.close)


@register
class SalesGrowth(Factor):
    """Sales growth: year-over-year growth in quarterly revenue.

    Source: the growth family flagged as US-effective in Orient Securities report 22.
    NOTE the tension: Lakonishok-Shleifer-Vishny (1994) found high past sales growth
    ("glamour") predicts LOWER returns. We encode growth naturally and label the prior
    POSITIVE (per the report); a significant NEGATIVE IC would be the glamour effect and
    is surfaced by the sign check.
    """

    name = "sales_growth"
    category = "fundamental"
    requires = ("revenue_yoy",)
    point_in_time_provider = True

    def compute(self, data: FactorData) -> pd.DataFrame:
        f = _require_fundamentals(data, self.requires)
        return f["revenue_yoy"]


@register
class Accruals(Factor):
    """Balance-sheet accruals: (TTM net income - TTM operating cash flow) / total assets.

    Source: Sloan (1996), "Do Stock Prices Fully Reflect Information in Accruals and Cash
    Flows About Future Earnings?", The Accounting Review. Rationale: earnings propped up
    by accruals (not cash) mean-revert, so HIGH accruals predict LOWER returns. Encoded
    naturally; expected IC: NEGATIVE.
    """

    name = "accruals"
    category = "fundamental"
    requires = ("net_income_ttm", "op_cash_flow_ttm", "total_assets")
    point_in_time_provider = True

    def compute(self, data: FactorData) -> pd.DataFrame:
        f = _require_fundamentals(data, self.requires)
        # LOOK-AHEAD GUARD: all three panels are filing-dated (<= t); element-wise.
        assets = f["total_assets"].where(f["total_assets"] > 0)
        return (f["net_income_ttm"] - f["op_cash_flow_ttm"]) / assets


@register
class AssetGrowth(Factor):
    """Asset growth: year-over-year growth in total assets.

    Source: Cooper, Gulen & Schill (2008), "Asset Growth and the Cross-Section of Stock
    Returns", JF. Rationale: aggressively expanding firms subsequently underperform, so
    HIGH asset growth predicts LOWER returns. Encoded naturally; expected IC: NEGATIVE.
    """

    name = "asset_growth"
    category = "fundamental"
    requires = ("total_assets_yoy",)
    point_in_time_provider = True

    def compute(self, data: FactorData) -> pd.DataFrame:
        f = _require_fundamentals(data, self.requires)
        return f["total_assets_yoy"]


@register
class DebtToEquity(Factor):
    """Leverage: total debt / book equity.

    Source: the leverage/distress literature (e.g. Penman, Richardson & Tuna 2007 on the
    financial-leverage component of book-to-price). Rationale: higher leverage carries
    higher distress risk and has tended to predict LOWER returns in US samples once value
    is controlled. Encoded naturally; expected IC: NEGATIVE (ambiguous — flagged as such).
    """

    name = "debt_to_equity"
    category = "fundamental"
    requires = ("total_debt", "book_equity")
    point_in_time_provider = True

    def compute(self, data: FactorData) -> pd.DataFrame:
        f = _require_fundamentals(data, self.requires)
        equity = f["book_equity"].where(f["book_equity"] > 0)
        return f["total_debt"] / equity


@register
class QualityComposite(Factor):
    """Composite quality: cross-sectional z-score blend of the quality signals.

    Combines (per date, z-scored) gross profitability, operating profitability, gross
    margin, LOW accruals, and LOW asset growth into one score, in the spirit of a
    multi-signal quality factor (cf. Asness, Frazzini & Pedersen 2019, "Quality Minus
    Junk", RoF). Rationale: averaging correlated, individually-noisy quality signals
    raises the signal-to-noise ratio. Expected IC: POSITIVE.
    """

    name = "quality_composite"
    category = "fundamental"
    requires = ("gross_profit_ttm", "total_assets", "operating_income_ttm", "book_equity",
                "revenue_ttm", "net_income_ttm", "op_cash_flow_ttm", "total_assets_yoy")
    point_in_time_provider = True

    def compute(self, data: FactorData) -> pd.DataFrame:
        f = _require_fundamentals(data, self.requires)
        assets = f["total_assets"].where(f["total_assets"] > 0)
        equity = f["book_equity"].where(f["book_equity"] > 0)
        revenue = f["revenue_ttm"].where(f["revenue_ttm"] > 0)
        profitability = f["gross_profit_ttm"] / assets
        op_profitability = f["operating_income_ttm"] / equity
        margin = f["gross_profit_ttm"] / revenue
        accruals = (f["net_income_ttm"] - f["op_cash_flow_ttm"]) / assets
        asset_growth = f["total_assets_yoy"]
        # Higher = better: add the "good" signals, SUBTRACT accruals and asset growth.
        # LOOK-AHEAD GUARD: each z-score is cross-sectional (per date), inputs filing-dated.
        score = (_zscore_rows(profitability) + _zscore_rows(op_profitability)
                 + _zscore_rows(margin) - _zscore_rows(accruals) - _zscore_rows(asset_growth))
        return score / 5.0
