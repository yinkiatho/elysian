from typing import Dict, Optional

from elysian_core.core.order import LimitOrder, Order, RangeOrder


class OrderTracker:
    """
    In-memory registry of open orders.

    Keeps separate buckets for CEX orders (Order), LP limit orders (LimitOrder),
    and LP range orders (RangeOrder).
    """

    def __init__(self):
        self._orders: Dict[str, Order] = {}
        self._limit_orders: Dict[int, LimitOrder] = {}
        self._range_orders: Dict[int, RangeOrder] = {}
        self._balances: Dict[str, float] = {}

    # ── CEX orders ────────────────────────────────────────────────────────────

    @property
    def orders(self) -> Dict[str, Order]:
        return self._orders

    def add_order(self, order: Order):
        self._orders[order.id] = order

    def get_order(self, order_id: str) -> Optional[Order]:
        return self._orders.get(order_id)

    def remove_order(self, order_id: str):
        self._orders.pop(order_id, None)

    def active_orders(self) -> Dict[str, Order]:
        return {oid: o for oid, o in self._orders.items() if o.is_active}

    # ── LP limit orders ───────────────────────────────────────────────────────

    @property
    def limit_orders(self) -> Dict[int, LimitOrder]:
        return self._limit_orders

    def add_limit_order(self, order: LimitOrder):
        self._limit_orders[order.id] = order

    def get_limit_order(self, order_id: int) -> LimitOrder:
        return self._limit_orders[order_id]

    def remove_limit_order(self, order_id: int):
        del self._limit_orders[order_id]

    # ── LP range orders ───────────────────────────────────────────────────────

    @property
    def range_orders(self) -> Dict[int, RangeOrder]:
        return self._range_orders

    def add_range_order(self, order: RangeOrder):
        self._range_orders[order.id] = order

    def get_range_order(self, order_id: int) -> RangeOrder:
        return self._range_orders[order_id]

    def remove_range_order(self, order_id: int):
        del self._range_orders[order_id]

    # ── Balances ──────────────────────────────────────────────────────────────

    @property
    def balances(self) -> Dict[str, float]:
        return self._balances

    def set_balance(self, asset: str, amount: float):
        self._balances[asset] = amount

    def adjust_balance(self, asset: str, delta: float):
        self._balances[asset] = self._balances.get(asset, 0.0) + delta

    def __repr__(self) -> str:
        return (
            f"OrderTracker(cex={len(self._orders)} "
            f"limit={len(self._limit_orders)} "
            f"range={len(self._range_orders)})"
        )
