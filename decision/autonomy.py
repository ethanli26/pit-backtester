"""The autonomy gate: the single place where proposals become orders.

The gate reads ``AUTONOMY_MODE`` and behaves accordingly:

  * ``signal_only``   — alert, place nothing.
  * ``approve``       — confirm EACH order y/N (fine for a few-name strategy).
  * ``approve_batch`` — review the WHOLE book in one table, then ONE confirmation for all
    orders (practical for a 90-name portfolio strategy). Unusually large orders still
    get an individual y/N even inside the batch.

Every path runs through two hard safety checks before any order, in the same place:

  1. The connected account id must start with ``DU`` (an IBKR paper account).
  2. ``DRY_RUN`` must be False.

If either check fails, the gate refuses to place and logs an error.
"""

import logging

import config

log = logging.getLogger(__name__)

# IBKR paper account ids start with "DU"; live accounts start with "U".
PAPER_ACCOUNT_PREFIX = "DU"


def is_paper_account(account_id: str | None) -> bool:
    """True only for an IBKR paper account id (starts with ``DU``)."""
    return bool(account_id) and account_id.startswith(PAPER_ACCOUNT_PREFIX)


def assert_paper_account(broker) -> str:
    """Return the connected account id if it is a paper account, else raise.

    This is the safety guard: it refuses to let any caller proceed toward placing
    orders on a non-paper account.
    """
    account_id = broker.get_account_id()
    if not is_paper_account(account_id):
        log.error(
            "SAFETY GUARD: account %r is not a paper (DU) account; refusing to place orders.",
            account_id,
        )
        raise RuntimeError(f"Refusing to trade on non-paper account: {account_id!r}")
    log.info("Safety guard passed: paper account %s confirmed.", account_id)
    return account_id


def format_proposal(proposal: dict) -> str:
    """One readable multi-line block describing a proposal."""
    return (
        f"PROPOSAL  {proposal['action']} {proposal['shares']} {proposal['symbol']}\n"
        f"  entry ref : ${proposal['entry_ref']:,.2f}\n"
        f"  stop      : ${proposal['stop']:,.2f}  (ATR {proposal['atr']:,.2f})\n"
        f"  risk      : ${proposal['risk_dollars']:,.2f}\n"
        f"  est value : ${proposal['est_value']:,.2f}"
    )


def _place_proposal(broker, proposal: dict):
    """Place one proposal as a price-protected paper LIMIT order, after re-checking safety."""
    # Defense in depth: never place while DRY_RUN is set.
    if config.DRY_RUN:
        log.error("DRY_RUN is True; refusing to place order for %s.", proposal["symbol"])
        return None

    assert_paper_account(broker)  # re-confirm immediately before placing
    # LIMIT order priced off the reference (entry_ref): avoids the no-market-data block
    # (Error 354) and the TIF preset rejection (Error 10349). The broker logs the ACTUAL
    # order status (Filled / Working / Cancelled+reason) — we do NOT log "PLACED" blindly.
    return broker.place_limit_order(
        proposal["symbol"], proposal["shares"], proposal["action"],
        reference_price=proposal["entry_ref"])


def _limit_price(proposal: dict) -> float:
    """Display limit price the broker will use: BUY ref*(1+buffer), SELL ref*(1-buffer)."""
    sign = 1.0 if proposal["action"] == "BUY" else -1.0
    return round(proposal["entry_ref"] * (1.0 + sign * config.LIMIT_BUFFER), 2)


def _batch_equity(broker) -> float | None:
    """Account equity for weight%, read defensively (None if the broker can't supply it)."""
    try:
        summary = broker.get_account_summary()
        value = summary.get("net_liquidation") if summary else None
        return float(value) if value else None
    except Exception:  # noqa: BLE001 - weight% is a display nicety; never block on it
        return None


def _flag_outliers(proposals: list[dict]) -> set[int]:
    """Indices of orders that are unusually large vs the average (or an absolute $ cap).

    "Intended per-name weight" is approximated by the average order value; an order over
    ``BATCH_OUTLIER_MULT`` x that average (or over ``BATCH_MAX_ORDER_VALUE`` if set) still
    needs an individual y/N even in batch mode — a safety check against a fat-finger size.
    """
    values = [p["est_value"] for p in proposals]
    mean_value = (sum(values) / len(values)) if values else 0.0
    threshold = config.BATCH_OUTLIER_MULT * mean_value
    cap = config.BATCH_MAX_ORDER_VALUE
    return {i for i, p in enumerate(proposals)
            if (mean_value > 0 and p["est_value"] > threshold) or (cap > 0 and p["est_value"] > cap)}


def format_book(proposals: list[dict], equity: float | None) -> str:
    """One review table of the whole proposed book + totals (the batch human checkpoint)."""
    header = f"{'symbol':<8}{'action':>6}{'shares':>9}{'limit':>11}{'est_value':>14}{'weight%':>9}"
    lines = ["=== Proposed book (review before approving) ===", header, "-" * len(header)]
    gross = sum(p["est_value"] for p in proposals)
    basis = equity if equity else (gross or 1.0)
    for proposal in proposals:
        weight = proposal["est_value"] / basis * 100.0
        lines.append(f"{proposal['symbol']:<8}{proposal['action']:>6}{proposal['shares']:>9}"
                     f"{_limit_price(proposal):>11,.2f}{proposal['est_value']:>14,.2f}{weight:>8.1f}%")
    n_buys = sum(1 for p in proposals if p["action"] == "BUY")
    pct_equity = f"{gross / equity * 100:.1f}% of equity" if equity else "equity n/a"
    lines.append("-" * len(header))
    lines.append(f"Totals: {n_buys} buys, {len(proposals) - n_buys} sells | gross ${gross:,.0f} | "
                 f"{pct_equity}  (weight% basis: {'equity' if equity else 'book gross'})")
    return "\n".join(lines)


def _run_approve_batch(proposals: list[dict], broker) -> None:
    """Whole-book review + ONE confirmation; outliers still get an individual y/N."""
    # SAFETY GUARD (unchanged, same place as `approve`): fail fast on a non-paper account
    # before showing or placing anything.
    assert_paper_account(broker)
    print("\n" + format_book(proposals, _batch_equity(broker)))

    # SAFETY GUARD (unchanged): DRY_RUN runs BEFORE the batch — if set, place nothing.
    if config.DRY_RUN:
        log.error("DRY_RUN is True; batch reviewed but placing nothing.")
        print("DRY_RUN=True -> reviewed the book, placed nothing.")
        return

    flagged = _flag_outliers(proposals)
    if flagged:
        print(f"\n{len(flagged)} order(s) exceed {config.BATCH_OUTLIER_MULT:g}x the average size"
              + (f" or ${config.BATCH_MAX_ORDER_VALUE:,.0f}" if config.BATCH_MAX_ORDER_VALUE > 0 else "")
              + " — each will need its own confirmation.")
    answer = input(f"\nPlace all {len(proposals)} orders? [y/N]: ").strip().lower()
    if answer != "y":
        log.info("DECLINED by user: whole batch of %d order(s) skipped.", len(proposals))
        print("Skipped all orders.")
        return

    placed = 0
    for i, proposal in enumerate(proposals):
        if i in flagged:  # unusually large -> individual confirmation even in batch mode
            print("\n" + format_proposal(proposal))
            reply = input(f"  LARGER-THAN-USUAL — place {proposal['action']} {proposal['shares']} "
                          f"{proposal['symbol']}? [y/N]: ").strip().lower()
            if reply != "y":
                log.info("DECLINED (outlier) by user: %s.", proposal["symbol"])
                print(f"Skipped {proposal['symbol']}.")
                continue
        if _place_proposal(broker, proposal) is not None:  # re-checks DRY_RUN + DU per order
            placed += 1
    log.info("Batch: submitted %d of %d order(s).", placed, len(proposals))
    print(f"\nBatch complete: submitted {placed} of {len(proposals)} order(s).")


def run_gate(proposals: list[dict], broker) -> None:
    """Route proposals through the autonomy gate according to ``AUTONOMY_MODE``."""
    mode = config.AUTONOMY_MODE
    log.info("Autonomy gate: mode=%s, %d proposal(s).", mode, len(proposals))

    if not proposals:
        log.info("No proposals to act on.")
        return

    if mode == "signal_only":
        for proposal in proposals:
            print("\n" + format_proposal(proposal))
            log.info("SIGNAL_ONLY: alerted on %s, placed nothing.", proposal["symbol"])
        return

    if mode == "approve":
        # Fail fast on the safety guard before prompting for anything.
        assert_paper_account(broker)
        for proposal in proposals:
            print("\n" + format_proposal(proposal))
            answer = input(f"Place this paper order for {proposal['symbol']}? [y/N]: ").strip().lower()
            if answer == "y":
                _place_proposal(broker, proposal)
            else:
                log.info("DECLINED by user: %s.", proposal["symbol"])
                print(f"Skipped {proposal['symbol']}.")
        return

    if mode == "approve_batch":
        _run_approve_batch(proposals, broker)
        return

    # semi_auto / full_auto are deliberately not implemented yet (Phase 5).
    log.warning("Autonomy mode %r is not implemented yet; placing nothing.", mode)
