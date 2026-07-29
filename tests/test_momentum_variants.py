"""Tests for the vol-management overlay and crash diagnostics (offline, deterministic)."""

import numpy as np
import pandas as pd

from ml.momentum_variants import subperiod_return, vol_managed, worst_rolling_12m


def _monthly(values, start="2005-01-31"):
    idx = pd.date_range(start, periods=len(values), freq="ME")
    return pd.Series(values, index=idx, dtype=float)


def test_vol_managed_is_look_ahead_safe():
    """The scale for month t uses only returns realized BEFORE t (shift(1) guard).

    Mutating the LAST month's return must not change any earlier scaled value.
    """
    rng = np.random.default_rng(0)
    r = _monthly(rng.normal(0.01, 0.05, 60))
    split = r.index[40]
    base = vol_managed(r, split)
    tampered = r.copy()
    tampered.iloc[-1] = 5.0          # enormous future shock
    after = vol_managed(tampered, split)
    common = base.index.intersection(after.index)[:-1]   # all but the last month
    assert np.allclose(base.loc[common].to_numpy(), after.loc[common].to_numpy())


def test_vol_managed_delevers_after_high_vol():
    """A calm stretch then a volatile stretch -> later weights shrink (inverse-vol)."""
    calm = [0.01] * 24
    wild = list(np.tile([0.20, -0.20], 12))   # same mean ~0, much higher vol
    r = _monthly(calm + wild)
    split = r.index[-1]
    managed = vol_managed(r, split)
    # Average gross scaling in the wild stretch is below that in the calm stretch.
    calm_scale = (managed.iloc[:24] / r.reindex(managed.index).iloc[:24]).replace([np.inf, -np.inf], np.nan).dropna()
    wild_scale = (managed.iloc[-24:] / r.reindex(managed.index).iloc[-24:]).replace([np.inf, -np.inf], np.nan).dropna()
    assert wild_scale.mean() < calm_scale.mean()


def test_vol_managed_respects_leverage_cap():
    """When realized vol falls well below target, inverse-vol weight is clamped at cap."""
    rng = np.random.default_rng(2)
    wild = list(np.tile([0.20, -0.20], 12))            # high vol sets a non-trivial target
    calm = list(0.01 + rng.normal(0, 0.001, 24))       # then very low vol -> wants huge leverage
    r = _monthly(wild + calm)
    managed = vol_managed(r, r.index[-1], cap=2.0)
    scale = (managed / r.reindex(managed.index)).replace([np.inf, -np.inf], np.nan).dropna()
    assert scale.max() <= 2.0 + 1e-9


def test_worst_rolling_12m():
    r = _monthly([0.02] * 12 + [-0.10] * 12)   # a clear down-year follows an up-year
    worst = worst_rolling_12m(r)
    expected = (1.0 - 0.10) ** 12 - 1.0
    assert abs(worst - expected) < 1e-9


def test_subperiod_return_compounds_window():
    r = _monthly([0.05, -0.05, 0.10], start="2008-11-30")  # spans into 2009
    val = subperiod_return(r, "2008-01-01", "2009-12-31")
    expected = (1.05 * 0.95 * 1.10) - 1.0
    assert abs(val - expected) < 1e-9
    assert subperiod_return(r, "2030-01-01", "2030-12-31") is None  # empty window
