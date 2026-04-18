"""
Binance isolated margin user data stream manager.

Isolated margin differs from spot and futures:
  - Each isolated margin symbol has its OWN listen key (one per symbol).
  - Listen keys are created via POST /sapi/v1/userDataStream/isolated?symbol=X
    using just the API key header (no HMAC signature required for stream endpoints).
  - The WebSocket URL is wss://stream.binance.com:9443/ws/{listenKey}.
  - Events mirror the spot stream: executionReport, balanceUpdate,
    outboundAccountPosition — plus marginCall for risk alerts.
  - Listen keys expire after 60 minutes; keepalive via PUT every 55 minutes.

Architecture
------------
``BinanceMarginUserDataClientManager`` owns one WebSocket reader task per symbol
and one shared keepalive task.  Events are parsed into typed dataclasses and
published directly to the injected private ``EventBus``, tagged with
``asset_type=AssetType.MARGIN`` so downstream consumers can filter by asset type.

Usage (via BinanceMarginExchange.initialize())::

    manager = BinanceMarginUserDataClientManager(api_key, api_secret, ["ETHUSDT"])
    manager.set_event_bus(private_event_bus)
    manager.set_token_info({"ETHUSDT": {"base_asset": "ETH", "quote_asset": "USDT"}})
    await manager.start()
    # … trading …
    await manager.stop()
"""

import asyncio
import datetime
import json
import time
from typing import Callable, Dict, List, Optional

import requests
import websockets

from elysian_core.core.enums import (
    AssetType,
    OrderStatus,
    OrderType,
    Side,
    Venue,
    _BINANCE_STATUS,
)
from elysian_core.core.events import BalanceUpdateEvent, OrderUpdateEvent
from elysian_core.core.order import Order
import elysian_core.utils.logger as log


class BinanceMarginUserDataClientManager:
    """Manages per-symbol isolated margin user-data WebSocket streams.

    Each isolated margin symbol requires its own listen key and WebSocket
    connection.  This manager creates one reader task per symbol and one shared
    keepalive task.

    Events are parsed into typed ``OrderUpdateEvent`` / ``BalanceUpdateEvent``
    and published to the private ``EventBus`` with ``asset_type=AssetType.MARGIN``.

    Optional raw-dict callbacks (via ``register``) are also supported for
    connectors that need the unparsed payload (e.g. to update internal state
    before the event bus publish happens).
    """

    SAPI_BASE = "https://api.binance.com"
    WS_BASE = "wss://stream.binance.com:9443/ws"
    KEEPALIVE_INTERVAL_S = 55 * 60  # 55 min — listen key expires at 60 min

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        symbols: List[str],
    ):
        self.logger = log.setup_custom_logger("root")
        self._api_key = api_key
        self._api_secret = api_secret
        self._symbols: List[str] = list(symbols)

        self._running: bool = False
        self._listen_keys: Dict[str, str] = {}           # symbol -> listenKey
        self._reader_tasks: Dict[str, asyncio.Task] = {} # symbol -> reader task
        self._keepalive_task: Optional[asyncio.Task] = None

        # Private event bus (injected by BinanceMarginExchange)
        self._event_bus = None
        # symbol -> {base_asset, quote_asset, ...}
        self._token_infos: Dict[str, dict] = {}
        # Optional raw-dict callbacks (receive the unparsed WS message dict)
        self._subscribers: List[Callable[[dict], None]] = []

    # ── Dependency injection ───────────────────────────────────────────────────

    def set_event_bus(self, event_bus) -> None:
        """Inject the private EventBus for typed event publishing."""
        self._event_bus = event_bus

    def set_token_info(self, token_infos: Dict[str, dict]) -> None:
        """Provide symbol metadata (base_asset, quote_asset) for event population."""
        self._token_infos = token_infos

    def register(self, callback: Callable[[dict], None]) -> None:
        """Register a callback to receive raw WS event dicts before parsing."""
        self._subscribers.append(callback)
        self.logger.debug(
            f"BinanceMarginUserDataClientManager: registered callback {callback.__name__}"
        )

    def unregister(self, callback: Callable[[dict], None]) -> None:
        """Unregister a previously registered callback."""
        try:
            self._subscribers.remove(callback)
            self.logger.debug(
                f"BinanceMarginUserDataClientManager: unregistered callback {callback.__name__}"
            )
        except ValueError:
            pass

    # ── Listen key management ─────────────────────────────────────────────────

    async def _get_listen_key(self, symbol: str) -> str:
        """POST /sapi/v1/userDataStream/isolated to obtain a fresh listen key."""
        def _sync() -> str:
            resp = requests.post(
                f"{self.SAPI_BASE}/sapi/v1/userDataStream/isolated",
                headers={"X-MBX-APIKEY": self._api_key},
                params={"symbol": symbol},
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()["listenKey"]
        return await asyncio.to_thread(_sync)

    async def _keepalive_listen_key(self, symbol: str, listen_key: str) -> None:
        """PUT /sapi/v1/userDataStream/isolated to extend the listen key validity."""
        def _sync() -> None:
            resp = requests.put(
                f"{self.SAPI_BASE}/sapi/v1/userDataStream/isolated",
                headers={"X-MBX-APIKEY": self._api_key},
                params={"symbol": symbol, "listenKey": listen_key},
                timeout=10,
            )
            resp.raise_for_status()
        await asyncio.to_thread(_sync)

    async def _close_listen_key(self, symbol: str, listen_key: str) -> None:
        """DELETE /sapi/v1/userDataStream/isolated to close the stream server-side."""
        def _sync() -> None:
            try:
                requests.delete(
                    f"{self.SAPI_BASE}/sapi/v1/userDataStream/isolated",
                    headers={"X-MBX-APIKEY": self._api_key},
                    params={"symbol": symbol, "listenKey": listen_key},
                    timeout=10,
                )
            except Exception:
                pass  # best-effort on shutdown
        await asyncio.to_thread(_sync)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start one reader task per symbol and the shared keepalive task."""
        if self._running:
            return
        self._running = True

        for symbol in self._symbols:
            task = asyncio.create_task(self._reader_loop(symbol))
            self._reader_tasks[symbol] = task

        self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        self.logger.info(
            f"BinanceMarginUserDataClientManager: started for {self._symbols}"
        )

    async def stop(self) -> None:
        """Cancel all tasks and close listen keys."""
        if not self._running:
            return
        self._running = False

        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except asyncio.CancelledError:
                pass

        for task in self._reader_tasks.values():
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._reader_tasks.clear()

        for symbol, lk in list(self._listen_keys.items()):
            await self._close_listen_key(symbol, lk)
        self._listen_keys.clear()

        self.logger.info("BinanceMarginUserDataClientManager: stopped")

    # ── Keepalive loop ─────────────────────────────────────────────────────────

    async def _keepalive_loop(self) -> None:
        """Send keepalive PUT for all active listen keys every KEEPALIVE_INTERVAL_S."""
        while self._running:
            try:
                await asyncio.sleep(self.KEEPALIVE_INTERVAL_S)
                for symbol, lk in list(self._listen_keys.items()):
                    try:
                        await self._keepalive_listen_key(symbol, lk)
                        self.logger.debug(
                            f"BinanceMarginUserDataClientManager: keepalive sent for {symbol}"
                        )
                    except Exception as e:
                        self.logger.error(
                            f"BinanceMarginUserDataClientManager: keepalive error for {symbol}: {e}"
                        )
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(
                    f"BinanceMarginUserDataClientManager: keepalive loop error: {e}"
                )

    # ── Reader loop (one per symbol) ───────────────────────────────────────────

    async def _reader_loop(self, symbol: str) -> None:
        """Connect to the isolated margin user-data stream for *symbol*.

        Reconnects with exponential backoff (1s → 60s) on any connection error.
        """
        reconnect_delay = 1
        while self._running:
            try:
                listen_key = await self._get_listen_key(symbol)
                self._listen_keys[symbol] = listen_key
                ws_url = f"{self.WS_BASE}/{listen_key}"

                async with websockets.connect(
                    ws_url,
                    ping_interval=180,
                    ping_timeout=10,
                ) as ws:
                    reconnect_delay = 1
                    self.logger.info(
                        f"BinanceMarginUserDataClientManager: connected for {symbol}"
                    )

                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            self.logger.warning(
                                f"BinanceMarginUserDataClientManager: decode error for {symbol}"
                            )
                            continue

                        # Raw callbacks first (allow connector to update internal state)
                        for cb in self._subscribers:
                            try:
                                cb(msg)
                            except Exception as e:
                                self.logger.error(
                                    f"BinanceMarginUserDataClientManager: callback error: {e}"
                                )

                        # Parse and publish typed event to private bus
                        if self._event_bus is not None:
                            parsed = self._parse_event(symbol, msg)
                            if parsed is not None:
                                await self._dispatch_to_event_bus(symbol, parsed)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(
                    f"BinanceMarginUserDataClientManager: connection error for {symbol}: {e}"
                )
                if self._running:
                    self.logger.info(
                        f"BinanceMarginUserDataClientManager: reconnecting {symbol} "
                        f"in {reconnect_delay}s..."
                    )
                    await asyncio.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, 60)

        self.logger.info(
            f"BinanceMarginUserDataClientManager: reader loop exited for {symbol}"
        )

    # ── Event parsing ──────────────────────────────────────────────────────────

    def _parse_event(self, symbol: str, event: dict) -> Optional[dict]:
        """Parse a raw WS event dict into a normalized intermediate structure.

        Returns ``None`` for unrecognised or unhandled event types.
        """
        et = event.get("e")
        ts = int(event.get("E", int(time.time() * 1000)))

        if et == "balanceUpdate":
            return {
                "type": "balance_update",
                "asset": event.get("a"),
                "delta": float(event.get("d", 0)),
                "ts": ts,
            }
        if et == "outboundAccountPosition":
            return {
                "type": "account_position",
                "balances": [
                    {
                        "asset": b.get("a"),
                        "free": float(b.get("f", 0)),
                        "locked": float(b.get("l", 0)),
                    }
                    for b in event.get("B", [])
                ],
                "ts": ts,
            }
        if et == "executionReport":
            order = self._parse_execution_report(event)
            if order is None:
                return None
            return {
                "type": "execution_report",
                "order": order,
                "exec_type": event.get("x"),
                "ts": ts,
            }
        return None

    async def _dispatch_to_event_bus(self, symbol: str, parsed: dict) -> None:
        """Publish a typed event to the private EventBus."""
        t = parsed["type"]
        ts = parsed["ts"]

        if t == "balance_update":
            await self._event_bus.publish(BalanceUpdateEvent(
                asset=parsed["asset"],
                venue=Venue.BINANCE,
                delta=parsed["delta"],
                new_balance=0.0,
                timestamp=ts,
                asset_type=AssetType.MARGIN,
            ))

        elif t == "execution_report":
            order = parsed["order"]
            sym = order.symbol
            await self._event_bus.publish(OrderUpdateEvent(
                symbol=sym,
                base_asset=self._token_infos.get(sym, {}).get("base_asset", ""),
                quote_asset=self._token_infos.get(sym, {}).get("quote_asset", ""),
                venue=Venue.BINANCE,
                order=order,
                timestamp=ts,
                asset_type=AssetType.MARGIN,
            ))

    @staticmethod
    def _parse_execution_report(msg: dict) -> Optional[Order]:
        """Parse a raw ``executionReport`` WS event into an :class:`Order`.

        Returns ``None`` for unknown or unmapped status codes.
        """
        status = _BINANCE_STATUS.get(msg.get("X"))
        if status is None:
            return None

        side = Side.BUY if msg.get("S") == "BUY" else Side.SELL
        raw_type = msg.get("o", "LIMIT").upper()
        if raw_type == "MARKET":
            order_type = OrderType.MARKET
        elif raw_type == "LIMIT":
            order_type = OrderType.LIMIT
        else:
            order_type = OrderType.OTHERS

        cum_filled = float(msg.get("z", 0))
        cum_quote = float(msg.get("Z", 0))
        avg_price = (cum_quote / cum_filled) if cum_filled > 0 else float(msg.get("L", 0))

        return Order(
            id=str(msg.get("i")),
            symbol=msg.get("s"),
            side=side,
            order_type=order_type,
            quantity=float(msg.get("q", 0)),
            price=float(msg.get("p", 0)) or None,
            status=status,
            avg_fill_price=avg_price,
            last_fill_price=float(msg.get("L", 0)),
            filled_qty=cum_filled,
            timestamp=datetime.datetime.fromtimestamp(
                msg.get("O", 0) / 1000, tz=datetime.timezone.utc
            ),
            venue=Venue.BINANCE,
            commission=float(msg.get("n", 0)),
            commission_asset=msg.get("N"),
            last_updated_timestamp=int(msg.get("E", 0)),
            strategy_id=msg.get("j"),
        )
