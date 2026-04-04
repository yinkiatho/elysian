"""
Lightweight in-process async event bus.

Workers publish typed events after mutating feed state; the bus dispatches
to all registered async callbacks keyed by :class:`EventType`.

``publish`` is awaited (not fire-and-forget) so backpressure is natural —
if a strategy hook is slow the emitting worker waits, preventing unbounded
queue growth.
"""

from collections import defaultdict
from typing import Callable, Dict, List

from elysian_core.core.events import EventType
import elysian_core.utils.logger as log

class EventBus:
    """In-process async event bus.  Zero serialization overhead."""

    def __init__(self):
        self.logger = log.setup_custom_logger("root")
        self._subscribers: Dict[EventType, List[Callable]] = defaultdict(list)

    def subscribe(self, event_type: EventType, callback: Callable):
        """Register an async callback for a specific event type."""
        self._subscribers[event_type].append(callback)

    def unsubscribe(self, event_type: EventType, callback: Callable):
        """Remove a previously registered callback."""
        try:
            self._subscribers[event_type].remove(callback)
        except ValueError:
            pass

    async def publish(self, event):
        """Dispatch *event* to every subscriber registered for its type.

        Callbacks are awaited sequentially so that backpressure propagates
        to the emitting worker.  Exceptions in one callback are logged but
        do not prevent remaining callbacks from running.
        """
        for cb in self._subscribers.get(event.event_type, []):
            try:
                await cb(event)
            except Exception as e:
                self.logger.error(f"EventBus: callback error for {event.event_type}: {e}")
