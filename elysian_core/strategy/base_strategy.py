"""
Event-driven strategy base class for SPOT trading.

Subclass :class:`SpotStrategy` and override the ``on_*`` hooks you care about.
All hooks are async and receive typed event dataclasses.  Hooks that are not
overridden are silently ignored (no-op).

Usage::

    class MyArb(SpotStrategy):
        async def on_kline(self, event: KlineEvent):
            price = event.kline.close
            ...

        async def on_orderbook_update(self, event: OrderBookUpdateEvent):
            spread = event.orderbook.spread
            ...

    strategy = MyArb(
        exchanges={Venue.BINANCE: exchange},
        event_bus=bus,
    )
    await strategy.start()
"""

import asyncio
from concurrent.futures import ProcessPoolExecutor
from typing import Dict, Optional

from elysian_core.connectors.base import SpotExchangeConnector, AbstractDataFeed
from elysian_core.core.enums import Side, Venue
from elysian_core.core.event_bus import EventBus
from elysian_core.core.events import (
    EventType,
    KlineEvent,
    OrderBookUpdateEvent,
    OrderUpdateEvent,
    BalanceUpdateEvent,
)
import elysian_core.utils.logger as log

logger = log.setup_custom_logger("root")

_STABLECOINS = frozenset({"USDC", "USDT", "BUSD"})


class SpotStrategy:
    """Base class for event-driven spot strategies.

    All ``on_*`` hooks have default no-op implementations — override only the
    ones you need.  For CPU-heavy calculations, use :meth:`run_heavy` to
    offload work to a :class:`ProcessPoolExecutor`.
    """

    def __init__(
        self,
        exchanges: Dict[Venue, SpotExchangeConnector],
        event_bus: EventBus,
        feeds: Optional[Dict[str, AbstractDataFeed]] = None,
        max_heavy_workers: int = 4,
    ):
        self._exchanges = exchanges
        self._event_bus = event_bus
        self._feeds = feeds or {}
        self._executor = ProcessPoolExecutor(max_workers=max_heavy_workers)
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def start(self):
        """Register all hooks with the event bus and call :meth:`on_start`."""
        self._loop = asyncio.get_running_loop()

        self._event_bus.subscribe(EventType.KLINE, self._dispatch_kline)
        self._event_bus.subscribe(EventType.ORDERBOOK_UPDATE, self._dispatch_ob)
        self._event_bus.subscribe(EventType.ORDER_UPDATE, self._dispatch_order)
        self._event_bus.subscribe(EventType.BALANCE_UPDATE, self._dispatch_balance)

        await self.on_start()
        logger.info("SpotStrategy started")

    async def stop(self):
        """Unsubscribe from bus, shut down executor, call :meth:`on_stop`."""
        self._event_bus.unsubscribe(EventType.KLINE, self._dispatch_kline)
        self._event_bus.unsubscribe(EventType.ORDERBOOK_UPDATE, self._dispatch_ob)
        self._event_bus.unsubscribe(EventType.ORDER_UPDATE, self._dispatch_order)
        self._event_bus.unsubscribe(EventType.BALANCE_UPDATE, self._dispatch_balance)

        self._executor.shutdown(wait=False)
        await self.on_stop()
        logger.info("SpotStrategy stopped")

    # ── Internal dispatch (error isolation) ────────────────────────────────────

    async def _dispatch_kline(self, event: KlineEvent):
        try:
            await self.on_kline(event)
        except Exception as e:
            logger.error(f"SpotStrategy.on_kline error: {e}", exc_info=True)

    async def _dispatch_ob(self, event: OrderBookUpdateEvent):
        try:
            await self.on_orderbook_update(event)
        except Exception as e:
            logger.error(f"SpotStrategy.on_orderbook_update error: {e}", exc_info=True)

    async def _dispatch_order(self, event: OrderUpdateEvent):
        try:
            await self.on_order_update(event)
        except Exception as e:
            logger.error(f"SpotStrategy.on_order_update error: {e}", exc_info=True)

    async def _dispatch_balance(self, event: BalanceUpdateEvent):
        try:
            await self.on_balance_update(event)
        except Exception as e:
            logger.error(f"SpotStrategy.on_balance_update error: {e}", exc_info=True)

    # ── Hook functions (override in subclass) ──────────────────────────────────

    async def on_start(self):
        """Called once when the strategy starts. Override for init logic."""
        pass

    async def on_stop(self):
        """Called on shutdown. Override for cleanup."""
        pass

    async def run_forever(self):
        """Optional long-running coroutine for background strategy tasks.

        Override this if your strategy needs its own event loop (e.g. periodic
        rebalancing, timer-based signals).  The runner will ``asyncio.gather``
        this alongside the feed tasks so both run concurrently.

        Default implementation does nothing (returns immediately).  If you
        override this, it should block (e.g. ``while True: await asyncio.sleep(...)``).
        """
        pass

    async def on_kline(self, event: KlineEvent):
        """Called on each closed kline candle."""
        pass

    async def on_orderbook_update(self, event: OrderBookUpdateEvent):
        """Called on each orderbook depth update (~100ms per symbol)."""
        pass

    async def on_order_update(self, event: OrderUpdateEvent):
        """Called when an order status changes (new, partial fill, fill, cancel)."""
        pass

    async def on_balance_update(self, event: BalanceUpdateEvent):
        """Called when account balance changes."""
        pass

    # ── Exchange access ────────────────────────────────────────────────────────

    def get_exchange(self, venue: Venue = Venue.BINANCE) -> SpotExchangeConnector:
        """Return the exchange connector for the given venue."""
        return self._exchanges[venue]

    # ── Feed access ────────────────────────────────────────────────────────────

    def get_feed(self, symbol: str) -> Optional[AbstractDataFeed]:
        return self._feeds.get(symbol)

    # ── Heavy compute offloading ───────────────────────────────────────────────

    async def run_heavy(self, fn, *args):
        """Run a CPU-bound function in a separate process.

        ``fn`` must be a top-level picklable function (not a lambda or method).
        Returns the function's result.
        """
        return await self._loop.run_in_executor(self._executor, fn, *args)

    # ── Price helpers (reused from StrategyEngine) ─────────────────────────────

    async def get_current_price(
        self, pair: str, side: Optional[Side] = None
    ) -> Optional[float]:
        """Return the current price for *pair*.

        Resolution order:
          1. Direct feed lookup (bid/ask/mid depending on *side*)
          2. Inverted pair (quote+base)
          3. Synthetic construction via USDT legs (base/USDT / quote/USDT)
        """
        try:
            feed = self._feeds.get(pair)
            if feed and feed.data:
                if side == Side.BUY:
                    return feed.latest_ask_price
                elif side == Side.SELL:
                    return feed.latest_bid_price
                else:
                    return feed.latest_price

            base, quote = self._split_pair(pair)

            # Inverted pair
            inverted = (quote or "") + (base or "")
            inv_feed = self._feeds.get(inverted)
            if inv_feed and inv_feed.data:
                if side == Side.BUY:
                    return 1.0 / inv_feed.latest_ask_price
                elif side == Side.SELL:
                    return 1.0 / inv_feed.latest_bid_price
                else:
                    return 1.0 / inv_feed.latest_price

            # Stablecoin proxy
            if quote and quote in _STABLECOINS and base and base not in _STABLECOINS:
                alt_quote = next(
                    (s for s in _STABLECOINS if s != quote and base + s in self._feeds),
                    None,
                )
                if alt_quote:
                    return await self.get_current_price(base + alt_quote, side=side)

            # Synthetic via USDT legs
            if base and quote:
                feed_base = self._feeds.get(base + "USDT")
                feed_quote = self._feeds.get(quote + "USDT")
                if feed_base and feed_base.data and feed_quote and feed_quote.data:
                    if side == Side.BUY:
                        return feed_base.latest_ask_price / feed_quote.latest_bid_price
                    elif side == Side.SELL:
                        return feed_base.latest_bid_price / feed_quote.latest_ask_price
                    else:
                        return feed_base.latest_price / feed_quote.latest_price

            logger.warning(f"[SpotStrategy] No price found for {pair}")
            return None

        except Exception as e:
            logger.error(f"[SpotStrategy] get_current_price({pair}) error: {e}")
            return None


    def get_current_vol(self, symbol: str) -> Optional[float]:
        """Return annualised rolling volatility in bps for a feed, or None."""
        feed = self._feeds.get(symbol)
        if feed and feed.volatility is not None:
            return feed.volatility * 10_000
        return None
    

    def _split_pair(self, pair: str):
        """Split a symbol like 'ETHUSDT' into ('ETH', 'USDT')."""
        known_tokens: set = set()
        for sym in self._feeds:
            for stable in _STABLECOINS:
                if sym.endswith(stable):
                    known_tokens.add(sym[: -len(stable)])
                    known_tokens.add(stable)

        for token in known_tokens:
            if pair.startswith(token) and pair[len(token):] in known_tokens:
                return token, pair[len(token):]
            if pair.endswith(token) and pair[: -len(token)] in known_tokens:
                return pair[: -len(token)], token

        return None, None
