import asyncio
import datetime
from abc import ABC, abstractmethod
from typing import Dict, List, Optional

from elysian_core.connectors.base import AbstractDataFeed
from elysian_core.core.enums import Side
import elysian_core.utils.logger as log

logger = log.setup_custom_logger("root")

_STABLECOINS = frozenset({"USDC", "USDT", "BUSD"})


class StrategyEngine(ABC):
    """
    Base class for event-driven trading strategies.

    Wires together a set of data feeds and an exchange client.
    Subclasses implement `on_event()` with their signal logic.

    Usage:
        class MyStrategy(StrategyEngine):
            async def on_event(self, event: dict):
                price = await self.get_current_price("ETHUSDT")
                ...

        engine = MyStrategy(feeds={"ETHUSDT": ob_feed}, exchange=exchange)
        await engine.run()
    """

    def __init__(
        self,
        feeds: Dict[str, AbstractDataFeed],
        exchange,                              # BinanceSpotExchange or any exchange client
    ):
        self._feeds = feeds
        self._exchange = exchange
        self._trade_id = 0
        self._utc8 = datetime.timezone(datetime.timedelta(hours=8))

    # ── Feed access ───────────────────────────────────────────────────────────

    def get_feed(self, symbol: str) -> Optional[AbstractDataFeed]:
        return self._feeds.get(symbol)

    # ── Price helpers ─────────────────────────────────────────────────────────

    async def get_current_price(
        self, pair: str, side: Optional[Side] = None
    ) -> Optional[float]:
        """
        Return the current price for `pair`.

        Resolution order:
          1. Direct feed lookup (bid/ask/mid depending on `side`)
          2. Inverted pair (quote+base)
          3. Synthetic construction via USDT legs (base/USDT ÷ quote/USDT)
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

            # Identify base / quote by scanning known feed symbols
            base, quote = self._split_pair(pair)

            # Try inverted pair
            inverted = (quote or "") + (base or "")
            inv_feed = self._feeds.get(inverted)
            if inv_feed and inv_feed.data:
                if side == Side.BUY:
                    return 1.0 / inv_feed.latest_ask_price
                elif side == Side.SELL:
                    return 1.0 / inv_feed.latest_bid_price
                else:
                    return 1.0 / inv_feed.latest_price

            # Stablecoin proxy: ETHUSDC → use ETHUSDT
            if quote and quote in _STABLECOINS and base and base not in _STABLECOINS:
                alt_quote = next(
                    (s for s in _STABLECOINS if s != quote and base + s in self._feeds),
                    None,
                )
                if alt_quote:
                    return await self.get_current_price(base + alt_quote, side=side)

            # Synthetic via USDT legs: base/USDT ÷ quote/USDT
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

            logger.warning(f"[StrategyEngine] No price found for {pair}")
            return None

        except Exception as e:
            logger.error(f"[StrategyEngine] get_current_price({pair}) error: {e}")
            return None

    def get_current_vol(self, symbol: str) -> Optional[float]:
        """Return annualised rolling volatility in bps for a feed, or None."""
        feed = self._feeds.get(symbol)
        if feed and feed.volatility is not None:
            return feed.volatility * 10_000
        return None

    def _split_pair(self, pair: str):
        """
        Split a symbol like "ETHUSDT" into ("ETH", "USDT") using known feed names
        as a reference set for token boundaries.
        """
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

    # ── SUI / cross-chain price ───────────────────────────────────────────────

    async def get_to_sui_price(self, token: str) -> Optional[float]:
        """
        Return how many SUI one unit of `token` is worth.
        Tries direct SUI pair first, then constructs via USDT.
        """
        try:
            direct = self._feeds.get(f"SUI{token}")
            if direct and direct.data:
                return 1.0 / direct.latest_price

            token_usdt = self._feeds.get(f"{token}USDT")
            sui_usdt = self._feeds.get("SUIUSDT")
            if token_usdt and token_usdt.data and sui_usdt and sui_usdt.data:
                return token_usdt.latest_price / sui_usdt.latest_price

            logger.warning(f"[StrategyEngine] No SUI price for {token}")
            return None
        except Exception as e:
            logger.error(f"[StrategyEngine] get_to_sui_price({token}) error: {e}")
            return None

    # ── Strategy hook ─────────────────────────────────────────────────────────

    @abstractmethod
    async def on_event(self, event: dict):
        """
        Called for each incoming event (swap, auction tick, etc.).
        Subclasses implement their signal + order logic here.
        """
        ...

    # ── Background loops ──────────────────────────────────────────────────────

    async def _event_loop(self, event_source):
        """
        Pull events from `event_source` (any object with a `.latest_event`
        and `.last_timestamp` attribute) and dispatch to `on_event()`.
        """
        last_ts = None
        while True:
            ts = getattr(event_source, "last_timestamp", None)
            if ts is None or ts == last_ts:
                await asyncio.sleep(0.0025)
                continue
            last_ts = ts
            event = getattr(event_source, "latest_event", None)
            if event is not None:
                await self.on_event(event)

    async def run(self, event_sources: Optional[List] = None):
        """
        Start the engine.  Pass a list of event_source objects to drive
        `on_event()`; each gets its own `_event_loop` task.
        """
        tasks = []
        for src in (event_sources or []):
            tasks.append(asyncio.create_task(self._event_loop(src)))

        logger.info("[StrategyEngine] Running.")
        if tasks:
            await asyncio.gather(*tasks)
        else:
            # Idle until cancelled — feeds run in their own tasks
            await asyncio.Event().wait()
