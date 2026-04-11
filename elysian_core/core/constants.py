"""
Shared constants for the Elysian trading system.

Centralises values that would otherwise be hard-coded (or re-defined) in
multiple modules.  Import from here instead of redeclaring locally.
"""

# Assets treated as quote / settlement currency (price = 1.0 by definition).
# Used throughout ShadowBook, ExecutionEngine, and exchange connectors to
# decide whether a commission or balance delta is denominated in a stablecoin.
STABLECOINS: frozenset = frozenset({"USDT", "USDC", "BUSD"})

# Maximum number of Fill records kept per ShadowBook (ring buffer).
FILL_HISTORY_MAXLEN: int = 10_000

# Quantity threshold below which a fill delta is treated as zero and ignored.
QTY_EPSILON: float = 1e-12
