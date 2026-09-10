"""Central configuration for the point-in-time backtesting & validation platform.

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


# --- Interactive Brokers (paper) ---
IB_HOST = os.getenv("IB_HOST", "127.0.0.1")          # TWS/Gateway host
IB_PORT = int(os.getenv("IB_PORT", "7497"))          # 7497 = paper port (keep on paper)
IB_CLIENT_ID = int(os.getenv("IB_CLIENT_ID", "1"))   # API client id

# --- Autonomy gate (decision/autonomy.py) ---
#   signal_only   — alert only, place nothing
#   approve       — confirm EACH order y/N
#   approve_batch — review the whole book, ONE confirmation for all orders; unusually
#                   large orders still get an individual y/N
#   semi_auto / full_auto — not implemented yet
AUTONOMY_MODE = os.getenv("AUTONOMY_MODE", "approve_batch")

# Cap any single position at this fraction of account equity.
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "0.10"))

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
