"""
Minimal test strategy that prints every event hook invocation.
Used to verify the EventBus wiring end-to-end.
"""

import datetime
from elysian_core.strategy.base_strategy import SpotStrategy
from elysian_core.core.events import (
    KlineEvent,
    OrderBookUpdateEvent,
    OrderUpdateEvent,
    BalanceUpdateEvent,
)
import elysian_core.utils.logger as log

logger = log.setup_custom_logger("root")

_UTC8 = datetime.timezone(datetime.timedelta(hours=8))


def _ts(epoch_ms: int) -> str:
    return datetime.datetime.fromtimestamp(epoch_ms / 1000, tz=_UTC8).strftime("%H:%M:%S.%f")[:-3]


class TestPrintStrategy(SpotStrategy):
    """Prints a one-liner for every event received. No trading logic."""

    async def on_start(self):
        self._kline_count = 0
        self._ob_count = 0
        self._order_count = 0
        self._balance_count = 0
        logger.info("[TestStrategy] started — listening for events")

    async def on_stop(self):
        logger.info(
            f"[TestStrategy] stopped — totals: "
            f"klines={self._kline_count}  ob={self._ob_count}  "
            f"orders={self._order_count}  balances={self._balance_count}"
        )

    async def on_kline(self, event: KlineEvent):
        self._kline_count += 1
        k = event.kline
        logger.info(
            f"[TestStrategy] KLINE  {event.symbol}  "
            f"O={k.open:.4f} H={k.high:.4f} L={k.low:.4f} C={k.close:.4f}  "
            f"ts={_ts(event.timestamp)}"
        )

    async def on_orderbook_update(self, event: OrderBookUpdateEvent):
        self._ob_count += 1
        ob = event.orderbook
        # Only print every 50th update per symbol to avoid flooding
        if self._ob_count % 50 == 0:
            logger.info(
                f"[TestStrategy] OB     {event.symbol}  "
                f"bid={ob.best_bid_price:.4f}  ask={ob.best_ask_price:.4f}  "
                f"spread={ob.spread:.6f}  "
                f"ts={_ts(event.timestamp)}  (total={self._ob_count})"
            )

    async def on_order_update(self, event: OrderUpdateEvent):
        self._order_count += 1
        o = event.order
        logger.info(
            f"[TestStrategy] ORDER  {event.symbol}  "
            f"id={o.id}  side={o.side.value}  status={o.status.value}  "
            f"qty={o.quantity}  filled={o.filled_qty}  avg={o.avg_fill_price:.6f}  "
            f"ts={_ts(event.timestamp)}"
        )

    async def on_balance_update(self, event: BalanceUpdateEvent):
        self._balance_count += 1
        logger.info(
            f"[TestStrategy] BAL    {event.asset}  "
            f"delta={event.delta:+.8f}  "
            f"ts={_ts(event.timestamp)}"
        )
