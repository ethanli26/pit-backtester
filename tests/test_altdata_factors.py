"""Tests for insider (SF2) and institutional (SF3) PIT panels + factors.

The critical guards: insider signals are keyed to the Form-4 FILING date (not the earlier
trade date), and 13F holdings are not visible until the 45-day filing deadline. Both are
verified on synthetic data (no Sharadar key, no big cache read).
"""

import numpy as np
import pandas as pd
import pytest

import factors.altdata  # noqa: F401  (registers the factors)
from data.sharadar_provider import (
    _to_master_day,
    build_insider_panels,
    build_institutional_panels,
)
from factors.altdata import ALTDATA_EXPECTED_SIGN, ALTDATA_FACTORS
from factors.base import all_factors


MASTER = pd.bdate_range("2021-01-01", periods=200)


def test_to_master_day_maps_to_next_trading_day():
    # 2021-01-16 is a Saturday -> next trading day is Monday 2021-01-18.
    out = _to_master_day(["2021-01-16"], MASTER)
    assert out[0] == pd.Timestamp("2021-01-18")
    # A date past the calendar end maps to NaT (never fabricated).
    assert pd.isna(_to_master_day(["2099-01-01"], MASTER)[0])


def _insider_tx(filing_dates, codes, shares, values, owners):
    return {"AAA": pd.DataFrame({
        "filingdate": pd.to_datetime(filing_dates), "transactioncode": codes,
        "transactionshares": shares, "transactionvalue": values, "ownername": owners})}


def test_insider_net_buy_uses_filing_date_not_before():
    """A purchase is invisible before its FILING date, then enters the trailing sum."""
    filing = MASTER[50]
    tx = _insider_tx([filing], ["P"], [1000], [50_000.0], ["Jane"])
    panels = build_insider_panels(tx, MASTER, window_days=60)
    net = panels["insider_net_buy_value"]["AAA"]
    assert np.isnan(net.loc[MASTER[49]])         # before the filing date -> unknown (NaN)
    assert net.loc[MASTER[50]] == 50_000.0       # visible on the filing date
    assert net.loc[MASTER[80]] == 50_000.0       # still inside the 60-day window


def test_insider_net_buy_is_signed_buys_minus_sells():
    filing = MASTER[40]
    tx = _insider_tx([filing, filing], ["P", "S"], [1000, -400], [50_000.0, 20_000.0], ["Jane", "Bob"])
    net = build_insider_panels(tx, MASTER, window_days=60)["insider_net_buy_value"]["AAA"]
    assert net.loc[MASTER[40]] == 30_000.0       # +50k buy, -20k sell


def test_insider_distinct_buyers_counts_unique_insiders():
    filing = MASTER[30]
    # Jane buys twice, Bob once -> 2 distinct buyers (cluster). Sales are ignored.
    tx = _insider_tx([filing, filing, filing], ["P", "P", "P"],
                     [100, 200, 300], [1.0, 2.0, 3.0], ["Jane", "Jane", "Bob"])
    buyers = build_insider_panels(tx, MASTER, window_days=60)["insider_distinct_buyers"]["AAA"]
    assert buyers.max() == 2.0                    # peak while both insiders are in the window
    assert buyers.loc[MASTER[-1]] == 0.0          # the buys age out of the trailing window


def test_institutional_45_day_lag_is_enforced():
    """A quarter's 13F holdings are invisible until calendardate + 45 days."""
    q1, q2 = pd.Timestamp("2021-03-31"), pd.Timestamp("2021-06-30")
    holdings = {"AAA": pd.DataFrame({"total_units": [100.0, 150.0], "breadth": [10, 14]},
                                    index=pd.DatetimeIndex([q1, q2]))}
    shares = pd.DataFrame({"AAA": 1000.0}, index=MASTER)
    panels = build_institutional_panels(holdings, MASTER, shares, lag_days=45)
    own = panels["inst_own_pct"]["AAA"]
    # Q1 ends 2021-03-31; not public until ~2021-05-15. So mid-April it must still be unknown.
    assert np.isnan(own.loc[pd.Timestamp("2021-04-15")])
    assert own.loc[pd.Timestamp("2021-05-17")] == 100.0 / 1000.0   # visible after the 45-day lag
    # QoQ ownership change appears only after Q2's availability date.
    own_chg = panels["inst_own_pct_chg"]["AAA"]
    assert own_chg.loc[MASTER[-1]] == pytest.approx(0.05)           # (150 - 100) / 1000
    breadth_chg = panels["inst_breadth_chg"]["AAA"]
    assert breadth_chg.loc[MASTER[-1]] == 4.0                       # 14 - 10 holders


def test_factors_registered_with_expected_signs():
    reg = all_factors()
    for name in ALTDATA_FACTORS:
        assert name in reg
        assert reg[name]().point_in_time_provider is True
    assert set(ALTDATA_EXPECTED_SIGN) == set(ALTDATA_FACTORS)


def test_factor_returns_nan_when_panels_absent():
    """A registered alt-data factor on data WITHOUT its panel yields NaN (never crashes)."""
    from factors.base import FactorData

    close = pd.DataFrame({"AAA": [10.0, 11.0]}, index=pd.bdate_range("2022-01-03", periods=2))
    data = FactorData(open=close, high=close, low=close, close=close, volume=close,
                      market=close["AAA"], fundamentals=None)
    out = all_factors()["insider_buying"]().compute(data)
    assert out.isna().all().all()
