"""Tests for EventBus: subscribe, unsubscribe, publish, error isolation."""

import asyncio
import pytest
from unittest.mock import AsyncMock

from elysian_core.core.event_bus import EventBus
from elysian_core.core.events import EventType, BalanceUpdateEvent
from elysian_core.core.enums import Venue


def _balance_event(asset="USDT", delta=100.0):
    return BalanceUpdateEvent(asset=asset, venue=Venue.BINANCE, delta=delta, new_balance=delta, timestamp=0)


class TestEventBusSubscribe:

    def test_subscribe_registers_callback(self):
        bus = EventBus()
        cb = AsyncMock()
        bus.subscribe(EventType.BALANCE_UPDATE, cb)
        assert cb in bus._subscribers[EventType.BALANCE_UPDATE]

    def test_subscribe_multiple_callbacks(self):
        bus = EventBus()
        cb1, cb2 = AsyncMock(), AsyncMock()
        bus.subscribe(EventType.BALANCE_UPDATE, cb1)
        bus.subscribe(EventType.BALANCE_UPDATE, cb2)
        assert len(bus._subscribers[EventType.BALANCE_UPDATE]) == 2

    def test_subscribe_different_event_types(self):
        bus = EventBus()
        cb1, cb2 = AsyncMock(), AsyncMock()
        bus.subscribe(EventType.BALANCE_UPDATE, cb1)
        bus.subscribe(EventType.KLINE, cb2)
        assert cb1 in bus._subscribers[EventType.BALANCE_UPDATE]
        assert cb2 in bus._subscribers[EventType.KLINE]
        assert cb1 not in bus._subscribers[EventType.KLINE]


class TestEventBusUnsubscribe:

    def test_unsubscribe_removes_callback(self):
        bus = EventBus()
        cb = AsyncMock()
        bus.subscribe(EventType.BALANCE_UPDATE, cb)
        bus.unsubscribe(EventType.BALANCE_UPDATE, cb)
        assert cb not in bus._subscribers[EventType.BALANCE_UPDATE]

    def test_unsubscribe_nonexistent_is_noop(self):
        bus = EventBus()
        cb = AsyncMock()
        bus.unsubscribe(EventType.BALANCE_UPDATE, cb)  # should not raise

    def test_unsubscribe_only_removes_specified(self):
        bus = EventBus()
        cb1, cb2 = AsyncMock(), AsyncMock()
        bus.subscribe(EventType.BALANCE_UPDATE, cb1)
        bus.subscribe(EventType.BALANCE_UPDATE, cb2)
        bus.unsubscribe(EventType.BALANCE_UPDATE, cb1)
        assert cb1 not in bus._subscribers[EventType.BALANCE_UPDATE]
        assert cb2 in bus._subscribers[EventType.BALANCE_UPDATE]


class TestEventBusPublish:

    def test_publish_calls_subscriber(self):
        bus = EventBus()
        cb = AsyncMock()
        bus.subscribe(EventType.BALANCE_UPDATE, cb)
        event = _balance_event()
        asyncio.run(bus.publish(event))
        cb.assert_awaited_once_with(event)

    def test_publish_calls_all_subscribers(self):
        bus = EventBus()
        cb1, cb2 = AsyncMock(), AsyncMock()
        bus.subscribe(EventType.BALANCE_UPDATE, cb1)
        bus.subscribe(EventType.BALANCE_UPDATE, cb2)
        event = _balance_event()
        asyncio.run(bus.publish(event))
        cb1.assert_awaited_once_with(event)
        cb2.assert_awaited_once_with(event)

    def test_publish_no_subscribers_is_noop(self):
        bus = EventBus()
        event = _balance_event()
        asyncio.run(bus.publish(event))  # should not raise

    def test_publish_wrong_event_type_not_dispatched(self):
        bus = EventBus()
        cb = AsyncMock()
        bus.subscribe(EventType.KLINE, cb)
        event = _balance_event()
        asyncio.run(bus.publish(event))
        cb.assert_not_awaited()

    def test_publish_error_in_callback_does_not_stop_others(self):
        bus = EventBus()
        failing_cb = AsyncMock(side_effect=RuntimeError("boom"))
        ok_cb = AsyncMock()
        bus.subscribe(EventType.BALANCE_UPDATE, failing_cb)
        bus.subscribe(EventType.BALANCE_UPDATE, ok_cb)
        event = _balance_event()
        asyncio.run(bus.publish(event))
        failing_cb.assert_awaited_once()
        ok_cb.assert_awaited_once()
