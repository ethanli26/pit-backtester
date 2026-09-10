"""Combinatorial Purged Cross-Validation, the Deflated Sharpe Ratio, and PBO.

A single walk-forward split (``backtest/walkforward.py``) is a real improvement over one
in-sample backtest, but it's still one draw — your 30% holdout might just happen to be a
friendly stretch. This module answers the harder question a single split can't: **given
how many effectively-independent looks this strategy (or this search over variants) got,
what's the chance the result you're looking at is luck?**

Three pieces, all from the same combinatorial-partition machinery:

  * ``cpcv_splits`` — Combinatorial Purged Cross-Validation (Lopez de Prado, "Advances in
    Financial Machine Learning", ch. 12). Partition the rebalance timeline into N groups
    and evaluate EVERY way of picking k of them as the test set (C(N,k) splits), purged
    and embargoed at each boundary so a forward-return label can never straddle a
    train/test split. This produces a *distribution* of out-of-sample Sharpes for one
    strategy, not a single number.

  * ``deflated_sharpe_ratio`` — Bailey & Lopez de Prado (2014), "The Deflated Sharpe
    Ratio". Shrinks the observed Sharpe for (a) how many effectively-independent trials
    were searched — estimated here from the variance of the CPCV path Sharpes — and
    (b) the return distribution's skew/kurtosis (a Sharpe ratio assumes normal returns;
    real monthly returns rarely are). Reports the probability the TRUE Sharpe exceeds the
    trial-adjusted benchmark, not just zero.

  * ``probability_of_backtest_overfitting`` — Bailey, Borwein, Lopez de Prado & Zhu
    (2014), "The Probability of Backtest Overfitting" (the CSCV procedure). Applied to
    the five pre-specified momentum-crash-fix variants in
    ``research.momentum_variants.build_variant_returns`` — a genuine multi-candidate
    "trials matrix" that already exists in this codebase, not fabricated to make PBO
    computable. Answers: across many train/test splits, how often does the
    in-sample-best variant turn out to be BELOW MEDIAN out of sample? Near 50% means the
    "winner" is indistinguishable from noise; well below it means the winner generalizes.

None of this replaces the walk-forward chapter — it's the next layer of skepticism on top
of it. Read-only research. No orders.

    python -m research.cpcv
"""

import itertools
import logging
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.stats import kurtosis, norm, skew

from research.combine_train import _monthly_metrics

log = logging.getLogger("cpcv")

EULER_GAMMA = 0.5772156649015329  # the Euler-Mascheroni constant, used by the expected-max-SR formula


# --- pure combinatorics: group partitioning, splits, purge/embargo ---------------------
# No market data touches this section, which is why it's unit-tested directly (tests/test_cpcv.py)
# without needing the Sharadar cache.

def make_groups(n_dates: int, n_groups: int) -> list[np.ndarray]:
    """Positions ``0..n_dates-1`` split into ``n_groups`` contiguous, near-equal blocks."""
    if n_groups < 2 or n_groups > n_dates:
        raise ValueError(f"n_groups must be in [2, {n_dates}], got {n_groups}")
    return np.array_split(np.arange(n_dates), n_groups)


@dataclass(frozen=True)
class CPCVSplit:
    """One combinatorial train/test partition."""

    test_group_ids: tuple[int, ...]
    train_positions: np.ndarray
    test_blocks: list[np.ndarray]   # one contiguous position-array per test group


def cpcv_splits(n_dates: int, n_groups: int, n_test_groups: int,
                embargo_periods: int = 1) -> list[CPCVSplit]:
    """Every C(n_groups, n_test_groups) way of choosing test groups, purged and embargoed.

    PURGE: the one position immediately before a test group's start is dropped from
    train — its forward-return LABEL would span from before the test group into it.
    EMBARGO: ``embargo_periods`` positions immediately after a test group's end are also
    dropped from train — serial correlation could otherwise leak test-period information
    back into a training block that starts right after it.
    """
    groups = make_groups(n_dates, n_groups)
    splits = []
    for test_ids in itertools.combinations(range(n_groups), n_test_groups):
        exclude: set[int] = set()
        test_blocks = []
        for g in test_ids:
            block = groups[g]
            test_blocks.append(block)
            exclude.update(block.tolist())
            start, end = int(block[0]), int(block[-1])
            if start - 1 >= 0:
                exclude.add(start - 1)                        # purge
            for e in range(1, embargo_periods + 1):
                if end + e < n_dates:
                    exclude.add(end + e)                       # embargo
        train_positions = np.array([i for i in range(n_dates) if i not in exclude])
        splits.append(CPCVSplit(test_group_ids=test_ids, train_positions=train_positions,
                                test_blocks=test_blocks))
    return splits


# --- CPCV applied to one strategy: a distribution of OOS Sharpes -----------------------

def run_cpcv(strategy, data, eligible, rebal: pd.DatetimeIndex, *,
            n_groups: int = 8, n_test_groups: int = 2, embargo_periods: int = 1) -> list[dict]:
    """CPCV over one portfolio strategy. One result dict per split, with its test Sharpe.

    Each split's test set may be several non-contiguous blocks; each block gets its own
    vol target fixed from data strictly before ITS start (the same convention as
    ``backtest.walkforward``), then the blocks' returns are concatenated into one test
    path before scoring — so a split's Sharpe reflects genuinely out-of-sample exposure
    throughout, never a target calibrated on the block itself.
    """
    results = []
    for split in cpcv_splits(len(rebal), n_groups, n_test_groups, embargo_periods):
        block_returns = []
        for block in split.test_blocks:
            start, end = rebal[block[0]], rebal[block[-1]]
            ret = strategy.portfolio_returns(data, eligible, train_end=start, start=start, end=end)
            block_returns.append(ret)
        test_returns = pd.concat(block_returns).sort_index()
        test_returns = test_returns[~test_returns.index.duplicated()]
        if len(test_returns) < 3:
            continue
        metrics = _monthly_metrics(test_returns)
        results.append({"test_group_ids": split.test_group_ids, "n": len(test_returns),
                        **metrics, "returns": test_returns})
    return results


# --- Deflated Sharpe Ratio ---------------------------------------------------------------

def deflated_sharpe_ratio(observed_returns: pd.Series, trial_sharpes_annualized: list,
                          periods_per_year: int = 12) -> dict:
    """Bailey & Lopez de Prado (2014). See the module docstring for what this corrects for.

    ``observed_returns``: the strategy's own (monthly) net-return series to judge — e.g.
    the full-history or OOS series, NOT one CPCV path.
    ``trial_sharpes_annualized``: the Sharpes of the "attempts" this result should be
    judged against — the CPCV path Sharpes are a natural, non-fabricated choice: they are
    literally how many effectively-independent looks the validation process took.
    """
    returns = observed_returns.dropna()
    n = len(returns)
    if n < 3:
        raise ValueError("need at least 3 return observations to estimate skew/kurtosis")
    mean, std = returns.mean(), returns.std(ddof=1)
    if std == 0:
        raise ValueError("zero-variance return series — Sharpe is undefined")
    sr_hat = mean / std                                    # per-period (monthly) Sharpe
    g3 = float(skew(returns))
    g4 = float(kurtosis(returns, fisher=False))            # NON-excess kurtosis; normal = 3

    trials = np.asarray([s for s in trial_sharpes_annualized
                        if s is not None and np.isfinite(s)]) / math.sqrt(periods_per_year)
    n_trials = len(trials)
    sr0 = 0.0
    if n_trials >= 2 and trials.var(ddof=1) > 0:
        # Expected maximum Sharpe ratio achievable by chance across n_trials attempts
        # whose Sharpes have variance Var[SR] (Bailey & Lopez de Prado, eq. 7-8).
        z1 = norm.ppf(1.0 - 1.0 / n_trials)
        z2 = norm.ppf(1.0 - 1.0 / (n_trials * math.e))
        sr0 = math.sqrt(trials.var(ddof=1)) * ((1 - EULER_GAMMA) * z1 + EULER_GAMMA * z2)

    denom = math.sqrt(max(1e-12, 1 - g3 * sr_hat + (g4 - 1) / 4.0 * sr_hat ** 2))
    z = (sr_hat - sr0) * math.sqrt(n - 1) / denom
    dsr = float(norm.cdf(z))
    return {
        "sharpe_annualized": float(sr_hat * math.sqrt(periods_per_year)),
        "sharpe_monthly": float(sr_hat),
        "n_obs": n,
        "n_trials": n_trials,
        "sr0_annualized": float(sr0 * math.sqrt(periods_per_year)),
        "skew": g3,
        "kurtosis": g4,
        "z": float(z),
        "dsr": dsr,
    }


# --- Probability of Backtest Overfitting (CSCV) -----------------------------------------

def probability_of_backtest_overfitting(variant_returns: dict, n_groups: int = 8) -> dict:
    """Bailey, Borwein, Lopez de Prado & Zhu (2014) CSCV procedure.

    ``variant_returns``: ``{variant_name: monthly net-return series}`` for >= 2 candidate
    configurations sharing a common date range — here, the five momentum-crash-fix
    variants from ``research.momentum_variants.build_variant_returns``. Splits history
    into ``n_groups`` blocks; for every way of using exactly half as train and the
    complementary half as test (C(n_groups, n_groups//2) combinations), finds the
    IN-SAMPLE-best variant and checks its OUT-OF-SAMPLE rank among all variants. PBO is
    the fraction of splits where that in-sample winner finished at or below the OOS
    median — i.e. where picking "the best" would have been picking noise.
    """
    if len(variant_returns) < 2:
        raise ValueError("PBO needs at least two candidate variants to rank against each other")
    common_index = None
    for series in variant_returns.values():
        common_index = series.index if common_index is None else common_index.intersection(series.index)
    matrix = pd.DataFrame({name: s.reindex(common_index) for name, s in variant_returns.items()}).dropna()
    names = list(matrix.columns)
    n_variants = len(names)
    n_dates = len(matrix)
    if n_dates < n_groups * 2:
        raise ValueError(f"only {n_dates} common dates for {n_groups} groups — too few to split")

    groups = make_groups(n_dates, n_groups)
    half = n_groups // 2
    logits = []
    for train_ids in itertools.combinations(range(n_groups), half):
        test_ids = [g for g in range(n_groups) if g not in train_ids]
        train_pos = np.concatenate([groups[g] for g in train_ids])
        test_pos = np.concatenate([groups[g] for g in test_ids])
        train_block, test_block = matrix.iloc[train_pos], matrix.iloc[test_pos]

        train_sharpe = train_block.mean() / train_block.std(ddof=1)
        test_sharpe = test_block.mean() / test_block.std(ddof=1)
        best = train_sharpe.idxmax()

        # Relative rank of the in-sample winner's OOS Sharpe, omega_c = rank / (N+1)
        # (Bailey et al. 2014, eq. 6) -- NOT rank/N: dividing by N+1 is what centers the
        # median rank exactly on 0.5 (logit 0), so pure noise averages to PBO ~= 50%.
        oos_rank = test_sharpe.rank(method="average")[best]   # 1..n_variants
        rank = oos_rank / (n_variants + 1)
        logits.append(math.log(rank / (1 - rank)))

    logits_arr = np.asarray(logits)
    return {
        "n_splits": len(logits_arr),
        "n_variants": n_variants,
        "variant_names": names,
        "logits": logits_arr.tolist(),
        "pbo": float((logits_arr <= 0).mean()),
    }


# --- reporting -----------------------------------------------------------------------

def print_cpcv_report(strategy_name: str, cpcv_results: list, dsr_result: dict, pbo_result: dict | None) -> None:
    sharpes = [r["sharpe"] for r in cpcv_results if r["sharpe"] is not None]
    print("\n" + "=" * 84)
    print(f"CPCV — combinatorial purged cross-validation ({strategy_name})")
    print("=" * 84)
    print(f"  {len(cpcv_results)} splits | Sharpe distribution: min {min(sharpes):+.2f}, "
          f"median {np.median(sharpes):+.2f}, max {max(sharpes):+.2f}; "
          f"{sum(s > 0 for s in sharpes)}/{len(sharpes)} positive.")

    print("\n--- Deflated Sharpe Ratio (Bailey & Lopez de Prado 2014) ---")
    print(f"  Observed Sharpe (annualized)      : {dsr_result['sharpe_annualized']:+.2f}")
    print(f"  Benchmark SR0 from {dsr_result['n_trials']} CPCV trials (annualized): {dsr_result['sr0_annualized']:+.2f}")
    print(f"  Return skew / kurtosis             : {dsr_result['skew']:+.2f} / {dsr_result['kurtosis']:.2f} (normal = 0 / 3)")
    print(f"  Deflated Sharpe Ratio (probability true Sharpe > SR0): {dsr_result['dsr']*100:.1f}%")

    if pbo_result is not None:
        print("\n--- Probability of Backtest Overfitting (Bailey et al. 2014, CSCV) ---")
        print(f"  {pbo_result['n_variants']} candidate variants, {pbo_result['n_splits']} train/test splits")
        print(f"  PBO = {pbo_result['pbo']*100:.1f}% (fraction of splits where the in-sample-best "
              f"variant finished at/below the OOS median)")
        verdict = "LOW — the winning variant tends to generalize." if pbo_result["pbo"] < 0.3 else (
            "HIGH — the 'winner' is hard to distinguish from noise." if pbo_result["pbo"] > 0.5 else
            "MODERATE — some generalization, but not decisive.")
        print(f"  => {verdict}")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("data.sharadar_provider").setLevel(logging.WARNING)

    from factors.evaluate import _rebalance_dates
    from factors.run_factor_eval import build_sharadar_factor_data
    from research.momentum_variants import build_variant_returns
    from strategies.registry import get_portfolio

    data, eligible = build_sharadar_factor_data()
    rebal = _rebalance_dates(data.close.index, "M")
    strategy = get_portfolio("vol_managed_momentum")()

    cpcv_results = run_cpcv(strategy, data, eligible, rebal)
    trial_sharpes = [r["sharpe"] for r in cpcv_results]
    full_returns = strategy.portfolio_returns(data, eligible)
    dsr_result = deflated_sharpe_ratio(full_returns, trial_sharpes)

    pbo_result = None
    try:
        variant_returns, _, _, _ = build_variant_returns(data, eligible)
        pbo_result = probability_of_backtest_overfitting(variant_returns)
    except ValueError as error:
        log.warning("Skipping PBO: %s", error)

    print_cpcv_report(strategy.name, cpcv_results, dsr_result, pbo_result)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
