"""
Execution engine (Stage 5).

Converts validated target weights into exchange orders, submits them, and
returns a :class:`RebalanceResult` summary.

Pipeline per rebalance cycle:
  1. Snapshot mark prices from feeds
  2. Compute total portfolio value
  3. For each symbol: target_qty = weight * total_value / price
  4. delta = target_qty - current_qty
  5. Round to step_size, filter near-zero deltas
  6. Execute *sells first* (free capital), then *buys*
  7. Return RebalanceResult

Note: min_notional and balance checks are handled by the exchange connector's
``order_health_check()`` — they are NOT duplicated here.
"""

import time
from typing import Dict, List, Optional

from elysian_core.connectors.base import AbstractDataFeed, SpotExchangeConnector
from elysian_core.core.enums import OrderType, Side, Venue
from elysian_core.core.portfolio import Portfolio
from elysian_core.core.signals import OrderIntent, RebalanceResult, ValidatedWeights
import elysian_core.utils.logger as log

logger = log.setup_custom_logger("root")


def _round_step(value: float, step: float) -> float:
    """Round *value* down to the nearest multiple of *step*."""
    if step <= 0:
        return value
    return (int(value / step)) * step


class ExecutionEngine:

    def __init__(
        self,
        exchanges: Dict[Venue, SpotExchangeConnector],
        portfolio: Portfolio,
        feeds: Dict[str, AbstractDataFeed],
        default_venue: Venue = Venue.BINANCE,
        default_order_type: OrderType = OrderType.MARKET,
        symbol_venue_map: Optional[Dict[str, Venue]] = None,
    ):
        self._exchanges = exchanges
        self._portfolio = portfolio
        self._feeds = feeds
        self._default_venue = default_venue
        self._default_order_type = default_order_type
        self._symbol_venue_map = symbol_venue_map or {}

    # ── Public ───────────────────────────────────────────────────────────────

    async def execute(self, validated: ValidatedWeights) -> RebalanceResult:
        """Execute a full rebalance cycle from validated weights to exchange orders."""
        mark_prices = self._get_mark_prices()
        intents = self.compute_order_intents(validated, mark_prices)

        if not intents:
            now_ms = int(time.time() * 1000)
            return RebalanceResult(
                intents=(), submitted=0, failed=0, timestamp=now_ms,
            )

        # Partition: sells first (free capital), then buys
        sells = [i for i in intents if i.side == Side.SELL]
        buys = [i for i in intents if i.side == Side.BUY]

        submitted = 0
        failed = 0
        errors: List[str] = []

        for intent in sells + buys:
            ok = await self._submit_order(intent)
            if ok:
                submitted += 1
            else:
                failed += 1
                errors.append(f"{intent.side.value} {intent.symbol} qty={intent.quantity:.6f}")

        now_ms = int(time.time() * 1000)
        result = RebalanceResult(
            intents=tuple(intents),
            submitted=submitted,
            failed=failed,
            timestamp=now_ms,
            errors=tuple(errors),
        )

        logger.info(
            f"[ExecutionEngine] Rebalance complete: "
            f"{submitted} submitted, {failed} failed, "
            f"{len(intents)} total intents"
        )
        return result

    def compute_order_intents(
        self, validated: ValidatedWeights, mark_prices: Dict[str, float]
    ) -> List[OrderIntent]:
        """Pure computation: validated weights -> order intents.  No side effects.

        Step-size rounding is applied here.  Min-notional and balance checks
        are deferred to the exchange connector's ``order_health_check()``.
        """
        total_value = self._portfolio.total_value(mark_prices)
        if total_value <= 0:
            logger.warning("[ExecutionEngine] Portfolio total value <= 0, skipping")
            return []

        intents: List[OrderIntent] = []

        for symbol, target_weight in validated.weights.items():
            price = mark_prices.get(symbol)
            if price is None or price <= 0:
                logger.warning(f"[ExecutionEngine] No mark price for {symbol}, skipping")
                continue

            venue = self._symbol_venue_map.get(symbol, self._default_venue)
            exchange = self._exchanges.get(venue)
            if exchange is None:
                logger.warning(f"[ExecutionEngine] No exchange for venue {venue}, skipping {symbol}")
                continue

            # Target vs current quantity
            target_notional = target_weight * total_value
            target_qty = target_notional / price
            current_qty = self._portfolio.position(symbol).quantity
            delta_qty = target_qty - current_qty

            if abs(delta_qty) < 1e-12:
                continue

            # Round to exchange step size
            step_size = exchange._token_infos.get(symbol, {}).get("step_size", 0.0001)
            abs_qty = _round_step(abs(delta_qty), step_size)

            if abs_qty <= 0:
                continue

            side = Side.BUY if delta_qty > 0 else Side.SELL

            intents.append(OrderIntent(
                symbol=symbol,
                venue=venue,
                side=side,
                quantity=abs_qty,
                order_type=self._default_order_type,
                price=None if self._default_order_type == OrderType.MARKET else price,
            ))

        return intents

    # ── Private ──────────────────────────────────────────────────────────────

    async def _submit_order(self, intent: OrderIntent) -> bool:
        """Submit a single OrderIntent to the appropriate exchange connector.

        The connector's ``order_health_check()`` handles min-notional and
        balance validation — the engine does not duplicate those checks.
        """
        exchange = self._exchanges.get(intent.venue)
        if exchange is None:
            logger.error(f"[ExecutionEngine] No exchange for {intent.venue}")
            return False

        try:
            if intent.order_type == OrderType.MARKET:
                await exchange.place_market_order(
                    symbol=intent.symbol,
                    side=intent.side,
                    quantity=intent.quantity,
                )
            elif intent.order_type == OrderType.LIMIT and intent.price is not None:
                await exchange.place_limit_order(
                    symbol=intent.symbol,
                    side=intent.side,
                    price=intent.price,
                    quantity=intent.quantity,
                )
            else:
                logger.error(
                    f"[ExecutionEngine] Unsupported order type {intent.order_type} for {intent.symbol}"
                )
                return False

            return True
        except Exception as e:
            logger.error(f"[ExecutionEngine] Order failed {intent.symbol}: {e}")
            return False

    def _get_mark_prices(self) -> Dict[str, float]:
        """Snapshot current mid prices from all feeds."""
        prices: Dict[str, float] = {}
        for symbol, feed in self._feeds.items():
            try:
                price = feed.latest_price
                if price and price > 0:
                    prices[symbol] = price
            except Exception:
                continue
        return prices
