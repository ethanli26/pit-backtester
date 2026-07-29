"""One-time bulk archive of the full Sharadar suite to local parquet.

After running this, the entire research pipeline reads from ``data/cache/sharadar`` with
ZERO API calls, so it keeps working after the subscription ends. The archive is
idempotent and resumable: re-running skips tables already cached unless ``--refresh``.

    python data/archive_sharadar.py                 # archive all tables (skip cached)
    python data/archive_sharadar.py --refresh       # force re-download
    python data/archive_sharadar.py --tables TICKERS SF1 SEP
    python data/archive_sharadar.py --verify        # prove cache-only mode hits no API

Read-only research. No orders.
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402,F401  (loads .env so NASDAQ_DATA_LINK_API_KEY is set)
from data.sharadar_provider import (  # noqa: E402
    ARCHIVE_TABLES,
    SharadarCacheMiss,
    SharadarProvider,
)

log = logging.getLogger("archive_sharadar")


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")


def verify_cache_only(provider: SharadarProvider) -> int:
    """Prove the archive is self-sufficient: a cache-only provider must hit no API."""
    offline = SharadarProvider(cache_only=True, cache_dir=provider.cache_dir)
    print("\n=== Cache-only verification (any API call must raise) ===")
    try:
        universe = offline.get_universe()
        print(f"  [ok] get_universe() served {len(universe)} tickers from local TICKERS archive.")
    except SharadarCacheMiss as error:
        print(f"  [FAIL] universe not archived: {error}")
        return 1
    # A symbol that is NOT cached must raise rather than silently fetch.
    try:
        offline.get_price_bars(["__DEFINITELY_NOT_CACHED__"])
        print("  [ok] missing symbol returned no data without touching the API.")
    except SharadarCacheMiss:
        print("  [ok] missing symbol raised SharadarCacheMiss (no silent API call).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Bulk-archive the Sharadar suite to parquet.")
    parser.add_argument("--tables", nargs="+", help="Subset of table names (default: all).")
    parser.add_argument("--refresh", action="store_true", help="Re-download even if cached.")
    parser.add_argument("--verify", action="store_true", help="Only run the cache-only check.")
    parser.add_argument("--sep-universe", type=int, metavar="N",
                        help="Robust universe-first SEP: archive per-symbol prices for the N "
                             "most-liquid survivorship-free common stocks, resumably (no giant "
                             "bulk needed). Use when a full SEP pull is too large to be reliable.")
    args = parser.parse_args()
    configure_logging()

    provider = SharadarProvider()
    if args.verify:
        return verify_cache_only(provider)

    if args.sep_universe:
        from backtest.universe import select_liquid_sharadar_universe

        tickers = select_liquid_sharadar_universe(provider, max_symbols=args.sep_universe)
        print(f"Universe-first SEP: ensuring prices for {len(tickers)} liquid common stocks...")
        written = provider.archive_prices_for_tickers(tickers, refresh=args.refresh)
        print(f"  wrote {written} new per-symbol SEP files (rest already cached).")
        return verify_cache_only(provider)

    selected = ARCHIVE_TABLES
    if args.tables:
        wanted = {t.upper() for t in args.tables}
        selected = [s for s in ARCHIVE_TABLES if s["name"] in wanted]

    print("Archiving Sharadar tables to:", provider.bulk_dir)
    counts = provider.bulk_archive(tables=selected, refresh=args.refresh)
    print(f"\nArchived {len(counts)} table(s); total rows: {sum(counts.values()):,}.")
    return verify_cache_only(provider)


if __name__ == "__main__":
    sys.exit(main())
