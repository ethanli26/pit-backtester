#!/usr/bin/env python3
"""
V1 quality gate — screens the local Sharadar Core US Equities bundle with DuckDB.

Gate (do not weaken):
  * ROIC >= 15%
  * FCF positive AND growing YoY
  * Revenue growing YoY
  * Net debt / EBITDA < 2x   (net debt = debt - cash & equivalents)
  * Market cap in the $1B .. $100B band
  * Financials and Real Estate excluded

Writes v1_candidates.csv and prints how many names cleared the gate + the top 25.
"""
from pathlib import Path

import duckdb
import pandas as pd

# ============================= CONFIG =============================
# Whole-table Sharadar parquet archives (one file per table).
BULK = Path(__file__).resolve().parent / "data" / "cache" / "sharadar" / "bulk"
SF1_PATH     = str(BULK / "SF1.parquet")      # Core US Fundamentals
DAILY_PATH   = str(BULK / "DAILY.parquet")    # Daily market cap / valuation ratios
SEP_PATH     = str(BULK / "SEP.parquet")      # Daily prices (adjusted close)
TICKERS_PATH = str(BULK / "TICKERS.parquet")  # Ticker metadata (sector, table)

MCAP_MIN = 1_000_000_000      # $1B floor
MCAP_MAX = 100_000_000_000    # $100B ceiling — exclude mega-caps held via index funds

DIMENSION          = "ARY"    # as-reported annual (no restatement; LAG => prior fiscal year)
ROIC_MIN           = 0.15     # 15% (Sharadar roic is a decimal ratio)
MAX_NETDEBT_EBITDA = 2.0
EXCLUDE_SECTORS    = ("Financial Services", "Real Estate")

# Universe filter (NOT part of the quality gate) — restrict to currently-listed
# names so the screen returns buyable candidates, not delisted/acquired shells
# still passing on their final-year financials.
REQUIRE_ACTIVE      = True
MIN_LAST_PRICE_DATE = "2026-06-01"   # keep tickers priced on/after this date

MOM_SKIP_DAYS     = 21    # 12-1 momentum: skip the most recent ~1 month
MOM_LOOKBACK_DAYS = 252   # ...measured over the ~12 months before that

OUT_CSV = "v1_candidates.csv"
TOP_N   = 25
# =================================================================

_UNIVERSE_CLAUSE = (
    f"AND isdelisted = 'N' AND TRY_CAST(lastpricedate AS DATE) >= DATE '{MIN_LAST_PRICE_DATE}'"
    if REQUIRE_ACTIVE else ""
)

QUERY = f"""
WITH sf1_annual AS (
    -- one row per (ticker, fiscal year): keep the latest-filed version so the
    -- ROW_NUMBER/LAG below are deterministic (ARY has restated/reused-ticker dupes)
    SELECT *
    FROM read_parquet('{SF1_PATH}')
    WHERE dimension = '{DIMENSION}'
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY ticker, calendardate ORDER BY datekey DESC, lastupdated DESC
    ) = 1
),
fund_ranked AS (
    -- annual as-reported fundamentals, with prior fiscal year pulled in via LAG
    SELECT
        ticker,
        calendardate,
        datekey,
        roic,
        revenue,
        fcf,
        fxusd,                                                               -- local units per USD
        ebitda,
        (debt - cashneq)                                                     AS netdebt,
        ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY calendardate DESC)   AS rn,
        LAG(revenue) OVER (PARTITION BY ticker ORDER BY calendardate)        AS revenue_prior,
        LAG(fcf)     OVER (PARTITION BY ticker ORDER BY calendardate)        AS fcf_prior
    FROM sf1_annual
),
fund AS (                                   -- latest fiscal year per ticker
    SELECT
        *,
        CASE WHEN ebitda > 0 THEN netdebt / ebitda END AS netdebt_ebitda
    FROM fund_ranked
    WHERE rn = 1
),
daily_latest AS (                           -- current valuation (DAILY mktcap/ev are $mm -> $)
    SELECT ticker,
           arg_max(marketcap, date) * 1e6 AS marketcap,
           arg_max(ev, date)        * 1e6 AS ev,
           arg_max(pe, date)              AS pe
    FROM read_parquet('{DAILY_PATH}')
    GROUP BY ticker
),
price_latest AS (                           -- latest adjusted close
    SELECT ticker, arg_max(closeadj, date) AS closeadj
    FROM read_parquet('{SEP_PATH}')
    GROUP BY ticker
),
meta AS (                                   -- sector/name for fundamental-covered equities
    SELECT ticker, name, sector, industry, isdelisted, lastpricedate
    FROM read_parquet('{TICKERS_PATH}')
    WHERE "table" = 'SF1'
),
joined AS (
    SELECT
        f.ticker,
        m.name,
        m.sector,
        m.industry,
        m.isdelisted,
        m.lastpricedate,
        d.marketcap,
        d.ev,
        d.pe,
        p.closeadj,
        f.roic,
        f.revenue,
        f.revenue_prior,
        f.fcf,
        f.fcf_prior,
        f.fxusd,
        f.ebitda,
        f.netdebt,
        f.netdebt_ebitda,
        f.calendardate,
        f.datekey
    FROM fund f
    JOIN meta         m ON f.ticker = m.ticker
    JOIN daily_latest d ON f.ticker = d.ticker
    LEFT JOIN price_latest p ON f.ticker = p.ticker
),
passed AS (
    SELECT *
    FROM joined
    WHERE roic >= {ROIC_MIN}
      AND fcf > 0 AND fcf > fcf_prior                       -- positive & growing FCF
      AND revenue > revenue_prior                           -- positive revenue growth
      AND ebitda > 0 AND netdebt_ebitda < {MAX_NETDEBT_EBITDA}  -- leverage
      AND sector NOT IN {EXCLUDE_SECTORS}                   -- ex-Financials/Real Estate
      AND marketcap >= {MCAP_MIN} AND marketcap <= {MCAP_MAX}
      {_UNIVERSE_CLAUSE}                                    -- currently-listed only
)
SELECT * FROM passed
ORDER BY roic DESC
"""


def momentum(tickers) -> "duckdb.DuckDBPyRelation":
    """12-1 price momentum (%) per ticker from SEP adjusted closes.

    Ranks each ticker's sessions newest-first: rn=1 is the latest close, so the
    close ~1 month back is rn = MOM_SKIP_DAYS+1 and ~12 months back is
    rn = MOM_LOOKBACK_DAYS+1. Momentum = P(1m ago) / P(12m ago) - 1.
    """
    p_1m  = MOM_SKIP_DAYS + 1
    p_12m = MOM_LOOKBACK_DAYS + 1
    ticker_list = ", ".join("'" + t.replace("'", "''") + "'" for t in tickers)
    q = f"""
    WITH ranked AS (
        SELECT ticker, closeadj,
               ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY date DESC) AS rn
        FROM read_parquet('{SEP_PATH}')
        WHERE ticker IN ({ticker_list})
    )
    SELECT
        ticker,
        (MAX(CASE WHEN rn = {p_1m}  THEN closeadj END)
         / NULLIF(MAX(CASE WHEN rn = {p_12m} THEN closeadj END), 0) - 1) * 100
            AS mom_12_1_pct
    FROM ranked
    WHERE rn IN ({p_1m}, {p_12m})
    GROUP BY ticker
    """
    return duckdb.sql(q).df()


def pct_rank(s, ascending=True):
    """Cross-sectional percentile rank in 0-100; NaNs stay NaN (skipped later)."""
    return s.rank(pct=True, ascending=ascending) * 100.0


def main() -> None:
    df = duckdb.sql(QUERY).df()

    n = len(df)
    print(f"V1 quality gate: {n} names cleared "
          f"(ROIC>=15%, +growing FCF, +rev growth, netdebt/EBITDA<2x, "
          f"${MCAP_MIN/1e9:.0f}B-${MCAP_MAX/1e9:.0f}B cap, ex-Fin/RE"
          f"{'; currently-listed only' if REQUIRE_ACTIVE else ''}).\n")

    # ---- derived factor columns -------------------------------------------
    df["mcap_bn"]        = df["marketcap"] / 1e9
    df["roic_pct"]       = df["roic"] * 100.0
    rev_prior            = df["revenue_prior"].where(df["revenue_prior"] > 0)  # avoid inf
    df["rev_growth_pct"] = (df["revenue"] / rev_prior - 1.0) * 100.0
    # FCF is in reporting currency; marketcap/ev are USD -> convert (fxusd = local/USD)
    fcf_usd              = df["fcf"] / df["fxusd"].where(df["fxusd"] > 0)
    df["fcf_yield"]      = fcf_usd / df["marketcap"] * 100.0             # sanity check
    df["ev_fcf"]         = df["ev"] / fcf_usd
    df = df.merge(momentum(df["ticker"].tolist()), on="ticker", how="left")

    # ---- rank-percentile composite (3 equal buckets) ----------------------
    # sign each factor so higher = better; lower is better for pe/ev_fcf/leverage.
    pe_pos   = df["pe"].where(df["pe"] > 0)          # negative/undefined P/E -> excluded
    evfcf_ok = df["ev_fcf"].where(df["ev_fcf"] > 0)  # negative EV/FCF -> excluded
    quality = pd.concat([
        pct_rank(df["roic_pct"]),
        pct_rank(df["rev_growth_pct"]),
        pct_rank(df["netdebt_ebitda"], ascending=False),
    ], axis=1).mean(axis=1)
    value = pd.concat([
        pct_rank(df["fcf_yield"]),
        pct_rank(evfcf_ok, ascending=False),
        pct_rank(pe_pos,   ascending=False),
    ], axis=1).mean(axis=1)   # skipna: names missing P/E still get a value score
    mom = pct_rank(df["mom_12_1_pct"])
    df["composite"] = pd.concat([quality, value, mom], axis=1).mean(axis=1)

    df = df.sort_values("composite", ascending=False).reset_index(drop=True)

    n_no_mom = int(df["mom_12_1_pct"].isna().sum())
    if n_no_mom:
        print(f"Note: {n_no_mom} names lack {MOM_LOOKBACK_DAYS}+ sessions of price "
              f"history -> momentum NaN (composite uses quality+value for those).\n")

    df.to_csv(OUT_CSV, index=False)
    print(f"Wrote {OUT_CSV} ({n} rows).\n")

    if n:
        cols = ["ticker", "sector", "mcap_bn", "roic_pct", "rev_growth_pct",
                "netdebt_ebitda", "ev_fcf", "pe", "mom_12_1_pct", "composite",
                "fcf_yield"]
        show = df.head(TOP_N)[cols].round(
            {"mcap_bn": 2, "roic_pct": 1, "rev_growth_pct": 1, "netdebt_ebitda": 2,
             "ev_fcf": 1, "pe": 1, "mom_12_1_pct": 1, "composite": 1, "fcf_yield": 2})
        print(f"Top {min(TOP_N, n)} by composite:")
        print(show.to_string(index=False, max_colwidth=24))


if __name__ == "__main__":
    main()
