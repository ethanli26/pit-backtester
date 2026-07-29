"""Interactive Brokers paper-trading connection built on ib_async.

This wraps a single ``ib_async.IB`` session and exposes a small, readable API:
connect, get_account_summary, place_limit_order (the path used for paper orders),
place_market_order, and disconnect. All connection settings come from :mod:`config`
(host, port, client id) and are never hardcoded.

ib_async is built on asyncio, but its synchronous methods (``connect``,
``accountSummary``, ``placeOrder``, ``disconnect``) run ib_async's own event loop
under the hood. That means a plain script can call these methods directly without
managing ``asyncio`` itself — which is exactly what ``scripts/test_connection.py``
relies on.
"""

import logging
import time

from ib_async import IB, LimitOrder, MarketOrder, Stock

import config

log = logging.getLogger(__name__)

# Orders are placed for US stocks routed through IB's SMART router in USD.
ROUTING_EXCHANGE = "SMART"
ORDER_CURRENCY = "USD"

# Order statuses IBKR reports while an order is alive (not yet terminal).
_WORKING_STATUSES = {"PendingSubmit", "ApiPending", "PreSubmitted", "Submitted"}


def build_limit_order(action: str, qty: int, reference_price: float,
                      buffer: float = config.LIMIT_BUFFER, tif: str = "DAY") -> tuple[LimitOrder, float]:
    """Build a marketable-but-protected LIMIT order priced off a reference price.

    BUY limit = ``ref * (1 + buffer)``; SELL limit = ``ref * (1 - buffer)`` (rounded to a
    penny). ``tif`` is set EXPLICITLY on the order so a TWS preset cannot override it
    (that override is the Error 10349 rejection). DAY is used by default: it is accepted
    cleanly and a momentum rebalance should not rest overnight. Pure/offline — no network.
    """
    action = action.upper()
    if action not in ("BUY", "SELL"):
        raise ValueError(f"action must be 'BUY' or 'SELL', got {action!r}")
    if qty <= 0:
        raise ValueError(f"qty must be a positive integer, got {qty!r}")
    if reference_price is None or reference_price <= 0:
        raise ValueError(f"reference_price must be > 0 for a limit order, got {reference_price!r}")

    sign = 1.0 if action == "BUY" else -1.0
    limit_price = round(reference_price * (1.0 + sign * buffer), 2)
    order = LimitOrder(action, qty, limit_price)
    order.tif = tif   # explicit, so a TWS preset can't override it (avoids Error 10349)
    return order, limit_price

# Account summary tags we care about, mapped to the friendly keys we return.
_SUMMARY_TAGS = {
    "NetLiquidation": "net_liquidation",
    "AvailableFunds": "available_funds",
    "BuyingPower": "buying_power",
}


class IBBroker:
    """A thin, reconnecting wrapper around an ib_async IB session.

    Reconnection: :meth:`connect` (and every operation, via
    :meth:`ensure_connected`) retries with capped exponential backoff, so a
    refused or dropped connection is retried a few times before giving up.
    """

    def __init__(
        self,
        host: str = config.IB_HOST,
        port: int = config.IB_PORT,
        client_id: int = config.IB_CLIENT_ID,
        max_retries: int = 5,
        base_backoff: float = 1.0,
        backoff_cap: float = 30.0,
        connect_timeout: float = 10.0,
    ) -> None:
        self.host = host
        self.port = port
        self.client_id = client_id
        self.max_retries = max_retries
        self.base_backoff = base_backoff
        self.backoff_cap = backoff_cap
        self.connect_timeout = connect_timeout

        self.ib = IB()
        # Distinguishes a deliberate disconnect() from an unexpected drop.
        self._intentional_disconnect = False
        self.ib.disconnectedEvent += self._on_disconnected

    # --- Connection lifecycle ---------------------------------------------

    def connect(self) -> None:
        """Connect to TWS/IB Gateway, wait until ready, and log success.

        Retries with exponential backoff if the connection cannot be
        established. Raises :class:`ConnectionError` if every attempt fails.
        """
        self._intentional_disconnect = False
        self._connect_with_backoff()

    def _connect_with_backoff(self) -> None:
        """Attempt to connect, retrying with capped exponential backoff."""
        delay = self.base_backoff
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            log.info(
                "Connecting to IBKR at %s:%s (client id %s) — attempt %d/%d",
                self.host,
                self.port,
                self.client_id,
                attempt,
                self.max_retries,
            )
            try:
                self.ib.connect(
                    self.host,
                    self.port,
                    clientId=self.client_id,
                    timeout=self.connect_timeout,
                )
                if not self.ib.isConnected():
                    raise ConnectionError("connect() returned but socket is not connected")

                # By the time connect() returns, ib_async has received the list
                # of managed accounts, so the session is ready to use.
                accounts = self.ib.managedAccounts()
                log.info("Connected to IBKR. Managed account(s): %s", ", ".join(accounts))
                return
            except Exception as error:  # noqa: BLE001 - log and retry any failure
                last_error = error
                log.warning("Connection attempt %d/%d failed: %s", attempt, self.max_retries, error)
                if attempt < self.max_retries:
                    log.info("Retrying in %.1fs...", delay)
                    time.sleep(delay)
                    delay = min(delay * 2, self.backoff_cap)

        raise ConnectionError(
            f"Could not connect to IBKR after {self.max_retries} attempts"
        ) from last_error

    def ensure_connected(self) -> None:
        """Reconnect (with backoff) if the session has dropped."""
        if self.ib.isConnected():
            return
        log.warning("IBKR connection is down; attempting to reconnect...")
        self._connect_with_backoff()

    def _on_disconnected(self) -> None:
        """Log dropped connections so they are visible in the logs.

        Recovery is handled lazily by :meth:`ensure_connected` before the next
        operation, which keeps reconnection off ib_async's event-loop callback
        and avoids re-entrant connect calls.
        """
        if self._intentional_disconnect:
            log.info("Disconnected from IBKR (requested).")
        else:
            log.warning("Lost connection to IBKR unexpectedly.")

    def disconnect(self) -> None:
        """Cleanly disconnect from TWS/IB Gateway."""
        self._intentional_disconnect = True
        if self.ib.isConnected():
            log.info("Disconnecting from IBKR...")
            self.ib.disconnect()
        else:
            log.info("Already disconnected from IBKR.")

    # --- Account and orders ------------------------------------------------

    def get_account_id(self) -> str:
        """Return the connected (first managed) account id, e.g. 'DU1234567'."""
        self.ensure_connected()
        return self.ib.managedAccounts()[0]

    def get_positions(self) -> list[dict]:
        """Return current open positions as a list of dicts (read-only).

        Each entry is ``{symbol, shares, market_value}``. Returns an empty list
        when there are no open positions. Places no orders.
        """
        self.ensure_connected()
        positions = []
        for item in self.ib.portfolio():
            positions.append(
                {
                    "symbol": item.contract.symbol,
                    "shares": item.position,
                    "market_value": item.marketValue,
                }
            )
        return positions

    def get_account_summary(self) -> dict[str, float]:
        """Return net liquidation, available funds, and buying power.

        Values are returned as floats keyed by ``net_liquidation``,
        ``available_funds``, and ``buying_power``.
        """
        self.ensure_connected()

        account = self.ib.managedAccounts()[0]
        rows = self.ib.accountSummary(account)

        summary: dict[str, float] = {}
        for row in rows:
            key = _SUMMARY_TAGS.get(row.tag)
            if key is not None:
                summary[key] = float(row.value)

        log.info("Account summary for %s: %s", account, summary)
        return summary

    def place_market_order(self, symbol: str, qty: int, action: str):
        """Place a market order for a US stock and return the Trade object.

        ``action`` must be ``"BUY"`` or ``"SELL"``. The contract is a SMART-routed
        US stock in USD. This method is intentionally not called anywhere yet.
        """
        action = action.upper()
        if action not in ("BUY", "SELL"):
            raise ValueError(f"action must be 'BUY' or 'SELL', got {action!r}")
        if qty <= 0:
            raise ValueError(f"qty must be a positive integer, got {qty!r}")

        self.ensure_connected()

        contract = Stock(symbol, ROUTING_EXCHANGE, ORDER_CURRENCY)
        self.ib.qualifyContracts(contract)

        order = MarketOrder(action, qty)
        trade = self.ib.placeOrder(contract, order)
        log.info("Placed %s market order: %d %s", action, qty, symbol)
        return trade

    def place_limit_order(self, symbol: str, qty: int, action: str, reference_price: float,
                          buffer: float = config.LIMIT_BUFFER, tif: str = "DAY",
                          status_wait: float = 1.5):
        """Place a price-protected LIMIT order and log its ACTUAL status (not optimistic).

        A limit order carries a price, so a paper account without a live market-data
        subscription is not blocked from submitting it (the Error 354 case). After
        submitting, we wait briefly and log the real order status — Filled / Working /
        or Cancelled WITH the reason — so a rejection/cancel is visible immediately.
        """
        order, limit_price = build_limit_order(action, qty, reference_price, buffer, tif)
        self.ensure_connected()

        contract = Stock(symbol, ROUTING_EXCHANGE, ORDER_CURRENCY)
        self.ib.qualifyContracts(contract)

        trade = self.ib.placeOrder(contract, order)
        log.info("Submitted %s LIMIT %d %s @ %.2f (ref %.2f, buffer %.2f%%, TIF %s).",
                 order.action, qty, symbol, limit_price, reference_price, buffer * 100, tif)
        self._log_order_status(trade, status_wait)
        return trade

    def _log_order_status(self, trade, status_wait: float) -> str:
        """Wait briefly for IB to respond, then log the order's ACTUAL status + reason."""
        try:
            self.ib.sleep(status_wait)   # run ib_async's event loop so status/log update
        except Exception as error:  # noqa: BLE001 - never let status polling raise
            log.warning("Could not poll order status: %s", error)

        status = trade.orderStatus.status
        symbol = trade.contract.symbol
        # The cancel/reject reason (e.g. 354, 10349) arrives as a trade-log message.
        reason = trade.log[-1].message if getattr(trade, "log", None) else ""
        if status == "Filled":
            log.info("FILLED %s %d %s @ %.2f.", trade.order.action,
                     trade.orderStatus.filled, symbol, trade.orderStatus.avgFillPrice)
        elif status in _WORKING_STATUSES:
            log.info("WORKING %s %d %s @ %.2f (status=%s; not yet filled).", trade.order.action,
                     trade.order.totalQuantity, symbol, trade.order.lmtPrice, status)
        else:  # Cancelled / ApiCancelled / Inactive — surface it, do not mask as "placed"
            log.warning("NOT WORKING %s %d %s @ %.2f: status=%s%s", trade.order.action,
                        trade.order.totalQuantity, symbol, trade.order.lmtPrice, status,
                        f" — {reason}" if reason else "")
        return status
