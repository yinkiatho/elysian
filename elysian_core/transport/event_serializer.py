"""
Stateless codec for frozen event dataclasses to/from Redis wire format.

Only KlineEvent and OrderBookUpdateEvent cross the network boundary
(market data bus).  Account events (ORDER_UPDATE, BALANCE_UPDATE) stay
in-process on the private EventBus inside each strategy container.

Channel naming:
    elysian:md:{venue_lower}:{asset_type_lower}:{event_type}:{SYMBOL}
    e.g.  elysian:md:binance:spot:kline:BTCUSDT
          elysian:md:binance:futures:orderbook:ETHUSDT
"""

from __future__ import annotations

import datetime
import json
from typing import List

from elysian_core.core.enums import AssetType, Venue
from elysian_core.core.events import KlineEvent, OrderBookUpdateEvent
from elysian_core.core.market_data import Kline, OrderBook


# ── Channel helpers ───────────────────────────────────────────────────────────

def channel_prefix(venue: Venue, asset_type: AssetType) -> str:
    """Return the Redis channel prefix for a (venue, asset_type) pair.

    Isolated margin shares the same Binance WebSocket streams as spot, so
    margin market data is routed to spot channels to avoid duplicate feeds.

    e.g.  (Venue.BINANCE, AssetType.SPOT)   → "elysian:md:binance:spot"
          (Venue.BINANCE, AssetType.MARGIN)  → "elysian:md:binance:spot"
          (Venue.BINANCE, AssetType.PERPETUAL) → "elysian:md:binance:perpetual"
    """
    if asset_type == AssetType.MARGIN:
        asset_type = AssetType.SPOT
    return f"elysian:md:{venue.value.lower()}:{asset_type.value.lower()}"


def kline_channel(prefix: str, symbol: str) -> str:
    return f"{prefix}:kline:{symbol}"


def orderbook_channel(prefix: str, symbol: str) -> str:
    return f"{prefix}:orderbook:{symbol}"


def strategy_channels(symbols: List[str], venue: Venue, asset_type: AssetType) -> List[str]:
    """Return all Redis channels a strategy should subscribe to.

    Subscribes to both kline and orderbook channels for every symbol.
    """
    prefix = channel_prefix(venue, asset_type)
    channels = []
    for sym in symbols:
        channels.append(kline_channel(prefix, sym))
        channels.append(orderbook_channel(prefix, sym))
    return channels


# ── JSON encoder/decoder ──────────────────────────────────────────────────────

class _EventEncoder(json.JSONEncoder):
    """Custom encoder that handles types used in event dataclasses."""

    def default(self, obj):
        if isinstance(obj, datetime.datetime):
            return {"__datetime__": obj.isoformat()}
        if hasattr(obj, "value"):          # any Enum
            return obj.value
        if hasattr(obj, "items"):          # SortedDict / any mapping
            return list(obj.items())
        return super().default(obj)


def _decode_hook(d: dict):
    if "__datetime__" in d:
        return datetime.datetime.fromisoformat(d["__datetime__"])
    return d


# ── Serialise ─────────────────────────────────────────────────────────────────

def serialize_event(event: KlineEvent | OrderBookUpdateEvent) -> bytes:
    """Serialise a market data event to JSON bytes for Redis pub/sub."""
    if isinstance(event, KlineEvent):
        payload = {
            "_type": "KlineEvent",
            "symbol": event.symbol,
            "venue": event.venue.value,
            "timestamp": event.timestamp,
            "kline": {
                "ticker": event.kline.ticker,
                "interval": event.kline.interval,
                "start_time": {"__datetime__": event.kline.start_time.isoformat()},
                "end_time": {"__datetime__": event.kline.end_time.isoformat()},
                "open": event.kline.open,
                "high": event.kline.high,
                "low": event.kline.low,
                "close": event.kline.close,
                "volume": event.kline.volume,
            },
        }
    elif isinstance(event, OrderBookUpdateEvent):
        ob = event.orderbook
        payload = {
            "_type": "OrderBookUpdateEvent",
            "symbol": event.symbol,
            "venue": event.venue.value,
            "timestamp": event.timestamp,
            "orderbook": {
                "last_update_id": ob.last_update_id,
                "last_timestamp": ob.last_timestamp,
                "ticker": ob.ticker,
                "interval": ob.interval,
                "bids": list(ob.bid_orders.items()),
                "asks": list(ob.ask_orders.items()),
            },
        }
    else:
        raise TypeError(f"serialize_event: unsupported event type {type(event)}")

    return json.dumps(payload).encode()


# ── Deserialise ───────────────────────────────────────────────────────────────

def deserialize_event(payload: bytes) -> KlineEvent | OrderBookUpdateEvent:
    """Deserialise JSON bytes from Redis back into a frozen event dataclass."""
    d = json.loads(payload)
    _type = d["_type"]

    if _type == "KlineEvent":
        kd = d["kline"]
        kline = Kline(
            ticker=kd["ticker"],
            interval=kd["interval"],
            start_time=datetime.datetime.fromisoformat(kd["start_time"]["__datetime__"]),
            end_time=datetime.datetime.fromisoformat(kd["end_time"]["__datetime__"]),
            open=kd.get("open"),
            high=kd.get("high"),
            low=kd.get("low"),
            close=kd.get("close"),
            volume=kd.get("volume"),
        )
        return KlineEvent(
            symbol=d["symbol"],
            venue=Venue(d["venue"]),
            kline=kline,
            timestamp=d["timestamp"],
        )

    if _type == "OrderBookUpdateEvent":
        od = d["orderbook"]
        orderbook = OrderBook.from_lists(
            last_update_id=od["last_update_id"],
            last_timestamp=od["last_timestamp"],
            ticker=od["ticker"],
            interval=od.get("interval"),
            bid_levels=od["bids"],
            ask_levels=od["asks"],
        )
        return OrderBookUpdateEvent(
            symbol=d["symbol"],
            venue=Venue(d["venue"]),
            orderbook=orderbook,
            timestamp=d["timestamp"],
        )

    raise ValueError(f"deserialize_event: unknown _type '{_type}'")
