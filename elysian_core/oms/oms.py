import asyncio
from abc import ABC, abstractmethod
from typing import List, Optional

import pylru

from elysian_core.core.enums import RangeOrderType
from elysian_core.core.order import LimitOrder, Order, RangeOrder
from elysian_core.oms.order_tracker import OrderTracker
import elysian_core.utils.logger as log

logger = log.setup_custom_logger("root")


class AbstractOMS(ABC):
    """
    Generic Order Management System interface.

    Concrete subclasses implement _place_order / _cancel_order for their
    specific exchange protocol (Chainflip API, Binance REST, TQ NATS, etc.).
    """

    def __init__(self, account_id: str):
        self._account_id = account_id
        self._tracker = OrderTracker()
        self._sent_cache: pylru.lrucache = pylru.lrucache(size=200)
        self._cancelled_cache: pylru.lrucache = pylru.lrucache(size=200)

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def open_orders(self):
        return self._tracker.active_orders()

    @property
    def open_limit_orders(self):
        return self._tracker.limit_orders

    @property
    def open_range_orders(self):
        return self._tracker.range_orders

    @property
    def balances(self):
        return self._tracker.balances

    # ── Abstract exchange interface ───────────────────────────────────────────

    @abstractmethod
    async def _place_limit_order(self, order: LimitOrder) -> bool:
        """Send a limit order to the exchange. Return True on success."""
        ...

    @abstractmethod
    async def _cancel_limit_order(self, order: LimitOrder) -> bool:
        """Cancel a limit order on the exchange. Return True on success."""
        ...

    @abstractmethod
    async def _place_range_order(self, order: RangeOrder) -> bool:
        """Place a range (LP) order. Return True on success."""
        ...

    @abstractmethod
    async def _cancel_range_order(self, order: RangeOrder) -> bool:
        """Cancel a range (LP) order. Return True on success."""
        ...

    @abstractmethod
    async def refresh_balances(self):
        """Fetch current balances from the exchange and update tracker."""
        ...

    # ── Limit order lifecycle ─────────────────────────────────────────────────

    async def send_limit_order(self, order: LimitOrder):
        if order.amount == 0:
            logger.info(f"Skipping zero-amount limit order id={order.id}")
            return
        if order.id in self._sent_cache:
            return
        ok = await self._place_limit_order(order)
        if ok:
            self._sent_cache[order.id] = True
            self._tracker.add_limit_order(order)
            logger.info(f"Limit order placed: {order}")

    async def cancel_limit_order(self, order: LimitOrder):
        if order.id in self._cancelled_cache:
            return
        try:
            order = self._tracker.get_limit_order(order.id)
        except KeyError:
            logger.error(f"cancel_limit_order: id={order.id} not in tracker")
            return
        order.amount = 0
        ok = await self._cancel_limit_order(order)
        if ok:
            self._cancelled_cache[order.id] = True
            self._tracker.remove_limit_order(order.id)
            logger.info(f"Limit order cancelled: id={order.id}")

    async def update_limit_order(
        self,
        order: LimitOrder,
        price: Optional[float] = None,
        amount: Optional[float] = None,
    ):
        if price is not None:
            order.price = price
        if amount is not None:
            order.amount = amount
        ok = await self._place_limit_order(order)
        if ok:
            self._tracker.add_limit_order(order)
            logger.info(f"Limit order updated: id={order.id}")

    async def send_limit_orders(self, orders: List[LimitOrder]):
        for order in orders:
            asyncio.create_task(self.send_limit_order(order))

    async def cancel_limit_orders(self, orders: List[LimitOrder]):
        for order in orders:
            asyncio.create_task(self.cancel_limit_order(order))

    # ── Range order lifecycle ─────────────────────────────────────────────────

    async def send_range_order(self, order: RangeOrder):
        if order.amount == 0:
            logger.info(f"Skipping zero-amount range order id={order.id}")
            return
        if order.id in self._sent_cache:
            return
        ok = await self._place_range_order(order)
        if ok:
            self._sent_cache[order.id] = True
            self._tracker.add_range_order(order)
            logger.info(f"Range order placed: {order}")

    async def cancel_range_order(self, order: RangeOrder):
        order.amount = 0
        order.order_type = RangeOrderType.LIQUIDITY
        ok = await self._cancel_range_order(order)
        if ok:
            self._tracker.remove_range_order(order.id)
            logger.info(f"Range order cancelled: id={order.id}")

    async def update_range_order(
        self,
        order: RangeOrder,
        amount: Optional[float] = None,
        lower_price: Optional[float] = None,
        upper_price: Optional[float] = None,
    ):
        if amount is not None:
            order.amount = amount
        if lower_price is not None:
            order.lower_price = lower_price
        if upper_price is not None:
            order.upper_price = upper_price
        ok = await self._place_range_order(order)
        if ok:
            self._tracker.add_range_order(order)
            logger.info(f"Range order updated: id={order.id}")

    async def send_range_orders(self, orders: List[RangeOrder]):
        for order in orders:
            asyncio.create_task(self.send_range_order(order))

    async def cancel_range_orders(self, orders: List[RangeOrder]):
        for order in orders:
            asyncio.create_task(self.cancel_range_order(order))

    # ── Bulk cancel ───────────────────────────────────────────────────────────

    async def cancel_all(self):
        """Cancel all tracked open orders."""
        await self.cancel_limit_orders(list(self._tracker.limit_orders.values()))
        await self.cancel_range_orders(list(self._tracker.range_orders.values()))
