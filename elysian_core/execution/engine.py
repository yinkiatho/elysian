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

"""

import asyncio
import time
from typing import Any, Dict, List, Optional, Union, TYPE_CHECKING

from elysian_core.connectors.base import AbstractDataFeed, SpotExchangeConnector
from elysian_core.core.enums import AssetType, OrderType, Side, Venue

from elysian_core.core.portfolio import Portfolio
from elysian_core.core.signals import OrderIntent, RebalanceResult, ValidatedWeights
import elysian_core.utils.logger as log

_STABLECOINS = frozenset({"USDT", "USDC", "BUSD"})

if TYPE_CHECKING:
    from elysian_core.core.shadow_book import ShadowBook

logger = log.setup_custom_logger("root")


def _round_step(value: float, step: float) -> float:
    """Round *value* down to the nearest multiple of *step*."""
    if step <= 0:
        return value
    return (int(value / step)) * step


class ExecutionEngine:
    '''
    Currently designed for one execution engine per venue
    '''

    def __init__(
        self,
        exchanges: Dict[Venue, SpotExchangeConnector],
        portfolio: Union[Portfolio, "ShadowBook"],
        feeds: Dict[str, AbstractDataFeed],
        default_venue: Venue = Venue.BINANCE,
        default_order_type: OrderType = OrderType.MARKET,
        symbol_venue_map: Optional[Dict[str, Venue]] = None,
        cfg: Optional[Any] = None,
        asset_type: AssetType = None,
        venue: Venue = None
        
    ):
        self._exchanges = exchanges
        self._portfolio = portfolio
        self._feeds = feeds
        self._default_venue = default_venue
        self._default_order_type = default_order_type
        self._symbol_venue_map = symbol_venue_map or {}
        self.cfg = cfg
        self.asset_type = asset_type
        self.venue = venue
        # order_id -> strategy_id mapping: populated on placement, read by ShadowBooks for routing
        self._order_strategy_map: Dict[str, int] = {}
        # Lock to prevent concurrent execute() calls from multiple strategy FSMs
        self._execute_lock = asyncio.Lock()

    # ── Public ───────────────────────────────────────────────────────────────

    async def execute(self, validated: ValidatedWeights, **ctx) -> RebalanceResult:
        """Execute a full rebalance cycle from validated weights to exchange orders."""
        async with self._execute_lock:
            logger.info(
                f"[ExecutionEngine] Starting rebalance: {len(validated.weights)} symbols"
            )
            mark_prices = self._get_mark_prices()
            logger.info(f"[ExecutionEngine] Mark prices snapshot: {len(mark_prices)} symbols")

            # Expose mark prices and portfolio value for downstream fill attribution
            ctx["_mark_prices"] = mark_prices
            ctx["_portfolio_total_value"] = self._portfolio.total_value(mark_prices)

            intents = self.compute_order_intents(validated, mark_prices, ctx.get('order_type', OrderType.MARKET))

            if not intents:
                logger.info("[ExecutionEngine] No order intents generated — portfolio already aligned")
                now_ms = int(time.time() * 1000)
                return RebalanceResult(
                    intents=(), submitted=0, failed=0, timestamp=now_ms,
                )

            # Partition: sells first (free capital), then buys
            sells = [i for i in intents if i.side == Side.SELL]
            buys = [i for i in intents if i.side == Side.BUY]
            logger.info(
                f"[ExecutionEngine] Order plan: {len(sells)} sells, {len(buys)} buys "
                f"(sells execute first)"
            )

            submitted, failed = 0, 0
            errors: List[str] = []
            submitted_orders_map: Dict[str, OrderIntent] = {}

            for intent in sells + buys:
                order_id = await self._submit_order(intent)
                if order_id:
                    submitted += 1
                    self._portfolio.lock_for_order(order_id, intent)
                    self._order_strategy_map[order_id] = intent.strategy_id
                    submitted_orders_map[order_id] = intent
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
                submitted_orders=submitted_orders_map,
            )

            if failed > 0:
                logger.warning(
                    f"[ExecutionEngine] Rebalance complete with errors: "
                    f"{submitted} submitted, {failed} FAILED — {errors}"
                )
            else:
                logger.info(
                    f"[ExecutionEngine] Rebalance complete: "
                    f"{submitted}/{len(intents)} orders submitted successfully"
                )
            return result

    def compute_order_intents(
        self, validated: ValidatedWeights, mark_prices: Dict[str, float], order_type: OrderType
    ) -> List[OrderIntent]:
        """Pure computation: validated weights -> order intents.  No side effects.

        Step-size rounding is applied here.  Min-notional and balance checks
        are deferred to the exchange connector's ``order_health_check()``.
        """
        strategy_id = int(validated.original.strategy_id) if validated.original.strategy_id else 0
        total_value = self._portfolio.total_value(mark_prices)
        if total_value <= 0:
            logger.warning("[ExecutionEngine] Portfolio total value <= 0, skipping")
            return []

        logger.info(f"[ExecutionEngine] Portfolio total value: {total_value:.2f}")
        intents: List[OrderIntent] = []

        price_overrides = validated.original.price_overrides or {}
        for symbol, target_weight in validated.weights.items():
            # Stablecoins must never appear as tradeable symbols — cash is always
            # implicit (1 - sum(weights)).  If a strategy emits a stablecoin key
            # it is a bug; warn loudly and skip so no order is placed.
            if symbol in _STABLECOINS:
                logger.warning(
                    f"[ExecutionEngine] Stablecoin '{symbol}' found in validated weights "
                    f"(w={target_weight:.4f}) — cash is implicit, skipping. "
                    f"Check strategy.compute_weights() for stablecoin key emission."
                )
                continue

            # Use strategy-provided limit price if given, else fall back to mark price
            price = price_overrides.get(symbol) or mark_prices.get(symbol)
            if price is None or price <= 0:
                logger.warning(f"[ExecutionEngine] No mark price for {symbol}, skipping")
                continue

            venue = self._symbol_venue_map.get(symbol, self._default_venue)
            exchange = self._exchanges.get(venue)
            if exchange is None:
                logger.warning(f"[ExecutionEngine] No exchange for venue {venue}, skipping {symbol}")
                continue

            # Weight-delta filter: skip legs whose deviation is too small to matter.
            # Avoids churning tiny rebalances that won't clear min-notional checks.
            current_qty = self._portfolio.position(symbol).quantity
            current_weight = (current_qty * price) / total_value
            weight_delta = abs(target_weight - current_weight)
            if weight_delta < 0.001:
                logger.debug(
                    f"[ExecutionEngine] Skipping {symbol}: "
                    f"weight_delta={weight_delta:.4f} below 0.001 threshold"
                )
                continue

            # Target vs current quantity
            target_notional = target_weight * total_value
            target_qty = target_notional / price
            delta_qty = target_qty - current_qty

            # Round to exchange step size
            step_size = exchange._token_infos.get(symbol, {}).get("step_size", 0.0001)
            abs_qty = _round_step(abs(delta_qty), step_size)

            if abs_qty <= 0:
                logger.debug(f"[ExecutionEngine] Skipping {symbol}: rounded qty is 0")
                continue

            side = Side.BUY if delta_qty > 0 else Side.SELL

            logger.info(
                f"[ExecutionEngine] Intent: {side.value} {symbol} "
                f"qty={abs_qty:.6f} @ ~{price:.4f} "
                f"(target_w={target_weight:.4f} current_w={current_weight:.4f} "
                f"delta_w={weight_delta:.4f})"
            )

            effective_order_type = order_type if order_type is not None else self._default_order_type
            intents.append(OrderIntent(
                symbol=symbol,
                venue=venue,
                side=side,
                quantity=abs_qty,
                order_type=effective_order_type,
                price=None if effective_order_type == OrderType.MARKET else price,
                strategy_id=strategy_id,
            ))

        logger.info(f"[ExecutionEngine] Generated {len(intents)} order intents")
        return intents

    # ── Private ──────────────────────────────────────────────────────────────

    async def _submit_order(self, intent: OrderIntent) -> Optional[str]:
        """Submit a single OrderIntent to the appropriate exchange connector.

        Returns the exchange-assigned order_id string on success, None on failure.
        The connector's ``order_health_check()`` handles min-notional and
        balance validation — the engine does not duplicate those checks.
        """
        exchange = self._exchanges.get(intent.venue)
        if exchange is None:
            logger.error(f"[ExecutionEngine] No exchange for {intent.venue}")
            return None

        try:
            logger.info(
                f"[ExecutionEngine] Submitting {intent.order_type.value} {intent.side.value} "
                f"{intent.symbol} qty={intent.quantity:.6f} on {intent.venue.value}"
            )
            resp = None
            if intent.order_type == OrderType.MARKET:
                resp = await exchange.place_market_order(
                    symbol=intent.symbol,
                    side=intent.side,
                    quantity=intent.quantity,
                    strategy_id=intent.strategy_id,
                    strategy_name="",
                )
            elif intent.order_type == OrderType.LIMIT and intent.price is not None:
                resp = await exchange.place_limit_order(
                    symbol=intent.symbol,
                    side=intent.side,
                    price=intent.price,
                    quantity=intent.quantity,
                    strategy_id=intent.strategy_id
                )
            else:
                logger.error(
                    f"[ExecutionEngine] Unsupported order type {intent.order_type} for {intent.symbol}"
                )
                return None

            order_id = str(resp.get("orderId", "")) if resp else ""
            if order_id:
                logger.info(
                    f"[ExecutionEngine] Order submitted OK: {intent.side.value} {intent.symbol} "
                    f"order_id={order_id}"
                )
                return order_id
            return None

        except Exception as e:
            logger.error(
                f"[ExecutionEngine] Order FAILED: {intent.side.value} {intent.symbol} "
                f"qty={intent.quantity:.6f} — {e}",
                exc_info=True,
            )
            return None

    def _get_mark_prices(self) -> Dict[str, float]:
        """Snapshot current mid prices from all feeds."""
        prices: Dict[str, float] = {}
        for symbol, feed in self._feeds.items():
            try:
                price = feed.latest_price
                if price and price > 0:
                    prices[symbol] = price
            except Exception as e:
                logger.error(f'[ExecutionEngine] Unable to get mark price for {symbol}: {e}')
                continue
        return prices
