"""Size-tier universes (large / mid / small) for the cap-spectrum comparison.

Each universe is a fixed, representative, sector-keyed constituent list. The large
universe is a hardcoded set of liquid large-caps per sector; mid and small are
hardcoded S&P 400 / Russell 2000-style names.

SURVIVORSHIP CAVEAT (read this): these are TODAY'S surviving tickers. Without
point-in-time constituent data, delisted/merged/failed names are absent — a bias
that is worst for small-caps (where failures are common). This comparison is
SUGGESTIVE, NOT CONCLUSIVE; strong small-cap results would need point-in-time data
to trust with real money.

This module also loads OHLCV bars (with Volume, needed for the liquidity filter)
into its own cache, leaving the OHLC-only cache used elsewhere untouched.
"""

import logging
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)

BENCHMARK = "SPY"
HISTORY_YEARS = 15
MIN_HISTORY_BARS = 252
OHLCV_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]
CACHE_DIR = Path(__file__).resolve().parent / "cache" / "ohlcv"
THROTTLE_SECONDS = 0.15

# The 11 SPDR sector ETFs that make up the investable sector universe.
SECTOR_ETFS = ["XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLB", "XLU", "XLRE", "XLC"]

# Large-cap baseline: a handful of large, liquid constituents per sector.
LARGE: dict[str, list[str]] = {
    "XLK":  ["AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "CRM", "AMD", "ADBE"],
    "XLF":  ["BRK-B", "JPM", "V", "MA", "BAC", "WFC", "GS", "SPGI"],
    "XLE":  ["XOM", "CVX", "COP", "EOG", "SLB", "MPC", "PSX"],
    "XLV":  ["LLY", "UNH", "JNJ", "ABBV", "MRK", "TMO", "ABT", "PFE"],
    "XLY":  ["AMZN", "TSLA", "HD", "MCD", "NKE", "LOW", "BKNG", "SBUX"],
    "XLP":  ["PG", "COST", "KO", "PEP", "WMT", "PM", "MO"],
    "XLI":  ["GE", "CAT", "RTX", "UNP", "HON", "BA", "UPS", "DE"],
    "XLB":  ["LIN", "SHW", "FCX", "ECL", "NEM", "APD", "DOW"],
    "XLU":  ["NEE", "SO", "DUK", "CEG", "AEP", "D", "EXC"],
    "XLRE": ["PLD", "AMT", "EQIX", "WELL", "SPG", "O", "PSA", "CCI"],
    "XLC":  ["META", "GOOGL", "NFLX", "DIS", "TMUS", "VZ", "CMCSA"],
}

# Mid-cap (S&P 400-style) representative names, sector-keyed. Today's survivors.
MID: dict[str, list[str]] = {
    "XLK": ["NTAP", "JBL", "ZBRA", "TYL", "MANH", "FFIV"],
    "XLF": ["RJF", "SF", "CBSH", "WAL", "PB", "EWBC"],
    "XLI": ["NDSN", "AOS", "GGG", "WWD", "CSL", "HUBB"],
    "XLV": ["MASI", "CHE", "HAE", "BRKR", "PEN", "NBIX"],
    "XLY": ["POOL", "WSM", "TPX", "CROX", "DECK", "BBY"],
    "XLP": ["CASY", "LANC", "JJSF", "INGR", "CHD"],
    "XLE": ["MUR", "SM", "RRC", "CHRD", "PR"],
    "XLB": ["RPM", "CE", "WLK", "SON", "AVY"],
    "XLU": ["IDA", "POR", "NWE", "OGE", "BKH"],
    "XLRE": ["EGP", "STAG", "CUZ", "HIW", "ELS"],
    "XLC": ["TTWO", "NYT", "IPG", "OMC"],
}

# Small-cap (Russell 2000-style) representative names, sector-keyed. Today's survivors.
SMALL: dict[str, list[str]] = {
    "XLK": ["EXTR", "DGII", "CEVA", "PLXS", "MXL"],
    "XLF": ["CATY", "HOPE", "TRMK", "BANF", "INDB", "FFBC"],
    "XLI": ["ALG", "MLI", "GVA", "AIN", "THRM"],
    "XLV": ["SUPN", "AMPH", "CORT", "LMAT", "TARO"],
    "XLY": ["SHOO", "BKE", "CATO", "CRMT"],
    "XLP": ["NATH", "BGS", "MGPI", "USNA"],
    "XLE": ["CLB", "NGS", "REX", "DMLP"],
    "XLB": ["KOP", "HWKN", "SCL", "IOSP"],
    "XLU": ["MGEE", "NWN", "YORW", "UTL"],
    "XLRE": ["GTY", "LTC", "UMH", "ALEX"],
    "XLC": ["SALM", "CCO"],
}

# Additional liquid, long-listed US names (mega/large/mid) to broaden the sample
# across the cap spectrum and all sectors. Still TODAY'S survivors.
BROAD_EXTRA: dict[str, list[str]] = {
    "XLK": ["INTC", "CSCO", "QCOM", "TXN", "IBM", "INTU", "AMAT", "MU", "ADI", "KLAC",
            "GLW", "HPQ", "WDC", "SWKS", "MCHP", "NTAP", "AKAM", "JNPR"],
    "XLF": ["AXP", "USB", "PNC", "TFC", "COF", "SCHW", "CME", "ICE", "MMC", "MET",
            "PRU", "AIG", "TRV", "ALL", "AFL", "BK", "STT", "FITB", "HBAN", "RF"],
    "XLV": ["AMGN", "GILD", "BMY", "CVS", "CI", "MCK", "ZTS", "BDX", "SYK", "BSX",
            "MDT", "EW", "ISRG", "IDXX", "BIIB", "VRTX", "REGN", "A", "DGX", "LH"],
    "XLY": ["TGT", "GM", "F", "EBAY", "MAR", "YUM", "CMG", "ORLY", "AZO", "ROST",
            "TJX", "DG", "GPC", "LVS", "WYNN", "EXPE", "RCL", "CCL", "LEN", "DHI"],
    "XLP": ["CL", "KMB", "GIS", "K", "HSY", "STZ", "MDLZ", "KR", "SYY", "ADM",
            "MNST", "CLX", "EL", "TAP", "TSN", "HRL", "MKC", "CAG"],
    "XLI": ["LMT", "NOC", "GD", "EMR", "ETN", "ITW", "MMM", "FDX", "CSX", "NSC",
            "WM", "RSG", "PH", "ROK", "CMI", "PCAR", "GWW", "FAST", "DOV", "EFX"],
    "XLE": ["OXY", "VLO", "WMB", "KMI", "OKE", "HAL", "BKR", "DVN", "HES", "CTRA", "EQT"],
    "XLB": ["NUE", "STLD", "VMC", "MLM", "PPG", "IFF", "ALB", "CF", "MOS", "FMC",
            "EMN", "IP", "PKG", "BALL"],
    "XLU": ["SRE", "PEG", "ED", "EIX", "XEL", "WEC", "ES", "AEE", "DTE", "FE",
            "ETR", "PPL", "CMS", "CNP", "ATO", "AES", "NI", "PNW"],
    "XLRE": ["DLR", "SBAC", "WY", "AVB", "EQR", "ARE", "VTR", "MAA", "ESS", "KIM",
             "REG", "FRT", "BXP", "HST", "UDR"],
    "XLC": ["T", "CHTR", "EA", "FOXA", "LYV", "MTCH", "PARA", "NWSA"],
}


def _merge_universes(*universes: dict[str, list[str]]) -> dict[str, list[str]]:
    """Merge sector-keyed universes, deduping each symbol to its first sector seen."""
    merged: dict[str, list[str]] = {}
    seen: set[str] = set()
    for universe in universes:
        for sector, symbols in universe.items():
            for symbol in symbols:
                if symbol in seen:
                    continue
                merged.setdefault(sector, []).append(symbol)
                seen.add(symbol)
    return merged


# Broad universe: ~300+ liquid US names spanning the cap spectrum and all sectors.
# SURVIVORSHIP CAVEAT: still today's survivors — widening the sample reduces large-cap
# concentration bias but does NOT remove survivorship bias (only point-in-time paid
# data can). The liquidity filter still excludes untradeable names at evaluation time.
BROAD: dict[str, list[str]] = _merge_universes(LARGE, MID, SMALL, BROAD_EXTRA)

UNIVERSES: dict[str, dict[str, list[str]]] = {
    "large": LARGE, "mid": MID, "small": SMALL, "broad": BROAD,
}


def sector_map(name: str) -> dict[str, str]:
    """Map each constituent symbol to its sector ETF for a universe."""
    return {sym: etf for etf, members in UNIVERSES[name].items() for sym in members}


def constituents(name: str) -> list[str]:
    """Sorted unique constituent tickers of a universe."""
    return sorted({sym for members in UNIVERSES[name].values() for sym in members})


def _symbols(name: str) -> list[str]:
    """All symbols needed: constituents + the 11 sector ETFs + the benchmark."""
    return sorted(set(constituents(name)) | set(SECTOR_ETFS) | {BENCHMARK})


def _cache_path(symbol: str) -> Path:
    return CACHE_DIR / f"{symbol}.parquet"


def fetch_ohlcv(symbol: str, years: int = HISTORY_YEARS, refresh: bool = False) -> pd.DataFrame | None:
    """Fetch (or load) ~``years`` of adjusted daily OHLCV for one symbol.

    Includes Volume (needed for the liquidity filter). Returns ``None`` on failure.
    """
    path = _cache_path(symbol)
    if not refresh and path.exists():
        return pd.read_parquet(path)

    end = date.today() + timedelta(days=1)
    start = end - timedelta(days=int(years * 365.25) + 5)
    try:
        bars = yf.Ticker(symbol).history(
            start=start.isoformat(), end=end.isoformat(), interval="1d", auto_adjust=True
        )
    except Exception as error:  # noqa: BLE001 - any provider failure is non-fatal
        log.warning("Download failed for %s: %s", symbol, error)
        return None
    if bars is None or bars.empty or not set(OHLCV_COLUMNS).issubset(bars.columns):
        log.warning("No OHLCV data for %s.", symbol)
        return None

    bars = bars[OHLCV_COLUMNS].dropna()
    if bars.empty:
        return None
    bars.index = pd.DatetimeIndex(pd.to_datetime(bars.index).date)
    bars.index.name = "Date"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    bars.to_parquet(path)
    time.sleep(THROTTLE_SECONDS)  # throttle only on a live fetch
    return bars


def load_bars(name: str, years: int = HISTORY_YEARS, refresh: bool = False) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """Load OHLCV bars for a universe (constituents + ETFs + benchmark).

    Skips symbols with fewer than ``MIN_HISTORY_BARS`` rows (logged).
    """
    bars: dict[str, pd.DataFrame] = {}
    skipped: list[str] = []
    for symbol in _symbols(name):
        frame = fetch_ohlcv(symbol, years=years, refresh=refresh)
        if frame is None or len(frame) < MIN_HISTORY_BARS:
            skipped.append(symbol)
            continue
        bars[symbol] = frame
    log.info("Universe '%s': loaded %d symbols (%d skipped).", name, len(bars), len(skipped))
    return bars, skipped


# --- Survivorship-free Sharadar universe ------------------------------------------

SHARADAR_BROAD = "sharadar_broad"
SHARADAR_MIN_DOLLAR_VOLUME = 5_000_000.0  # liquidity floor, same spirit as the free filter
SHARADAR_BENCHMARK = "SPY"
# Categories treated as US common equity (the factor study's universe). Delisted names
# in these categories are KEPT — that is what makes the universe survivorship-free.
COMMON_STOCK_CATEGORIES = {
    "Domestic Common Stock", "Domestic Common Stock Primary Class",
    "Domestic Common Stock Secondary Class", "ADR Common Stock",
    "ADR Common Stock Primary Class",
}


def select_liquid_sharadar_universe(provider, max_symbols: int = 1500,
                                    min_dollar_volume: float = SHARADAR_MIN_DOLLAR_VOLUME) -> list[str]:
    """Pick the most-liquid US common stocks (survivorship-free) from the local archive.

    Reads the bulk SEP parquet ONCE to compute each ticker's median daily dollar volume
    and history length, restricts to common-stock categories (delisted INCLUDED), keeps
    names clearing the liquidity/history floors, and returns the top ``max_symbols`` by
    median dollar volume. Selecting by a full-history liquidity summary (not by survival)
    keeps the sample survivorship-free while bounding panel size.
    """
    import pandas as _pd

    tickers_frame = provider.get_universe()
    common = set(tickers_frame.loc[
        tickers_frame["category"].isin(COMMON_STOCK_CATEGORIES), "ticker"])

    sep_path = provider.bulk_dir / "SEP.parquet"
    if not sep_path.exists():
        raise FileNotFoundError(
            f"{sep_path} not found; run the SEP bulk archive before selecting the universe.")
    # Only the columns needed for a liquidity summary (keeps memory modest).
    sep = _pd.read_parquet(sep_path, columns=["ticker", "closeadj", "volume"])
    sep = sep[sep["ticker"].isin(common)]
    sep["dollar_volume"] = sep["closeadj"] * sep["volume"]
    grouped = sep.groupby("ticker")["dollar_volume"].agg(["median", "count"])
    liquid = grouped[(grouped["median"] >= min_dollar_volume) & (grouped["count"] >= MIN_HISTORY_BARS)]
    ranked = liquid.sort_values("median", ascending=False)
    return ranked.index[:max_symbols].tolist()


def load_sharadar_broad(provider=None, min_dollar_volume: float = SHARADAR_MIN_DOLLAR_VOLUME,
                        max_symbols: int | None = None, tickers: list[str] | None = None):
    """Load the survivorship-free US universe from Sharadar, INCLUDING delisted names.

    This is the real fix for survivorship bias. Unlike the hardcoded ``large/mid/small/
    broad`` lists above — which are TODAY's survivors — the symbol master comes from the
    point-in-time TICKERS table and KEEPS names that later delisted. A stock is in the
    universe on date ``t`` if it was listed and liquid as of ``t`` (it simply has price
    bars up to its delisting and none after), regardless of whether it survives to today;
    the liquidity filter is still applied as-of each rebalance date in the harness.

    ``tickers`` supplies an explicit symbol list (e.g. from
    :func:`select_liquid_sharadar_universe`); otherwise it falls back to the raw TICKERS
    order capped at ``max_symbols``. Returns ``(bars, filings_by_symbol, benchmark_close)``.
    Raises ``SharadarUnavailable`` in stub mode (no API key).
    """
    from data.sharadar_provider import SF1_FIELDS, SharadarProvider

    provider = provider or SharadarProvider()
    if tickers is None:
        tickers = provider.get_universe()["ticker"].tolist()  # includes delisted
        if max_symbols is not None:
            tickers = tickers[:max_symbols]

    raw_bars = provider.get_price_bars(list(tickers))
    bars: dict[str, pd.DataFrame] = {}
    for symbol, frame in raw_bars.items():
        if frame is None or len(frame) < MIN_HISTORY_BARS:
            continue
        # Liquidity floor on the FULL history (the harness re-checks as-of each date).
        if (frame["Close"] * frame["Volume"]).median() < min_dollar_volume:
            continue
        bars[symbol] = frame
    # Sharadar SEP is equities-only (no ETFs), so there is no SPY to use as the market.
    # Build an EQUAL-WEIGHTED index from the universe itself as the market proxy for the
    # beta / idiosyncratic-vol factors — fully offline and survivorship-free by construction.
    benchmark_close = equal_weight_index(bars)
    filings = provider.get_fundamentals(list(bars), SF1_FIELDS)
    log.info("Sharadar universe: %d tradable names (survivorship-free, delisted included).",
             len(bars))
    return bars, filings, benchmark_close


def equal_weight_index(bars: dict[str, pd.DataFrame]) -> pd.Series:
    """Equal-weighted daily-return price index across the universe (a market proxy).

    LOOK-AHEAD GUARD: each day's level uses only that day's cross-sectional mean return,
    so the series at t depends on no future data.
    """
    closes = pd.DataFrame({s: frame["Close"] for s, frame in bars.items()}).sort_index()
    market_return = closes.pct_change(fill_method=None).mean(axis=1)  # equal-weight daily return
    return (1.0 + market_return.fillna(0.0)).cumprod().rename("market")
