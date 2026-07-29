"""Tests for the vol-managed momentum strategy and walk-forward windowing."""

import numpy as np
import pandas as pd

import config
from backtest.walkforward import make_windows
from factors.base import FactorData
from strategies.registry import get_portfolio, portfolio_strategies
from strategies.vol_managed_momentum import VolManagedMomentum, vol_target_weights


def _data(n=900, n_symbols=15, seed=0):
    # ~3.5y of business days so there are >12 valid momentum months (the vol window needs them).
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2016-01-01", periods=n, freq="B")
    close = pd.DataFrame({f"S{i:02d}": 50 + np.cumsum(rng.normal(0.05, 1, n)) for i in range(n_symbols)},
                         index=idx).abs() + 10
    data = FactorData(open=close, high=close * 1.01, low=close * 0.99, close=close,
                      volume=close * 0 + 5e6, market=close.iloc[:, 0])
    eligible = pd.DataFrame(True, index=close.index, columns=close.columns)
    return data, eligible


def test_registered_as_portfolio_strategy():
    assert "vol_managed_momentum" in portfolio_strategies()
    assert get_portfolio("vol_managed_momentum") is VolManagedMomentum


def test_vol_target_weights_are_look_ahead_safe():
    """A weight for month t uses only returns realized BEFORE t (shift(1) guard)."""
    rng = np.random.default_rng(1)
    r = pd.Series(rng.normal(0.01, 0.05, 60), index=pd.date_range("2015-01-31", periods=60, freq="ME"))
    train_end = r.index[40]
    base = vol_target_weights(r, train_end, window=12, cap=2.0)
    tampered = r.copy()
    tampered.iloc[-1] = 9.0          # huge FUTURE return
    after = vol_target_weights(tampered, train_end, window=12, cap=2.0)
    common = base.dropna().index.intersection(after.dropna().index)
    common = common[common < r.index[-1]]    # all weights before the tampered month
    assert np.allclose(base.loc[common], after.loc[common])


def test_vol_target_weights_respect_cap():
    r = pd.Series([0.2, -0.2] * 12 + list(0.01 + np.random.default_rng(2).normal(0, 0.001, 24)),
                  index=pd.date_range("2010-01-31", periods=48, freq="ME"))
    weights = vol_target_weights(r, r.index[-1], window=12, cap=2.0).dropna()
    assert weights.max() <= 2.0 + 1e-9


def test_long_only_and_long_short_differ_and_run():
    data, eligible = _data()
    ls = VolManagedMomentum(long_only=False).portfolio_returns(data, eligible)
    lo = VolManagedMomentum(long_only=True).portfolio_returns(data, eligible)
    assert len(ls) > 0 and len(lo) > 0
    # The two variants are genuinely different return streams.
    common = ls.index.intersection(lo.index)
    assert not np.allclose(ls.loc[common], lo.loc[common])


def test_portfolio_returns_slice_matches_full():
    """With the SAME vol target, slicing to a window equals the full series on that window.

    (A different ``train_end`` deliberately changes the target, so we hold it fixed here.)
    """
    data, eligible = _data()
    strat = VolManagedMomentum(long_only=False)
    base = strat.portfolio_returns(data, eligible)
    split = base.index[len(base) // 2]
    full = strat.portfolio_returns(data, eligible, train_end=split)            # full series, fixed target
    sliced = strat.portfolio_returns(data, eligible, train_end=split, start=split)  # same target, windowed
    assert np.allclose(full.loc[sliced.index], sliced)


def test_paper_book_is_equal_weight_and_respects_caps():
    """The decile book is equal-weight, scaled by gross exposure, per-name capped."""
    data, eligible = _data(n_symbols=120)   # a real-sized decile (~12 names) so gross scaling shows
    strat = VolManagedMomentum()
    scores = strat.score(data)
    last = scores.index[-1]
    equity = 1_000_000.0
    book = strat.paper_book(equity, scores.loc[last], data.close.loc[last], eligible.loc[last],
                            gross_weight=1.0)
    assert book, "expected a non-empty top-decile book"
    values = [shares * data.close.loc[last, s] for s, shares in book.items()]
    for value in values:
        assert value <= equity * config.MAX_POSITION_PCT + 1e-6      # per-name cap respected
    # Lower gross exposure deploys strictly less capital (vol scaling has bite below the cap).
    smaller = strat.paper_book(equity, scores.loc[last], data.close.loc[last], eligible.loc[last],
                               gross_weight=0.5)
    assert sum(sh * data.close.loc[last, s] for s, sh in smaller.items()) < sum(values)


def test_make_windows_are_non_overlapping():
    idx = pd.date_range("1998-01-01", "2026-06-01", freq="ME")
    windows = make_windows(idx, window_years=3)
    assert len(windows) >= 8
    for (s1, e1), (s2, e2) in zip(windows, windows[1:]):
        assert e1 <= s2 + pd.Timedelta(days=1)   # consecutive, non-overlapping
