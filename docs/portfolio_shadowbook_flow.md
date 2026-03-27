# Portfolio & ShadowBook Event Flow

## Overview

The system maintains two layers of position accounting:

| Layer | Class | Scope | Source of truth |
|-------|-------|-------|-----------------|
| **Portfolio** | `elysian_core/core/portfolio.py` | Entire account — all strategies combined | Exchange (fills, balances) |
| **ShadowBook** | `elysian_core/core/shadow_book.py` | Single strategy's slice | Portfolio fills filtered by `strategy_id` |

The Portfolio is the canonical ledger. ShadowBooks are derived views: each strategy reads its own ShadowBook to compute target weights, never the aggregate Portfolio.

**Invariant**: `sum(ShadowBook[s].position(sym).quantity for s) ≈ Portfolio.position(sym).quantity`

---

## 1. Balance Update Flow

```
Binance WebSocket (outboundAccountPosition / balanceUpdate)
  │
  ▼
BinanceUserDataClientManager._on_message()
  │  Constructs BalanceUpdateEvent(
  │      asset   = "USDT",
  │      delta   = +500.0,          ← incremental change
  │      new_balance = 0.0,         ← BUG-2: always 0, not used
  │      venue   = Venue.BINANCE
  │  )
  ▼
EventBus.publish(EventType.BALANCE_UPDATE, event)
  │
  ├──▶ Portfolio._on_balance(event)          [subscribed in Portfolio.start()]
  │      if asset in {"USDT","USDC","BUSD"} and venue matches:
  │        _cash_dict[asset] += event.delta
  │        _cash = sum(_cash_dict.values())
  │        _refresh_derived()                 ← NAV / weights / drawdown updated
  │
  └──▶ (ShadowBook does NOT subscribe to BALANCE_UPDATE)
         Cash for each ShadowBook is set at init and adjusted only through fills.
         On restart: init_from_portfolio_cash() re-proportions total cash.
```

**Note on BUG-2**: `new_balance=0.0` is always published by the current connector.
`_on_balance` correctly uses `event.delta`, so there is no live impact — but the
field is misleading. Fix: compute `new_balance = exchange.get_balance(asset)` before
publishing.

---

## 2. Order Update → Portfolio Flow

```
Binance WebSocket (executionReport)
  │
  ▼
BinanceUserDataClientManager._on_message()
  │
  ▼
_parse_execution_to_order(msg) → Order(
    id              = msg["i"],
    symbol          = msg["s"],
    side            = Side.BUY / SELL,
    filled_qty      = float(msg["z"]),         ← cumulative filled
    avg_fill_price  = cum_quote / cum_filled,  ← cumulative average
    commission      = float(msg["n"]),         ← per-FILL (field "n", NOT cumulative)
    commission_asset= msg["N"],
    status          = OrderStatus.FILLED / PARTIALLY_FILLED / CANCELLED / ...,
    is_terminal     = True if status in {FILLED, CANCELLED, REJECTED, EXPIRED}
)
  │
  ▼
EventBus.publish(EventType.ORDER_UPDATE, OrderUpdateEvent(order, venue))
  │
  ├──▶ Portfolio._on_order_update(event)
  │      [BUG-1 FIX: now subscribed via Portfolio.start()]
  │
  │      if venue != self.venue: return
  │      if status not in (FILLED, PARTIALLY_FILLED):
  │        if is_terminal: _fill_tracker.pop(order_id)
  │        return
  │
  │      prev_filled = _fill_tracker.get(order_id, 0.0)
  │      delta_filled = order.filled_qty - prev_filled     ← incremental only
  │      if delta_filled < 1e-10: return                  ← deduplicate events
  │
  │      _fill_tracker[order_id] = order.filled_qty       ← advance cursor
  │      qty_delta = +delta_filled (BUY) or -delta_filled (SELL)
  │
  │      ▼
  │      Portfolio.update_position(
  │          symbol, qty_delta,
  │          price=order.avg_fill_price,
  │          commission=order.commission    ← per-fill direct, no tracker needed
  │      )
  │        old_qty = pos.quantity           ← captured BEFORE mutation
  │        new_qty = old_qty + qty_delta
  │
  │        if same_direction:
  │          avg_entry = weighted average (VWAP cost basis)
  │        else (reducing / closing / flipping):
  │          pnl = (fill_price - entry) * closing_qty * sign
  │          _total_realized_pnl += pnl
  │          if new_qty == 0: entry = 0
  │          elif (new_qty > 0) != (old_qty > 0):   ← BUG-5 FIX: uses old_qty
  │            entry = fill_price                   ← flip: reset to new direction
  │
  │        pos.total_commission += commission
  │        _total_commission += commission
  │        _cash -= qty_delta * price               ← cash leg of trade
  │        _cash -= commission                      ← fee leg
  │        _mark_prices[symbol] = price
  │        _refresh_derived()                       ← NAV / weights / drawdown
  │        _fills.append(Fill(...))                 ← audit trail
  │
  │      if is_terminal: _fill_tracker.pop(order_id)
  │
  └──▶ ShadowBook._on_order_update(event)   ← PROPOSED: see Section 3
```

### _refresh_derived() — what it recomputes

```
_nav    = _cash + sum(pos.quantity * _mark_prices.get(sym, pos.avg_entry_price))
_weights = {sym: (pos.qty * mark_price) / _nav}   (flat positions excluded)
_peak_equity = max(_peak_equity, _nav)
_max_drawdown = max(_max_drawdown, (peak - nav) / peak)
```

---

## 3. Order Update → ShadowBook Flow (Proposed Design)

### The Problem with Current Approach

`reconcile_shadow_books()` is called after a rebalance cycle. It computes the *target*
qty for each symbol and calls `apply_fill()` with the delta. This means:

- Attribution uses **mark price**, not actual **fill price** — cost basis is wrong
- Commission is **never attributed** to any ShadowBook (BUG-3)
- Reconciliation only runs after a rebalance — intra-cycle partial fills are invisible
- A single symbol traded by two strategies cannot be individually attributed

### Proposed: Tag Orders with strategy_id, Subscribe ShadowBook to ORDER_UPDATE

**Step 1 — Add `strategy_id` to `Order`**

```python
# order.py
@dataclass
class Order:
    ...
    strategy_id: Optional[int] = None   # ← new field
```

**Step 2 — Tag orders at placement**

In `base_strategy.py` or the execution layer, set `order.strategy_id = self.strategy_id`
before placing via the exchange connector.

**Step 3 — ShadowBook subscribes to ORDER_UPDATE**

```python
# shadow_book.py

def start(self, event_bus, fill_tracker: Optional[Dict[str, float]] = None):
    """Subscribe to ORDER_UPDATE on the EventBus."""
    self._event_bus = event_bus
    self._fill_tracker = fill_tracker or {}
    event_bus.subscribe(EventType.ORDER_UPDATE, self._on_order_update)

def stop(self):
    if self._event_bus:
        self._event_bus.unsubscribe(EventType.ORDER_UPDATE, self._on_order_update)
        self._event_bus = None

async def _on_order_update(self, event):
    order = event.order

    # Filter: only process fills for THIS strategy's orders
    if order.strategy_id != self._strategy_id:
        return
    if order.status not in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
        if order.is_terminal:
            self._fill_tracker.pop(order.id, None)
        return

    prev_filled = self._fill_tracker.get(order.id, 0.0)
    delta_filled = order.filled_qty - prev_filled
    if delta_filled < 1e-10:
        if order.is_terminal:
            self._fill_tracker.pop(order.id, None)
        return

    self._fill_tracker[order.id] = order.filled_qty
    qty_delta = delta_filled if order.side == Side.BUY else -delta_filled

    self.apply_fill(
        symbol=order.symbol,
        qty_delta=qty_delta,
        price=order.avg_fill_price,      # ← actual fill price, not mark
        commission=order.commission,     # ← per-fill, correct attribution
        venue=event.venue,
    )
    # apply_fill() calls _refresh_derived() → NAV / weights updated immediately

    if order.is_terminal:
        self._fill_tracker.pop(order.id, None)
```

### Resulting Flow

```
EventBus.publish(ORDER_UPDATE)
  │
  ├──▶ Portfolio._on_order_update()   ← aggregate accounting
  │      delta fill → update_position() → _refresh_derived()
  │
  └──▶ ShadowBook._on_order_update()  ← per-strategy accounting
         filter order.strategy_id == self._strategy_id
         delta fill → apply_fill() → _refresh_derived()
           NAV updated at fill price
           weights recalculated
           commission attributed correctly
```

### Key Properties

| Property | Old (reconcile) | New (event-driven) |
|----------|----------------|--------------------|
| Attribution price | Mark price at rebalance | Actual fill price |
| Commission | Never attributed | Per-fill from field "n" |
| Timing | After rebalance only | Immediately on fill |
| Partial fill visibility | Invisible intra-cycle | Real-time |
| Multi-strategy same symbol | Shared attribution drift | Clean per-strategy |

---

## 4. Kline Update → Mark Price Flow

```
Binance WebSocket (kline stream)
  │
  ▼
EventBus.publish(EventType.KLINE, KlineEvent(symbol, kline, venue))
  │
  ├──▶ Portfolio._on_kline(event)
  │      _mark_prices[symbol] = kline.close
  │      _refresh_derived()
  │
  └──▶ Strategy._on_kline(event) [if subscribed]
         strategy.shadow_book.update_mark_prices({symbol: kline.close})
           → _refresh_derived()   ← ShadowBook weights updated on every candle
```

---

## 5. Startup Sequence

```
StrategyRunner.__init__()
  │
  ├── load_app_config()                   ← two-tier YAML: trading_config + strategy YAML
  │
  ▼
StrategyRunner.setup()
  ├── exchange = BinanceExchange(...)
  ├── exchange.run()                      ← WebSocket connects, balances populated
  ├── portfolio = Portfolio(exchange, venue=Venue.BINANCE, cfg=cfg)
  ├── portfolio.sync_from_exchange(feeds)
  │     cash synced from stablecoin balances
  │     positions seeded from exchange._balances
  │     mark prices seeded from feeds
  │     _fill_tracker seeded from partially-filled open orders
  │     _refresh_derived()
  │
  ├── for each strategy:
  │     shadow_book = ShadowBook(strategy_id, allocation, venue)
  │     if fresh_start:
  │       shadow_book.init_from_portfolio_cash(portfolio.cash, mark_prices)
  │     else:
  │       shadow_book.init_from_existing(portfolio.cash, portfolio.positions, mark_prices)
  │     shadow_book.start(event_bus)      ← PROPOSED: subscribe to ORDER_UPDATE
  │
  └── portfolio.start(event_bus, feeds)
        subscribe KLINE, BALANCE_UPDATE, ORDER_UPDATE
```

---

## 6. Bug Register

### Fixed

| ID | File | Description | Fix Applied |
|----|------|-------------|-------------|
| BUG-1 / GAP-2 | portfolio.py | `start()` never subscribed to `ORDER_UPDATE` | Added subscription + `_on_order_update` handler |
| BUG-4 | portfolio.py | `_on_balance()` did not call `_refresh_derived()` | Added call at end of handler |
| BUG-5 | portfolio.py | Flip detection read mutated `pos.quantity` | Captured `old_qty` before mutation |
| Commission tracker | portfolio.py | `_commission_tracker` referenced but never declared | Removed; `order.commission` is per-fill (field "n") |
| f-string malform | BinanceExchange.py | `return order` merged into f-string opening brace | Separated into two statements |

### Open

#### BUG-3 / GAP-1 (HIGH) — ShadowBook commission never attributed; mark price not fill price

**Root cause**: `reconcile_shadow_books()` uses `mark_prices` passed in from the
optimizer context. It never sees actual fill prices and never receives commission data.

**Impact**: Every ShadowBook's `avg_entry_price` is systematically wrong; `net_pnl()`
understates cost by the total commission paid; per-strategy performance attribution is
unreliable.

**Fix**: Replace `reconcile_shadow_books()` with event-driven `_on_order_update` as
described in Section 3. Keep `reconcile_shadow_books()` as a periodic drift-correction
pass only (not primary accounting).

---

#### BUG-7 (LOW) — Stale `avg_entry_price` fallback in weight computation

**Location**: `portfolio.py` `_refresh_derived()` and `shadow_book.py` `_refresh_derived()`

```python
# Current — falls back to avg_entry_price when mark price missing
(pos.quantity * self._mark_prices.get(sym, pos.avg_entry_price)) / self._nav
```

**Problem**: If a symbol drops out of `_mark_prices` (feed outage, symbol delisted),
the weight vector silently uses stale cost basis as proxy price. This can overstate or
understate weights significantly on illiquid or delisted assets, potentially triggering
incorrect rebalance orders.

**Improvement**:
```python
# Option A — exclude from weights if no mark price (conservative)
if sym in self._mark_prices:
    weight = (pos.quantity * self._mark_prices[sym]) / self._nav

# Option B — log a warning and skip, so strategy knows the feed is stale
if sym not in self._mark_prices:
    logger.warning(f"[Portfolio] No mark price for {sym} — excluded from weights")
    continue
```

Option A is safer for spot-only. Option B provides observability. Both prevent silent
mis-pricing. The NAV computation itself has the same fallback and should be treated
consistently — either both use it or neither does.

---

#### BUG-8 (LOW) — Short position NAV unsound for spot-only accounts

**Location**: `portfolio.py` `_compute_total_value()`, `update_position()`

```python
# Current — works for futures/margin, incorrect for spot
pos_value = sum(pos.quantity * prices.get(pos.symbol, pos.avg_entry_price)
                for pos in self._positions.values())
```

**Problem**: On a spot account, `pos.quantity` can go negative (e.g., BUG-5 flip
scenario, or initialization error). A negative quantity multiplied by a positive price
produces negative NAV contribution, which is financially meaningless for spot. For
futures it is correct (short = negative notional), but Elysian currently targets spot.

**Improvements**:
1. Add a guard in `update_position()` that asserts `new_qty >= 0` for spot accounts:
   ```python
   if self._asset_type == AssetType.SPOT and new_qty < -1e-10:
       logger.error(f"[Portfolio] Spot short detected: {symbol} qty={new_qty}")
       # clamp or raise depending on risk tolerance
   ```
2. Store `AssetType` on Portfolio (already on StrategyRunner) and use it to gate
   short logic. This also makes the `sign` in PnL computation self-documenting.

---

#### BUG-2 (MEDIUM latent) — `new_balance=0.0` always published

**Location**: `BinanceExchange.py` (UserDataClientManager)

**Problem**: `BalanceUpdateEvent(new_balance=0.0)` is always zero. If any future
consumer reads `new_balance` instead of `delta`, it will silently set cash to zero.
`Portfolio._on_balance` is currently correct (uses `delta`), but the field is a trap.

**Improvement**: Compute actual new balance before publishing:
```python
new_balance = self.exchange.get_balance(asset)   # after internal update
event = BalanceUpdateEvent(asset=asset, delta=delta, new_balance=new_balance, venue=venue)
```

---

## 7. Proposed Changes Summary

| Change | File(s) | Priority |
|--------|---------|----------|
| Add `strategy_id: Optional[int]` to `Order` | `order.py` | HIGH |
| Tag orders with `strategy_id` at placement | `base_strategy.py` / execution layer | HIGH |
| Add `ShadowBook.start(event_bus)` + `_on_order_update` | `shadow_book.py` | HIGH |
| Call `shadow_book.start(event_bus)` in `StrategyRunner.setup()` | `run_strategy.py` | HIGH |
| Deprecate `reconcile_shadow_books()` as primary accounting | `shadow_book.py` | HIGH |
| Exclude symbols with no mark price from weights (BUG-7) | `portfolio.py`, `shadow_book.py` | LOW |
| Add spot short guard in `update_position()` (BUG-8) | `portfolio.py` | LOW |
| Publish correct `new_balance` in balance event (BUG-2) | `BinanceExchange.py` | MEDIUM |
