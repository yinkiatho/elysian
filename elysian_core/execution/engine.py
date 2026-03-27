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
from typing import Any, Dict, List, Optional

from elysian_core.connectors.base import AbstractDataFeed, SpotExchangeConnector
from elysian_core.core.enums import OrderType, Side, Venue, AssetType
from elysian_core.core.portfolio import Portfolio
from elysian_core.core.signals import OrderIntent, RebalanceResult, ValidatedWeights
from elysian_core.risk.risk_config import RiskConfig
import elysian_core.utils.logger as log

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
        portfolio: Portfolio,
        feeds: Dict[str, AbstractDataFeed],
        risk_config: Optional[RiskConfig] = None,
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
        self._risk_config = risk_config or RiskConfig()
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

            intents = self.compute_order_intents(validated, mark_prices)

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
        self, validated: ValidatedWeights, mark_prices: Dict[str, float]
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

            # Weight-delta filter: skip legs below threshold
            current_weight = (self._portfolio.position(symbol).quantity * price) / total_value
            weight_delta = abs(target_weight - current_weight)
            if weight_delta < self._risk_config.min_weight_delta:
                logger.debug(
                    f"[ExecutionEngine] Skipping {symbol}: weight_delta={weight_delta:.4f} "
                    f"< min={self._risk_config.min_weight_delta:.4f}"
                )
                continue

            # Target vs current quantity
            target_notional = target_weight * total_value
            target_qty = target_notional / price
            current_qty = self._portfolio.position(symbol).quantity
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

            intents.append(OrderIntent(
                symbol=symbol,
                venue=venue,
                side=side,
                quantity=abs_qty,
                order_type=self._default_order_type,
                price=None if self._default_order_type == OrderType.MARKET else price,
                strategy_id=strategy_id,
            ))

        logger.info(f"[ExecutionEngine] Generated {len(intents)} order intents")
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
                )
            elif intent.order_type == OrderType.LIMIT and intent.price is not None:
                resp = await exchange.place_limit_order(
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

            # Record order_id -> strategy_id so ShadowBooks can route ORDER_UPDATE events
            if resp and intent.strategy_id:
                order_id = str(resp.get("orderId", ""))
                if order_id:
                    self._order_strategy_map[order_id] = intent.strategy_id

            logger.info(f"[ExecutionEngine] Order submitted OK: {intent.side.value} {intent.symbol}")
            return True
        except Exception as e:
            logger.error(
                f"[ExecutionEngine] Order FAILED: {intent.side.value} {intent.symbol} "
                f"qty={intent.quantity:.6f} — {e}",
                exc_info=True,
            )
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
