# pit-backtester

<!-- Replace OWNER/REPO with your GitHub path once pushed. -->
![CI](https://github.com/OWNER/pit-backtester/actions/workflows/ci.yml/badge.svg)
![coverage](https://img.shields.io/badge/coverage-pytest--cov-blue)

A point-in-time-correct backtesting and validation platform. It treats "is this signal
real, or did I just get lucky?" as an engineering problem: survivorship-bias-free data,
factor predictive power measured before any strategy is built, combinations that are
provably not overfit, walk-forward out-of-sample validation, and a safety-gated paper
execution path to Interactive Brokers. The one strategy it ships (12-1 momentum, volatility
managed) is deliberately simple — the depth is in the system that proves it's trustworthy,
not in a proprietary formula.

See `ARCHITECTURE.md` for the full guided tour — how each piece works, why it's built that
way, and an honest read of what the results actually say.

## Prerequisites

- Python 3.11 or higher
- An Interactive Brokers paper trading account, with TWS or IB Gateway running locally and
  the API enabled (Global Configuration > API > Settings), for the `paper` command
- A Nasdaq Data Link (Sharadar) API key for the point-in-time data — free-tier commands
  still work without one; see `ARCHITECTURE.md`, Chapter 5

## Setup

1. Clone the repo and move into it:
   ```
   git clone <your-repo-url>
   cd pit-backtester
   ```

2. Create and activate a virtual environment:
   ```
   python -m venv venv
   source venv/bin/activate      # Mac/Linux
   venv\Scripts\activate         # Windows
   ```

3. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

4. Set up your environment variables:
   ```
   cp .env.example .env
   ```
   Then open `.env` and fill in your real keys. Never commit `.env`.

5. Build the offline Sharadar archive once (needs `NASDAQ_DATA_LINK_API_KEY`; every command
   after this runs with zero API calls):
   ```
   python data/archive_sharadar.py
   ```

## Running

`main.py` is the single entry point with four subcommands:

```
python main.py factors                          # factor IC scorecard (free data + point-in-time Sharadar)
python main.py altdata                           # insider/institutional factor gauntlet vs momentum
python main.py strategy vol_managed_momentum     # backtest + benchmark + walk-forward validation
python main.py paper vol_managed_momentum        # one monthly paper rebalance (needs TWS/IB Gateway)
```

Add `--no-broker` to `paper` for an offline preview that computes the target book and
orders without connecting to anything.

## Testing

The core math and the safety guards are covered by an offline, deterministic pytest suite
(no network). Install the dev deps and run it:

```
pip install -r requirements-dev.txt
pytest
```

What's covered:

- **factors/** — the IC/decile harness, the price and point-in-time fundamental factor
  libraries, and the look-ahead-safety property (a factor's value at *t* is unchanged when
  future bars are dropped).
- **strategies/** — the vol-managed-momentum overlay and its crash-fix variants.
- **decision/** — the paper-rebalance diff-to-orders logic.
- **safety guards** — the DU-account (paper-only) guard and `DRY_RUN` both block order
  placement and cannot be bypassed (broker mocked; no live connection).

CI (`.github/workflows/ci.yml`) runs the full offline suite with coverage on every push and
PR and fails the build on any test failure.

## Project structure

```
pit-backtester/
  config.py          # settings: autonomy_mode, risk params, ports, keys from env
  data/              # point-in-time data (Sharadar) + the provider interface
  factors/           # factor library, IC/decile harness, evaluation scripts
  research/          # factor-combination and crash-fix validation scripts
  strategies/        # the strategy that survived validation (vol-managed momentum)
  backtest/          # universes, walk-forward validation, benchmarks, risk metrics
  decision/          # the autonomy/safety gate + the paper-rebalance runner
  execution/         # ib_async wrapper
  storage/           # sqlite layer (paper rebalance / target / order history)
  main.py
```

See `ARCHITECTURE.md`'s "Map of the repository" for what each file does.

## Safety

- The `paper` command talks to an IBKR paper account. Port 7497 is paper, 7496 is live.
  This project has never placed a live order and isn't intended to.
- Never commit API keys or your `.env` file.

## Status

Every command in "Running" above works end to end offline against the local Sharadar
cache. See `ARCHITECTURE.md`'s "Honest limitations" for what this system genuinely can't
do yet.
