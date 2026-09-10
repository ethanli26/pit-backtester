"""Correctness tests for the CPCV combinatorics, the Deflated Sharpe Ratio, and PBO.

These are pure-math tests — no Sharadar cache, no network. The combinatorics are checked
against `math.comb`; the purge/embargo logic is checked by construction (no excluded
position leaks back into train); DSR and PBO are checked against synthetic series with a
known answer (an obviously-real edge vs. pure noise).
"""

import math

import numpy as np
import pandas as pd
import pytest

from research.cpcv import (
    cpcv_splits,
    deflated_sharpe_ratio,
    make_groups,
    probability_of_backtest_overfitting,
)


# --- make_groups / cpcv_splits: pure combinatorics --------------------------------------

def test_make_groups_partitions_every_position_exactly_once():
    groups = make_groups(100, 7)
    all_positions = np.concatenate(groups)
    assert sorted(all_positions.tolist()) == list(range(100))
    assert len(groups) == 7


@pytest.mark.parametrize("n_dates,n_groups,n_test_groups", [(120, 6, 2), (90, 5, 1), (144, 9, 3)])
def test_cpcv_splits_count_matches_binomial_coefficient(n_dates, n_groups, n_test_groups):
    splits = cpcv_splits(n_dates, n_groups, n_test_groups, embargo_periods=1)
    assert len(splits) == math.comb(n_groups, n_test_groups)


def test_cpcv_splits_train_never_touches_a_test_or_purged_or_embargoed_position():
    n_dates, n_groups, n_test_groups, embargo = 120, 6, 2, 2
    groups = make_groups(n_dates, n_groups)
    for split in cpcv_splits(n_dates, n_groups, n_test_groups, embargo_periods=embargo):
        train_set = set(split.train_positions.tolist())
        for block in split.test_blocks:
            # No test position is ever in train.
            assert train_set.isdisjoint(block.tolist())
            start, end = int(block[0]), int(block[-1])
            # The purge position (immediately before the block) is excluded from train.
            if start - 1 >= 0:
                assert start - 1 not in train_set
            # Every embargo position (immediately after the block) is excluded from train.
            for e in range(1, embargo + 1):
                if end + e < n_dates:
                    assert end + e not in train_set


def test_cpcv_splits_rejects_degenerate_group_counts():
    with pytest.raises(ValueError):
        make_groups(10, 1)
    with pytest.raises(ValueError):
        make_groups(10, 11)


# --- Deflated Sharpe Ratio: a real-looking edge vs. pure noise --------------------------

def _monthly_returns(mean: float, std: float, n: int, seed: int) -> pd.Series:
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2010-01-31", periods=n, freq="ME")
    return pd.Series(rng.normal(mean, std, size=n), index=dates)


def test_deflated_sharpe_ratio_is_high_for_a_strong_consistent_edge():
    # A consistently positive-mean series against a handful of near-zero "trials" should
    # clear the trial-adjusted benchmark with a high probability.
    returns = _monthly_returns(mean=0.02, std=0.03, n=120, seed=1)
    trial_sharpes = [0.1, -0.2, 0.05, 0.15, -0.1]   # weak/noisy CPCV paths, annualized
    result = deflated_sharpe_ratio(returns, trial_sharpes)
    assert result["dsr"] > 0.95
    assert result["sharpe_annualized"] > 1.0


def test_deflated_sharpe_ratio_penalizes_more_dispersed_trials():
    # The correction (SR0) should grow with how DISPERSED the trial Sharpes were, not
    # their absolute level -- wide-ranging trials imply a bigger search space, and a
    # realized Sharpe similar in size to that dispersion is less impressive. Same
    # returns series, scored against low- vs high-variance trial sets.
    returns = _monthly_returns(mean=0.01, std=0.03, n=80, seed=2)
    low_dispersion = [0.85, 0.9, 0.8, 0.88, 0.82]     # trials all similar
    high_dispersion = [2.0, -1.8, 1.9, -2.1, 1.7]     # same-ish mean, much wider spread
    low = deflated_sharpe_ratio(returns, low_dispersion)
    high = deflated_sharpe_ratio(returns, high_dispersion)
    assert high["sr0_annualized"] > low["sr0_annualized"]
    assert high["dsr"] <= low["dsr"]


def test_deflated_sharpe_ratio_ignores_trials_when_too_few_to_estimate_dispersion():
    returns = _monthly_returns(mean=0.01, std=0.03, n=80, seed=2)
    result = deflated_sharpe_ratio(returns, [0.5])   # a single trial has no variance
    assert result["n_trials"] == 1
    assert result["sr0_annualized"] == 0.0


def test_deflated_sharpe_ratio_rejects_degenerate_input():
    with pytest.raises(ValueError):
        deflated_sharpe_ratio(pd.Series([0.01, 0.02]), [0.1, 0.2])   # too few observations
    with pytest.raises(ValueError):
        deflated_sharpe_ratio(pd.Series([0.0] * 10), [0.1, 0.2])     # zero variance


# --- Probability of Backtest Overfitting: a real winner vs. five noise variants ---------

def test_pbo_is_low_when_one_variant_is_genuinely_and_consistently_better():
    rng = np.random.default_rng(3)
    dates = pd.date_range("2010-01-31", periods=96, freq="ME")
    variants = {
        "winner": pd.Series(rng.normal(0.02, 0.03, size=96), index=dates),
        "loser_a": pd.Series(rng.normal(0.0, 0.03, size=96), index=dates),
        "loser_b": pd.Series(rng.normal(0.0, 0.03, size=96), index=dates),
        "loser_c": pd.Series(rng.normal(0.0, 0.03, size=96), index=dates),
    }
    result = probability_of_backtest_overfitting(variants, n_groups=8)
    assert result["pbo"] < 0.3
    assert result["n_splits"] == math.comb(8, 4)


def test_pbo_averages_to_a_coin_flip_when_all_variants_are_indistinguishable_noise():
    # PBO from a SINGLE finite sample is a noisy estimator (a known property of CSCV, not
    # a bug) -- an individual seed can land anywhere. Averaging several seeds is what
    # should land near the true null of ~50%: for pure noise, "the in-sample winner" is
    # exactly as likely to end up above or below the OOS median.
    pbos = []
    for seed in range(10):
        rng = np.random.default_rng(seed)
        dates = pd.date_range("2010-01-31", periods=96, freq="ME")
        variants = {name: pd.Series(rng.normal(0.0, 0.03, size=96), index=dates)
                   for name in ("a", "b", "c", "d", "e")}
        pbos.append(probability_of_backtest_overfitting(variants, n_groups=8)["pbo"])
    assert 0.3 < (sum(pbos) / len(pbos)) < 0.7


def test_pbo_requires_at_least_two_variants():
    dates = pd.date_range("2010-01-31", periods=48, freq="ME")
    with pytest.raises(ValueError):
        probability_of_backtest_overfitting({"only_one": pd.Series(0.01, index=dates)})
