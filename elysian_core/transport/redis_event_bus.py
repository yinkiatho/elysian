"""
Redis pub/sub backed event bus implementations.

RedisEventBusPublisher  — used by MarketDataService to publish market data events.
RedisEventBusSubscriber — used by strategy containers to receive market data events.

Both implement the same interface as the in-process EventBus so they are drop-in
replacements at injection points (set_event_bus, strategy.start()).

Backpressure semantics are preserved: subscriber callbacks are awaited sequentially,
so a slow strategy hook slows message consumption from Redis — matching the
behaviour of the original EventBus.publish().
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Callable, Dict, List, Optional

import redis.asyncio as aioredis

from elysian_core.core.events import EventType
from elysian_core.transport.event_serializer import (
    deserialize_event,
    serialize_event,
    kline_channel,
    orderbook_channel,
)


log = logging.getLogger(__name__)


# ── Publisher (market-data container side) ────────────────────────────────────

class RedisEventBusPublisher:
    """Publish market data events to Redis pub/sub.

    One instance is created per (venue, asset_type) pair in MarketDataService
    and injected into the corresponding ClientManager via set_event_bus().

    The channel is derived from the event type and symbol:
        {channel_prefix}:kline:{symbol}
        {channel_prefix}:orderbook:{symbol}
    where channel_prefix is e.g. "elysian:md:binance:spot".
    """

    def __init__(self, redis_url: str, channel_prefix: str) -> None:
        self._redis_url = redis_url
        self._channel_prefix = channel_prefix
        self._redis: Optional[aioredis.Redis] = None

    async def _get_redis(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = await aioredis.from_url(
                self._redis_url,
                encoding="utf-8",
                decode_responses=False,
            )
        return self._redis

    async def publish(self, event) -> None:
        """Serialise event and publish to the appropriate Redis channel."""
        try:
            r = await self._get_redis()
            symbol = event.symbol
            event_type = event.event_type

            if event_type == EventType.KLINE:
                channel = kline_channel(self._channel_prefix, symbol)
            elif event_type == EventType.ORDERBOOK_UPDATE:
                channel = orderbook_channel(self._channel_prefix, symbol)
            else:
                return  # only market data crosses the network

            payload = serialize_event(event)
            await r.publish(channel, payload)
        except Exception as e:
            log.error(f"RedisEventBusPublisher.publish error: {e}")

    # No-ops — publisher never receives subscriptions
    def subscribe(self, event_type: EventType, callback: Callable) -> None:
        pass

    def unsubscribe(self, event_type: EventType, callback: Callable) -> None:
        pass

    async def close(self) -> None:
        if self._redis:
            await self._redis.aclose()
            self._redis = None


# ── Subscriber (strategy container side) ─────────────────────────────────────

class RedisEventBusSubscriber:
    """Subscribe to market data events from Redis pub/sub.

    Strategies call subscribe(event_type, callback) exactly as they would
    with EventBus.  When start_listener(channels) is called, a background
    task connects to Redis, deserialises incoming messages, and dispatches
    them to registered callbacks sequentially (backpressure preserved).
    """

    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url
        self._subscribers: Dict[EventType, List[Callable]] = defaultdict(list)
        self._listener_task: Optional[asyncio.Task] = None
        self._channels: List[str] = []

    # ── EventBus-compatible interface ────────────────────────────────────────

    def subscribe(self, event_type: EventType, callback: Callable) -> None:
        """Register an async callback — identical to EventBus.subscribe()."""
        self._subscribers[event_type].append(callback)

    def unsubscribe(self, event_type: EventType, callback: Callable) -> None:
        """Remove a previously registered callback."""
        try:
            self._subscribers[event_type].remove(callback)
        except ValueError:
            pass

    async def publish(self, event) -> None:
        """No-op — strategy containers never publish to the shared bus."""
        pass

    # ── Listener lifecycle ───────────────────────────────────────────────────

    async def start_listener(self, channels: List[str]) -> None:
        """Subscribe to Redis channels and begin dispatching events.

        Must be called before strategies start subscribing to events, but
        after subscribe() calls have been registered (strategies call
        subscribe in start(), which is called before this method in run()).

        The listener runs as a persistent background task for the lifetime
        of the strategy container.
        """
        if not channels:
            log.warning("RedisEventBusSubscriber.start_listener: no channels provided")
            return

        self._channels = channels
        self._listener_task = asyncio.create_task(
            self._listener_loop(channels),
            name="redis-event-listener",
        )
        log.info(
            f"RedisEventBusSubscriber: listening on {len(channels)} channels: "
            f"{channels[:4]}{'...' if len(channels) > 4 else ''}"
        )

    async def _listener_loop(self, channels: List[str]) -> None:
        """Background task: receive messages from Redis and dispatch to callbacks."""
        redis_conn = await aioredis.from_url(
            self._redis_url,
            encoding="utf-8",
            decode_responses=False,
        )
        try:
            async with redis_conn.pubsub() as ps:
                await ps.subscribe(*channels)
                log.info(f"RedisEventBusSubscriber: subscribed to {len(channels)} channels")
                async for message in ps.listen():
                    if message["type"] != "message":
                        continue
                    try:
                        event = deserialize_event(message["data"])
                    except Exception as e:
                        log.error(f"RedisEventBusSubscriber: deserialize error: {e}")
                        continue

                    callbacks = self._subscribers.get(event.event_type, [])
                    for cb in callbacks:
                        try:
                            await cb(event)
                        except Exception as e:
                            log.error(
                                f"RedisEventBusSubscriber: callback error "
                                f"for {event.event_type}: {e}"
                            )
        except asyncio.CancelledError:
            log.info("RedisEventBusSubscriber: listener task cancelled")
        except Exception as e:
            log.error(f"RedisEventBusSubscriber: listener loop error: {e}", exc_info=True)
        finally:
            await redis_conn.aclose()

    async def close(self) -> None:
        """Cancel the listener task and clean up."""
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
        self._listener_task = None
