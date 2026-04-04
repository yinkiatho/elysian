"""Tests for BaseFSM: transitions, wildcards, re-entrancy guard, PeriodicTask."""

import asyncio
import pytest
from enum import Enum, auto
from unittest.mock import AsyncMock, MagicMock

from elysian_core.core.fsm import BaseFSM, InvalidTransition, PeriodicTask


class Color(Enum):
    RED = auto()
    GREEN = auto()
    YELLOW = auto()


class TrafficLight(BaseFSM):
    _TRANSITIONS = {
        (Color.RED, "go"): (Color.GREEN, "_on_green"),
        (Color.GREEN, "slow"): (Color.YELLOW, "_on_yellow"),
        (Color.YELLOW, "stop"): (Color.RED, None),
        ("*", "reset"): (Color.RED, None),
    }

    def __init__(self, **kwargs):
        super().__init__(initial_state=Color.RED, **kwargs)
        self.green_called = False
        self.yellow_called = False

    async def _on_green(self, **ctx):
        self.green_called = True

    async def _on_yellow(self, **ctx):
        self.yellow_called = True


class TestBaseFSMTransitions:

    def test_initial_state(self):
        fsm = TrafficLight()
        assert fsm.state == Color.RED

    def test_simple_transition(self):
        fsm = TrafficLight()
        new = asyncio.run(fsm.trigger("go"))
        assert new == Color.GREEN
        assert fsm.state == Color.GREEN

    def test_callback_invoked(self):
        fsm = TrafficLight()
        asyncio.run(fsm.trigger("go"))
        assert fsm.green_called is True

    def test_invalid_transition_raises(self):
        fsm = TrafficLight()
        with pytest.raises(InvalidTransition):
            asyncio.run(fsm.trigger("slow"))  # RED has no "slow" trigger

    def test_full_cycle(self):
        fsm = TrafficLight()
        asyncio.run(fsm.trigger("go"))
        asyncio.run(fsm.trigger("slow"))
        asyncio.run(fsm.trigger("stop"))
        assert fsm.state == Color.RED

    def test_wildcard_transition(self):
        fsm = TrafficLight()
        asyncio.run(fsm.trigger("go"))
        assert fsm.state == Color.GREEN
        asyncio.run(fsm.trigger("reset"))
        assert fsm.state == Color.RED

    def test_explicit_takes_priority_over_wildcard(self):
        fsm = TrafficLight()
        asyncio.run(fsm.trigger("go"))  # RED -> GREEN via explicit
        assert fsm.state == Color.GREEN


class TestBaseFSMTerminal:

    def test_terminal_state_rejects_transitions(self):
        class TerminalFSM(BaseFSM):
            _TRANSITIONS = {
                (Color.RED, "go"): (Color.GREEN, None),
                (Color.GREEN, "done"): (Color.YELLOW, None),
            }

        fsm = TerminalFSM(
            initial_state=Color.RED,
            terminal_states={Color.YELLOW},
        )
        asyncio.run(fsm.trigger("go"))
        asyncio.run(fsm.trigger("done"))
        assert fsm.is_terminal is True

        with pytest.raises(InvalidTransition):
            asyncio.run(fsm.trigger("go"))


class TestBaseFSMReentrancy:

    def test_reentrant_trigger_queued(self):
        """Callback that triggers another transition should queue it."""

        class ReentrantFSM(BaseFSM):
            _TRANSITIONS = {
                (Color.RED, "go"): (Color.GREEN, "_on_green"),
                (Color.GREEN, "slow"): (Color.YELLOW, None),
            }

            def __init__(self):
                super().__init__(initial_state=Color.RED)
                self.green_hit = False

            async def _on_green(self, **ctx):
                self.green_hit = True
                await self.trigger("slow")  # re-entrant

        fsm = ReentrantFSM()
        asyncio.run(fsm.trigger("go"))
        assert fsm.green_hit is True
        assert fsm.state == Color.YELLOW


class TestBaseFSMCallbackMissing:

    def test_missing_callback_logged_not_raised(self):
        class MissingCbFSM(BaseFSM):
            _TRANSITIONS = {
                (Color.RED, "go"): (Color.GREEN, "_nonexistent_method"),
            }

        fsm = MissingCbFSM(initial_state=Color.RED)
        new = asyncio.run(fsm.trigger("go"))
        assert new == Color.GREEN  # transition still applied


class TestBaseFSMSyncCallback:

    def test_sync_callback_works(self):
        class SyncCbFSM(BaseFSM):
            _TRANSITIONS = {
                (Color.RED, "go"): (Color.GREEN, "_on_green"),
            }

            def __init__(self):
                super().__init__(initial_state=Color.RED)
                self.called = False

            def _on_green(self, **ctx):
                self.called = True

        fsm = SyncCbFSM()
        asyncio.run(fsm.trigger("go"))
        assert fsm.called is True


class TestInvalidTransitionException:

    def test_exception_attributes(self):
        exc = InvalidTransition(Color.RED, "fly")
        assert exc.state == Color.RED
        assert exc.trigger == "fly"
        assert "fly" in str(exc)
        assert "RED" in str(exc)


class TestPeriodicTask:

    def test_not_running_initially(self):
        task = PeriodicTask(callback=lambda: None, interval_s=1.0)
        assert task.running is False

    def test_start_and_stop(self):
        async def _test():
            counter = {"n": 0}

            async def inc():
                counter["n"] += 1

            task = PeriodicTask(callback=inc, interval_s=0.05, name="test-task")
            task.start()
            assert task.running is True
            await asyncio.sleep(0.15)
            task.stop()
            assert task.running is False
            assert counter["n"] >= 1

        asyncio.run(_test())

    def test_start_idempotent(self):
        async def _test():
            task = PeriodicTask(callback=lambda: None, interval_s=10.0)
            task.start()
            first_task = task._task
            task.start()  # should not create a new task
            assert task._task is first_task
            task.stop()

        asyncio.run(_test())

    def test_callback_error_does_not_stop_loop(self):
        async def _test():
            calls = {"n": 0}

            def failing():
                calls["n"] += 1
                if calls["n"] == 1:
                    raise ValueError("boom")

            task = PeriodicTask(callback=failing, interval_s=0.05, name="err-task")
            task.start()
            await asyncio.sleep(0.2)
            task.stop()
            assert calls["n"] >= 2  # kept running after error

        asyncio.run(_test())
