"""Strategy library: pluggable, registered portfolio strategies held to an honest bar.

Importing this package registers the built-in portfolio strategies (vol-managed
momentum) so the portfolio registry is populated automatically.
"""

from strategies import vol_managed_momentum  # noqa: F401  (registers on import)
