"""Central configuration for the swing trading agent.

Loads environment variables from a local .env file and exposes them as simple
module-level constants. Secrets live in .env (git-ignored) and are never
hardcoded here.
"""

import os

from dotenv import load_dotenv

# Read key=value pairs from .env into the process environment.
load_dotenv()


def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean from the environment, accepting common truthy strings."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# --- Data and news APIs ---
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")

# --- Interactive Brokers (paper) ---
IB_HOST = os.getenv("IB_HOST", "127.0.0.1")          # TWS/Gateway host
IB_PORT = int(os.getenv("IB_PORT", "7497"))          # 7497 = paper port (keep on paper)
IB_CLIENT_ID = int(os.getenv("IB_CLIENT_ID", "1"))   # API client id

# --- Agent behavior ---
# Autonomy gate mode (decision/autonomy.py):
#   signal_only   — alert only, place nothing
#   approve       — confirm EACH order y/N (fine for a few-name strategy)
#   approve_batch — review the whole book, ONE confirmation for all orders (portfolio
#                   strategies); unusually large orders still get an individual y/N
#   semi_auto / full_auto — not implemented yet
AUTONOMY_MODE = os.getenv("AUTONOMY_MODE", "approve_batch")

# Fraction of account equity risked per trade (1% default).
RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.01"))

# --- Signal generation ---
# Breakout entry: latest close must exceed the high of the prior N sessions.
BREAKOUT_LOOKBACK = int(os.getenv("BREAKOUT_LOOKBACK", "20"))

# Pullback entry: uptrend (close above MA), a recent dip to within TOUCH_PCT of the
# MA, then a confirmed bounce over the last BOUNCE_LOOKBACK sessions.
PULLBACK_MA = int(os.getenv("PULLBACK_MA", "50"))
PULLBACK_TOUCH_PCT = float(os.getenv("PULLBACK_TOUCH_PCT", "0.02"))
PULLBACK_BOUNCE_LOOKBACK = int(os.getenv("PULLBACK_BOUNCE_LOOKBACK", "3"))

# --- Risk and position sizing ---
# ATR (Average True Range) settings for volatility-based stops.
ATR_PERIOD = int(os.getenv("ATR_PERIOD", "14"))
ATR_MULTIPLE = float(os.getenv("ATR_MULTIPLE", "2.0"))
# Cap any single position at this fraction of account equity.
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "0.10"))

# --- Portfolio-level risk limits ---
# Cap all names within one sector at this fraction of equity.
MAX_SECTOR_PCT = float(os.getenv("MAX_SECTOR_PCT", "0.30"))
# Cap total deployed capital at this fraction of equity.
MAX_TOTAL_EXPOSURE = float(os.getenv("MAX_TOTAL_EXPOSURE", "0.60"))

# Minimum position size floor. A position is dropped only if it is below BOTH the
# share floor AND the value floor (a fraction of equity), which removes negligible
# 1-share "stub" positions left behind by portfolio trimming.
MIN_POSITION_SHARES = int(os.getenv("MIN_POSITION_SHARES", "10"))
MIN_POSITION_VALUE_PCT = float(os.getenv("MIN_POSITION_VALUE_PCT", "0.01"))

# --- Regime-based scaling (backtest entry/sizing filter) ---
# When enabled, the PRIOR day's market regime scales new entries (see
# backtest/engine.py). When disabled, the engine matches the unfiltered baseline.
REGIME_FILTER_ENABLED = _env_bool("REGIME_FILTER_ENABLED", True)
BEAR_SIZE_MULT = float(os.getenv("BEAR_SIZE_MULT", "0.5"))                    # half-size in bear
BEAR_MAX_TOTAL_EXPOSURE = float(os.getenv("BEAR_MAX_TOTAL_EXPOSURE", "0.30"))  # tighter total cap in bear
CRASH_BLOCK_NEW_ENTRIES = _env_bool("CRASH_BLOCK_NEW_ENTRIES", True)          # no new entries in crash

# --- Trend-riding exit (let winners run; backtest exit filter) ---
# Once a position is up >= 1 ATR, switch to a wider chandelier trail and also exit
# when the close breaks below the trend MA. When disabled, the standard trail is used.
TREND_EXIT_ENABLED = _env_bool("TREND_EXIT_ENABLED", True)
TREND_EXIT_MA = int(os.getenv("TREND_EXIT_MA", "50"))                  # exit when close breaks below this MA
CHANDELIER_ATR_MULT = float(os.getenv("CHANDELIER_ATR_MULT", "3.0"))   # trail = highest high since entry - mult*ATR

# --- Conviction sizing (bet bigger on the best setups) ---
# Scales RISK_PER_TRADE by a per-trade multiplier in [MIN, MAX] before the caps,
# so the strongest setups risk more (but the per-name and portfolio caps still bind).
CONVICTION_SIZING_ENABLED = _env_bool("CONVICTION_SIZING_ENABLED", True)
CONVICTION_MAX_MULT = float(os.getenv("CONVICTION_MAX_MULT", "2.0"))   # strongest setups up to 2x base risk
CONVICTION_MIN_MULT = float(os.getenv("CONVICTION_MIN_MULT", "0.5"))   # weakest qualifying setups down to 0.5x

# --- Earnings catalyst (post-earnings-announcement drift) ---
# Enter AFTER a positive-surprise report; never hold through the announcement.
EARNINGS_SURPRISE_MIN = float(os.getenv("EARNINGS_SURPRISE_MIN", "0.05"))      # >=5% positive EPS surprise
EARNINGS_ENTRY_DELAY_DAYS = int(os.getenv("EARNINGS_ENTRY_DELAY_DAYS", "1"))   # signal session(s) AFTER the report
EARNINGS_CONFIRM_UP = _env_bool("EARNINGS_CONFIRM_UP", True)                   # post-report session must close up
EARNINGS_BACKTEST_YEARS = int(os.getenv("EARNINGS_BACKTEST_YEARS", "7"))       # earnings-data window (free depth limited)

# Earnings blackout: never ENTER a new position within this many sessions before a
# known upcoming report (so price strategies don't hold through an announcement).
EARNINGS_BLACKOUT_ENABLED = _env_bool("EARNINGS_BLACKOUT_ENABLED", True)
EARNINGS_BLACKOUT_DAYS = int(os.getenv("EARNINGS_BLACKOUT_DAYS", "2"))

# --- Liquidity filter (so we don't model trades we couldn't fill) ---
MIN_DOLLAR_VOLUME = float(os.getenv("MIN_DOLLAR_VOLUME", "5000000"))        # >= $5M avg daily $ volume
MIN_PRICE = float(os.getenv("MIN_PRICE", "5.0"))                           # avoid sub-$5 names
LIQUIDITY_LOOKBACK = int(os.getenv("LIQUIDITY_LOOKBACK", "20"))
MAX_ADV_PARTICIPATION = float(os.getenv("MAX_ADV_PARTICIPATION", "0.01"))  # cap a position at 1% of ADV

# --- Realistic per-side slippage (basis points) by size tier ---
# Less-liquid tiers cost more to trade; applied on both entry and exit.
SLIPPAGE_BPS_LARGE = float(os.getenv("SLIPPAGE_BPS_LARGE", "5"))
SLIPPAGE_BPS_MID = float(os.getenv("SLIPPAGE_BPS_MID", "15"))
SLIPPAGE_BPS_SMALL = float(os.getenv("SLIPPAGE_BPS_SMALL", "40"))

# --- Index overlay (deploy idle cash into the market instead of holding cash) ---
OVERLAY_ENABLED = _env_bool("OVERLAY_ENABLED", False)
OVERLAY_INSTRUMENT = os.getenv("OVERLAY_INSTRUMENT", "SPY")

# --- Vol-managed momentum strategy (the validated, walk-forward-tested portfolio) ---
# 12-1 momentum (skip the last month) ranked cross-sectionally on the survivorship-free
# liquid universe, formed monthly as a top/bottom-decile portfolio, then scaled to a
# target volatility (Barroso & Santa-Clara 2015) to tame the momentum crash. All values
# are research defaults; none were tuned to the result (vol target is set from the TRAIN
# window only, never the test window).
VMM_VOL_WINDOW = int(os.getenv("VMM_VOL_WINDOW", "12"))            # trailing months for realized vol
VMM_LEVERAGE_CAP = float(os.getenv("VMM_LEVERAGE_CAP", "2.0"))     # max gross leverage from vol scaling
VMM_DECILES = int(os.getenv("VMM_DECILES", "10"))                 # sort into this many buckets
VMM_COST_BPS_PER_SIDE = float(os.getenv("VMM_COST_BPS_PER_SIDE", "15"))  # per-side cost on actual turnover
VMM_STOP_PCT = float(os.getenv("VMM_STOP_PCT", "0.20"))           # stop width for size_position translation
VMM_LONG_ONLY = _env_bool("VMM_LONG_ONLY", False)                 # paper accounts that can't short set this True

# --- Execution safety ---
# When True, the decision runner prints proposals but places no orders.
DRY_RUN = _env_bool("DRY_RUN", False)

# Orders are placed as LIMIT orders priced off the reference price (a limit carries a
# price, so IBKR does not block it for lacking a live market-data subscription — the
# Error 354 case — and it gives price protection on volatile momentum names). The limit
# sits this fraction beyond the reference so the order is marketable but protected:
# BUY at ref*(1+buffer), SELL at ref*(1-buffer). 0.5% default.
LIMIT_BUFFER = float(os.getenv("LIMIT_BUFFER", "0.005"))

# Batch approval (AUTONOMY_MODE="approve_batch"): one confirmation for the whole book,
# EXCEPT unusually large orders, which STILL get an individual y/N. An order is "large"
# if its value exceeds this multiple of the average order value, or an absolute $ cap
# (BATCH_MAX_ORDER_VALUE=0 disables the absolute cap).
BATCH_OUTLIER_MULT = float(os.getenv("BATCH_OUTLIER_MULT", "2.0"))
BATCH_MAX_ORDER_VALUE = float(os.getenv("BATCH_MAX_ORDER_VALUE", "0"))

# --- Capstone research surface (read by main.py) ---
# Universe for `research` / `backtest`: large | mid | small | broad. Default "large"
# reproduces the canonical numbers; the lists live in backtest/universe.py.
RESEARCH_UNIVERSE = os.getenv("RESEARCH_UNIVERSE", "large")
# Strategies active in the canonical backtest, by registered name (see strategies/).
ACTIVE_STRATEGIES = tuple(s.strip() for s in os.getenv("ACTIVE_STRATEGIES", "breakout,pullback").split(",") if s.strip())
# Research/backtest window: price backtests use the full cached history (~15y, set by
# backtest/data.HISTORY_YEARS); factor & earnings tests use EARNINGS_BACKTEST_YEARS above.
