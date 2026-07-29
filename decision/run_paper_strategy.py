"""Monthly paper rebalance for the long-only vol-managed momentum strategy.

This is MONITORED PAPER VALIDATION of the walk-forward survivor (long-only vol-managed
momentum: positive Sharpe in 9/9 windows, market-beating on return 5/6, ~flat through the
GFC) — a modest, robust-but-not-Sharpe-dominant strategy. We deploy it to a PAPER account
to accumulate live forward evidence; this is NOT a confirmed edge.

Every order goes through the EXISTING safety path, unchanged:
  * the DU-account guard (``assert_paper_account``) refuses any non-paper account,
  * ``config.DRY_RUN`` short-circuits to print-only,
  * the autonomy gate (``run_gate``) confirms each trade (``approve`` / ``signal_only``).
No guard is bypassed or weakened here.

    python main.py paper vol_managed_momentum                # real cycle (needs TWS/Gateway)
    python main.py paper vol_managed_momentum --no-broker    # offline preview (places nothing)

Run monthly (manually, or via a scheduler later).
"""

import logging
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import config
from decision.autonomy import assert_paper_account, run_gate
from execution.broker import IBBroker
from factors.run_factor_eval import build_sharadar_factor_data
from storage.database import load_paper_track_record, record_paper_rebalance
from strategies import vol_managed_momentum  # noqa: F401  (registers the portfolio strategy)
from strategies.registry import get_portfolio, portfolio_strategies

log = logging.getLogger("run_paper_strategy")

SPY_CACHE = Path(__file__).resolve().parent.parent / "backtest" / "cache" / "ohlcv" / "SPY.parquet"
PREVIEW_EQUITY = 1_000_000.0   # assumed equity for an offline (--no-broker) preview only


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    for noisy in ("ib_async", "data.sharadar_provider", "yfinance"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def diff_to_orders(target: dict[str, int], current_positions: list[dict],
                   prices: pd.Series) -> list[dict]:
    """Orders to move from current paper positions to the target book.

    Returns gate-compatible proposal dicts (the existing autonomy gate's format), one per
    name whose share delta is non-zero. Long-only: names not in the target are fully sold.
    """
    current = {p["symbol"]: int(p["shares"]) for p in current_positions}
    # Per-name reference price for the LIMIT order: prefer the cached close; for a held
    # name missing from the cache (e.g. one that dropped out of the universe), fall back
    # to its current per-share market value so every exit still gets a protected limit.
    pos_price = {p["symbol"]: abs(float(p["market_value"]) / p["shares"])
                 for p in current_positions if p.get("shares") and p.get("market_value")}

    orders: list[dict] = []
    for symbol in sorted(set(target) | set(current)):
        delta = int(target.get(symbol, 0)) - int(current.get(symbol, 0))
        if delta == 0:
            continue
        cached = float(prices.get(symbol)) if symbol in getattr(prices, "index", []) else None
        price = cached if cached and not np.isnan(cached) else pos_price.get(symbol, 0.0)
        orders.append({
            "symbol": symbol, "action": "BUY" if delta > 0 else "SELL", "shares": abs(delta),
            # The gate prices a LIMIT off entry_ref; momentum has no per-name ATR stop, so
            # the risk fields are 0 (the per-name cap is applied in sizing, not here).
            "entry_ref": round(price, 4), "stop": 0.0, "atr": 0.0,
            "risk_dollars": 0.0, "est_value": round(abs(delta) * price, 2),
        })
    return orders


def latest_spy_level() -> float | None:
    """Latest cached SPY close, for the paper-vs-SPY track record (offline)."""
    if not SPY_CACHE.exists():
        return None
    spy = pd.read_parquet(SPY_CACHE)["Close"].dropna()
    return float(spy.iloc[-1]) if len(spy) else None


def print_track_record(strategy_name: str) -> None:
    """Print the cumulative live paper NAV vs SPY since the first live rebalance."""
    record = load_paper_track_record(strategy_name)
    print("\n=== Cumulative LIVE paper track record (vs SPY) ===")
    if record.empty or len(record) < 1:
        print("  No live NAV points recorded yet — this accumulates one row per monthly cycle.")
        return
    first = record.iloc[0]
    last = record.iloc[-1]
    strat_ret = last["equity"] / first["equity"] - 1.0 if first["equity"] else None
    spy_ret = (last["spy_level"] / first["spy_level"] - 1.0
               if first["spy_level"] and last["spy_level"] else None)
    print(f"  Since {first['run_timestamp'][:10]} ({len(record)} cycle(s)):")
    print(f"    Paper NAV : {_pct(strat_ret)}   (${first['equity']:,.0f} -> ${last['equity']:,.0f})")
    print(f"    SPY       : {_pct(spy_ret)}")
    if strat_ret is not None and spy_ret is not None:
        print(f"    Excess    : {_pct(strat_ret - spy_ret)} vs SPY so far.")


def _pct(v):
    return f"{v * 100:+.2f}%" if v is not None else "n/a"


def run_paper_cycle(strategy_name: str, *, use_broker: bool = True,
                    equity_override: float = PREVIEW_EQUITY) -> int:
    """Run one monthly paper-rebalance cycle through the existing safety path."""
    configure_logging()
    if strategy_name not in portfolio_strategies():
        log.error("Unknown portfolio strategy %r.", strategy_name)
        return 1
    # Deploy the LONG-ONLY variant (the walk-forward survivor), regardless of config default.
    strategy = get_portfolio(strategy_name)(long_only=True)

    broker, live, account_id = None, False, None
    equity, positions = equity_override, []
    if use_broker:
        broker = IBBroker()
        try:
            broker.connect()
            equity = broker.get_account_summary().get("net_liquidation", equity_override)
            account_id = assert_paper_account(broker)   # DU GUARD — raises on a non-paper account
            positions = broker.get_positions()
            live = True
        except (ConnectionError, RuntimeError) as error:
            log.error("Broker path unavailable: %s", error)
            print(f"\nCould not run the live paper cycle: {error}\n"
                  "Start TWS/IB Gateway on the paper port, or use --no-broker for an offline preview.")
            broker.disconnect()
            return 1

    # --- compute the target book (look-ahead-safe momentum + vol scaling) ----------
    log.info("Loading survivorship-free universe (offline cache) and computing target...")
    data, eligible = build_sharadar_factor_data()
    book, gross_weight, asof = strategy.target_book(equity, data, eligible)
    prices = data.close.loc[asof]
    orders = diff_to_orders(book, positions, prices)
    spy_level = latest_spy_level()
    run_ts = datetime.now(timezone.utc).isoformat()

    _print_header(strategy_name, live)
    _print_guard_status(account_id, live)
    _print_target(book, gross_weight, equity, asof, prices)
    _print_orders(orders, positions)

    # --- route through the EXISTING guards, unchanged ------------------------------
    if not use_broker:
        print("\nPREVIEW (--no-broker): nothing is placed. The DU-account guard runs only on a "
              "live connection; with a broker, DRY_RUN/autonomy gate apply as below.")
    elif config.DRY_RUN:
        log.info("DRY_RUN is True: printing only, placing nothing.")
        print("\nDRY_RUN=True -> printed only, placed nothing. Set DRY_RUN=False to arm the autonomy gate.")
    else:
        run_gate(orders, broker)   # approve/signal_only; re-checks DU + DRY_RUN before any order

    # --- log for live evidence + show the cumulative record ------------------------
    record_paper_rebalance(strategy_name, run_ts, str(asof.date()), gross_weight, equity,
                           config.AUTONOMY_MODE, config.DRY_RUN, live, spy_level, book, orders)
    print_track_record(strategy_name)

    if broker is not None:
        broker.disconnect()
    return 0


def _print_header(strategy_name: str, live: bool) -> None:
    print("\n" + "=" * 84)
    print(f"MONITORED PAPER VALIDATION — {strategy_name} (LONG-ONLY vol-managed momentum)")
    print("=" * 84)
    print("This is forward paper validation of a MODEST, robust-but-not-Sharpe-dominant strategy")
    print("(walk-forward: positive Sharpe 9/9 windows, beat market on return 5/6, flat through the")
    print("GFC). Collecting live evidence — NOT a confirmed edge. " + ("LIVE broker." if live else "Offline preview."))


def _print_guard_status(account_id: str | None, live: bool) -> None:
    print("\n--- Safety-guard status (unchanged existing guards) ---")
    print(f"  DU-account guard : {('PASSED ' + account_id) if account_id else 'deferred (no live connection)'}")
    print(f"  DRY_RUN          : {config.DRY_RUN}  ({'print-only, nothing placed' if config.DRY_RUN else 'ARMED — gate will prompt'})")
    print(f"  Autonomy mode    : {config.AUTONOMY_MODE}  "
          f"(safest first; step up later: DRY_RUN=False + approve -> semi_auto/full_auto when proven)")


def _print_target(book: dict[str, int], gross_weight: float, equity: float,
                  asof: pd.Timestamp, prices: pd.Series) -> None:
    invested = sum(sh * float(prices.get(s, 0.0)) for s, sh in book.items())
    print(f"\n--- Target portfolio (as of {asof.date()}; equity ${equity:,.0f}) ---")
    print(f"  Vol-scaling gross exposure: {gross_weight*100:.0f}% (Barroso-Santa-Clara), "
          f"{len(book)} names, ${invested:,.0f} invested (~{invested/equity*100:.0f}% of equity).")
    if book:
        rows = [{"symbol": s, "target_shares": sh, "price": round(float(prices.get(s, 0.0)), 2),
                 "est_value": round(sh * float(prices.get(s, 0.0)), 0),
                 "weight_%": round(sh * float(prices.get(s, 0.0)) / equity * 100, 2)}
                for s, sh in sorted(book.items())]
        print(pd.DataFrame(rows).head(15).to_string(index=False))
        if len(rows) > 15:
            print(f"  ... and {len(rows) - 15} more names.")


def _print_orders(orders: list[dict], positions: list[dict]) -> None:
    print(f"\n--- Orders to reach target (from {len(positions)} current position(s)) ---")
    if orders:
        rows = [{"symbol": o["symbol"], "action": o["action"], "shares": o["shares"],
                 "est_value": o["est_value"]} for o in orders]
        print(pd.DataFrame(rows).to_string(index=False))
        print(f"  {sum(o['action']=='BUY' for o in orders)} buys, {sum(o['action']=='SELL' for o in orders)} sells.")
    else:
        print("  (already at target — no orders)")
