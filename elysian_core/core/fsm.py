"""
Lightweight async finite-state machine base class.

Provides a generic ``BaseFSM`` driven by an enum-based transition table.
Designed for the Elysian trading system — integrates with the EventBus for
state-change notifications and includes a re-entrancy guard so that
callbacks which publish events won't deadlock.

Usage::

    class TrafficLight(BaseFSM):
        _TRANSITIONS = {
            (Color.RED,    "timer"): (Color.GREEN,  "_on_green"),
            (Color.GREEN,  "timer"): (Color.YELLOW, "_on_yellow"),
            (Color.YELLOW, "timer"): (Color.RED,    "_on_red"),
        }

        def __init__(self):
            super().__init__(initial_state=Color.RED)

        async def _on_green(self, **ctx):
            ...
"""

from __future__ import annotations

import asyncio
from collections import deque
from enum import Enum
from typing import Any, Deque, Dict, Optional, Set, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from elysian_core.core.event_bus import EventBus

import elysian_core.utils.logger as log

logger = log.setup_custom_logger("root")

class InvalidTransition(Exception):
    """Raised when a trigger is not valid for the current state."""

    def __init__(self, state: Enum, trigger: str):
        self.state = state
        self.trigger = trigger
        super().__init__(f"No transition for trigger '{trigger}' in state {state.name}")


class BaseFSM:
    """Async finite-state machine with dict-based transition table.

    Subclasses define ``_TRANSITIONS`` as a dict mapping
    ``(State, trigger_name)`` → ``(NextState, callback_method_name_or_None)``.

    Wildcard transitions are supported: use the string ``"*"`` as the state
    to match any current state (explicit state matches take priority).

    Parameters
    ----------
    initial_state:
        The starting state enum value.
    event_bus:
        Optional EventBus for publishing state-change events.
    terminal_states:
        Set of states that reject all further transitions.
    name:
        Human-readable name for logging.
    """

    _TRANSITIONS: Dict[Tuple[Any, str], Tuple[Any, Optional[str]]] = {}

    def __init__(
        self,
        initial_state: Enum,
        event_bus: Optional["EventBus"] = None,
        terminal_states: Optional[Set[Enum]] = None,
        name: Optional[str] = None,
    ):
        self._state = initial_state
        self._event_bus = event_bus
        self._terminal_states: Set[Enum] = terminal_states or set()
        self._name = name or self.__class__.__name__

        # Re-entrancy guard: if a callback triggers another transition we
        # queue it instead of recursing.
        self._transitioning = False
        self._pending: Deque[Tuple[str, dict]] = deque()

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def state(self) -> Enum:
        """Current state."""
        return self._state

    @property
    def is_terminal(self) -> bool:
        """True if the current state is terminal (no further transitions)."""
        return self._state in self._terminal_states

    async def trigger(self, event: str, **ctx: Any) -> Enum:
        """Fire a trigger, transition to the next state, and run the callback.

        Parameters
        ----------
        event:
            Trigger name (e.g. ``"timer_tick"``, ``"complete"``).
        **ctx:
            Arbitrary context forwarded to the callback method.

        Returns
        -------
        The new state after the transition.

        Raises
        ------
        InvalidTransition
            If no matching transition exists for ``(current_state, event)``.
        """
        if self._transitioning:
            # Queue it — will be drained after the current transition finishes.
            self._pending.append((event, ctx))
            return self._state

        self._transitioning = True
        try:
            new_state = await self._do_transition(event, **ctx)

            # Drain any transitions queued by callbacks.
            while self._pending:
                queued_event, queued_ctx = self._pending.popleft()
                new_state = await self._do_transition(queued_event, **queued_ctx)

            return new_state
        finally:
            self._transitioning = False

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _do_transition(self, event: str, **ctx: Any) -> Enum:
        """Resolve the transition, update state, call the callback."""
        
        
        if self.is_terminal:
            raise InvalidTransition(self._state, event)

        # Explicit match first, then wildcard.
        key = (self._state, event)
        entry = self._TRANSITIONS.get(key)
        if entry is None:
            wildcard_key = ("*", event)
            entry = self._TRANSITIONS.get(wildcard_key)
        if entry is None:
            raise InvalidTransition(self._state, event)

        next_state, callback_name = entry
        old_state = self._state
        self._state = next_state

        # Inject old_state into ctx so callbacks can access the pre-transition state
        ctx["_fsm_old_state"] = old_state

        logger.debug(
            "[%s] %s --%s--> %s",
            self._name, old_state.name, event, next_state.name,
        )

        if callback_name is not None:
            callback = getattr(self, callback_name, None)
            if callback is None:
                logger.warning(
                    "[%s] Callback '%s' not found — skipping",
                    self._name, callback_name,
                )
            elif asyncio.iscoroutinefunction(callback):
                await callback(**ctx)
            else:
                callback(**ctx)

        return self._state


class PeriodicTask:
    """Simple repeating async task — replaces bare ``while True`` + sleep loops.

    Usage::

        async def _save_snapshot():
            portfolio.save_snapshot()

        task = PeriodicTask(_save_snapshot, interval_s=60, name="snapshot")
        task.start()    # returns immediately, runs in background
        ...
        task.stop()     # cancels the background task
    """

    def __init__(
        self,
        callback,
        interval_s: float,
        name: str = "PeriodicTask",
    ):
        self._callback = callback
        self._interval = interval_s
        self._name = name
        self._task: Optional[asyncio.Task] = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start the periodic loop as a background asyncio task."""
        if self.running:
            return
        self._task = asyncio.get_running_loop().create_task(
            self._loop(), name=self._name,
        )

    def stop(self) -> None:
        """Cancel the background task."""
        if self._task and not self._task.done():
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                result = self._callback()
                if asyncio.iscoroutine(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("[%s] Error: %s", self._name, e, exc_info=True)
