"""Data-source abstraction that ``SharadarProvider`` implements.

A ``DataProvider`` exposes tidy, date-indexed, look-ahead-safe data behind a stable
interface, so a factor declares WHAT it needs (price bars, fundamental fields) and
never WHERE it comes from.

POINT-IN-TIME CONTRACT (every provider must honor):
  The value returned for a given (symbol, field, date) must have been KNOWABLE at
  that date — no future revision and no look-ahead. Prices are split/dividend
  adjusted; that adjustment is retroactive but does not create tradable look-ahead
  for returns or ratios. Fundamentals MUST be dated to their public FILING date, not
  the fiscal period end, or point-in-time correctness is violated.

Read-only research: no IBKR, no orders.
"""

from abc import ABC, abstractmethod

import pandas as pd


class DataProvider(ABC):
    """Interface for a look-ahead-safe data source."""

    name: str

    @abstractmethod
    def get_price_bars(self, symbols: list[str], start=None, end=None) -> dict[str, pd.DataFrame]:
        """Return ``{symbol: OHLC(V) DataFrame}`` indexed by tz-naive date."""

    @abstractmethod
    def get_fundamentals(self, symbols: list[str], fields: list[str],
                         start=None, end=None) -> dict[str, pd.DataFrame]:
        """Return ``{symbol: DataFrame}`` of fundamental ``fields`` dated to filing."""
