import argparse
import asyncio
import math
import statistics
from abc import ABC, abstractmethod
from collections import deque
from pathlib import Path
from typing import List, Optional, Dict, Any
import collections
import datetime
import pandas as pd
from elysian_core.core.enums import Side, OrderStatus, Venue
from elysian_core.core.order import Order
from elysian_core.core.market_data import OrderBook
import elysian_core.utils.logger as log

logger = log.setup_custom_logger("root")


class AbstractDataFeed(ABC):
    """
    Base class for all exchange order-book feeds.

    Subclasses only need to implement:
      - create_new()       set _name / _interval
      - __call__()         open the WebSocket and drive process_event_to_data()

    Everything else — order-book delta application, slippage pricing,
    rolling volatility, periodic CSV logging — lives here once.
    """

    def __init__(self, save_data: bool = False, file_dir: Optional[str] = None):
        self._name: Optional[str] = None
        self._interval: Optional[str] = None
        self._data: Optional[OrderBook] = None
        self._vol: Optional[float] = None
        self._historical: deque = deque([], maxlen=60)       # mid prices, 1/s
        self._historical_ob: deque = deque([], maxlen=60)    # OrderBook snapshots, 1/s
        self._full_df: pd.DataFrame = pd.DataFrame()
        self.save_data: bool = save_data
        self.file_dir: Optional[str] = file_dir
        
        self.fetched_initial_snapshot = False

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def name(self) -> Optional[str]:
        return self._name

    @property
    def interval(self) -> Optional[str]:
        return self._interval

    @property
    def data(self) -> Optional[OrderBook]:
        return self._data

    @property
    def volatility(self) -> Optional[float]:
        return self._vol

    @property
    def latest_price(self) -> float:
        return self._historical[-1]

    @property
    def latest_bid_price(self) -> float:
        return self._historical_ob[-1].best_bid_price

    @property
    def latest_ask_price(self) -> float:
        return self._historical_ob[-1].best_ask_price

    # ── Slippage pricing ──────────────────────────────────────────────────────
    def executed_buy_price(self, amount: float, amount_in_base: bool = True) -> float:
        """VWAP ask price for a buy of `amount` (base or quote)."""
        ob = self._data
        ask_orders = ob.ask_orders
        total_cost = 0.0

        if amount_in_base:
            remaining = amount
            for price, qty in ask_orders:
                if remaining <= qty:
                    total_cost += remaining * price
                    return total_cost / amount
                total_cost += qty * price
                remaining -= qty
        else:
            remaining_cost = amount
            total_base = 0.0
            for price, qty in ask_orders:
                available = qty * price
                if remaining_cost <= available:
                    total_base += remaining_cost / price
                    return amount / total_base
                total_cost += available
                total_base += qty
                remaining_cost -= available

        return ask_orders[-1][0]

    def executed_sell_price(self, amount: float, amount_in_base: bool = True) -> float:
        """VWAP bid price for a sell of `amount` (base or quote)."""
        ob = self._data
        bid_orders = ob.bid_orders
        total_proceeds = 0.0

        if amount_in_base:
            remaining = amount
            for price, qty in bid_orders:
                if remaining <= qty:
                    total_proceeds += remaining * price
                    return total_proceeds / amount
                total_proceeds += qty * price
                remaining -= qty
        else:
            remaining_cost = amount
            total_base = 0.0
            for price, qty in bid_orders:
                available = qty * price
                if remaining_cost <= available:
                    total_base += remaining_cost / price
                    return amount / total_base
                total_proceeds += available
                total_base += qty
                remaining_cost -= available

        return bid_orders[-1][0]

    # ── Order-book delta application ──────────────────────────────────────────

    @staticmethod
    def apply_orderbook_delta(
        orders: List[List[float]],
        deltas: list,
        descending: bool = True,
    ) -> List[List[float]]:
        """
        Apply a list of price-level deltas to an order-book side.

        Handles both 2-element [price, qty] deltas (Binance/Bybit)
        and 4-element [price, qty, _, _] deltas (OKX).
        """
        for delta in deltas:
            price = float(delta[0])
            qty = float(delta[1])
            if qty == 0:
                orders = [o for o in orders if float(o[0]) != price]
            else:
                for i, o in enumerate(orders):
                    if float(o[0]) == price:
                        orders[i] = [price, qty]
                        break
                else:
                    orders.append([price, qty])

        return sorted(orders, key=lambda x: -float(x[0]) if descending else float(x[0]))

    # ── Background stats loop ─────────────────────────────────────────────────

    async def update_current_stats(self):
        """
        Runs every second: append mid-price to rolling window and compute
        60-second realised volatility.
        """
        while True:
            try:
                if self._data:
                    mid = self._data.mid_price
                    self._historical.append(mid)
                    self._historical_ob.append(self._data)

                    if len(self._historical) == 60:
                        returns = [
                            (self._historical[i] - self._historical[i - 1]) / self._historical[i - 1]
                            for i in range(1, len(self._historical))
                        ]
                        self._vol = statistics.stdev(returns) * math.sqrt(0.8)

                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"[{self._name}] update_current_stats error: {e}")
                break

    # ── Periodic CSV logging ──────────────────────────────────────────────────

    async def periodically_log_data(self, interval_secs: int = 60):
        while True:
            try:
                if not self._full_df.empty and self.file_dir:
                    snapshot = self._full_df.copy()
                    path = Path(self.file_dir) / f"{self._name}_ob.csv"
                    snapshot.to_csv(path)
                    logger.info(f"[{self._name}] OB snapshot saved to {path}")
                await asyncio.sleep(interval_secs)
            except Exception as e:
                logger.error(f"[{self._name}] periodically_log_data error: {e}")
                break

    async def _append_to_df(self, ob: OrderBook):
        row = ob.to_dataframe_row()
        self._full_df = pd.concat([self._full_df, row], ignore_index=True)

    # ── Abstract interface ────────────────────────────────────────────────────

    @abstractmethod
    def create_new(self, asset: str, interval: str = "1s"):
        """Configure the feed for a specific asset and interval."""
        ...

    @abstractmethod
    async def __call__(self):
        """Open the connection and run the feed until cancelled."""
        ...

    def __str__(self) -> str:
        return f"{self.__class__.__name__}({self._name})"




# ──────────────────────────────────────────────────────────────────────────────
# Feed specialisations
# ──────────────────────────────────────────────────────────────────────────────


class AbstractOrderBookFeed(AbstractDataFeed):
    """Marker subclass for order-book specific behaviour.

    Exists mainly for typing/clarity – all of the logic remains in
    :class:`AbstractDataFeed` for the time being.  Concrete implementations
    should subclass this rather than the raw :class:`AbstractDataFeed`.
    """


class AbstractKlineFeed(ABC):
    """Base class for kline/candle feeds.

    Provides the rolling history/volatility functionality that parallels the
    order-book feed but operates on closed kline objects instead of raw
    book snapshots.  Implementations must manage a ``_kline`` attribute and
    provide a ``_process_kline_event`` helper; they may also reuse parts of
    the :class:`AbstractDataFeed` logic if desired.
    """

    def __init__(self, save_data: bool = False, file_dir: Optional[str] = None):
        self._name: Optional[str] = None
        self._interval: Optional[str] = None
        self._kline: Optional[Any] = None
        self._vol: Optional[float] = None
        self._historical: deque = deque([], maxlen=60)       # close prices, 1/interval
        self._full_df: pd.DataFrame = pd.DataFrame()
        self.save_data: bool = save_data
        self.file_dir: Optional[str] = file_dir

    @property
    def name(self) -> Optional[str]:
        return self._name

    @property
    def interval(self) -> Optional[str]:
        return self._interval

    @property
    def volatility(self) -> Optional[float]:
        return self._vol

    @property
    def latest_close(self) -> Optional[float]:
        return self._historical[-1] if self._historical else None

    @abstractmethod
    def create_new(self, asset: str, interval: str = "1m"):
        ...

    @abstractmethod
    async def __call__(self):
        ...


# ──────────────────────────────────────────────────────────────────────────────
# Client managers
# ──────────────────────────────────────────────────────────────────────────────


class AbstractClientManager(ABC):
    """Generic interface for a multiplexing client manager.

    Concrete subclasses orchestrate a shared connection and distribute
    messages to registered feed instances.  ``register_feed``/
    ``unregister_feed`` operate on a mapping from symbol to feed object.
    """

    def __init__(self):
        self._running: bool = False
        self._active_feeds: Dict[str, Any] = {}
        self._queue: asyncio.Queue = asyncio.Queue()
        self._reader_task: Optional[asyncio.Task] = None
        self._worker_tasks: List[asyncio.Task] = []
        self._event_bus = None  # Optional EventBus — set via set_event_bus()

    def set_event_bus(self, event_bus):
        """Inject an EventBus for pushing typed events to strategy hooks."""
        self._event_bus = event_bus

    @abstractmethod
    async def start(self):
        """Bring the manager online (open socket, etc.)."""
        ...

    @abstractmethod
    def register_feed(self, feed: Any):
        ...

    @abstractmethod
    def unregister_feed(self, symbol: str):
        ...

    @abstractmethod
    async def run_multiplex_feeds(self):
        """Reader/worker pool entry point."""
        ...


class OrderBookClientManager(AbstractClientManager):
    """Specialised manager for order-book feeds."""
    pass


class KlineClientManager(AbstractClientManager):
    """Specialised manager for kline feeds."""
    pass




# ──────────────────────────────────────────────────────────────────────────────
# Connector base classes
# ──────────────────────────────────────────────────────────────────────────────
class SpotExchangeConnector(ABC):
    """Abstract base for spot-exchange connectors.

    Concrete implementations (e.g. BinanceSpotExchange, AsterSpotExchange)
    should derive from this class.  It defines a common interface for
    producing order-book and kline feeds and performing any shared setup
    such as authentication, URL configuration, etc.

    The methods here mirror the *feed* operations rather than the
    higher‑level account/market operations that live in the exchange
    classes themselves.
    """

    def __init__(self, args: argparse.Namespace,
                       api_key: str,
                       api_secret: str,
                       symbols: List[str],
                       file_path: Optional[str] = None,
                       kline_manager: Optional[KlineClientManager]= None,
                       ob_manager: Optional[OrderBookClientManager] = None):
        
        # Initit 
        self._api_key = api_key
        self._api_secret = api_secret
        self._symbols = symbols
        self._file_path = file_path
        self.cfg = args

        # Per-symbol feeds
        self.kline_manager = kline_manager
        self.ob_manager = ob_manager
        
        # Account state
        self._balances: Dict[str, float] = {}
        self._open_orders: Dict[str, collections.OrderedDict[str, Order]] = collections.defaultdict(collections.OrderedDict)
        self._token_infos: Dict[str, dict] = {}
        self._utc8 = datetime.timezone(datetime.timedelta(hours=8))
                
        
    # ── Feed accessors ────────────────────────────────────────────────────────
    def kline_feed(self, symbol: str):
        return self.kline_manager.get_feed(symbol)

    def ob_feed(self, symbol: str):
        return self.ob_manager.get_feed(symbol)


    def last_price(self, symbol: str) -> Optional[float]:
        """Best available mid-price: OB mid → last kline close → None."""
        ob = self.ob_feed(symbol)
        if ob and ob.data:
            return ob.data.mid_price
        
        kf = self.kline_feed(symbol)
        if kf and kf.latest_close:
            return kf.latest_close
        return None
    
    
    # ── Initialisation ────────────────────────────────────────────────────────
    @abstractmethod
    async def initialize(self):
        """Perform any necessary setup (e.g. authentication, token info fetch)."""
        ...
    
    @abstractmethod
    async def _fetch_symbol_info(self, symbol: str) -> dict:
        """Fetch and return exchange-specific info for the given symbol."""
        ...
        

    
    # ── Account & Balances ───────────────────────────────────────────────────────────────
    @abstractmethod
    async def refresh_balances(self) -> Dict[str, float]:
        """Fetch and return current account balances."""
        ...
        

    async def monitor_balances(self, interval_secs: int = 5, name: str = ""):
        """Background task to periodically refresh balances."""
        while True:
            try:
                await self.refresh_balances()
                await asyncio.sleep(interval_secs)
            except Exception as e:
                logger.error(f"{name} Balance monitoring error: {e}")
                break
            
    def get_balance(self, asset: str) -> float:
        '''
        Fetch the current balance for a specific asset. Returns 0.0 if the asset is not found.
        '''
        return self._balances.get(asset, 0.0)
    
    
    
    # -------------- Depositing and Withdrawing -------------------------------------------# 
    @abstractmethod
    async def get_deposit_address(self, coin: str, network: Optional[str] = None) -> Optional[str]:
        """Fetch the deposit address for a specific coin and network."""
        ...
        
        
    @abstractmethod 
    async def deposit_asset(self, coin: str, amount: float, network: Optional[str] = None) -> bool:
        """Initiate a deposit of the specified amount of coin to the exchange."""
        ...
        
    
    @abstractmethod
    async def withdraw_asset(self, coin: str, amount: float, address: str, network: Optional[str] = None) -> bool:
        """Initiate a withdrawal of the specified amount of coin from the exchange to the given address."""
        ...
        
    # ---------------- Orders ---------------------------------------------------------------# 
    @abstractmethod
    async def place_limit_order(self, symbol: str, side: Side, price: float, quantity: float, strategy_id: int):
        """Place a limit order adds order into the self._open_orders"""
        ...
        
    @abstractmethod
    async def place_market_order(self, symbol: str, side: Side, quantity: float, use_quote_order_qty: bool = False):
        """Place a market order adds order into the self._open_orders"""
        ...
        
        
    @abstractmethod
    async def cancel_order(self, symbol: str, order_id: str):
        """Cancel an existing order"""
        ...
        
    @abstractmethod
    async def get_open_orders(self, symbol: str):
        """Fetch and return a list of open orders for the given symbol. Updates self._open_orders[symbol]"""
        ...
    
    async def get_all_open_orders(self):
        """Fetch and return a dict of all open orders for all tracked symbols."""
        for symbol in self._symbols:
            await self.get_open_orders(symbol)
    
    
    async def monitor_open_orders(self, poll_interval: int = 60*5):
        while True:
            await self.get_all_open_orders()
            await asyncio.sleep(poll_interval)
            
            
    async def print_snapshot(self, poll_interval: int = 60 * 2, balance_filter: float = 1e-5):
        """
        Prints out account balances and all open order snapshots
        """
        while True:
            try:
                # ── Filter and format balances ────────────────────────────────
                filtered = {
                    asset: bal
                    for asset, bal in self._balances.items()
                    if bal >= balance_filter
                }

                if filtered:
                    col_w = max(len(a) for a in filtered) + 2
                    val_w = max(len(f"{v:.8f}") for v in filtered.values()) + 2
                    divider  = f"║  ├{'─' * col_w}┬{'─' * val_w}┤"
                    top_rule = f"║  ┌{'─' * col_w}┬{'─' * val_w}┐"
                    bot_rule = f"║  └{'─' * col_w}┴{'─' * val_w}┘"
                    header   = f"║  │{'ASSET'.center(col_w)}│{'BALANCE'.center(val_w)}│"

                    balance_lines = [
                        top_rule,
                        header,
                        divider,
                        *[
                            f"║  │ {asset:<{col_w - 1}}│ {bal:<{val_w - 1}.8f}│"
                            for asset, bal in sorted(filtered.items())
                        ],
                        bot_rule,
                        f"║  ({len(self._balances) - len(filtered)} asset(s) below 1e-5 hidden)",
                    ]
                else:
                    balance_lines = ["║  (no balances above 1e-5)"]

                output_str = ""
                output_str += "╔══════════════════════════════════════════════════╗\n"
                output_str += "║              ACCOUNT SNAPSHOT                    ║\n"
                output_str += "╠══════════════════════════════════════════════════╣\n"
                output_str += "║  BALANCES\n"
                for line in balance_lines:
                    output_str += f"{line}\n"
                output_str += "╠══════════════════════════════════════════════════╣\n"

                counter = 0
                for symbol, orders in self._open_orders.items():
                    output_str += f"║  ── {symbol} ({len(orders)} order(s))\n"
                    for _, (order_id, order) in enumerate(orders.items(), start=1):
                        for line in str(order).splitlines():
                            output_str += f"║    {line}\n"
                        output_str += "║\n"
                        counter += 1

                output_str += "╠══════════════════════════════════════════════════╣\n"
                output_str += f"║  Total Open Orders : {counter}\n"
                output_str += "╚══════════════════════════════════════════════════╝"
                
                #print(output_str)
                await asyncio.sleep(poll_interval)
            except Exception as e:
                logger.error(f"Snapshot monitoring error: {e}")
                break
        
    async def cancel_all_orders(self, symbol: str):
        """Cancel all open orders for a given symbol."""
        orders = list(self._open_orders.get(symbol, {}).keys())
        await asyncio.gather(*(self.cancel_order(symbol, oid) for oid in orders))
                


class FuturesExchangeConnector(ABC):
    """Abstract base for futures/perpetual-exchange connectors.

    Parallel to :class:`SpotExchangeConnector` but tailored for derivatives:
      - Positions (long/short) with entry price and unrealised PnL
      - Per-symbol leverage management
      - Margin type (ISOLATED / CROSSED)
      - Futures-specific order types (stop-market, take-profit, etc.)

    Concrete implementations (e.g. BinanceFuturesExchange) should derive
    from this class.
    """

    def __init__(self, args: argparse.Namespace,
                       api_key: str,
                       api_secret: str,
                       symbols: List[str],
                       file_path: Optional[str] = None,
                       kline_manager: Optional[KlineClientManager] = None,
                       ob_manager: Optional[OrderBookClientManager] = None,
                       default_leverage: int = 1):

        # Common state (mirrors SpotExchangeConnector)
        self._api_key = api_key
        self._api_secret = api_secret
        self._symbols = symbols
        self._file_path = file_path
        self.cfg = args

        # Per-symbol feeds
        self.kline_manager = kline_manager
        self.ob_manager = ob_manager

        # Account state
        self._balances: Dict[str, float] = {}
        self._open_orders: Dict[str, collections.OrderedDict[str, Order]] = collections.defaultdict(collections.OrderedDict)
        self._token_infos: Dict[str, dict] = {}
        self._utc8 = datetime.timezone(datetime.timedelta(hours=8))

        # Futures-specific state
        self._positions: Dict[str, dict] = {}
        self._leverages: Dict[str, int] = {}
        self._default_leverage: int = default_leverage
        self._margin_types: Dict[str, str] = {}   # symbol -> "ISOLATED" | "CROSSED"

    # ── Feed accessors ────────────────────────────────────────────────────────
    def kline_feed(self, symbol: str):
        return self.kline_manager.get_feed(symbol)

    def ob_feed(self, symbol: str):
        return self.ob_manager.get_feed(symbol)

    def last_price(self, symbol: str) -> Optional[float]:
        """Best available mid-price: OB mid → last kline close → None."""
        ob = self.ob_feed(symbol)
        if ob and ob.data:
            return ob.data.mid_price
        kf = self.kline_feed(symbol)
        if kf and kf.latest_close:
            return kf.latest_close
        return None

    # ── Initialisation ────────────────────────────────────────────────────────
    @abstractmethod
    async def initialize(self):
        """Perform any necessary setup (e.g. authentication, token info fetch)."""
        ...

    @abstractmethod
    async def _fetch_symbol_info(self, symbol: str) -> dict:
        """Fetch and return exchange-specific info for the given symbol."""
        ...

    # ── Account & Balances ────────────────────────────────────────────────────
    @abstractmethod
    async def refresh_balances(self) -> Dict[str, float]:
        """Fetch and return current account balances."""
        ...

    async def monitor_balances(self, interval_secs: int = 5, name: str = ""):
        """Background task to periodically refresh balances."""
        while True:
            try:
                await self.refresh_balances()
                await asyncio.sleep(interval_secs)
            except Exception as e:
                logger.error(f"{name} Balance monitoring error: {e}")
                break

    def get_balance(self, asset: str) -> float:
        return self._balances.get(asset, 0.0)

    # ── Positions (futures-specific) ──────────────────────────────────────────
    @abstractmethod
    async def refresh_positions(self):
        """Fetch and update current positions from the exchange."""
        ...

    def get_position(self, symbol: str) -> Optional[dict]:
        return self._positions.get(symbol)

    # ── Leverage & Margin (futures-specific) ──────────────────────────────────
    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: int):
        """Set leverage for a symbol."""
        ...

    @abstractmethod
    async def set_margin_type(self, symbol: str, margin_type: str):
        """Set margin type (ISOLATED or CROSSED) for a symbol."""
        ...

    def get_leverage(self, symbol: str) -> int:
        return self._leverages.get(symbol, self._default_leverage)

    # ── Orders ────────────────────────────────────────────────────────────────
    @abstractmethod
    async def place_limit_order(self, symbol: str, side: Side, price: float, quantity: float, reduce_only: bool = False):
        """Place a limit order. Adds order into self._open_orders."""
        ...

    @abstractmethod
    async def place_market_order(self, symbol: str, side: Side, quantity: float, reduce_only: bool = False):
        """Place a market order. Adds order into self._open_orders."""
        ...

    @abstractmethod
    async def place_stop_order(self, symbol: str, side: Side, quantity: float, stop_price: float, reduce_only: bool = True):
        """Place a stop-market order."""
        ...

    @abstractmethod
    async def cancel_order(self, symbol: str, order_id: str):
        """Cancel an existing order."""
        ...

    @abstractmethod
    async def get_open_orders(self, symbol: str):
        """Fetch and return open orders for the given symbol. Updates self._open_orders[symbol]."""
        ...

    async def get_all_open_orders(self):
        """Fetch and return a dict of all open orders for all tracked symbols."""
        for symbol in self._symbols:
            await self.get_open_orders(symbol)

    async def monitor_open_orders(self, poll_interval: int = 60 * 5):
        while True:
            await self.get_all_open_orders()
            await asyncio.sleep(poll_interval)

    async def cancel_all_orders(self, symbol: str):
        """Cancel all open orders for a given symbol."""
        orders = list(self._open_orders.get(symbol, {}).values())
        await asyncio.gather(*(self.cancel_order(symbol, order.id) for order in orders))

    # ── Position management (futures-specific) ────────────────────────────────
    @abstractmethod
    async def close_position(self, symbol: str):
        """Close entire position for a symbol."""
        pass

    # ── Snapshot printing ─────────────────────────────────────────────────────
    async def print_snapshot(self, poll_interval: int = 60 * 2, balance_filter: float = 1e-5):
        """Prints out account balances, positions, and all open order snapshots."""
        while True:
            try:
                # ── Filter and format balances ────────────────────────────────
                filtered = {
                    asset: bal
                    for asset, bal in self._balances.items()
                    if bal >= balance_filter
                }

                if filtered:
                    col_w = max(len(a) for a in filtered) + 2
                    val_w = max(len(f"{v:.8f}") for v in filtered.values()) + 2
                    divider  = f"║  ├{'─' * col_w}┬{'─' * val_w}┤"
                    top_rule = f"║  ┌{'─' * col_w}┬{'─' * val_w}┐"
                    bot_rule = f"║  └{'─' * col_w}┴{'─' * val_w}┘"
                    header   = f"║  │{'ASSET'.center(col_w)}│{'BALANCE'.center(val_w)}│"

                    balance_lines = [
                        top_rule, header, divider,
                        *[
                            f"║  │ {asset:<{col_w - 1}}│ {bal:<{val_w - 1}.8f}│"
                            for asset, bal in sorted(filtered.items())
                        ],
                        bot_rule,
                        f"║  ({len(self._balances) - len(filtered)} asset(s) below 1e-5 hidden)",
                    ]
                else:
                    balance_lines = ["║  (no balances above 1e-5)"]

                output_str = ""
                output_str += "╔══════════════════════════════════════════════════╗\n"
                output_str += "║           FUTURES ACCOUNT SNAPSHOT                ║\n"
                output_str += "╠══════════════════════════════════════════════════╣\n"
                output_str += "║  BALANCES\n"
                for line in balance_lines:
                    output_str += f"{line}\n"

                # ── Positions ─────────────────────────────────────────────────
                output_str += "╠══════════════════════════════════════════════════╣\n"
                output_str += "║  POSITIONS\n"
                if self._positions:
                    for sym, pos in self._positions.items():
                        amt = pos.get("amount", 0)
                        entry = pos.get("entry_price", 0)
                        pnl = pos.get("unrealized_pnl", 0)
                        lev = pos.get("leverage", self._default_leverage)
                        direction = "LONG" if amt > 0 else "SHORT"
                        output_str += f"║  {sym:<12} {direction:<6} amt={abs(amt):<12} entry={entry:<12.6f} uPnL={pnl:<12.4f} lev={lev}x\n"
                else:
                    output_str += "║  (no open positions)\n"

                # ── Open orders ───────────────────────────────────────────────
                output_str += "╠══════════════════════════════════════════════════╣\n"
                counter = 0
                for symbol, orders in self._open_orders.items():
                    output_str += f"║  ── {symbol} ({len(orders)} order(s))\n"
                    for _, (order_id, order) in enumerate(orders.items(), start=1):
                        for line in str(order).splitlines():
                            output_str += f"║    {line}\n"
                        output_str += "║\n"
                        counter += 1

                output_str += "╠══════════════════════════════════════════════════╣\n"
                output_str += f"║  Total Open Orders : {counter}\n"
                output_str += "╚══════════════════════════════════════════════════╝"

                print(output_str)
                await asyncio.sleep(poll_interval)
            except Exception as e:
                logger.error(f"Snapshot monitoring error: {e}")
                break