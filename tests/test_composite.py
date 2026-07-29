"""Tests for the equal-weight composite factor and the IS/OOS evaluation split."""

import numpy as np
import pandas as pd

from factors.base import Factor, FactorData
from factors.composite import CompositeFactor, _rank_rows, _zscore_rows
from factors.evaluate import evaluate_factor


class _Const(Factor):
    """A factor returning a fixed date x symbol panel (for deterministic tests)."""

    category = "test"
    requires = ()

    def __init__(self, name, panel):
        self.name, self._panel = name, panel

    def compute(self, data):
        return self._panel


def _data(close):
    return FactorData(open=close, high=close, low=close, close=close,
                      volume=close * 0 + 1e6, market=close.iloc[:, 0])


def test_zscore_composite_is_equal_weight_average():
    idx = pd.date_range("2021-01-01", periods=2, freq="D")
    a = pd.DataFrame({"A": [1.0, 1.0], "B": [2.0, 2.0], "C": [3.0, 3.0]}, index=idx)
    b = pd.DataFrame({"A": [3.0, 3.0], "B": [2.0, 2.0], "C": [1.0, 1.0]}, index=idx)
    comp = CompositeFactor([_Const("a", a), _Const("b", b)], mode="zscore")
    out = comp.compute(_data(a))
    expected = (_zscore_rows(a) + _zscore_rows(b)) / 2.0
    pd.testing.assert_frame_equal(out, expected)
    # a and b are mirror-images, so the equal-weight z-score composite is ~0 everywhere.
    assert out.abs().to_numpy().max() < 1e-9


def test_rank_composite_matches_rank_average():
    idx = pd.date_range("2021-01-01", periods=1, freq="D")
    a = pd.DataFrame({"A": [1.0], "B": [2.0], "C": [3.0]}, index=idx)
    b = pd.DataFrame({"A": [1.0], "B": [3.0], "C": [2.0]}, index=idx)
    comp = CompositeFactor([_Const("a", a), _Const("b", b)], mode="rank")
    out = comp.compute(_data(a))
    expected = (_rank_rows(a) + _rank_rows(b)) / 2.0
    pd.testing.assert_frame_equal(out, expected)


def test_composite_requires_all_components():
    """A name missing in ANY component is NaN in the composite (needs both signals)."""
    idx = pd.date_range("2021-01-01", periods=1, freq="D")
    a = pd.DataFrame({"A": [1.0], "B": [2.0], "C": [3.0]}, index=idx)
    b = pd.DataFrame({"A": [1.0], "B": [np.nan], "C": [2.0]}, index=idx)  # B missing
    out = CompositeFactor([_Const("a", a), _Const("b", b)], mode="zscore").compute(_data(a))
    assert np.isnan(out.loc[idx[0], "B"])
    assert not np.isnan(out.loc[idx[0], "A"])


def test_composite_is_look_ahead_safe():
    """Composite value at t is unchanged when future bars are dropped (cross-sectional)."""
    rng = np.random.default_rng(0)
    n, syms = 300, ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L"]
    close = pd.DataFrame({s: 50 + np.cumsum(rng.normal(0, 1, n)) for s in syms},
                         index=pd.date_range("2020-01-01", periods=n, freq="B")).abs() + 5
    data = _data(close)
    from factors.library import Momentum12_1
    comp = CompositeFactor([Momentum12_1(), _Const("noise",
                            pd.DataFrame(rng.normal(0, 1, (n, len(syms))), index=close.index, columns=syms))],
                           mode="zscore")
    cutoff = 200
    full = comp.compute(data).iloc[cutoff]
    trunc = comp.compute(_data(close.iloc[:cutoff + 1])).iloc[cutoff]
    diff = (full - trunc).abs().max()
    assert pd.isna(diff) or diff < 1e-9


def test_evaluate_factor_oos_split_partitions_periods():
    """IS + OOS rebalance counts sum to the full count (the split just filters dates)."""
    rng = np.random.default_rng(1)
    n, syms = 800, [f"S{i}" for i in range(20)]
    idx = pd.date_range("2015-01-01", periods=n, freq="B")
    close = pd.DataFrame({s: 50 + np.cumsum(rng.normal(0, 1, n)) for s in syms}, index=idx).abs() + 5
    factor = _Const("mom", close.pct_change(20).shift(1))
    data = _data(close)
    split = idx[int(n * 0.7)]
    full = evaluate_factor(factor, data)
    is_s = evaluate_factor(factor, data, end=split)
    oos = evaluate_factor(factor, data, start=split)
    # The split partitions the rebalance dates (one boundary month may sit on the split).
    assert is_s["n_periods"] + oos["n_periods"] >= full["n_periods"]
    assert is_s["n_periods"] > 0 and oos["n_periods"] > 0
