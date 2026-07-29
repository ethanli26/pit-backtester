"""Safety-guard tests (critical): the DU-account guard and DRY_RUN must block orders.

A fake broker records any placement, so we can prove that orders flow ONLY when the
account is a paper (DU) account AND DRY_RUN is False — and that neither guard can be
bypassed. No live IBKR connection is required.
"""

import pytest

import config
from decision.autonomy import (
    _place_proposal,
    assert_paper_account,
    is_paper_account,
    run_gate,
)


class FakeBroker:
    """Minimal broker stub: configurable account id; records placements."""

    def __init__(self, account_id):
        self._account_id = account_id
        self.placed = []
        self.limit_orders = []

    def get_account_id(self):
        return self._account_id

    def get_account_summary(self):
        return {"net_liquidation": 1_000_000.0}   # for weight% in the batch review table

    def place_market_order(self, symbol, shares, action):
        self.placed.append((symbol, shares, action))
        return ("trade", symbol)

    def place_limit_order(self, symbol, shares, action, reference_price,
                          buffer=config.LIMIT_BUFFER, tif="DAY"):
        # The gate routes through here now; record the same tuple plus the limit details.
        self.placed.append((symbol, shares, action))
        self.limit_orders.append({"symbol": symbol, "shares": shares, "action": action,
                                  "reference_price": reference_price, "buffer": buffer, "tif": tif})
        return ("trade", symbol)


def _proposal():
    return {"symbol": "AAA", "action": "BUY", "shares": 10, "entry_ref": 100.0,
            "stop": 96.0, "atr": 2.0, "risk_dollars": 1000.0, "est_value": 1000.0}


def test_is_paper_account_only_accepts_du():
    assert is_paper_account("DU1234567") is True
    assert is_paper_account("U1234567") is False   # live account
    assert is_paper_account("") is False
    assert is_paper_account(None) is False


def test_assert_paper_account_rejects_live():
    with pytest.raises(RuntimeError):
        assert_paper_account(FakeBroker("U999"))


def test_assert_paper_account_accepts_paper():
    assert assert_paper_account(FakeBroker("DU1")) == "DU1"


def test_dry_run_blocks_placement_even_on_paper(monkeypatch):
    monkeypatch.setattr(config, "DRY_RUN", True)
    broker = FakeBroker("DU1")  # a valid paper account ...
    assert _place_proposal(broker, _proposal()) is None  # ... but DRY_RUN refuses
    assert broker.placed == []


def test_live_account_blocks_placement_even_when_not_dry_run(monkeypatch):
    monkeypatch.setattr(config, "DRY_RUN", False)  # DRY_RUN off ...
    broker = FakeBroker("U999")                    # ... but a live account
    with pytest.raises(RuntimeError):
        _place_proposal(broker, _proposal())
    assert broker.placed == []


def test_placement_only_when_paper_and_not_dry_run(monkeypatch):
    monkeypatch.setattr(config, "DRY_RUN", False)
    broker = FakeBroker("DU1")
    _place_proposal(broker, _proposal())
    assert broker.placed == [("AAA", 10, "BUY")]  # both guards satisfied -> order flows


def test_place_proposal_routes_through_limit_with_reference_price(monkeypatch):
    """Placement now uses a LIMIT order carrying the proposal's reference price + TIF."""
    monkeypatch.setattr(config, "DRY_RUN", False)
    broker = FakeBroker("DU1")
    _place_proposal(broker, _proposal())
    assert len(broker.limit_orders) == 1
    placed = broker.limit_orders[0]
    assert placed["reference_price"] == 100.0   # the proposal's entry_ref flows through
    assert placed["tif"] == "DAY"


def test_dry_run_blocks_limit_placement(monkeypatch):
    """DRY_RUN must still block the limit path (no order recorded)."""
    monkeypatch.setattr(config, "DRY_RUN", True)
    broker = FakeBroker("DU1")
    assert _place_proposal(broker, _proposal()) is None
    assert broker.limit_orders == [] and broker.placed == []


def test_run_gate_approve_rejects_live_before_any_order(monkeypatch):
    monkeypatch.setattr(config, "AUTONOMY_MODE", "approve")
    monkeypatch.setattr(config, "DRY_RUN", False)
    broker = FakeBroker("U999")
    with pytest.raises(RuntimeError):  # fails fast on the DU guard, before prompting
        run_gate([_proposal()], broker)
    assert broker.placed == []


def test_run_gate_signal_only_never_places(monkeypatch):
    monkeypatch.setattr(config, "AUTONOMY_MODE", "signal_only")
    monkeypatch.setattr(config, "DRY_RUN", False)
    broker = FakeBroker("DU1")
    run_gate([_proposal()], broker)  # alerts only
    assert broker.placed == []


# --- approve_batch (whole-book, one confirmation) -------------------------------

def _book(values=(1000.0, 1000.0, 1000.0)):
    """A small book of same-shaped proposals (equal sizes => no outliers by default)."""
    return [{"symbol": f"S{i}", "action": "BUY", "shares": 10, "entry_ref": v / 10,
             "stop": 0.0, "atr": 0.0, "risk_dollars": 0.0, "est_value": v}
            for i, v in enumerate(values)]


def _answers(monkeypatch, *replies):
    """Feed a sequence of input() answers to the gate."""
    it = iter(replies)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(it))


def test_approve_batch_places_all_on_yes(monkeypatch):
    monkeypatch.setattr(config, "AUTONOMY_MODE", "approve_batch")
    monkeypatch.setattr(config, "DRY_RUN", False)
    _answers(monkeypatch, "y")                       # ONE confirmation for the whole book
    broker = FakeBroker("DU1")
    book = _book()
    run_gate(book, broker)
    assert [s for s, _, _ in broker.placed] == ["S0", "S1", "S2"]
    assert len(broker.limit_orders) == 3            # routed through the limit path


def test_approve_batch_places_nothing_on_no(monkeypatch):
    monkeypatch.setattr(config, "AUTONOMY_MODE", "approve_batch")
    monkeypatch.setattr(config, "DRY_RUN", False)
    _answers(monkeypatch, "n")
    broker = FakeBroker("DU1")
    run_gate(_book(), broker)
    assert broker.placed == []


def test_approve_batch_respects_dry_run(monkeypatch):
    monkeypatch.setattr(config, "AUTONOMY_MODE", "approve_batch")
    monkeypatch.setattr(config, "DRY_RUN", True)     # blocks BEFORE the batch
    # input would raise if called — proves DRY_RUN returns before prompting.
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("must not prompt under DRY_RUN"))
    broker = FakeBroker("DU1")
    run_gate(_book(), broker)
    assert broker.placed == []


def test_approve_batch_du_guard_blocks_before_any_order(monkeypatch):
    monkeypatch.setattr(config, "AUTONOMY_MODE", "approve_batch")
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("must not prompt on a live account"))
    broker = FakeBroker("U999")                      # live account
    with pytest.raises(RuntimeError):                # DU guard fails fast, before prompting
        run_gate(_book(), broker)
    assert broker.placed == []


def test_approve_batch_outlier_gets_individual_confirmation(monkeypatch):
    """A larger-than-usual order still needs its own y/N even after the batch 'y'."""
    monkeypatch.setattr(config, "AUTONOMY_MODE", "approve_batch")
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "BATCH_OUTLIER_MULT", 2.0)
    monkeypatch.setattr(config, "BATCH_MAX_ORDER_VALUE", 0.0)
    broker = FakeBroker("DU1")
    book = _book((1000.0, 1000.0, 9000.0))           # S2 is the outlier (>2x the average)
    _answers(monkeypatch, "y", "n")                  # batch yes, but decline the big one
    run_gate(book, broker)
    placed = [s for s, _, _ in broker.placed]
    assert placed == ["S0", "S1"]                    # outlier S2 skipped on its individual 'n'
