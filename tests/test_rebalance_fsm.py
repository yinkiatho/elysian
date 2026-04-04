"""Tests for RebalanceFSM: full rebalance cycle, error handling, suspend/resume."""

import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from elysian_core.core.enums import RebalanceState
from elysian_core.core.rebalance_fsm import RebalanceFSM
from elysian_core.core.fsm import InvalidTransition
from elysian_core.core.signals import (
    TargetWeights, ValidatedWeights, RebalanceResult, OrderIntent,
)
from elysian_core.core.enums import Venue, Side, OrderType


def _mock_strategy(weights=None, strategy_id=1):
    s = MagicMock()
    s.strategy_id = strategy_id
    s.compute_weights = MagicMock(return_value=weights or {"BTCUSDT": 0.5})
    return s


def _mock_optimizer(rejected=False, venue=Venue.BINANCE):
    o = MagicMock()
    o.venue = venue

    def validate(target, **ctx):
        return ValidatedWeights(
            original=target,
            weights=target.weights,
            clipped={},
            rejected=rejected,
            rejection_reason="risk violation" if rejected else "",
            timestamp=0,
        )

    o.validate.side_effect = validate
    return o


def _mock_execution_engine(submitted=1, failed=0):
    e = MagicMock()

    async def execute(validated, **ctx):
        return RebalanceResult(
            intents=(), submitted=submitted, failed=failed,
            timestamp=0, submitted_orders={},
        )

    e.execute = AsyncMock(side_effect=execute)
    return e


def _fsm(strategy=None, optimizer=None, engine=None, cooldown_s=0.0, event_bus=None):
    return RebalanceFSM(
        strategy=strategy or _mock_strategy(),
        optimizer=optimizer or _mock_optimizer(),
        execution_engine=engine or _mock_execution_engine(),
        event_bus=event_bus,
        cooldown_s=cooldown_s,
    )


class TestRebalanceFSMHappyPath:

    def test_initial_state_is_idle(self):
        fsm = _fsm()
        assert fsm.state == RebalanceState.IDLE

    def test_full_cycle_returns_to_idle(self):
        fsm = _fsm()
        result = asyncio.run(fsm.request())
        assert result is True
        assert fsm.state == RebalanceState.IDLE

    def test_request_returns_true_when_idle(self):
        fsm = _fsm()
        assert asyncio.run(fsm.request()) is True

    def test_last_result_populated(self):
        fsm = _fsm()
        asyncio.run(fsm.request())
        assert fsm.last_result is not None
        assert fsm.last_result.submitted == 1


class TestRebalanceFSMNoSignal:

    def test_empty_weights_returns_to_idle(self):
        strategy = _mock_strategy(weights={})
        fsm = _fsm(strategy=strategy)
        asyncio.run(fsm.request())
        assert fsm.state == RebalanceState.IDLE

    def test_none_weights_returns_to_idle(self):
        strategy = _mock_strategy(weights=None)
        fsm = _fsm(strategy=strategy)
        asyncio.run(fsm.request())
        assert fsm.state == RebalanceState.IDLE


class TestRebalanceFSMRejection:

    def test_rejected_weights_go_to_cooldown(self):
        optimizer = _mock_optimizer(rejected=True)
        fsm = _fsm(optimizer=optimizer)
        asyncio.run(fsm.request())
        assert fsm.state == RebalanceState.IDLE  # cooldown_s=0 -> immediate IDLE


class TestRebalanceFSMErrors:

    def test_compute_weights_error_returns_to_idle(self):
        strategy = MagicMock()
        strategy.strategy_id = 1
        strategy.compute_weights.side_effect = RuntimeError("compute failed")
        fsm = _fsm(strategy=strategy)
        asyncio.run(fsm.request())
        assert fsm.state == RebalanceState.IDLE

    def test_optimizer_error_goes_to_cooldown(self):
        optimizer = MagicMock()
        optimizer.venue = Venue.BINANCE
        optimizer.validate.side_effect = RuntimeError("validate failed")
        fsm = _fsm(optimizer=optimizer)
        asyncio.run(fsm.request())
        assert fsm.state == RebalanceState.IDLE  # cooldown_s=0 -> immediate IDLE

    def test_execution_error_goes_to_error_then_idle(self):
        engine = MagicMock()
        engine.execute = AsyncMock(side_effect=RuntimeError("exec failed"))
        fsm = _fsm(engine=engine)
        asyncio.run(fsm.request())
        # cooldown_s=0 -> auto-retry -> IDLE
        assert fsm.state == RebalanceState.IDLE


class TestRebalanceFSMBusy:

    def test_request_while_not_idle_returns_false(self):
        """If FSM is not IDLE, request() should return False."""
        fsm = _fsm()
        # Manually set to COMPUTING to simulate busy state
        fsm._state = RebalanceState.COMPUTING
        result = asyncio.run(fsm.request())
        assert result is False


class TestRebalanceFSMSuspendResume:

    def test_suspend_from_idle(self):
        fsm = _fsm()
        asyncio.run(fsm.trigger("suspend"))
        assert fsm.state == RebalanceState.SUSPENDED

    def test_resume_from_suspended(self):
        fsm = _fsm()
        asyncio.run(fsm.trigger("suspend"))
        asyncio.run(fsm.trigger("resume"))
        assert fsm.state == RebalanceState.IDLE

    def test_request_while_suspended_returns_false(self):
        fsm = _fsm()
        asyncio.run(fsm.trigger("suspend"))
        assert asyncio.run(fsm.request()) is False


class TestRebalanceFSMEventBus:

    def test_cycle_events_published(self):
        bus = MagicMock()
        bus.publish = AsyncMock()
        fsm = _fsm(event_bus=bus)
        asyncio.run(fsm.request())
        # Should have published multiple events (cycle events + RebalanceCompleteEvent)
        assert bus.publish.await_count >= 1


class TestRebalanceFSMAsyncStrategy:

    def test_async_compute_weights(self):
        strategy = MagicMock()
        strategy.strategy_id = 1

        async def async_weights(**ctx):
            return {"ETHUSDT": 0.3}

        strategy.compute_weights = async_weights
        fsm = _fsm(strategy=strategy)
        asyncio.run(fsm.request())
        assert fsm.state == RebalanceState.IDLE
        assert fsm.last_result is not None
