"""Minimal SQLite persistence for the live paper-trading track record.

Every monthly paper rebalance is appended (header + targets + orders), each row
tagged with the run's timestamp, so the cumulative live-vs-SPY record can be
rebuilt at any time.
"""

import logging
import sqlite3
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

# Database file lives at the repo root and is git-ignored (*.db in .gitignore).
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "trading_agent.db"

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
