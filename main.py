"""pit-backtester — single integrated entry point.

One CLI over the point-in-time research/validation pipeline. Each subcommand is a
thin wrapper that reuses the existing modules — no strategy logic, guard, or result
is changed here:

  factors   — factor IC evaluation scorecard (price + point-in-time fundamentals,
              large vs broad universe, survivorship-free Sharadar run).
  altdata   — the insider (SF2) / institutional (SF3) factor gauntlet: IC bar,
              walk-forward consistency, and momentum-diversification check.
  strategy  — a registered portfolio strategy's OOS benchmark comparison +
              walk-forward validation (e.g. vol_managed_momentum).
  paper     — one monthly paper rebalance for a portfolio strategy, through the
              existing DU-account guard + DRY_RUN + autonomy gate.

config.py is the single config surface (risk params, autonomy mode, DRY_RUN, vol-
target windows). At startup a self-check asserts the paper-safety guards (DU-account
check, DRY_RUN) are intact. Read-only research + the existing paper path; no new
alpha.

    python main.py factors
    python main.py altdata
    python main.py strategy vol_managed_momentum
    python main.py paper vol_managed_momentum --no-broker
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd  # noqa: E402

import config  # noqa: E402

log = logging.getLogger("main")


def configure_logging() -> None:
    """Timestamped logs; quiet the chatty libraries."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    for noisy in ("yfinance", "ib_async"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _pct(v):
    return f"{v * 100:+.2f}%" if v is not None else "n/a"


def _ratio(v):
    return f"{v:.2f}" if v is not None else "n/a"


def paper_safety_self_check() -> None:
    """Assert the paper-safety guards hold before dispatching any subcommand."""
    from decision.autonomy import is_paper_account  # the DU-account guard

    assert is_paper_account("DU1234567") and not is_paper_account("U1234567") and not is_paper_account(None)
    assert isinstance(config.DRY_RUN, bool)
    print("=== Paper-safety self-check ===")
    print("  DU-account guard   : DU accepted, U/None rejected  OK")
    print(f"  Paper safety       : DRY_RUN={config.DRY_RUN}, AUTONOMY_MODE={config.AUTONOMY_MODE}")
    print()


# --- Subcommands ------------------------------------------------------------------

def cmd_factors(args) -> int:
    """Factor-evaluation harness scorecard (price + point-in-time fundamentals), unchanged."""
    from factors.run_factor_eval import main as run_factor_main
    return run_factor_main()


def cmd_altdata(args) -> int:
    """Insider (SF2) / institutional (SF3) factor gauntlet, unchanged."""
    from factors.run_altdata_eval import main as run_altdata_main
    return run_altdata_main()


def cmd_strategy(args) -> int:
    """Run a portfolio strategy's backtest + benchmark + walk-forward, headline first."""
    from backtest import walkforward
    from factors.evaluate import _rebalance_dates
    from factors.run_factor_eval import build_sharadar_factor_data
    from research.combine_train import (OOS_FRACTION, SPY_CACHE, _monthly_metrics,
                                        spy_monthly_returns, vol_matched_blend)
    from strategies.registry import get_portfolio, portfolio_strategies

    name = args.name
    if name not in portfolio_strategies():
        log.error("Unknown portfolio strategy '%s'. Available: %s", name, ", ".join(portfolio_strategies()) or "none")
        return 1
    strategy = get_portfolio(name)()
    variant = "long-only" if strategy.long_only else "long/short"

    log.info("Loading survivorship-free Sharadar universe (offline cache)...")
    data, eligible = build_sharadar_factor_data()
    close = data.close
    rebal = _rebalance_dates(close.index, "M")
    split = rebal[int(len(rebal) * (1.0 - OOS_FRACTION))]

    # OOS (last 30%) strategy returns + passive, after costs.
    oos_ret = strategy.portfolio_returns(data, eligible, train_end=split, start=split)
    m_oos = _monthly_metrics(oos_ret)
    spy_close = pd.read_parquet(SPY_CACHE)["Close"]
    pairs = [(c, n) for c, n in zip(rebal[:-1], rebal[1:]) if c >= split]
    spy_oos = spy_monthly_returns(pairs, spy_close).reindex(oos_ret.index).dropna()
    m_spy = _monthly_metrics(spy_oos)
    m_blend = _monthly_metrics(vol_matched_blend(spy_oos, target_vol=oos_ret.std(ddof=1)))

    # Walk-forward across non-overlapping windows.
    wf = walkforward.run_walkforward(strategy, data, eligible)
    spy_rows = [r for r in wf["windows"] if r["spy"]]
    beat_spy = sum(1 for r in spy_rows if (r["metrics"]["sharpe"] or -9) > (r["spy"]["sharpe"] or -9))
    beats_oos = (m_oos["sharpe"] or -9) > (m_spy["sharpe"] or -9) and (m_oos["sharpe"] or -9) > (m_blend["sharpe"] or -9)
    consistent = spy_rows and beat_spy * 2 >= len(spy_rows)

    # --- HEADLINE FIRST -----------------------------------------------------------
    print("\n" + "=" * 78)
    print(f"HEADLINE — {name} ({variant}), survivorship-free, after costs")
    print("=" * 78)
    print(f"  OOS (last {int(OOS_FRACTION*100)}%): Sharpe {_ratio(m_oos['sharpe'])} vs SPY {_ratio(m_spy['sharpe'])} "
          f"vs vol-matched blend {_ratio(m_blend['sharpe'])}  -> {'BEATS' if beats_oos else 'TRAILS'} passive risk-adjusted.")
    print(f"  Walk-forward: beat SPY on Sharpe in {beat_spy}/{len(spy_rows)} non-overlapping windows "
          f"(SPY cache covers post-2011 windows).")
    print(f"  Crash control: OOS maxDD {_pct(m_oos['max_drawdown'] and -m_oos['max_drawdown'])} "
          f"vs SPY {_pct(m_spy['max_drawdown'] and -m_spy['max_drawdown'])}.")
    print(f"  => {'CONSISTENT across walk-forward windows.' if consistent else 'NOT consistent across windows — caution.'}")

    # --- details ------------------------------------------------------------------
    print(f"\n=== OOS benchmark comparison ({oos_ret.index[0].date()} -> {oos_ret.index[-1].date()}, after costs) ===")
    bench = pd.DataFrame([
        {"series": f"{name} ({variant})", "CAGR": _pct(m_oos["cagr"]), "Sharpe": _ratio(m_oos["sharpe"]),
         "Sortino": _ratio(m_oos["sortino"]), "maxDD": _pct(m_oos["max_drawdown"] and -m_oos["max_drawdown"])},
        {"series": "SPY buy-hold", "CAGR": _pct(m_spy["cagr"]), "Sharpe": _ratio(m_spy["sharpe"]),
         "Sortino": _ratio(m_spy["sortino"]), "maxDD": _pct(m_spy["max_drawdown"] and -m_spy["max_drawdown"])},
        {"series": "vol-matched SPY/cash", "CAGR": _pct(m_blend["cagr"]), "Sharpe": _ratio(m_blend["sharpe"]),
         "Sortino": _ratio(m_blend["sortino"]), "maxDD": _pct(m_blend["max_drawdown"] and -m_blend["max_drawdown"])},
    ])
    print(bench.to_string(index=False))
    print("\n=== Walk-forward (non-overlapping OOS windows, after costs) ===")
    walkforward.print_walkforward(wf)
    walkforward.capacity_report(strategy, data, eligible)
    return 0


def cmd_paper(args) -> int:
    """Monthly paper rebalance for a portfolio strategy, through the existing safety path."""
    from decision.run_paper_strategy import run_paper_cycle
    return run_paper_cycle(args.name, use_broker=not args.no_broker, equity_override=args.equity)


def build_parser() -> argparse.ArgumentParser:
    """CLI with the four subcommands."""
    parser = argparse.ArgumentParser(prog="main.py", description="pit-backtester — integrated entry point.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, func, help_text in [
        ("factors", cmd_factors, "factor IC evaluation scorecard (price + point-in-time fundamentals)"),
        ("altdata", cmd_altdata, "insider/institutional factor gauntlet (SF2/SF3 vs momentum)"),
    ]:
        subparsers.add_parser(name, help=help_text).set_defaults(func=func)
    # Portfolio-strategy backtest + walk-forward (e.g. vol_managed_momentum).
    strat_parser = subparsers.add_parser(
        "strategy", help="run a portfolio strategy's backtest + benchmark + walk-forward")
    strat_parser.add_argument("name", help="registered portfolio strategy (e.g. vol_managed_momentum)")
    strat_parser.set_defaults(func=cmd_strategy)
    # Monthly paper rebalance for a portfolio strategy (existing DU + DRY_RUN + gate path).
    paper_parser = subparsers.add_parser(
        "paper", help="run one monthly paper rebalance for a portfolio strategy")
    paper_parser.add_argument("name", help="registered portfolio strategy (e.g. vol_managed_momentum)")
    paper_parser.add_argument("--no-broker", action="store_true",
                              help="offline preview: compute target/orders, place nothing, no connection")
    paper_parser.add_argument("--equity", type=float, default=1_000_000.0,
                              help="assumed equity for an offline preview (real cycles read broker NAV)")
    paper_parser.set_defaults(func=cmd_paper)
    return parser


def main(argv=None) -> int:
    """Parse args, run the startup self-check, and dispatch the subcommand."""
    configure_logging()
    args = build_parser().parse_args(argv)
    paper_safety_self_check()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
