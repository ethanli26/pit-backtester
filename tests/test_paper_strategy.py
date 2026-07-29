"""Tests for the paper-rebalance order diff and the paper track-record storage."""

import pandas as pd

from decision.run_paper_strategy import diff_to_orders
from storage.database import load_paper_track_record, record_paper_rebalance


def _prices(d):
    return pd.Series(d)


def test_diff_to_orders_buys_sells_and_exits():
    target = {"AAA": 100, "BBB": 50, "CCC": 30}      # CCC is new
    current = [{"symbol": "AAA", "shares": 40, "market_value": 0},   # need +60
               {"symbol": "BBB", "shares": 50, "market_value": 0},   # already at target
               {"symbol": "DDD", "shares": 20, "market_value": 0}]   # not in target -> exit
    orders = diff_to_orders(target, current, _prices({"AAA": 10.0, "BBB": 10.0, "CCC": 5.0, "DDD": 2.0}))
    by = {o["symbol"]: o for o in orders}
    assert by["AAA"]["action"] == "BUY" and by["AAA"]["shares"] == 60
    assert "BBB" not in by                                   # no delta -> no order
    assert by["CCC"]["action"] == "BUY" and by["CCC"]["shares"] == 30
    assert by["DDD"]["action"] == "SELL" and by["DDD"]["shares"] == 20  # full exit
    # Orders carry the gate-compatible fields the autonomy formatter needs.
    for o in orders:
        assert {"symbol", "action", "shares", "entry_ref", "stop", "atr", "risk_dollars", "est_value"} <= o.keys()


def test_diff_to_orders_empty_when_at_target():
    target = {"AAA": 100}
    current = [{"symbol": "AAA", "shares": 100, "market_value": 0}]
    assert diff_to_orders(target, current, _prices({"AAA": 10.0})) == []


def test_paper_track_record_round_trips_live_rows(tmp_path):
    db = tmp_path / "t.db"
    record_paper_rebalance("vol_managed_momentum", "2025-01-31T00:00:00Z", "2025-01-31",
                           0.8, 1_000_000.0, "approve", True, True, 500.0,
                           {"AAA": 100}, [{"symbol": "AAA", "action": "BUY", "shares": 100, "est_value": 1000.0}],
                           db_path=db)
    record_paper_rebalance("vol_managed_momentum", "2025-02-28T00:00:00Z", "2025-02-28",
                           0.9, 1_050_000.0, "approve", True, True, 510.0,
                           {"AAA": 110}, [], db_path=db)
    # A preview row (live=0) must NOT enter the track record.
    record_paper_rebalance("vol_managed_momentum", "2025-03-31T00:00:00Z", "2025-03-31",
                           1.0, 1_000_000.0, "approve", True, False, 520.0, {}, [], db_path=db)
    record = load_paper_track_record("vol_managed_momentum", db_path=db)
    assert len(record) == 2                                  # only the two live rows
    assert list(record["equity"]) == [1_000_000.0, 1_050_000.0]
    assert list(record["spy_level"]) == [500.0, 510.0]
