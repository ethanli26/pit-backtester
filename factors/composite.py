"""Transparent equal-weight composites of already-vetted factors.

The survivorship-free scorecard left exactly two individually-significant, correctly-
signed, economically DISTINCT factors: ``momentum_12_1`` (price) and ``profitability``
(fundamental). Price and fundamental signals are nearly uncorrelated by construction, so
combining them is the legitimate, non-overfit diversification case — NOT data mining.

DELIBERATELY UNTUNED. The combination is a fixed 50/50 of the two components, with NO
weight fitting: tuning weights to the scorecard would just overfit the very results we
are trying to validate. Two equally-simple combination rules are provided:

  * ``zscore`` — average of each component's cross-sectional z-score (per date).
  * ``rank``   — average of each component's cross-sectional percentile rank (per date).

LOOK-AHEAD SAFETY: each component is look-ahead safe; the z-score / rank and the average
are CROSS-SECTIONAL (computed within a single date's column vector), so no time-series
information crosses dates. A name is scored only when ALL components are present, so the
composite genuinely requires both signals.
"""

import pandas as pd

from factors.base import Factor, FactorData


def _zscore_rows(panel: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional (per-date) z-score; standardization uses only that date's names."""
    mean = panel.mean(axis=1)
    std = panel.std(axis=1, ddof=0)
    return panel.sub(mean, axis=0).div(std.where(std > 0), axis=0)


def _rank_rows(panel: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional percentile rank in [0, 1] (per date)."""
    return panel.rank(axis=1, pct=True)


class CompositeFactor(Factor):
    """Equal-weight average of several component factors, z-score or rank normalized."""

    category = "composite"
    requires = ()
    point_in_time_provider = True  # a component (profitability) needs PIT data

    def __init__(self, components: list[Factor], mode: str = "zscore", name: str | None = None):
        if mode not in ("zscore", "rank"):
            raise ValueError("mode must be 'zscore' or 'rank'")
        self.components = components
        self.mode = mode
        self.name = name or f"composite_{mode}"

    def compute(self, data: FactorData) -> pd.DataFrame:
        normalize = _zscore_rows if self.mode == "zscore" else _rank_rows
        normalized = []
        for factor in self.components:
            panel = factor.compute(data).replace([float("inf"), float("-inf")], pd.NA).astype(float)
            normalized.append(normalize(panel))
        # Equal weight (no tuning). Summing aligns on date x symbol; NaN where any
        # component is missing, so a name needs EVERY component to score.
        total = normalized[0].copy()
        for panel in normalized[1:]:
            total = total + panel
        return total / float(len(normalized))
