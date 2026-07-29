"""Swing Trading Agent — single integrated entry point.

One CLI that ties the whole system together. Each subcommand is a thin wrapper that
reuses the existing modules (no strategy logic, guard, or result is changed here):

  research  — full research pipeline -> one consolidated report:
              screener watchlist, strategy-library scorecard (each vs the
              risk-matched bar), factor IC scorecard, and the benchmark comparison
              (strategy vs S&P vs vol-matched blend, with equity/drawdown charts).
              Artifacts are saved under output/<timestamped run folder>.
  backtest  — the canonical best-config backtest (breakout + pullback, min-size
              floor, regime filter, trend-exit, large-cap) + benchmark comparison.
  live      — the paper decision loop, exactly as decision/run_decision.py: it
              respects DRY_RUN, the DU-account (paper) guard, and the autonomy gate.
  factors   — the factor-evaluation harness scorecard.

config.py is the single config surface (universe, active strategies, risk params,
autonomy mode, DRY_RUN, windows). At startup an integration self-check asserts the
canonical backtest still reproduces 1675 trades / $1,967,830 and that the paper-
safety guards (DU-account check, DRY_RUN) are intact. Read-only research + the
existing paper path; no new alpha.

    python main.py research
    python main.py backtest
    python main.py live
    python main.py factors
"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd  # noqa: E402

import config  # noqa: E402
from backtest import report  # noqa: E402
from backtest.benchmark import buy_and_hold, risk_matched_blend, vol_matched_weight  # noqa: E402
from backtest.engine import STARTING_EQUITY, run_engine  # noqa: E402
from backtest.evaluate import EvaluationContext, build_context, evaluate_strategy  # noqa: E402
from backtest.risk_metrics import DEFAULT_RISK_FREE, compute_metrics, relative_metrics  # noqa: E402
from screener.sectors import SECTOR_ETFS  # noqa: E402
from signals.breakout import BreakoutStrategy  # noqa: E402
from signals.pullback import PullbackStrategy  # noqa: E402
from strategies.registry import all_strategies, get  # noqa: E402
from strategies.selector import default_regime_selector  # noqa: E402
from run_library import scorecard_row  # noqa: E402  (reuse the library scorecard formatter)

log = logging.getLogger("main")

OUTPUT_ROOT = Path(__file__).resolve().parent / "output"
CANONICAL_TRADES = 1675
CANONICAL_FINAL = 1_967_830
SPY_LABEL = "S&P 500 B&H"
CANONICAL_FLAGS = dict(regime_filter=True, trend_exit=True, conviction_sizing=False)


def configure_logging() -> None:
    """Timestamped logs; quiet the chatty libraries."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    for noisy in ("yfinance", "ib_async", "risk.portfolio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _pct(v):
    return f"{v * 100:+.2f}%" if v is not None else "n/a"


def _ratio(v):
    return f"{v:.2f}" if v is not None else "n/a"


# --- Integration self-check (#4) ------------------------------------------------

def integration_self_check() -> None:
    """Assert the canonical backtest reproduces and the paper-safety guards hold.

    Reuses the engine on the fixed canonical config (breakout + pullback, large-cap,
    regime + trend-exit) as a regression anchor — independent of config overrides.
    """
    from backtest.data import BENCHMARK, build_sector_map, load_universe
    from backtest.regime import compute_regime
    from decision.autonomy import is_paper_account  # the DU-account guard

    bars, _ = load_universe()
    regime = compute_regime(bars[BENCHMARK]["Close"])
    equity, trades = run_engine(bars, regime, build_sector_map(), list(SECTOR_ETFS),
                                strategies=[BreakoutStrategy(), PullbackStrategy()], **CANONICAL_FLAGS)
    final = equity.iloc[-1]
    assert len(trades) == CANONICAL_TRADES and abs(final - CANONICAL_FINAL) < 5000, \
        f"REGRESSION: canonical = {len(trades)} trades / ${final:,.0f} (expected {CANONICAL_TRADES} / ${CANONICAL_FINAL:,.0f})"
    # DU-account guard logic must accept paper (DU…) and reject live/empty.
    assert is_paper_account("DU1234567") and not is_paper_account("U1234567") and not is_paper_account(None)
    assert isinstance(config.DRY_RUN, bool)

    print("=== Integration self-check ===")
    print(f"  Canonical backtest : {len(trades)} trades / ${final:,.0f}  (matches {CANONICAL_TRADES} / ${CANONICAL_FINAL:,.0f})  OK")
    print("  DU-account guard   : DU accepted, U/None rejected  OK")
    print(f"  Paper safety       : DRY_RUN={config.DRY_RUN}, AUTONOMY_MODE={config.AUTONOMY_MODE}")
    print()


# --- Shared research/backtest building blocks (thin orchestration) --------------

def _research_context(universe: str) -> EvaluationContext:
    """Build the evaluation context for a universe (reuses backtest.evaluate for large)."""
    if universe == "large":
        return build_context()
    # Non-large universes use the OHLCV loader; same context shape.
    from backtest.regime import compute_regime
    from backtest.universe import BENCHMARK as U_BENCHMARK, load_bars, sector_map
    from data.earnings import load_earnings
    from strategies.base import StrategyData

    bars, _ = load_bars(universe)
    regime = compute_regime(bars[U_BENCHMARK]["Close"])
    smap = sector_map(universe)
    tradable = [s for s in smap if s in bars]
    earnings, _, _ = load_earnings(tradable, years=config.EARNINGS_BACKTEST_YEARS)
    bundle = StrategyData(price_bars=bars, earnings=earnings)
    return EvaluationContext(bars, regime, smap, list(SECTOR_ETFS), bundle,
                             bars[U_BENCHMARK]["Close"], STARTING_EQUITY, DEFAULT_RISK_FREE)


def _active_strategies(ctx: EvaluationContext) -> list:
    """Build the configured active strategies (earnings data injected via from_data)."""
    return [get(name).from_data(ctx.bundle) for name in config.ACTIVE_STRATEGIES]


def _run_canonical(ctx: EvaluationContext) -> tuple[pd.Series, pd.DataFrame]:
    """Run the configured strategies through the engine with the canonical honest config."""
    return run_engine(ctx.bars, ctx.regime, ctx.sector_map, ctx.etfs,
                      strategies=_active_strategies(ctx), **CANONICAL_FLAGS)


def _benchmark_comparison(equity: pd.Series, ctx: EvaluationContext, run_dir: Path | None = None) -> dict:
    """Build strategy vs S&P vs vol-matched blend, print the table, return metrics.

    When ``run_dir`` is given, save the comparison CSV and the equity/drawdown charts.
    """
    buyhold = buy_and_hold(ctx.spy_close, equity, ctx.starting_equity)
    weight = vol_matched_weight(equity, buyhold)
    blend = risk_matched_blend(ctx.spy_close, equity.index, ctx.starting_equity, weight, ctx.rf)
    blend_label = f"SPY/cash blend ({weight * 100:.0f}% SPY)"
    curves = {"Strategy": equity, SPY_LABEL: buyhold, blend_label: blend}

    metrics_by = {label: compute_metrics(curve, ctx.rf) for label, curve in curves.items()}
    relative_by = {label: relative_metrics(curve, buyhold, ctx.rf) for label, curve in curves.items()}
    print(f"\n=== Benchmark comparison ({equity.index[0].date()} -> {equity.index[-1].date()}) ===")
    print(report.build_table(metrics_by, relative_by, list(curves)).to_string(index=False))

    if run_dir is not None:
        report.build_table(metrics_by, relative_by, list(curves)).to_csv(run_dir / "benchmark_comparison.csv", index=False)
        report.plot_equity(curves, run_dir / "equity.png")
        report.plot_drawdown(curves, run_dir / "drawdown.png")
    return {"metrics": metrics_by, "blend_label": blend_label}


def _library_scorecard(ctx: EvaluationContext) -> list[str]:
    """Score each registered strategy + the regime-selected combination; return passes."""
    rows = [scorecard_row(evaluate_strategy([cls], ctx, label=name)) for name, cls in all_strategies().items()]
    rows.append(scorecard_row(evaluate_strategy(list(all_strategies().values()), ctx,
                                                selector=default_regime_selector, label="combined+selector")))
    print("\n=== Strategy library scorecard (bar = beat vol-matched SPY/cash blend on Sharpe) ===")
    print(pd.DataFrame(rows).to_string(index=False))
    return [r["strategy"] for r in rows if r["vs bar"] == "PASS"]


def _factor_scorecard(universe: str) -> list[str]:
    """Run the factor IC harness on a universe; return factors that cleared the bar."""
    from factors.run_factor_eval import evaluate_universe, print_scorecard

    scores, deferred = evaluate_universe(universe)
    builds = print_scorecard(scores, "\n=== Factor IC scorecard (|IC|>0.02 & |t|>2 => BUILD; sorted by |t|) ===")
    if deferred:
        print(f"  (deferred, need point-in-time data: {', '.join(deferred)})")
    return builds


def _summary_lines(metrics_by: dict, blend_label: str, library_passes: list[str],
                   factor_builds: list[str]) -> list[str]:
    """Plain-language headline from the actual computed numbers (no spin)."""
    strat, spy, blend = metrics_by["Strategy"], metrics_by[SPY_LABEL], metrics_by[blend_label]
    beat_return = strat["cagr"] is not None and spy["cagr"] is not None and strat["cagr"] > spy["cagr"]
    beat_sharpe = strat["sharpe"] is not None and blend["sharpe"] is not None and strat["sharpe"] > blend["sharpe"]
    cleared = bool(library_passes or factor_builds)
    return [
        "Canonical strategy (breakout + pullback, large-cap, honest costs) vs the market:",
        f"  Raw return : CAGR {_pct(strat['cagr'])} vs S&P {_pct(spy['cagr'])}  -> "
        f"{'BEATS' if beat_return else 'TRAILS'} the index.",
        f"  Risk-adj   : Sharpe {_ratio(strat['sharpe'])} (strategy) vs {_ratio(spy['sharpe'])} (S&P) vs "
        f"{_ratio(blend['sharpe'])} (risk-matched blend)  -> {'BEATS' if beat_sharpe else 'TRAILS'} the risk-matched bar.",
        f"  Drawdown   : strategy {_pct(strat['max_drawdown'] and -strat['max_drawdown'])} vs "
        f"S&P {_pct(spy['max_drawdown'] and -spy['max_drawdown'])}.",
        f"  Cleared the bar? strategies: {', '.join(library_passes) if library_passes else 'NONE'}; "
        f"factors: {', '.join(factor_builds) if factor_builds else 'NONE'}.",
        ("  HEADLINE: nothing here beats a simple S&P position on a risk-adjusted basis — "
         "treat this as honest research, not a live edge." if not (beat_sharpe or cleared)
         else "  HEADLINE: at least one component cleared the risk-matched bar — see the scorecards above."),
    ]


# --- Subcommands ----------------------------------------------------------------

def cmd_research(args) -> int:
    """Full research pipeline -> one consolidated, timestamped report."""
    run_dir = OUTPUT_ROOT / f"research_{datetime.now():%Y%m%d_%H%M%S}"
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"Consolidated research run -> {run_dir}\n")

    print("----- [1/4] Screener watchlist -----")
    try:
        from screener.run_screener import main as run_screener_main
        run_screener_main()
    except Exception as error:  # noqa: BLE001 - screener needs live data; never block the report
        log.warning("Screener stage skipped (%s).", error)

    ctx = _research_context(config.RESEARCH_UNIVERSE)
    print("\n----- [2/4] Strategy library scorecard -----")
    library_passes = _library_scorecard(ctx)
    print("\n----- [3/4] Factor IC scorecard -----")
    factor_builds = _factor_scorecard(config.RESEARCH_UNIVERSE)

    print("\n----- [4/4] Benchmark comparison -----")
    equity, trades = _run_canonical(ctx)
    result = _benchmark_comparison(equity, ctx, run_dir=run_dir)

    summary = _summary_lines(result["metrics"], result["blend_label"], library_passes, factor_builds)
    print("\n========================= RUN SUMMARY =========================")
    for line in summary:
        print(line)
    print("===============================================================")
    (run_dir / "summary.txt").write_text("\n".join(summary) + "\n")
    print(f"\nArtifacts saved to {run_dir} (summary.txt, benchmark_comparison.csv, equity.png, drawdown.png).")
    return 0


def cmd_backtest(args) -> int:
    """Canonical best-config backtest + benchmark comparison."""
    ctx = _research_context(config.RESEARCH_UNIVERSE)
    log.info("Running canonical backtest: %s on '%s'...",
             ", ".join(config.ACTIVE_STRATEGIES), config.RESEARCH_UNIVERSE)
    equity, trades = _run_canonical(ctx)
    print(f"\nCanonical backtest: {len(trades)} trades, final equity ${equity.iloc[-1]:,.0f}.")
    result = _benchmark_comparison(equity, ctx)
    for line in _summary_lines(result["metrics"], result["blend_label"], [], []):
        print(line)
    return 0


def cmd_strategy(args) -> int:
    """Run a portfolio strategy's backtest + benchmark + walk-forward, headline first."""
    from backtest import walkforward
    from factors.evaluate import _rebalance_dates
    from factors.run_factor_eval import build_sharadar_factor_data
    from ml.combine_train import (OOS_FRACTION, SPY_CACHE, _monthly_metrics,
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


def cmd_live(args) -> int:
    """Paper decision loop (DRY_RUN + DU-account guard + autonomy gate), unchanged."""
    from decision.run_decision import main as run_decision_main
    return run_decision_main()


def cmd_factors(args) -> int:
    """Factor-evaluation harness scorecard, unchanged."""
    from factors.run_factor_eval import main as run_factor_main
    return run_factor_main()


def build_parser() -> argparse.ArgumentParser:
    """CLI with the four subcommands."""
    parser = argparse.ArgumentParser(prog="main.py", description="Swing Trading Agent — integrated entry point.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, func, help_text in [
        ("research", cmd_research, "full research pipeline -> consolidated report + artifacts"),
        ("backtest", cmd_backtest, "canonical best-config backtest + benchmark comparison"),
        ("live", cmd_live, "paper decision loop (DRY_RUN + DU guard + autonomy gate)"),
        ("factors", cmd_factors, "factor IC evaluation scorecard"),
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
    integration_self_check()  # asserts canonical reproduction + paper-safety guards
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
