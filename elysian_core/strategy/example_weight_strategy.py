"""
Example weight-vector strategy that demonstrates the Stage 3-5 pipeline.

Rebalances to equal weights across all symbols that have active kline feeds,
triggered periodically via ``run_forever()``.

Usage::

    strategy = EqualWeightStrategy(exchanges={}, event_bus=bus)
    runner = StrategyRunner()
    await runner.run(strategy=strategy)
"""

import asyncio
from elysian_core.strategy.base_strategy import SpotStrategy
from elysian_core.core.events import KlineEvent, RebalanceCompleteEvent
import elysian_core.utils.logger as log

logger = log.setup_custom_logger("root")


class EqualWeightStrategy(SpotStrategy):
    """Equal-weight across all tracked symbols, rebalanced on a timer.

    This is a reference implementation showing how to use ``submit_weights()``.
    Override ``_compute_weights()`` to plug in your own alpha logic.
    """

    def __init__(self, *args, rebalance_interval_s: float = 60.0, **kwargs):
        super().__init__(*args, **kwargs)
        self._rebalance_interval = rebalance_interval_s
        self._symbols: set = set()

    async def on_start(self):
        logger.info(
            f"[EqualWeight] Started — rebalance every {self._rebalance_interval}s"
        )

    async def on_kline(self, event: KlineEvent):
        # Track symbols that have live data
        self._symbols.add(event.symbol)

    async def run_forever(self):
        """Periodic rebalance loop."""
        # Wait for initial data to arrive
        await asyncio.sleep(20)
        
        logger.info(f'Initiating Example Weight Strategy ')
        while True:
            try:
                weights = self._compute_weights()
                if weights:
                    logger.info(f"[EqualWeight] Submitting weights: {weights}")
                    result = await self.submit_weights(weights)
                    if result:
                        logger.info(
                            f"[EqualWeight] Rebalance done: "
                            f"{result.submitted} submitted, {result.failed} failed"
                        )
                else:
                    logger.debug("[EqualWeight] No symbols tracked yet, skipping")
            except Exception as e:
                logger.error(f"[EqualWeight] Rebalance error: {e}", exc_info=True)

            await asyncio.sleep(self._rebalance_interval)

    async def on_rebalance_complete(self, event: RebalanceCompleteEvent):
        r = event.result
        if r.errors:
            logger.warning(f"[EqualWeight] Rebalance had errors: {r.errors}")

    def _compute_weights(self) -> dict:
        """Compute equal weights for all tracked symbols.

        Override this method to implement custom alpha logic.  Return a dict
        mapping symbol -> target weight (floats summing to <= 1.0).
        """
        if not self._symbols:
            return {}

        n = len(self._symbols)
        # Reserve 10% cash, distribute rest equally
        per_asset = 0.90 / n
        return {sym: per_asset for sym in sorted(self._symbols)}
