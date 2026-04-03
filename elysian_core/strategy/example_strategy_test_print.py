"""
A simple strategy that prints all received events for debugging or monitoring.
"""

from elysian_core.strategy.base_strategy import SpotStrategy
from elysian_core.core.events import (
    KlineEvent,
    OrderBookUpdateEvent,
    OrderUpdateEvent,
    BalanceUpdateEvent,
    RebalanceCompleteEvent,
)
import elysian_core.utils.logger as log

logger = log.setup_custom_logger("PrintEventsStrategy")


class PrintEventsStrategy(SpotStrategy):
    """
    Strategy that prints/logs every event it receives.

    Useful for debugging, verifying event flow, or monitoring system activity.
    """

    def __init__(self, *args, symbols: set = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._symbols = symbols or set()

    async def on_start(self):
        """Log strategy start and configure symbols if needed."""
        if not self._symbols and self.cfg:
            self._symbols = set(self.cfg.symbols.symbols_for("binance", "spot"))

        logger.info(
            f"[PrintEventsStrategy] Started. Monitoring symbols: {self._symbols}"
        )

    async def on_stop(self):
        """Convert all assets back to base currency on stop."""
        await self.request_rebalance(convert_all_base=True)


    async def run_forever(self):
        """Block until stop is called."""
        logger.info("[PrintEventsStrategy] Entering event loop (waiting for events).")
        await self._stop_event.wait()
        logger.info("[PrintEventsStrategy] Stopped.")


    async def on_kline(self, event: KlineEvent):
        """Print kline events."""
        if event.symbol not in self._symbols:
            return
        # logger.info(
        #     f"[KLINE] {event.venue.name} {event.symbol} "
        #     f"close={event.kline.close} "
        #     f"volume={event.kline.volume} "
        #     f"timestamp={event.kline.end_time}"
        # )

    async def on_orderbook_update(self, event: OrderBookUpdateEvent):
        """Print orderbook updates (truncated for readability)."""
        if event.symbol not in self._symbols:
            return
        best_bid = event.orderbook.best_bid_price
        best_ask = event.orderbook.best_ask_price
        # logger.info(
        #     f"[ORDERBOOK] {event.venue.name} {event.symbol} "
        #     f"bid={best_bid} ask={best_ask} "
        #     f"timestamp={event.timestamp}"
        # )

    async def on_order_update(self, event: OrderUpdateEvent):
        """Print order updates."""
        order = event.order
        logger.info(
            f"[ORDER] {event.venue.name} {event.symbol} "
            f"order_id={order.order.id} status={order.status} "
            f"filled_qty={order.filled_qty} remaining_qty={order.remaining_qty} "
            f"timestamp={event.timestamp}"
        )

    async def on_balance_update(self, event: BalanceUpdateEvent):
        """Print balance updates."""
        logger.info(
            f"[BALANCE] {event.venue.name} {event.asset} "
            f"delta={event.delta} new_balance={event.new_balance} "
            f"timestamp={event.timestamp}"
        )

    async def on_rebalance_complete(self, event: RebalanceCompleteEvent):
        """Print rebalance completion results."""
        logger.info(
            f"[REBALANCE COMPLETE] result={event.result} "
            f"errors={event.result.errors} "
            f"validated_weights={event.validated_weights}"
        )