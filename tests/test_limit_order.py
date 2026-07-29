"""Tests for limit-order construction (offline; no live IBKR connection).

Confirms the limit price, side, buffer, and explicit TIF — the fix for the paper-order
rejections (Error 354 no-market-data on blind MARKET orders, Error 10349 TIF preset).
"""

import pytest

import config
from execution.broker import build_limit_order


def test_buy_limit_sits_above_reference():
    order, price = build_limit_order("BUY", 10, 100.0, buffer=0.005, tif="DAY")
    assert price == 100.5                       # 100 * (1 + 0.005)
    assert order.action == "BUY"
    assert order.totalQuantity == 10
    assert order.lmtPrice == 100.5
    assert order.tif == "DAY"                    # set on the order, not left to a TWS preset


def test_sell_limit_sits_below_reference():
    order, price = build_limit_order("SELL", 5, 200.0, buffer=0.005)
    assert price == 199.0                        # 200 * (1 - 0.005)
    assert order.action == "SELL"
    assert order.lmtPrice == 199.0
    assert order.tif == "DAY"


def test_buffer_is_applied_and_rounded_to_penny():
    _, price = build_limit_order("BUY", 1, 33.333, buffer=0.01)
    assert price == round(33.333 * 1.01, 2)      # marketable + protected, penny-rounded


def test_default_buffer_comes_from_config():
    _, price = build_limit_order("BUY", 1, 100.0)
    assert price == round(100.0 * (1 + config.LIMIT_BUFFER), 2)


def test_custom_tif_is_set():
    order, _ = build_limit_order("BUY", 1, 100.0, tif="GTC")
    assert order.tif == "GTC"


def test_invalid_inputs_raise():
    with pytest.raises(ValueError):
        build_limit_order("HOLD", 10, 100.0)     # bad action
    with pytest.raises(ValueError):
        build_limit_order("BUY", 0, 100.0)       # non-positive qty
    with pytest.raises(ValueError):
        build_limit_order("BUY", 10, 0.0)        # non-positive reference price
    with pytest.raises(ValueError):
        build_limit_order("BUY", 10, None)       # missing reference price
