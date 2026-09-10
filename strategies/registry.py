"""Portfolio-strategy registry: register / lookup / list.

Cross-sectional, monthly, leverage-overlay strategies (e.g. vol-managed momentum)
register by their ``name`` — adding a new one to the library is one
``@register_portfolio`` decorator away.
"""

import logging

log = logging.getLogger(__name__)

_PORTFOLIO_REGISTRY: dict[str, type] = {}


def register_portfolio(strategy_cls: type) -> type:
    """Class decorator: register a portfolio Strategy subclass under its ``name``."""
    name = getattr(strategy_cls, "name", None)
    if not name:
        raise ValueError(f"{strategy_cls.__name__} must define a non-empty 'name'.")
    _PORTFOLIO_REGISTRY[name] = strategy_cls
    return strategy_cls


def get_portfolio(name: str) -> type:
    """Return the registered portfolio strategy class for ``name``."""
    return _PORTFOLIO_REGISTRY[name]


def portfolio_strategies() -> dict[str, type]:
    """Return a copy of the portfolio-strategy registry mapping name -> class."""
    return dict(_PORTFOLIO_REGISTRY)
