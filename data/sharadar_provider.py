"""Point-in-time fundamental & price data via Sharadar (Nasdaq Data Link).

This is the paid data the fundamental factor family has always needed. It targets three
Sharadar tables on Nasdaq Data Link:

  * ``SHARADAR/SF1``    — company fundamentals. We use dimension ``ARQ`` (As-Reported
    Quarterly) and key every value to its ``datekey`` (the SEC FILING date), NOT
    ``calendardate`` (the fiscal period end). This is the entire point of paying: a
    fundamental value is only KNOWABLE once it has been filed, so a factor on date ``t``
    may use a statement only if its ``datekey <= t``. Using ``calendardate`` would leak
    weeks-to-months of future information.
  * ``SHARADAR/SEP``    — daily prices (we use the dividend+split-adjusted close).
  * ``SHARADAR/TICKERS`` — the survivorship-free symbol master, INCLUDING delisted
    names, with first/last price dates so a universe can be reconstructed as-of any date.

STUB MODE. If no API key is present (env ``NASDAQ_DATA_LINK_API_KEY``), the provider
constructs fine but every REAL data call raises :class:`SharadarUnavailable` with a
descriptive message. That lets the whole pipeline — providers, factors, universe,
harness, tests — be built and verified without the subscription; only the final numbers
are gated on the key.

The point-in-time panel builders below (``build_fundamental_panels`` and helpers) are
PURE functions on filing-dated frames, so they are fully testable with synthetic
filings and never need the key.

Read-only research. No orders.
"""

import logging
import os
import tempfile
import zipfile
from pathlib import Path

import pandas as pd

from data.providers import DataProvider

log = logging.getLogger(__name__)

API_KEY_ENV = "NASDAQ_DATA_LINK_API_KEY"
CACHE_DIR = Path(__file__).resolve().parent / "cache" / "sharadar"
BULK_DIR = CACHE_DIR / "bulk"   # one whole-table parquet per archived Sharadar table

# The full Sharadar suite to archive locally so research runs offline after the
# subscription ends. ``split`` tables are ALSO exploded into the per-symbol cache the
# factor pipeline reads ("sep"/"sf1"); the rest are kept as whole-table parquet for
# future factor families. Tables outside the subscription are logged and skipped.
ARCHIVE_TABLES = [
    {"code": "SHARADAR/TICKERS", "name": "TICKERS", "split": None},     # survivorship-free master
    {"code": "SHARADAR/SF1", "name": "SF1", "split": "sf1"},            # fundamentals (filing-dated)
    {"code": "SHARADAR/SEP", "name": "SEP", "split": "sep"},            # daily prices
    {"code": "SHARADAR/SF2", "name": "SF2", "split": None},            # insider transactions
    {"code": "SHARADAR/SF3", "name": "SF3", "split": None},            # institutional holdings
    {"code": "SHARADAR/EVENTS", "name": "EVENTS", "split": None},      # corporate events
    {"code": "SHARADAR/SP500", "name": "SP500", "split": None},        # S&P 500 membership history
    {"code": "SHARADAR/ACTIONS", "name": "ACTIONS", "split": None},    # splits/dividends/listings
    {"code": "SHARADAR/DAILY", "name": "DAILY", "split": None},        # daily marketcap/ratios
    {"code": "SHARADAR/METRICS", "name": "METRICS", "split": None},    # daily valuation metrics
]

# Raw SF1 (ARQ) fields we pull; mapped to friendly names in the panels below.
SF1_FIELDS = ["gp", "assets", "netinc", "equity", "sharesbas", "opinc", "revenue",
              "fcf", "ncfo", "debt"]
SF1_RENAME = {"gp": "gross_profit", "assets": "total_assets", "netinc": "net_income",
              "equity": "book_equity", "sharesbas": "shares", "opinc": "operating_income",
              "revenue": "revenue", "fcf": "free_cash_flow", "ncfo": "op_cash_flow",
              "debt": "total_debt"}
FILING_DATE_COLUMN = "datekey"   # the FILING date — the point-in-time key (never calendardate)


class SharadarUnavailable(RuntimeError):
    """Raised when a real Sharadar call is attempted without an API key (stub mode)."""


class SharadarCacheMiss(SharadarUnavailable):
    """Raised in cache-only mode when data is missing from the local archive.

    Subclasses ``SharadarUnavailable`` so existing handlers still catch it, but it is
    distinct so a "must run offline" check can prove no code path hit the API.
    """


class SharadarProvider(DataProvider):
    """Point-in-time prices/fundamentals/universe from Sharadar, with a parquet cache."""

    name = "sharadar"

    def __init__(self, api_key: str | None = None, cache_dir: Path = CACHE_DIR,
                 cache_only: bool = False):
        self.api_key = api_key or os.getenv(API_KEY_ENV)
        self.stub = not self.api_key
        self.cache_only = cache_only      # if True, NEVER hit the API — local parquet only
        self.cache_dir = Path(cache_dir)
        self.bulk_dir = self.cache_dir / "bulk"
        if self.cache_only:
            log.info("SharadarProvider in CACHE-ONLY mode; any API call will raise.")
        elif self.stub:
            log.info("SharadarProvider in STUB mode (no %s); real data calls will raise.", API_KEY_ENV)

    # --- plumbing ---------------------------------------------------------------

    def _client(self, what: str = "Sharadar data"):
        """Return an authenticated client, or raise if offline/stub/cache-only.

        Called ONLY on a cache miss, so a fully-archived pipeline never reaches here.
        """
        if self.cache_only:
            raise SharadarCacheMiss(
                f"cache-only mode: '{what}' is not in the local archive and the API is "
                f"disabled. Run the bulk archive first, or drop cache_only to fetch it.")
        if self.stub:
            raise SharadarUnavailable(
                f"Cannot fetch {what}: no Sharadar API key. Set {API_KEY_ENV} to a Nasdaq "
                f"Data Link key with a Sharadar subscription. Until then the pipeline runs "
                f"in stub mode and fundamental factors are deferred.")
        try:
            import nasdaqdatalink
        except ImportError as error:  # optional dependency
            raise SharadarUnavailable(
                "The 'nasdaqdatalink' package is not installed; cannot reach Sharadar.") from error
        nasdaqdatalink.ApiConfig.api_key = self.api_key
        return nasdaqdatalink

    def _cache_path(self, kind: str, symbol: str) -> Path:
        return self.cache_dir / kind / f"{symbol}.parquet"

    # --- prices (SEP) -----------------------------------------------------------

    def get_price_bars(self, symbols: list[str], start=None, end=None) -> dict[str, pd.DataFrame]:
        """Daily adjusted OHLCV per symbol from SHARADAR/SEP (cached to parquet)."""
        bars: dict[str, pd.DataFrame] = {}
        for symbol in symbols:
            frame = self._load_or_fetch_prices(symbol)
            if frame is None or frame.empty:
                continue
            if start is not None:
                frame = frame[frame.index >= pd.Timestamp(start)]
            if end is not None:
                frame = frame[frame.index <= pd.Timestamp(end)]
            bars[symbol] = frame
        return bars

    def _load_or_fetch_prices(self, symbol: str, refresh: bool = False) -> pd.DataFrame | None:
        path = self._cache_path("sep", symbol)
        if not refresh and _is_valid_parquet(path):
            return pd.read_parquet(path)
        if self.cache_only:
            return None  # offline: a name missing from the archive is skipped, never fetched
        client = self._client(f"SEP prices for {symbol}")
        raw = client.get_table("SHARADAR/SEP", ticker=symbol, paginate=True)
        if raw is None or raw.empty:
            return None
        # closeadj = dividend+split adjusted close (total-return basis for factor returns).
        frame = self._sep_group_to_frame(raw)
        _save_parquet_atomic(path, frame)
        return frame

    # --- fundamentals (SF1, ARQ, filing-dated) ----------------------------------

    def get_fundamentals(self, symbols: list[str], fields: list[str] = SF1_FIELDS,
                         start=None, end=None) -> dict[str, pd.DataFrame]:
        """As-reported quarterly fundamentals per symbol, INDEXED BY FILING DATE.

        Returns ``{symbol: DataFrame}`` where the index is ``datekey`` (the SEC filing
        date) — the point-in-time availability date — and columns are the renamed
        ``fields``. We deliberately drop ``calendardate`` (period end) as an index to
        prevent look-ahead. Dimension is ARQ (as-reported quarterly).
        """
        out: dict[str, pd.DataFrame] = {}
        for symbol in symbols:
            frame = self._load_or_fetch_fundamentals(symbol, fields)
            if frame is not None and not frame.empty:
                out[symbol] = frame
        return out

    def _load_or_fetch_fundamentals(self, symbol: str, fields: list[str],
                                    refresh: bool = False) -> pd.DataFrame | None:
        path = self._cache_path("sf1", symbol)
        if not refresh and _is_valid_parquet(path):
            return pd.read_parquet(path)
        if self.cache_only:
            return None  # offline: a name with no archived fundamentals is skipped
        client = self._client(f"SF1 fundamentals for {symbol}")
        # POINT-IN-TIME GUARD: dimension ARQ = as-reported quarterly; we request datekey
        # (filing date) alongside the values and index by it, never by calendardate.
        columns = [FILING_DATE_COLUMN, *fields]
        raw = client.get_table("SHARADAR/SF1", ticker=symbol, dimension="ARQ",
                               qopts={"columns": columns}, paginate=True)
        if raw is None or raw.empty:
            return None
        frame = as_reported_frame(raw, fields)
        _save_parquet_atomic(path, frame)
        return frame

    # --- survivorship-free universe (TICKERS) -----------------------------------

    def get_universe(self, min_first_price_year: int | None = None) -> pd.DataFrame:
        """Survivorship-free symbol master from SHARADAR/TICKERS (INCLUDES delisted).

        Returns a DataFrame with at least ``ticker``, ``firstpricedate``,
        ``lastpricedate``, and ``isdelisted``. Delisted names are KEPT — that inclusion
        is the real fix for survivorship bias; a name in/out of the universe on date
        ``t`` is decided as-of ``t`` from its first/last price dates, not from whether it
        still trades today.

        Reads the local bulk TICKERS archive when present (offline); else fetches once.
        """
        bulk_path = self.bulk_dir / "TICKERS.parquet"
        if bulk_path.exists():
            raw = pd.read_parquet(bulk_path)
        else:
            client = self._client("TICKERS universe")
            raw = client.get_table("SHARADAR/TICKERS", table="SF1", paginate=True)
        cols = ["ticker", "firstpricedate", "lastpricedate", "isdelisted", "category"]
        frame = raw[[c for c in cols if c in raw.columns]].copy()
        for date_col in ("firstpricedate", "lastpricedate"):
            if date_col in frame.columns:
                frame[date_col] = pd.to_datetime(frame[date_col], errors="coerce")
        if min_first_price_year is not None and "firstpricedate" in frame.columns:
            frame = frame[frame["firstpricedate"].dt.year >= min_first_price_year]
        return frame.reset_index(drop=True)

    # --- one-time full-suite local archive --------------------------------------

    def bulk_archive(self, tables: list[dict] | None = None, refresh: bool = False) -> dict[str, int]:
        """Download the full Sharadar suite to local parquet so research runs offline.

        Idempotent and resumable: a table whose parquet already exists is skipped unless
        ``refresh``. Tables outside the subscription are logged and skipped (never fatal).
        ``split`` tables (SEP, SF1) are also exploded into the per-symbol cache the factor
        pipeline reads. Returns ``{table_name: row_count}`` for archived tables.
        """
        tables = tables or ARCHIVE_TABLES
        self.bulk_dir.mkdir(parents=True, exist_ok=True)
        counts: dict[str, int] = {}
        for spec in tables:
            name, code = spec["name"], spec["code"]
            parquet_path = self.bulk_dir / f"{name}.parquet"
            if parquet_path.exists() and not refresh:
                rows = _parquet_rows(parquet_path)
                counts[name] = rows
                log.info("SKIP %s (already archived): %d rows at %s", name, rows, parquet_path)
                print(f"  [skip]   {name:<8} {rows:>12,} rows  {parquet_path}")
                continue
            try:
                frame = self._download_full_table(code)
            except Exception as error:  # noqa: BLE001 - a missing entitlement must not abort the archive
                log.warning("Skipping %s: %s", name, error)
                print(f"  [absent] {name:<8} not archived ({type(error).__name__}: {error})")
                continue
            frame.to_parquet(parquet_path)
            counts[name] = len(frame)
            print(f"  [ok]     {name:<8} {len(frame):>12,} rows  {parquet_path}")
            if spec["split"] == "sep":
                self._split_prices_to_cache(frame)
            elif spec["split"] == "sf1":
                self._split_fundamentals_to_cache(frame)
        return counts

    def _download_full_table(self, code: str) -> pd.DataFrame:
        """Bulk-download an ENTIRE Sharadar table (full history) via qopts.export."""
        client = self._client(f"bulk export of {code}")
        with tempfile.TemporaryDirectory() as tmp:
            # export_table blocks until Sharadar's bulk file is ready, then downloads a zip.
            client.export_table(code, filename=tmp)
            zips = list(Path(tmp).glob("*.zip"))
            if not zips:
                raise RuntimeError("bulk export produced no file")
            with zipfile.ZipFile(zips[0]) as zf:
                inner = zf.namelist()[0]
                with zf.open(inner) as handle:
                    return pd.read_csv(handle, low_memory=False)

    @staticmethod
    def _sep_group_to_frame(group: pd.DataFrame) -> pd.DataFrame:
        """One ticker's SEP rows -> the OHLCV frame the pipeline reads (closeadj = total return)."""
        group = group.sort_values("date")
        return pd.DataFrame({
            "Open": group["open"].to_numpy(), "High": group["high"].to_numpy(),
            "Low": group["low"].to_numpy(), "Close": group["closeadj"].to_numpy(),
            "Volume": group["volume"].to_numpy(),
        }, index=pd.DatetimeIndex(pd.to_datetime(group["date"]).dt.date, name="Date"))

    def _split_prices_to_cache(self, sep: pd.DataFrame, only: set[str] | None = None,
                               refresh: bool = False, progress_every: int = 2000) -> int:
        """Explode a bulk SEP frame into per-symbol OHLCV parquet — resumable & atomic.

        Skips tickers already cached (unless ``refresh``), writes each via a temp-then-
        rename so a crash never leaves a half-written file, and logs progress. ``only``
        restricts the split to a ticker subset (e.g. the liquid universe first).
        """
        written = 0
        groups = sep.groupby("ticker")
        for i, (ticker, group) in enumerate(groups, 1):
            if only is not None and ticker not in only:
                continue
            path = self._cache_path("sep", ticker)
            if not refresh and _is_valid_parquet(path):
                continue  # resumable: already cached
            _save_parquet_atomic(path, self._sep_group_to_frame(group))
            written += 1
            if written % progress_every == 0:
                log.info("SEP split: %d symbols written (scanned %d).", written, i)
        return written

    def _split_fundamentals_to_cache(self, sf1: pd.DataFrame, refresh: bool = False,
                                     progress_every: int = 2000) -> int:
        """Explode a bulk SF1 frame into per-symbol filing-dated ARQ parquet — resumable.

        POINT-IN-TIME GUARD: keep only dimension ARQ and index each name by ``datekey``
        (filing date) via ``as_reported_frame`` — identical to the per-symbol fetch path.
        Atomic writes + skip-cached, so an interrupted split resumes cleanly.
        """
        if "dimension" in sf1.columns:
            sf1 = sf1[sf1["dimension"] == "ARQ"]
        written = 0
        for ticker, group in sf1.groupby("ticker"):
            path = self._cache_path("sf1", ticker)
            if not refresh and _is_valid_parquet(path):
                continue
            frame = as_reported_frame(group, SF1_FIELDS)
            if frame.empty:
                continue
            _save_parquet_atomic(path, frame)
            written += 1
            if written % progress_every == 0:
                log.info("SF1 split: %d symbols written.", written)
        return written

    def archive_prices_for_tickers(self, tickers: list[str], refresh: bool = False,
                                   progress_every: int = 100) -> int:
        """Robust, resumable per-symbol SEP archive for a ticker subset (universe-first).

        Fetches and writes ONE symbol at a time, so progress is saved continuously and a
        crash loses at most the symbol in flight. Skips already-cached symbols (idempotent).
        Prefers the local bulk SEP archive when present; otherwise pages the API per name.
        Returns the number of symbols newly written.
        """
        pending = [t for t in tickers if refresh or not _is_valid_parquet(self._cache_path("sep", t))]
        log.info("SEP universe archive: %d/%d symbols already cached; fetching %d.",
                 len(tickers) - len(pending), len(tickers), len(pending))
        if not pending:
            return 0
        written = 0
        for i, ticker in enumerate(pending, 1):
            frame = self._load_or_fetch_prices(ticker, refresh=refresh)  # writes its own cache
            if frame is not None:
                written += 1
            if i % progress_every == 0:
                log.info("SEP universe archive: %d/%d fetched.", i, len(pending))
        return written


# --- Point-in-time panel construction (pure, key-free, fully testable) -------------


def _parquet_rows(path: Path) -> int:
    """Row count of a parquet file from its footer metadata (no full read)."""
    import pyarrow.parquet as pq

    return pq.ParquetFile(path).metadata.num_rows


def _is_valid_parquet(path: Path) -> bool:
    """True if ``path`` is a complete, readable parquet (resumability + crash safety)."""
    if not path.exists():
        return False
    try:
        _parquet_rows(path)  # reads only the footer; fails on a truncated/partial file
        return True
    except Exception:  # noqa: BLE001 - any read failure means "not validly cached"
        return False


def _save_parquet_atomic(path: Path, frame: pd.DataFrame) -> None:
    """Write parquet via temp-then-rename so a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    frame.to_parquet(tmp)
    os.replace(tmp, path)   # atomic on POSIX


def as_reported_frame(raw: pd.DataFrame, fields: list[str]) -> pd.DataFrame:
    """Index a raw SF1 pull by FILING date and rename to friendly columns.

    POINT-IN-TIME GUARD: the index becomes ``datekey`` (filing date). One filing per
    datekey is kept (the last, if a restatement shares a datekey), sorted ascending.
    """
    if FILING_DATE_COLUMN not in raw.columns:
        raise ValueError(f"SF1 pull missing '{FILING_DATE_COLUMN}'; cannot guarantee point-in-time.")
    frame = raw.copy()
    frame[FILING_DATE_COLUMN] = pd.to_datetime(frame[FILING_DATE_COLUMN])
    frame = (frame.sort_values(FILING_DATE_COLUMN)
                  .drop_duplicates(subset=[FILING_DATE_COLUMN], keep="last")
                  .set_index(FILING_DATE_COLUMN))
    present = [f for f in fields if f in frame.columns]
    frame = frame[present].rename(columns={k: v for k, v in SF1_RENAME.items() if k in present})
    frame.index.name = FILING_DATE_COLUMN
    return frame


def _ttm(series: pd.Series, min_quarters: int = 4) -> pd.Series:
    """Trailing-twelve-month sum of a quarterly (filing-frequency) flow series.

    Uses the 4 most recent FILINGS (each with ``datekey <= t``), so it stays
    point-in-time. NaN until four quarters exist.
    """
    return series.rolling(4, min_periods=min_quarters).sum()


def _yoy(series: pd.Series) -> pd.Series:
    """Year-over-year growth of a quarterly series: (x_t - x_{t-4}) / |x_{t-4}|.

    Compares a quarter to the same quarter a year earlier (4 filings back), so both
    inputs are filed on or before ``t``. Absolute denominator keeps the sign meaningful
    when the year-ago value is negative.
    """
    prior = series.shift(4)
    return (series - prior) / prior.abs()


def _as_of_daily(quarterly: pd.Series, master: pd.DatetimeIndex) -> pd.Series:
    """Forward-fill a filing-dated quarterly series onto the daily ``master`` calendar.

    LOOK-AHEAD GUARD: the value is placed on its FILING date and forward-filled ONLY
    (never back-filled), so ``daily.loc[t]`` is the most recent filing with
    ``datekey <= t``. Asserts nothing is visible before its first filing date.
    """
    quarterly = quarterly[~quarterly.index.duplicated(keep="last")].sort_index()
    union = master.union(pd.DatetimeIndex(quarterly.index))
    daily = quarterly.reindex(union).ffill().reindex(master)
    first_filing = quarterly.first_valid_index()
    if first_filing is not None:
        before = daily[daily.index < first_filing]
        assert before.isna().all(), "point-in-time violation: value visible before its filing date"
    return daily


# Each derived panel: (output name, source filing column, transform). "level" = latest
# filed value forward-filled; "ttm" = trailing-4-quarter sum; "yoy" = year-over-year growth.
PANEL_SPEC = [
    ("gross_profit_ttm", "gross_profit", "ttm"),
    ("operating_income_ttm", "operating_income", "ttm"),
    ("revenue_ttm", "revenue", "ttm"),
    ("revenue_yoy", "revenue", "yoy"),
    ("net_income_ttm", "net_income", "ttm"),
    ("net_income_yoy", "net_income", "yoy"),
    ("free_cash_flow_ttm", "free_cash_flow", "ttm"),
    ("op_cash_flow_ttm", "op_cash_flow", "ttm"),
    ("total_assets", "total_assets", "level"),
    ("total_assets_yoy", "total_assets", "yoy"),
    ("book_equity", "book_equity", "level"),
    ("total_debt", "total_debt", "level"),
    ("shares", "shares", "level"),
]
_TRANSFORMS = {"level": lambda s: s, "ttm": _ttm, "yoy": _yoy}


def build_fundamental_panels(filings_by_symbol: dict[str, pd.DataFrame],
                             master: pd.DatetimeIndex) -> dict[str, pd.DataFrame]:
    """Turn per-symbol filing-dated frames into PIT daily ``date x symbol`` panels.

    Derived quarterly quantities (TTM sums, YoY growth) are computed at FILING
    frequency first, then forward-filled to the daily calendar — so every derived value
    on date ``t`` is built only from filings with ``datekey <= t``. Returns the panels
    the fundamental factors consume (see ``PANEL_SPEC``).
    """
    columns: dict[str, dict[str, pd.Series]] = {name: {} for name, _, _ in PANEL_SPEC}
    for symbol, filings in filings_by_symbol.items():
        filings = filings.sort_index()
        for name, source, transform in PANEL_SPEC:
            if source not in filings.columns:
                continue
            quarterly = _TRANSFORMS[transform](filings[source])
            # LOOK-AHEAD GUARD: _as_of_daily forward-fills from the FILING date only.
            columns[name][symbol] = _as_of_daily(quarterly, master)
    return {name: pd.DataFrame(cols, index=master) for name, cols in columns.items()}


# --- Insider (SF2) and institutional (SF3) point-in-time panels ---------------------

INSIDER_WINDOW_DAYS = 126          # ~6 trading months trailing window for insider signals
INST_FILING_LAG_DAYS = 45          # 13F holdings are not public until the 45-day deadline
_OPEN_MARKET_CODES = ("P", "S")    # SEC Form 4 open-market Purchase / Sale (ignore grants/exercises)


def _month_ends(master: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Last trading day of each month in ``master`` (the factor's evaluation grid)."""
    s = pd.Series(master, index=master)
    return pd.DatetimeIndex(s.groupby(master.to_period("M")).last().to_numpy())


def _to_master_day(dates, master: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Map each calendar date to the FIRST trading day on/after it (when it is actionable).

    A filing on a weekend/holiday only becomes tradeable on the next session, so mapping
    to the next master day is conservative (never earlier than the public date). Dates past
    the end of ``master`` map to NaT.
    """
    dates = pd.DatetimeIndex(pd.to_datetime(dates))
    pos = master.searchsorted(dates, side="left")
    out = [master[p] if p < len(master) else pd.NaT for p in pos]
    return pd.DatetimeIndex(out)


def build_insider_panels(transactions_by_symbol: dict[str, pd.DataFrame],
                         master: pd.DatetimeIndex, window_days: int = INSIDER_WINDOW_DAYS) -> dict[str, pd.DataFrame]:
    """Trailing insider-trading panels keyed to the FORM 4 FILING DATE (not the trade date).

    LOOK-AHEAD GUARD: every transaction is placed on the first trading day on/after its
    ``filingdate`` (the day the Form 4 is public), and trailing windows END at ``t``; no
    future filing enters the value at ``t``. Values before a name's first filing are NaN
    (absence of data, not "zero buying").

    Panels:
      * ``insider_net_buy_value``    — trailing-window net signed open-market $ (buys - sells)
      * ``insider_distinct_buyers``  — trailing-window count of DISTINCT insiders buying
    """
    month_ends = _month_ends(master)
    net_cols: dict[str, pd.Series] = {}
    buyer_cols: dict[str, pd.Series] = {}
    for symbol, tx in transactions_by_symbol.items():
        tx = tx.dropna(subset=["filingdate"])
        if tx.empty:
            continue
        avail = _to_master_day(tx["filingdate"], master)
        tx = tx.assign(avail=avail).dropna(subset=["avail"])
        openmkt = tx[tx["transactioncode"].isin(_OPEN_MARKET_CODES)]
        if openmkt.empty:
            continue
        first_day = openmkt["avail"].min()

        # Net signed open-market value: + for buys (P), - for sells (S).
        signed = openmkt["transactionvalue"].abs() * openmkt["transactionshares"].apply(
            lambda s: 1.0 if s >= 0 else -1.0)
        flow = signed.groupby(openmkt["avail"]).sum().reindex(master, fill_value=0.0)
        net = flow.rolling(window_days, min_periods=1).sum()
        net[master < first_day] = float("nan")     # no data before the first filing
        net_cols[symbol] = net

        # Distinct insiders buying (open-market P), trailing window, on the month-end grid.
        buyer_cols[symbol] = _trailing_distinct_buyers(
            openmkt[openmkt["transactioncode"] == "P"], master, month_ends, window_days, first_day)

    return {"insider_net_buy_value": pd.DataFrame(net_cols, index=master),
            "insider_distinct_buyers": pd.DataFrame(buyer_cols, index=master)}


def _trailing_distinct_buyers(buys: pd.DataFrame, master: pd.DatetimeIndex,
                              month_ends: pd.DatetimeIndex, window_days: int,
                              first_day) -> pd.Series:
    """Per month-end: distinct insiders who bought in the trailing window; ffill to daily."""
    out = pd.Series(float("nan"), index=master)
    if buys.empty:
        return out
    positions = master.get_indexer(pd.DatetimeIndex(buys["avail"]))
    frame = pd.DataFrame({"pos": positions, "owner": buys["ownername"].to_numpy()})
    frame = frame[frame["pos"] >= 0].sort_values("pos")
    for m in month_ends:
        if m < first_day:
            continue
        mi = master.get_loc(m)
        # LOOK-AHEAD GUARD: window is master positions (mi-window, mi] — only filings <= t.
        window = frame[(frame["pos"] > mi - window_days) & (frame["pos"] <= mi)]
        out.loc[m] = float(window["owner"].nunique())
    return out.ffill()


def build_institutional_panels(holdings_by_symbol: dict[str, pd.DataFrame],
                               master: pd.DatetimeIndex, shares_panel: pd.DataFrame,
                               lag_days: int = INST_FILING_LAG_DAYS) -> dict[str, pd.DataFrame]:
    """Institutional (13F) panels, made point-in-time by the 45-day filing LAG.

    CRITICAL LOOK-AHEAD GUARD: SF3 carries only ``calendardate`` (the quarter end). A 13F is
    not public until the SEC's 45-day deadline, so holdings for quarter Q are dated to
    ``calendardate + lag_days`` (mapped to the next trading day) before use — using
    ``calendardate`` directly would leak ~6 weeks. Quarter-over-quarter CHANGES are computed
    at that availability-dated quarterly grid, then forward-filled (never back-filled).

    Panels (each the QoQ CHANGE, the tradeable signal):
      * ``inst_own_pct_chg``  — change in institutional ownership % (Σ shares held / shares out)
      * ``inst_breadth_chg``  — change in breadth (count of distinct institutional holders)
      * ``inst_own_pct``      — the ownership level (for reference)
    """
    own_chg, breadth_chg, own_level = {}, {}, {}
    for symbol, q in holdings_by_symbol.items():
        q = q.sort_index()
        avail = _to_master_day(pd.DatetimeIndex(q.index) + pd.Timedelta(days=lag_days), master)
        q = q.assign(avail=avail).dropna(subset=["avail"])
        if q.empty:
            continue
        shares_out = (shares_panel[symbol].reindex(pd.DatetimeIndex(q["avail"])).to_numpy()
                      if symbol in shares_panel.columns else None)
        own = (q["total_units"].to_numpy() / shares_out) if shares_out is not None else None
        avail_index = pd.DatetimeIndex(q["avail"])
        if own is not None:
            own_series = pd.Series(own, index=avail_index).clip(upper=2.0)  # cap 13F double-count noise
            own_series = own_series[~own_series.index.duplicated(keep="last")]
            own_level[symbol] = _as_of_daily(own_series, master)
            own_chg[symbol] = _as_of_daily(own_series.diff(), master)        # QoQ change
        breadth_series = pd.Series(q["breadth"].to_numpy(), index=avail_index)
        breadth_series = breadth_series[~breadth_series.index.duplicated(keep="last")]
        breadth_chg[symbol] = _as_of_daily(breadth_series.diff(), master)    # QoQ change

    return {"inst_own_pct_chg": pd.DataFrame(own_chg, index=master),
            "inst_breadth_chg": pd.DataFrame(breadth_chg, index=master),
            "inst_own_pct": pd.DataFrame(own_level, index=master)}


# --- Bulk-archive loaders for the alternative-data tables (offline) -----------------

def load_insider_transactions(tickers, bulk_dir: Path = BULK_DIR) -> dict[str, pd.DataFrame]:
    """Read open-market-relevant SF2 insider rows for ``tickers`` from the local archive."""
    import pyarrow.dataset as ds

    path = Path(bulk_dir) / "SF2.parquet"
    cols = ["ticker", "filingdate", "transactioncode", "transactionshares",
            "transactionvalue", "ownername"]
    table = ds.dataset(str(path)).to_table(
        columns=cols, filter=ds.field("ticker").isin(list(tickers)))
    df = table.to_pandas()
    df["filingdate"] = pd.to_datetime(df["filingdate"], errors="coerce")
    return {ticker: group.drop(columns="ticker").reset_index(drop=True)
            for ticker, group in df.groupby("ticker")}


def load_institutional_holdings(tickers, bulk_dir: Path = BULK_DIR) -> dict[str, pd.DataFrame]:
    """Read SF3 13F holdings for ``tickers``, aggregated per ticker-quarter (offline).

    Returns ``{ticker: DataFrame(index=calendardate, columns=[total_units, breadth])}``,
    restricted to common-share (SHR) holdings.
    """
    import pyarrow.dataset as ds

    path = Path(bulk_dir) / "SF3.parquet"
    cols = ["ticker", "calendardate", "investorname", "units", "securitytype"]
    table = ds.dataset(str(path)).to_table(
        columns=cols,
        filter=(ds.field("ticker").isin(list(tickers)) & (ds.field("securitytype") == "SHR")))
    df = table.to_pandas()
    df["calendardate"] = pd.to_datetime(df["calendardate"], errors="coerce")
    grouped = df.groupby(["ticker", "calendardate"]).agg(
        total_units=("units", "sum"), breadth=("investorname", "nunique"))
    return {ticker: sub.droplevel(0).sort_index()
            for ticker, sub in grouped.groupby(level=0)}
