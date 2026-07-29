"""Tests for the point-in-time fundamental factor family and its plumbing.

Covers: provider stub-mode guards, filing-date indexing, TTM/YoY math, the
point-in-time forward-fill (no value visible before its filing date), fundamental
factor arithmetic, look-ahead safety, and the "needs PIT data" guard. All run without
a Sharadar key (synthetic filings).
"""

import numpy as np
import pandas as pd
import pytest

import factors  # noqa: F401  (registers factors, incl. fundamentals)
from data.sharadar_provider import (
    API_KEY_ENV,
    SharadarCacheMiss,
    SharadarProvider,
    SharadarUnavailable,
    _as_of_daily,
    _ttm,
    _yoy,
    as_reported_frame,
    build_fundamental_panels,
)
from factors.base import FactorData, all_factors


# ---- provider stub / cache-only guards ----------------------------------------

def test_provider_stub_mode_raises_on_real_calls(monkeypatch, tmp_path):
    """With no key and an empty cache, every real call raises descriptively."""
    monkeypatch.delenv(API_KEY_ENV, raising=False)  # force stub regardless of ambient .env
    provider = SharadarProvider(api_key=None, cache_dir=tmp_path)
    assert provider.stub is True
    with pytest.raises(SharadarUnavailable, match="NASDAQ_DATA_LINK_API_KEY"):
        provider.get_price_bars(["AAPL"])
    with pytest.raises(SharadarUnavailable):
        provider.get_fundamentals(["AAPL"])
    with pytest.raises(SharadarUnavailable):
        provider.get_universe()


def test_cache_only_mode_never_hits_api(tmp_path):
    """Cache-only mode never fetches: missing per-symbol data is skipped (no API), and a
    required archive (the universe master) raises rather than silently calling out."""
    provider = SharadarProvider(api_key="dummy-key", cache_dir=tmp_path, cache_only=True)
    # A name absent from the archive is skipped, not fetched -> empty result, zero API calls.
    assert provider.get_price_bars(["AAPL"]) == {}
    assert provider.get_fundamentals(["AAPL"]) == {}
    # The universe master is a required archive; with none present, refuse (no silent API).
    with pytest.raises(SharadarCacheMiss):
        provider.get_universe()


def test_cache_only_serves_archived_parquet_without_api(tmp_path):
    """A pre-archived per-symbol parquet is served offline with no API call."""
    sep = tmp_path / "sep"
    sep.mkdir()
    frame = pd.DataFrame({"Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0],
                          "Volume": [100.0]}, index=pd.DatetimeIndex(["2020-01-02"], name="Date"))
    frame.to_parquet(sep / "AAPL.parquet")
    provider = SharadarProvider(api_key=None, cache_dir=tmp_path, cache_only=True)
    out = provider.get_price_bars(["AAPL"])
    assert "AAPL" in out and len(out["AAPL"]) == 1


# ---- filing-date indexing -----------------------------------------------------

def test_as_reported_frame_indexes_by_filing_date():
    raw = pd.DataFrame({
        "datekey": ["2020-05-10", "2020-02-15", "2020-02-15"],  # unsorted + a dup datekey
        "calendardate": ["2020-03-31", "2019-12-31", "2019-12-31"],
        "gp": [30.0, 10.0, 99.0], "assets": [300.0, 100.0, 100.0],
    })
    frame = as_reported_frame(raw, ["gp", "assets"])
    assert frame.index.name == "datekey"
    assert list(frame.index) == [pd.Timestamp("2020-02-15"), pd.Timestamp("2020-05-10")]
    assert "gross_profit" in frame.columns and "total_assets" in frame.columns  # renamed
    assert frame.loc[pd.Timestamp("2020-02-15"), "gross_profit"] == 99.0  # dup -> keep last


def test_as_reported_frame_requires_datekey():
    with pytest.raises(ValueError, match="datekey"):
        as_reported_frame(pd.DataFrame({"gp": [1.0]}), ["gp"])


# ---- TTM / YoY math -----------------------------------------------------------

def test_ttm_is_trailing_four_quarter_sum():
    s = pd.Series([10.0, 20.0, 30.0, 40.0, 50.0])
    ttm = _ttm(s)
    assert np.isnan(ttm.iloc[2])           # < 4 quarters
    assert ttm.iloc[3] == 100.0            # 10+20+30+40
    assert ttm.iloc[4] == 140.0            # 20+30+40+50


def test_yoy_compares_to_four_quarters_prior():
    s = pd.Series([10.0, 20.0, 30.0, 40.0, 50.0, 60.0])
    yoy = _yoy(s)
    assert yoy.iloc[4] == pytest.approx(4.0)   # (50-10)/10
    assert yoy.iloc[5] == pytest.approx(2.0)   # (60-20)/20


# ---- point-in-time forward-fill ----------------------------------------------

def test_as_of_daily_never_visible_before_filing():
    filings = pd.Series([100.0, 110.0],
                        index=pd.to_datetime(["2020-02-15", "2020-05-10"]))
    master = pd.date_range("2020-01-01", "2020-06-30", freq="D")
    daily = _as_of_daily(filings, master)
    assert pd.isna(daily.loc["2020-02-14"])         # before first filing -> unknown
    assert daily.loc["2020-02-15"] == 100.0         # known on the filing date
    assert daily.loc["2020-05-09"] == 100.0         # still the prior filing
    assert daily.loc["2020-05-10"] == 110.0         # updates on the next filing date


# ---- fundamental factor arithmetic -------------------------------------------

def _factor_data_with(fundamentals: dict, close: pd.DataFrame) -> FactorData:
    z = close * 0
    return FactorData(open=close, high=close, low=close, close=close, volume=z + 1e6,
                      market=close.iloc[:, 0], fundamentals=fundamentals)


def test_fundamental_factor_values():
    idx = pd.date_range("2021-01-01", periods=3, freq="D")
    close = pd.DataFrame({"A": [10.0, 10.0, 10.0], "B": [20.0, 20.0, 20.0]}, index=idx)
    f = {
        "gross_profit_ttm": pd.DataFrame({"A": 40.0, "B": 60.0}, index=idx),
        "total_assets": pd.DataFrame({"A": 100.0, "B": 300.0}, index=idx),
        "net_income_ttm": pd.DataFrame({"A": 10.0, "B": 30.0}, index=idx),
        "book_equity": pd.DataFrame({"A": 50.0, "B": 200.0}, index=idx),
        "shares": pd.DataFrame({"A": 5.0, "B": 10.0}, index=idx),
        "net_income_yoy": pd.DataFrame({"A": 0.25, "B": -0.10}, index=idx),
    }
    data = _factor_data_with(f, close)
    reg = all_factors()
    prof = reg["profitability"]().compute(data)
    assert prof.loc[idx[0], "A"] == pytest.approx(0.40)   # 40/100
    assert prof.loc[idx[0], "B"] == pytest.approx(0.20)   # 60/300
    ey = reg["earnings_yield"]().compute(data)
    assert ey.loc[idx[0], "A"] == pytest.approx(10.0 / (5.0 * 10.0))   # ni/(shares*price)
    bp = reg["book_to_price"]().compute(data)
    assert bp.loc[idx[0], "B"] == pytest.approx(200.0 / (10.0 * 20.0))
    eg = reg["earnings_growth"]().compute(data)
    assert eg.loc[idx[0], "A"] == pytest.approx(0.25)


def test_fundamental_factor_requires_pit_data():
    idx = pd.date_range("2021-01-01", periods=2, freq="D")
    close = pd.DataFrame({"A": [10.0, 11.0]}, index=idx)
    data = FactorData(open=close, high=close, low=close, close=close, volume=close,
                      market=close["A"], fundamentals=None)
    with pytest.raises(NotImplementedError, match="point-in-time"):
        all_factors()["profitability"]().compute(data)


# ---- look-ahead safety (end to end through the panel builder) -----------------

ALL_FUNDAMENTAL = [
    "profitability", "operating_profitability", "gross_margin", "earnings_yield",
    "fcf_yield", "book_to_price", "sales_growth", "earnings_growth", "accruals",
    "asset_growth", "debt_to_equity", "quality_composite",
]


def _synthetic_filings(seed: int = 0, n_symbols: int = 12) -> dict[str, pd.DataFrame]:
    """Filing-dated frames carrying every raw field the full library needs."""
    rng = np.random.default_rng(seed)
    datekeys = pd.to_datetime(["2019-02-20", "2019-05-15", "2019-08-14", "2019-11-13",
                               "2020-02-19", "2020-05-13", "2020-08-12", "2020-11-10"])
    out = {}
    for i in range(n_symbols):
        out[f"S{i:02d}"] = pd.DataFrame({
            "gross_profit": rng.uniform(5, 50, len(datekeys)),
            "total_assets": rng.uniform(100, 500, len(datekeys)),
            "net_income": rng.uniform(-10, 40, len(datekeys)),
            "book_equity": rng.uniform(20, 200, len(datekeys)),
            "shares": rng.uniform(5, 50, len(datekeys)),
            "operating_income": rng.uniform(-5, 45, len(datekeys)),
            "revenue": rng.uniform(80, 600, len(datekeys)),
            "free_cash_flow": rng.uniform(-20, 50, len(datekeys)),
            "op_cash_flow": rng.uniform(-10, 60, len(datekeys)),
            "total_debt": rng.uniform(0, 300, len(datekeys)),
        }, index=pd.DatetimeIndex(datekeys, name="datekey"))
    return out


def test_accruals_arithmetic_and_sign():
    idx = pd.date_range("2021-01-01", periods=1, freq="D")
    close = pd.DataFrame({"A": [10.0]}, index=idx)
    f = {
        "net_income_ttm": pd.DataFrame({"A": 40.0}, index=idx),
        "op_cash_flow_ttm": pd.DataFrame({"A": 10.0}, index=idx),  # earnings >> cash => high accruals
        "total_assets": pd.DataFrame({"A": 100.0}, index=idx),
    }
    accr = all_factors()["accruals"]().compute(_factor_data_with(f, close))
    assert accr.loc[idx[0], "A"] == pytest.approx((40.0 - 10.0) / 100.0)  # 0.30


def test_fundamental_factors_are_look_ahead_safe():
    """Every fundamental factor's value at t is unchanged when future bars are dropped."""
    master = pd.date_range("2019-01-01", "2020-12-31", freq="B")
    filings = _synthetic_filings()
    panels = build_fundamental_panels(filings, master)
    close = pd.DataFrame({s: 50 + np.arange(len(master)) * 0.1 for s in filings}, index=master)
    data = _factor_data_with(panels, close)

    cutoff = master.get_loc(pd.Timestamp("2020-06-15"))
    trunc_panels = build_fundamental_panels(filings, master[: cutoff + 1])
    trunc_data = _factor_data_with(trunc_panels, close.iloc[: cutoff + 1])

    for name in ALL_FUNDAMENTAL:
        factor = all_factors()[name]()
        full = factor.compute(data).iloc[cutoff]
        trunc = factor.compute(trunc_data).iloc[cutoff]
        diff = (full - trunc).abs().max()
        assert pd.isna(diff) or diff < 1e-9, f"{name} leaks future data"
