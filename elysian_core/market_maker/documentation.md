# Market Maker Documentation

## Overview
The market maker module provides components for liquidity provision and market making strategies. It includes order book analysis, position management, and automated order lifecycle handling for AMM-style protocols.

## Key Classes and Functions

### OrderBook (order_book.py)
**LP-side order book management for AMM protocols like Chainflip.**

**Methods:**
- `__init__(base_asset, lp_id)`: Initialize with base asset and LP identifier
- `process_order_book(data, current_block_hash, current_block_number, tick_to_price, hex_to_decimal)`: Parse and update order book from RPC response
- `update_order_book()`: Refresh order book data
- `calculate_spread()`: Calculate bid-ask spread
- `find_optimal_price()`: Find optimal pricing for LP orders

**Order Book Processing:**
- Parses limit orders and range orders from pool data
- Separates LP's own orders from market orders
- Maintains top-of-book references
- Handles block hash validation to prevent stale updates

**Attributes:**
- `_base_asset`: Base asset symbol
- `_lp_id`: Liquidity provider ID
- `_bids`: List of bid LimitOrder objects
- `_asks`: List of ask LimitOrder objects
- `_range_orders`: List of RangeOrder objects
- `_lp_open_orders`: LP's active orders
- `_top_bid`: Best bid order
- `_top_ask`: Best ask order

### OrderManagement (order_management.py)
**Manages the lifecycle of market making orders.**

**Methods:**
- `__init__(oms, order_book, risk_manager)`: Initialize with dependencies
- `create_limit_order(side, price, amount)`: Create new limit order
- `create_range_order(lower_price, upper_price, amount)`: Create new range order
- `update_order(order_id, new_price, new_amount)`: Update existing order
- `cancel_order(order_id)`: Cancel specific order
- `cancel_all_orders()`: Cancel all active orders
- `rebalance_positions()`: Rebalance positions to target allocations

**Order Lifecycle Management:**
1. **Creation**: Generate orders based on strategy parameters
2. **Submission**: Send orders through OMS
3. **Monitoring**: Track order status and fills
4. **Adjustment**: Update orders based on market conditions
5. **Cancellation**: Remove orders when conditions change

### OrderTracker (order_tracker.py)
**Tracks market making orders and their execution status.**

**Methods:**
- `__init__()`: Initialize tracking structures
- `add_order(order)`: Add new order to tracking
- `update_order(order_id, status, filled_amount)`: Update order status
- `remove_order(order_id)`: Remove completed/cancelled order
- `get_order(order_id)`: Retrieve order details
- `get_active_orders()`: Get all active orders
- `calculate_inventory_imbalance()`: Calculate current inventory skew

**Tracking Features:**
- Order status monitoring
- Fill tracking and P&L calculation
- Inventory management
- Performance metrics collection

## Market Making Strategies

### Basic Market Making
```python
class BasicMarketMaker:
    def __init__(self, order_book, spread_bps=50):
        self.order_book = order_book
        self.spread_bps = spread_bps
    
    def generate_quotes(self, mid_price):
        """Generate bid and ask quotes around mid price."""
        half_spread = mid_price * (self.spread_bps / 10000)
        
        bid_price = mid_price - half_spread
        ask_price = mid_price + half_spread
        
        return bid_price, ask_price
```

### Inventory Management
```python
class InventoryManager:
    def __init__(self, target_inventory, max_skew):
        self.target_inventory = target_inventory
        self.max_skew = max_skew
    
    def adjust_quotes(self, current_inventory, base_bid, base_ask):
        """Adjust quotes based on inventory position."""
        inventory_skew = current_inventory - self.target_inventory
        
        if abs(inventory_skew) > self.max_skew:
            # Reduce exposure on skewed side
            if inventory_skew > 0:
                # Too long - reduce bid size or increase bid price
                adjusted_bid = base_bid * 1.001  # Slightly higher bid
                return adjusted_bid, base_ask
            else:
                # Too short - reduce ask size or decrease ask price
                adjusted_ask = base_ask * 0.999  # Slightly lower ask
                return base_bid, adjusted_ask
        
        return base_bid, base_ask
```

### Range Order Management
```python
class RangeOrderManager:
    def __init__(self, order_book, position_manager):
        self.order_book = order_book
        self.position_manager = position_manager
    
    def create_concentrated_position(self, lower_price, upper_price, amount):
        """Create concentrated liquidity position."""
        range_order = RangeOrder(
            id=self.generate_order_id(),
            base_asset=self.order_book._base_asset,
            quote_asset="USDC",
            lower_price=lower_price,
            upper_price=upper_price,
            amount=amount,
            order_type=RangeOrderType.LIQUIDITY
        )
        
        return range_order
    
    def adjust_position(self, current_price, volatility):
        """Adjust position based on price and volatility."""
        # Calculate optimal range based on current conditions
        range_width = self.calculate_optimal_range(volatility)
        lower_price = current_price * (1 - range_width)
        upper_price = current_price * (1 + range_width)
        
        # Adjust existing positions or create new ones
        self.rebalance_range_orders(lower_price, upper_price)
```

## Risk Management

### Position Limits
```python
class PositionLimits:
    def __init__(self, max_position, max_drawdown, max_volatility):
        self.max_position = max_position
        self.max_drawdown = max_drawdown
        self.max_volatility = max_volatility
    
    def check_limits(self, current_position, current_pnl, volatility):
        """Check if current state violates risk limits."""
        if abs(current_position) > self.max_position:
            return False, "Position limit exceeded"
        
        if current_pnl < -self.max_drawdown:
            return False, "Drawdown limit exceeded"
        
        if volatility > self.max_volatility:
            return False, "Volatility limit exceeded"
        
        return True, "Within limits"
```

### Circuit Breakers
```python
class CircuitBreaker:
    def __init__(self, error_threshold, recovery_time):
        self.error_threshold = error_threshold
        self.recovery_time = recovery_time
        self.error_count = 0
        self.last_error_time = None
        self.breaker_tripped = False
    
    def record_error(self):
        """Record an error and check if breaker should trip."""
        self.error_count += 1
        self.last_error_time = time.time()
        
        if self.error_count >= self.error_threshold:
            self.breaker_tripped = True
            logger.warning("Circuit breaker tripped")
    
    def can_proceed(self):
        """Check if operations can proceed."""
        if not self.breaker_tripped:
            return True
        
        # Check if recovery time has passed
        if time.time() - self.last_error_time > self.recovery_time:
            self.breaker_tripped = False
            self.error_count = 0
            logger.info("Circuit breaker reset")
            return True
        
        return False
```

## Performance Monitoring

### Metrics Collection
```python
class MarketMakerMetrics:
    def __init__(self):
        self.orders_placed = 0
        self.orders_filled = 0
        self.total_volume = 0
        self.realized_pnl = 0
        self.unrealized_pnl = 0
        self.spread_captured = 0
    
    def record_fill(self, order, fill_price, fill_amount):
        """Record order fill metrics."""
        self.orders_filled += 1
        self.total_volume += fill_amount
        
        # Calculate spread capture
        if order.side == Side.BUY:
            spread_capture = order.price - fill_price
        else:
            spread_capture = fill_price - order.price
        
        self.spread_captured += spread_capture * fill_amount
    
    def get_performance_stats(self):
        """Calculate performance statistics."""
        return {
            "fill_rate": self.orders_filled / max(self.orders_placed, 1),
            "average_spread_capture": self.spread_captured / max(self.total_volume, 1),
            "total_pnl": self.realized_pnl + self.unrealized_pnl,
            "sharpe_ratio": self.calculate_sharpe_ratio()
        }
```

### Logging and Alerting
```python
def log_market_making_activity(order_book, metrics):
    """Log market making activity and performance."""
    logger.info(f"Active orders: {len(order_book.lp_open_orders)}")
    logger.info(f"Top bid: {order_book.top_bid.price if order_book.top_bid else 'None'}")
    logger.info(f"Top ask: {order_book.top_ask.price if order_book.top_ask else 'None'}")
    logger.info(f"Spread: {order_book.calculate_spread()} bps")
    logger.info(f"24h P&L: ${metrics.realized_pnl:.2f}")
    
    # Alert on significant events
    if metrics.realized_pnl < -1000:
        logger.warning(f"Large loss detected: ${metrics.realized_pnl:.2f}")
    
    if len(order_book.lp_open_orders) == 0:
        logger.warning("No active orders - market making stopped")
```

## Integration with Core Systems

### OMS Integration
```python
class MarketMakerOMS:
    def __init__(self, oms, order_book):
        self.oms = oms
        self.order_book = order_book
    
    async def place_market_making_orders(self, orders):
        """Place market making orders through OMS."""
        for order in orders:
            if isinstance(order, LimitOrder):
                await self.oms.send_limit_order(order)
            elif isinstance(order, RangeOrder):
                await self.oms.send_range_order(order)
    
    async def monitor_and_adjust(self):
        """Monitor positions and adjust orders as needed."""
        while True:
            # Check current positions
            current_position = self.oms.tracker.get_position(self.order_book._base_asset)
            
            # Adjust orders based on position
            adjustments = self.calculate_adjustments(current_position)
            
            if adjustments:
                await self.place_market_making_orders(adjustments)
            
            await asyncio.sleep(60)  # Check every minute
```

### SpotStrategy Integration
```python
from elysian_core.strategy.base_strategy import SpotStrategy
from elysian_core.core.events import OrderBookUpdateEvent, KlineEvent

class MarketMakingStrategy(SpotStrategy):
    def __init__(self, market_maker, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.market_maker = market_maker

    async def on_orderbook_update(self, event: OrderBookUpdateEvent):
        """React to orderbook updates for market making."""
        # Update internal order book state
        await self.market_maker.order_book.update_order_book()

        # Generate new quotes based on live spread
        mid_price = event.orderbook.mid_price
        quotes = self.market_maker.generate_quotes(mid_price)

        # Place orders via exchange connector
        exchange = self.get_exchange()
        await self.market_maker.place_orders(quotes, exchange)

    async def on_kline(self, event: KlineEvent):
        """Adjust spread based on volatility from kline data."""
        vol = self.get_current_vol(event.symbol)
        if vol:
            self.market_maker.adjust_spread(vol)
```

## Configuration

### Market Making Parameters
```yaml
market_making:
  spread_bps: 50
  order_size: 1000
  max_inventory_skew: 5000
  rebalance_threshold: 1000
  max_orders: 10
  order_refresh_interval: 300
```

### Risk Parameters
```yaml
risk:
  max_position: 10000
  max_drawdown: 5000
  max_volatility: 0.1
  circuit_breaker_threshold: 5
  circuit_breaker_recovery: 300
```

## Testing and Simulation

### Backtesting Framework
```python
class MarketMakerBacktest:
    def __init__(self, historical_data, market_maker):
        self.data = historical_data
        self.market_maker = market_maker
        self.metrics = MarketMakerMetrics()
    
    def run_backtest(self):
        """Run market making backtest."""
        for timestamp, price, volume in self.data:
            # Simulate market conditions
            self.market_maker.update_market_conditions(price, volume)
            
            # Generate and simulate orders
            orders = self.market_maker.generate_orders()
            simulated_fills = self.simulate_fills(orders, price)
            
            # Record results
            for fill in simulated_fills:
                self.metrics.record_fill(fill.order, fill.price, fill.amount)
        
        return self.metrics.get_performance_stats()
```

### Unit Testing
```python
def test_order_book_processing():
    """Test order book parsing and management."""
    order_book = OrderBook("BTC", "lp_123")
    
    # Mock RPC response
    mock_data = {
        "limit_orders": {
            "bids": [{"id": 1, "tick": 1000, "sell_amount": "1000000"}],
            "asks": [{"id": 2, "tick": 1001, "sell_amount": "1000000"}]
        }
    }
    
    order_book.process_order_book(mock_data, "block_123", 12345, 
                                  lambda t, a: t, lambda h, a: int(h))
    
    assert len(order_book.bids) == 1
    assert len(order_book.asks) == 1
    assert order_book.top_bid.price == 1000
```

## Best Practices

### Order Management
- Regularly refresh orders to maintain market presence
- Use limit orders instead of market orders for better price control
- Implement order staggering to avoid concentrated fills
- Monitor order queue position and adjust pricing accordingly

### Risk Control
- Implement hard position limits
- Use volatility-based spread adjustment
- Monitor inventory skew and rebalance when needed
- Implement circuit breakers for adverse conditions

### Performance Optimization
- Batch order operations where possible
- Use efficient data structures for order tracking
- Minimize API calls through caching
- Profile and optimize critical paths

### Monitoring
- Track key metrics: spread capture, inventory efficiency, P&L
- Set up alerts for unusual conditions
- Log all order operations for audit trail
- Regular performance reviews and strategy adjustments</content>
<parameter name="filePath">c:\Users\yinki\Coding Work\elysian\elysian_core\market_maker\documentation.md