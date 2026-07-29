"""Minimal SQLite persistence for the screener.

Saves a ranked watchlist to a ``watchlist`` table, tagging every row with the
timestamp of the run that produced it so historical runs can be compared later.
"""

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

# Database file lives at the repo root and is git-ignored (*.db in .gitignore).
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "trading_agent.db"

_CREATE_WATCHLIST_TABLE = """
CREATE TABLE IF NOT EXISTS watchlist (
    run_timestamp  TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    sector         TEXT,
    momentum_score REAL,
    return_3m      REAL,
    return_6m      REAL
)
"""


def save_watchlist(
    watchlist: pd.DataFrame,
    db_path: Path = DEFAULT_DB_PATH,
    run_timestamp: str | None = None,
) -> str | None:
    """Append a watchlist DataFrame to the ``watchlist`` table.

    Each row is stamped with ``run_timestamp`` (UTC ISO-8601, generated if not
    supplied). Returns the timestamp used, or ``None`` if there was nothing to
    save.
    """
    if watchlist.empty:
        log.warning("Watchlist is empty; nothing to save.")
        return None

    run_timestamp = run_timestamp or datetime.now(timezone.utc).isoformat()

    # Prepend the run timestamp so column order matches the table schema.
    rows = watchlist.copy()
    rows.insert(0, "run_timestamp", run_timestamp)

    connection = sqlite3.connect(db_path)
    try:
        connection.execute(_CREATE_WATCHLIST_TABLE)
        rows.to_sql("watchlist", connection, if_exists="append", index=False)
        connection.commit()
    finally:
        connection.close()

    log.info("Saved %d watchlist rows to %s (run %s).", len(rows), db_path, run_timestamp)
    return run_timestamp


_CREATE_PAPER_REBALANCE_TABLE = """
CREATE TABLE IF NOT EXISTS paper_rebalance (
    run_timestamp   TEXT NOT NULL,
    strategy        TEXT NOT NULL,
    asof_date       TEXT,
    gross_weight    REAL,
    equity          REAL,
    autonomy_mode   TEXT,
    dry_run         INTEGER,
    live            INTEGER,
    spy_level       REAL,
    n_targets       INTEGER,
    n_orders        INTEGER
)
"""
_CREATE_PAPER_ORDER_TABLE = """
CREATE TABLE IF NOT EXISTS paper_order (
    run_timestamp  TEXT NOT NULL,
    strategy       TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    action         TEXT NOT NULL,
    shares         INTEGER,
    est_value      REAL
)
"""
_CREATE_PAPER_TARGET_TABLE = """
CREATE TABLE IF NOT EXISTS paper_target (
    run_timestamp  TEXT NOT NULL,
    strategy       TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    target_shares  INTEGER
)
"""


def record_paper_rebalance(strategy: str, run_timestamp: str, asof_date: str,
                           gross_weight: float, equity: float, autonomy_mode: str,
                           dry_run: bool, live: bool, spy_level: float | None,
                           targets: dict[str, int], orders: list[dict],
                           db_path: Path = DEFAULT_DB_PATH) -> None:
    """Append one rebalance (header + targets + orders) for the live paper track record.

    ``live`` marks whether ``equity`` came from a real broker NAV (vs a preview equity),
    so the paper-vs-SPY record can use only real NAV points.
    """
    connection = sqlite3.connect(db_path)
    try:
        for ddl in (_CREATE_PAPER_REBALANCE_TABLE, _CREATE_PAPER_ORDER_TABLE, _CREATE_PAPER_TARGET_TABLE):
            connection.execute(ddl)
        connection.execute(
            "INSERT INTO paper_rebalance VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run_timestamp, strategy, asof_date, gross_weight, equity, autonomy_mode,
             int(dry_run), int(live), spy_level, len(targets), len(orders)))
        connection.executemany(
            "INSERT INTO paper_target VALUES (?,?,?,?)",
            [(run_timestamp, strategy, sym, int(sh)) for sym, sh in targets.items()])
        connection.executemany(
            "INSERT INTO paper_order VALUES (?,?,?,?,?,?)",
            [(run_timestamp, strategy, o["symbol"], o["action"], int(o["shares"]),
              float(o.get("est_value", 0.0))) for o in orders])
        connection.commit()
    finally:
        connection.close()
    log.info("Recorded paper rebalance for %s at %s (%d targets, %d orders, live=%s).",
             strategy, run_timestamp, len(targets), len(orders), live)


def load_paper_track_record(strategy: str, db_path: Path = DEFAULT_DB_PATH) -> pd.DataFrame:
    """Return live rebalance NAV points (run_timestamp, equity, spy_level) for paper-vs-SPY."""
    if not Path(db_path).exists():
        return pd.DataFrame()
    connection = sqlite3.connect(db_path)
    try:
        rows = pd.read_sql_query(
            "SELECT run_timestamp, equity, spy_level FROM paper_rebalance "
            "WHERE strategy = ? AND live = 1 ORDER BY run_timestamp",
            connection, params=(strategy,))
    except (sqlite3.OperationalError, pd.errors.DatabaseError):
        return pd.DataFrame()
    finally:
        connection.close()
    return rows


def load_latest_watchlist(db_path: Path = DEFAULT_DB_PATH) -> pd.DataFrame:
    """Load the watchlist rows from the most recent run.

    Returns the rows for the latest ``run_timestamp``, sorted best momentum first,
    or an empty DataFrame if the database/table is missing or has no rows.
    """
    if not Path(db_path).exists():
        log.warning("Database %s does not exist; no watchlist to load.", db_path)
        return pd.DataFrame()

    connection = sqlite3.connect(db_path)
    try:
        latest = connection.execute("SELECT MAX(run_timestamp) FROM watchlist").fetchone()[0]
        if latest is None:
            log.warning("Watchlist table is empty.")
            return pd.DataFrame()
        rows = pd.read_sql_query(
            "SELECT * FROM watchlist WHERE run_timestamp = ? ORDER BY momentum_score DESC",
            connection,
            params=(latest,),
        )
    except sqlite3.OperationalError:
        log.warning("No watchlist table found in %s.", db_path)
        return pd.DataFrame()
    finally:
        connection.close()

    log.info("Loaded %d watchlist rows from run %s.", len(rows), latest)
    return rows
