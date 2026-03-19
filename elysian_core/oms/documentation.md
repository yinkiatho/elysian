# OMS Documentation

## Overview
The Order Management System (OMS) provides a unified interface for managing orders across different exchanges and protocols. It handles order lifecycle, position tracking, and risk management.

## Key Classes and Functions

### AbstractOMS (oms.py)
**Abstract base class defining the OMS interface for different exchange protocols.**

**Methods:**
- `__init__(account_id)`: Initialize with account identifier
- `_place_limit_order(order)`: Abstract method to place limit order
- `_cancel_limit_order(order)`: Abstract method to cancel limit order
- `_place_range_order(order)`: Abstract method to place range order
- `_cancel_range_order(order)`: Abstract method to cancel range order
- `refresh_balances()`: Abstract method to fetch current balances

**Order Management Methods:**
- `send_limit_order(order)`: Send limit order with duplicate prevention
- `cancel_limit_order(order)`: Cancel limit order
- `update_limit_order(order, price, amount)`: Update existing limit order
- `send_limit_orders(orders)`: Send multiple limit orders concurrently
- `cancel_limit_orders(orders)`: Cancel multiple limit orders

**Range Order Management:**
- `send_range_order(order)`: Send range order
- `cancel_range_order(order)`: Cancel range order
- `update_range_order(order, amount, lower_price, upper_price)`: Update range order
- `send_range_orders(orders)`: Send multiple range orders
- `cancel_range_orders(orders)`: Cancel multiple range orders

**Bulk Operations:**
- `cancel_all()`: Cancel all tracked orders

**Properties:**
- `open_orders`: All active orders
- `open_limit_orders`: Active limit orders
- `open_range_orders`: Active range orders
- `balances`: Current account balances

### OrderTracker (order_tracker.py)
**Tracks order state and account balances across the system.**

**Methods:**
- `add_limit_order(order)`: Add limit order to tracking
- `remove_limit_order(order_id)`: Remove limit order
- `get_limit_order(order_id)`: Retrieve limit order
- `add_range_order(order)`: Add range order to tracking
- `remove_range_order(order_id)`: Remove range order
- `get_range_order(order_id)`: Retrieve range order
- `update_balances(balances)`: Update account balances
- `active_orders()`: Get all active orders

**Attributes:**
- `limit_orders`: Dict of order_id → LimitOrder
- `range_orders`: Dict of order_id → RangeOrder
- `balances`: Current account balances

## Order Lifecycle

### Limit Order Flow
1. **Creation**: `send_limit_order()` called with LimitOrder object
2. **Validation**: Check for duplicates using sent_cache
3. **Submission**: Call exchange-specific `_place_limit_order()`
4. **Tracking**: Add to OrderTracker if successful
5. **Updates**: Monitor fills and status changes
6. **Cancellation**: `cancel_limit_order()` removes from tracking

### Range Order Flow
1. **Creation**: `send_range_order()` with RangeOrder object
2. **Validation**: Duplicate prevention and amount checks
3. **Submission**: Exchange-specific range order placement
4. **Tracking**: Add to range_orders dict
5. **Management**: Update positions and liquidity
6. **Cancellation**: Remove from tracking and cancel on exchange

## Exchange-Specific Implementations

### BinanceOMS (example implementation)
```python
class BinanceOMS(AbstractOMS):
    async def _place_limit_order(self, order: LimitOrder) -> bool:
        try:
            response = await self.binance_client.create_order(
                symbol=order.symbol,
                side=order.side.value,
                type="LIMIT",
                timeInForce="GTC",
                quantity=order.amount,
                price=order.price
            )
            return response['status'] == 'NEW'
        except Exception as e:
            logger.error(f"Failed to place limit order: {e}")
            return False
```

### ChainflipOMS (AMM-style)
```python
class ChainflipOMS(AbstractOMS):
    async def _place_range_order(self, order: RangeOrder) -> bool:
        # AMM-specific range order logic
        liquidity_params = self.calculate_liquidity_params(order)
        return await self.chainflip_client.set_range_order(liquidity_params)
```

## Risk Management

### Position Limits
```python
def check_position_limits(self, order: Order) -> bool:
    current_position = self.tracker.get_position(order.symbol)
    new_position = current_position + order.quantity
    
    if abs(new_position) > self.max_position_size:
        logger.warning(f"Position limit exceeded for {order.symbol}")
        return False
    return True
```

### Order Rate Limiting
```python
class RateLimiter:
    def __init__(self, max_orders_per_minute: int = 60):
        self.max_orders = max_orders_per_minute
        self.orders = deque(maxlen=max_orders_per_minute)
    
    def can_place_order(self) -> bool:
        now = time.time()
        # Remove orders outside the 1-minute window
        while self.orders and now - self.orders[0] > 60:
            self.orders.popleft()
        
        return len(self.orders) < self.max_orders
```

## Balance Management

### Balance Updates
```python
async def refresh_balances(self):
    """Fetch current balances from exchange and update tracker."""
    try:
        balances = await self.exchange_client.get_account()
        self.tracker.update_balances(balances)
        logger.info(f"Balances updated: {balances}")
    except Exception as e:
        logger.error(f"Failed to refresh balances: {e}")
```

### Balance Validation
```python
def validate_sufficient_balance(self, order: Order) -> bool:
    required_balance = order.quantity * order.price
    available_balance = self.tracker.balances.get(order.symbol, 0)
    
    if required_balance > available_balance:
        logger.error(f"Insufficient balance for {order.symbol}")
        return False
    return True
```

## Error Handling

### Order Failures
```python
async def handle_order_failure(self, order: Order, error: Exception):
    """Handle order placement failures with retry logic."""
    logger.error(f"Order failed: {order.id} - {error}")
    
    if isinstance(error, TemporaryError):
        # Retry with exponential backoff
        await asyncio.sleep(2 ** self.retry_count)
        self.retry_count += 1
        if self.retry_count < self.max_retries:
            await self.send_limit_order(order)
    else:
        # Permanent failure - notify risk management
        await self.risk_manager.handle_permanent_failure(order)
```

### Connection Issues
```python
async def handle_connection_error(self):
    """Handle exchange connectivity issues."""
    logger.warning("Exchange connection lost")
    
    # Pause order placement
    self.trading_paused = True
    
    # Attempt reconnection
    while not await self.test_connection():
        await asyncio.sleep(5)
    
    # Resume trading
    self.trading_paused = False
    logger.info("Exchange connection restored")
```

## Performance Optimization

### Caching
- **Sent Orders Cache**: Prevents duplicate order submission
- **Cancelled Orders Cache**: Prevents duplicate cancellation attempts
- **Balance Cache**: Reduces API calls for balance checks

### Concurrent Processing
```python
async def send_limit_orders(self, orders: List[LimitOrder]):
    """Send multiple orders concurrently."""
    tasks = [self.send_limit_order(order) for order in orders]
    await asyncio.gather(*tasks, return_exceptions=True)
```

### Batch Operations
```python
async def batch_cancel_orders(self, order_ids: List[str]):
    """Cancel multiple orders in batch."""
    # Group by exchange for efficient batching
    exchange_groups = self.group_orders_by_exchange(order_ids)
    
    tasks = []
    for exchange, ids in exchange_groups.items():
        tasks.append(self.batch_cancel_on_exchange(exchange, ids))
    
    await asyncio.gather(*tasks)
```

## Monitoring and Logging

### Order Tracking
```python
def log_order_event(self, order: Order, event: str):
    """Log order lifecycle events."""
    logger.info(f"Order {event}: {order.id} {order.symbol} {order.side} "
                f"{order.quantity} @ {order.price} - Status: {order.status}")
```

### Performance Metrics
```python
class OMSMetrics:
    def __init__(self):
        self.orders_placed = 0
        self.orders_cancelled = 0
        self.order_failures = 0
        self.average_execution_time = 0
    
    def record_order_placed(self, execution_time: float):
        self.orders_placed += 1
        self.average_execution_time = (
            (self.average_execution_time * (self.orders_placed - 1)) + execution_time
        ) / self.orders_placed
```

## Testing and Simulation

### Mock OMS
```python
class MockOMS(AbstractOMS):
    """OMS implementation for testing and simulation."""
    
    async def _place_limit_order(self, order: LimitOrder) -> bool:
        # Simulate order placement
        await asyncio.sleep(0.01)  # Simulate network latency
        order.id = f"mock_{len(self.tracker.limit_orders)}"
        return True
    
    async def refresh_balances(self):
        # Return mock balances
        self.tracker.update_balances({
            "USDT": 10000.0,
            "BTC": 0.5,
            "ETH": 10.0
        })
```

### Order Validation
```python
def validate_order(self, order: Order) -> List[str]:
    """Validate order parameters before submission."""
    errors = []
    
    if order.quantity <= 0:
        errors.append("Order quantity must be positive")
    
    if order.price <= 0:
        errors.append("Order price must be positive")
    
    if order.symbol not in self.valid_symbols:
        errors.append(f"Invalid symbol: {order.symbol}")
    
    return errors
```

## Integration Examples

### Full OMS Setup
```python
from oms import BinanceOMS
from core.order import LimitOrder
from core.enums import Side

# Initialize OMS
oms = BinanceOMS(account_id="trading_account_1")

# Create and send order
order = LimitOrder(
    id="12345",
    base_asset="BTC",
    quote_asset="USDT", 
    side=Side.BUY,
    price=50000.0,
    amount=0.001
)

success = await oms.send_limit_order(order)
if success:
    print(f"Order placed: {order.id}")
else:
    print("Order placement failed")
```

### Order Monitoring (Event-Driven)

With the EventBus architecture, order monitoring is event-driven rather than poll-based. The `BinanceUserDataClientManager` publishes `OrderUpdateEvent` and `BalanceUpdateEvent` to the EventBus, which dispatches to strategy hooks:

```python
from elysian_core.strategy.base_strategy import SpotStrategy
from elysian_core.core.events import OrderUpdateEvent, BalanceUpdateEvent
from elysian_core.core.enums import OrderStatus

class OrderMonitorStrategy(SpotStrategy):
    async def on_order_update(self, event: OrderUpdateEvent):
        """React to order status changes in real-time."""
        if event.order.status == OrderStatus.FILLED:
            await self.handle_fill(event.order)
        elif event.order.status == OrderStatus.REJECTED:
            await self.handle_rejection(event.order)

    async def on_balance_update(self, event: BalanceUpdateEvent):
        """React to balance changes in real-time."""
        logger.info(f"Balance {event.asset}: delta={event.delta:+.8f}")
```

This replaces the legacy polling pattern (`while True: check orders, sleep(1)`) with zero-latency event dispatch.

### Risk Management Integration
```python
class RiskManagedOMS(AbstractOMS):
    def __init__(self, risk_manager, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.risk_manager = risk_manager
    
    async def send_limit_order(self, order: LimitOrder):
        # Check risk limits before placing order
        if not self.risk_manager.check_order_risk(order):
            logger.warning(f"Order rejected by risk manager: {order.id}")
            return False
        
        return await super().send_limit_order(order)
```</content>
<parameter name="filePath">c:\Users\yinki\Coding Work\elysian\elysian_core\oms\documentation.md