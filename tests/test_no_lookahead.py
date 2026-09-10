"""Automated, registry-driven proof that no factor uses future data.

Every factor's docstring carries a hand-written "LOOK-AHEAD GUARD" comment claiming its
value on date ``t`` uses only data <= ``t``. That's an honor system: nothing stops a future
edit from silently breaking it. This test replaces the honor system with a mechanical
proof, generic over the registry — a factor registered later is checked automatically,
with zero new test code.

THE PROOF: for a random synthetic panel, compute every registered factor's value at a
cutoff date on the FULL panel, then again on the panel truncated to ``<= cutoff`` (every
future row deleted). If the factor is genuinely look-ahead safe, deleting the future cannot
change a value that never depended on it — the two must be identical. If they differ, the
factor peeked.

Synthetic data is built from a hypothesis-drawn seed + symbol count (rather than growing
DataFrames directly via hypothesis strategies) — a standard pattern for pandas-heavy
properties: hypothesis explores the small, meaningful parameter space and shrinks to the
smallest reproducing case, while a seeded numpy RNG does the actual data generation.
"""

import numpy as np
import pandas as pd
from hypothesis import given, settings
from hypothesis import strategies as st

import factors  # noqa: F401  (registers the price/volume + fundamental factors)
import factors.altdata  # noqa: F401  (registers the insider/institutional factors)
from factors.base import FactorData, all_factors

N_DAYS = 320          # comfortably more than the longest lookback (rolling(250), shift(252))
CUTOFF_INDEX = 300     # >= 250 trading days of history before the cutoff, for every factor
TOLERANCE = 1e-9


def _fundamental_fields() -> set[str]:
    """Union of every field any currently-registered PIT factor declares via ``requires``.

    Deriving this from the registry (rather than hardcoding a field list) is what makes the
    harness generic: a new point-in-time factor's fields are picked up automatically.
    """
    fields: set[str] = set()
    for cls in all_factors().values():
        if getattr(cls, "point_in_time_provider", False):
            fields.update(cls.requires)
    return fields


def _make_factor_data(seed: int, n_symbols: int) -> FactorData:
    """A random-walk price panel + random fundamental panels, fully synthetic."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2015-01-01", periods=N_DAYS)
    symbols = [f"SYM{i}" for i in range(n_symbols)]
    shape = (N_DAYS, n_symbols)

    close = pd.DataFrame(
        100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.01, size=shape), axis=0)),
        index=dates, columns=symbols)
    high = close * (1.0 + rng.uniform(0.0, 0.02, size=shape))
    low = close * (1.0 - rng.uniform(0.0, 0.02, size=shape))
    open_ = close.shift(1).bfill()
    volume = pd.DataFrame(rng.uniform(1e5, 1e6, size=shape), index=dates, columns=symbols)
    market = close.mean(axis=1).rename("market")

    fundamentals = {
        field: pd.DataFrame(rng.uniform(-50.0, 150.0, size=shape), index=dates, columns=symbols)
        for field in _fundamental_fields()
    }

    return FactorData(open=open_, high=high, low=low, close=close, volume=volume,
                      market=market, fundamentals=fundamentals)


def _truncate(data: FactorData, cutoff: pd.Timestamp) -> FactorData:
    """A copy of ``data`` with every panel sliced to dates <= ``cutoff`` (future deleted)."""
    def _slice(panel):
        return None if panel is None else panel.loc[:cutoff]

    return FactorData(
        open=_slice(data.open), high=_slice(data.high), low=_slice(data.low),
        close=_slice(data.close), volume=_slice(data.volume), market=_slice(data.market),
        fundamentals=({k: _slice(v) for k, v in data.fundamentals.items()}
                     if data.fundamentals is not None else None))


def _assert_unchanged_by_truncation(name: str, full_row: pd.Series, trunc_row: pd.Series,
                                    cutoff: pd.Timestamp) -> None:
    """Fail loudly, naming the factor and the symbols, if truncation changed anything."""
    both_nan = full_row.isna() & trunc_row.isna()
    close_enough = (full_row - trunc_row).abs() <= TOLERANCE
    bad = ~both_nan & ~close_enough
    if bad.any():
        leaked = full_row.index[bad].tolist()
        raise AssertionError(
            f"LOOK-AHEAD LEAK in factor '{name}' at {cutoff.date()}: value changed for "
            f"{leaked} when future data was deleted "
            f"(full={full_row[bad].to_dict()}, truncated={trunc_row[bad].to_dict()}).")


@settings(max_examples=15, deadline=None)
@given(seed=st.integers(min_value=0, max_value=2**31 - 1), n_symbols=st.integers(min_value=3, max_value=6))
def test_no_factor_uses_future_data(seed: int, n_symbols: int) -> None:
    """Every registered factor's value at a cutoff is identical whether or not the future exists."""
    data = _make_factor_data(seed, n_symbols)
    cutoff = data.close.index[CUTOFF_INDEX]
    truncated = _truncate(data, cutoff)

    factors_checked = all_factors()
    assert factors_checked, "the factor registry is empty — nothing was actually checked"

    for name, cls in factors_checked.items():
        factor = cls()
        full_row = factor.compute(data).loc[cutoff]
        trunc_row = factor.compute(truncated).loc[cutoff]
        _assert_unchanged_by_truncation(name, full_row, trunc_row, cutoff)


def test_registry_covers_the_expected_factor_count() -> None:
    """A floor on registry size, so a broken import silently shrinking it fails loudly."""
    assert len(all_factors()) >= 24, (
        "fewer factors registered than expected — check that `import factors` and "
        "`import factors.altdata` are both registering their classes.")
