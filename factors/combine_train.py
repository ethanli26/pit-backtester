"""Train a simple factor-combination model and evaluate it WITHOUT cheating on time.

Leakage controls (each enforced in code):
  * Chronological split — train on the earlier ~70% of DATES, test on the most recent
    ~30% never seen. No shuffling across time; asserted train.max < test.min.
  * The feature scaler is fit on TRAIN rows only and applied to test.
  * Held-out IC, decile spread, and coefficients are all computed on the test set.

Model: L2 (Ridge) linear regression on the next-period forward return. The bar is
NOT lowered: a combination only "wins" if it beats the best single factor's test IC.
Read-only research.
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from sklearn.linear_model import Ridge  # noqa: E402
from sklearn.preprocessing import StandardScaler  # noqa: E402

from factors.combine_dataset import CURATED_FACTORS  # noqa: E402
from factors.run_factor_eval import EXPECTED_SIGN  # noqa: E402

log = logging.getLogger(__name__)

TRAIN_FRAC = 0.70
RIDGE_ALPHA = 1.0           # fixed, not tuned
PERIODS_PER_YEAR = 12       # monthly rebalance
IC_THRESHOLD = 0.02
TSTAT_THRESHOLD = 2.0


def temporal_split(dataset: pd.DataFrame, train_frac: float = TRAIN_FRAC):
    """Split by DATE (earliest first). Returns ``(train, test, split_date)``.

    Every train date strictly precedes every test date — no temporal leakage.
    """
    dates = sorted(dataset["date"].unique())
    split_date = dates[int(len(dates) * train_frac)]
    train = dataset[dataset["date"] < split_date].copy()
    test = dataset[dataset["date"] >= split_date].copy()
    assert train["date"].max() < test["date"].min(), "temporal split leaked dates!"
    return train, test, split_date


def fit_model(train: pd.DataFrame):
    """Fit a Ridge model on TRAIN only (scaler fit on train only). Returns (scaler, model)."""
    x_train = train[CURATED_FACTORS].to_numpy(dtype=float)
    y_train = train["fwd_return"].to_numpy(dtype=float)
    scaler = StandardScaler().fit(x_train)               # LEAKAGE GUARD: train-only fit
    model = Ridge(alpha=RIDGE_ALPHA).fit(scaler.transform(x_train), y_train)
    return scaler, model


def predict_score(model, scaler, frame: pd.DataFrame) -> np.ndarray:
    """Combined alpha score = model's predicted forward return."""
    return model.predict(scaler.transform(frame[CURATED_FACTORS].to_numpy(dtype=float)))


#is the sector doing well
def cross_sectional_ic(frame: pd.DataFrame, score_col: str, ret_col: str = "fwd_return") -> dict:
    """Per-date Spearman IC of a score vs forward return; mean/std/IR/t-stat."""
    ics = []
    for _, group in frame.groupby("date"):
        ic = group[score_col].corr(group[ret_col], method="spearman")
        if pd.notna(ic):
            ics.append(ic)
    ic = pd.Series(ics, dtype=float)
    n = len(ic)
    mean_ic = float(ic.mean()) if n else None
    std_ic = float(ic.std(ddof=1)) if n > 1 else None
    ir = (mean_ic / std_ic * np.sqrt(PERIODS_PER_YEAR)) if std_ic else None
    t_stat = (mean_ic / (std_ic / np.sqrt(n))) if (std_ic and n > 1) else None
    return {"mean_ic": mean_ic, "std_ic": std_ic, "ir": ir, "t_stat": t_stat, "n": n}


def decile_spread(frame: pd.DataFrame, score_col: str, ret_col: str = "fwd_return") -> dict:
    """Per-date top-minus-bottom decile forward-return spread and its Sharpe."""
    spreads = []
    for _, group in frame.groupby("date"):
        try:
            labels = pd.qcut(group[score_col], 10, labels=False, duplicates="drop")
        except (ValueError, IndexError):
            continue
        per_decile = group[ret_col].groupby(labels).mean()
        if not per_decile.empty:
            spreads.append(per_decile.loc[labels.max()] - per_decile.loc[labels.min()])
    sp = pd.Series(spreads, dtype=float)
    mean_spread = float(sp.mean()) if len(sp) else None
    sharpe = (mean_spread / sp.std(ddof=1) * np.sqrt(PERIODS_PER_YEAR)) if len(sp) > 1 and sp.std(ddof=1) else None
    return {"mean_spread": mean_spread, "sharpe": sharpe}


def best_single_factor_ic(test: pd.DataFrame) -> tuple[str, dict, dict]:
    """Return (best_factor_name, its IC stats, {factor: IC stats}) on the test set.

    'Best' = largest |mean IC| — a deliberately HARD, hindsight bar for the combo.
    """
    per_factor = {f: cross_sectional_ic(test, f) for f in CURATED_FACTORS}
    best = max(per_factor, key=lambda f: abs(per_factor[f]["mean_ic"] or 0.0))
    return best, per_factor[best], per_factor


def coefficient_report(model) -> pd.DataFrame:
    """Coefficients with sign vs economic prior (flags reliance on inverted factors)."""
    rows = []
    for factor, coef in zip(CURATED_FACTORS, model.coef_):
        expected = EXPECTED_SIGN.get(factor)
        coef_sign = 0 if abs(coef) < 1e-12 else (1 if coef > 0 else -1)
        consistent = (expected is not None and coef_sign == expected)
        rows.append({"factor": factor, "coef": coef, "coef_sign": "+" if coef_sign > 0 else "-",
                     "expected": "+" if (expected or 0) > 0 else "-",
                     "prior_consistent": "yes" if consistent else "no"})
    return pd.DataFrame(rows).sort_values("coef", key=lambda s: s.abs(), ascending=False)


def prior_consistency(model) -> float:
    """|coef|-weighted fraction of the model that leans the prior-consistent way."""
    total = sum(abs(c) for c in model.coef_)
    if total == 0:
        return 0.0
    aligned = sum(abs(c) for f, c in zip(CURATED_FACTORS, model.coef_)
                  if EXPECTED_SIGN.get(f) == (1 if c > 0 else -1))
    return aligned / total


def main() -> int:
    """Build, split, fit, and print the held-out IC / coefficient comparison."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    logging.getLogger("yfinance").setLevel(logging.WARNING)
    from factors.combine_dataset import build_combined_dataset

    dataset, _ = build_combined_dataset()
    train, test, split_date = temporal_split(dataset)
    print(f"\nChronological split at {split_date.date()}: "
          f"train rows={len(train)} (..{train['date'].max().date()}), "
          f"test rows={len(test)} ({test['date'].min().date()}..)")

    scaler, model = fit_model(train)
    test = test.assign(combined_score=predict_score(model, scaler, test))

    combo_ic = cross_sectional_ic(test, "combined_score")
    combo_dec = decile_spread(test, "combined_score")
    best_name, best_ic, _ = best_single_factor_ic(test)

    print("\n=== Held-out test IC ===")
    print(f"  Combined model : mean IC {combo_ic['mean_ic']:+.4f}, IR {combo_ic['ir']:+.2f}, "
          f"t {combo_ic['t_stat']:+.2f}, decile spread {combo_dec['mean_spread'] * 100:+.2f}% "
          f"(Sharpe {combo_dec['sharpe']:+.2f})")
    print(f"  Best single ({best_name}): mean IC {best_ic['mean_ic']:+.4f}, t {best_ic['t_stat']:+.2f}")
    print("\n=== Combined-model coefficients (sign vs economic prior) ===")
    print(coefficient_report(model).to_string(index=False))
    print(f"\n  Prior-consistent weight: {prior_consistency(model) * 100:.0f}% of |coef| leans the "
          "theory-expected way.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
