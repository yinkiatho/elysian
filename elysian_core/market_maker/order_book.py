import time
from typing import List, Optional

from elysian_core.core.enums import Side, RangeOrderType
from elysian_core.core.order import LimitOrder, RangeOrder
import elysian_core.utils.logger as log

logger = log.setup_custom_logger("root")


class OrderBook:
    """
    LP-side order book for a single base/quote asset pair.

    Parses pool order responses into typed LimitOrder and RangeOrder objects,
    separates the LP's own orders, and exposes top-of-book references.

    The RPC call and price/amount formatting are handled externally and passed
    in via process_order_book() — this class owns no network I/O.
    """

    def __init__(self, base_asset: str, lp_id: Optional[str] = None):
        self._base_asset = base_asset
        self._lp_id = lp_id
        self._bids: List[LimitOrder] = []
        self._asks: List[LimitOrder] = []
        self._range_orders: List[RangeOrder] = []
        self._lp_open_orders: List = []
        self._top_bid: Optional[LimitOrder] = None
        self._top_ask: Optional[LimitOrder] = None
        self._current_block_hash: Optional[str] = None
        self._current_block_number: Optional[int] = None

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def top_bid(self) -> Optional[LimitOrder]:
        return self._top_bid

    @property
    def top_ask(self) -> Optional[LimitOrder]:
        return self._top_ask

    @property
    def bids(self) -> List[LimitOrder]:
        return self._bids

    @property
    def asks(self) -> List[LimitOrder]:
        return self._asks

    @property
    def range_orders(self) -> List[RangeOrder]:
        return self._range_orders

    @property
    def lp_open_orders(self) -> list:
        return self._lp_open_orders

    # ── Processing ────────────────────────────────────────────────────────────

    def process_order_book(
        self,
        data: dict,
        current_block_hash: str,
        current_block_number: int,
        tick_to_price,       # callable: (tick, base_asset) -> float
        hex_to_decimal,      # callable: (hex_amount, asset) -> float
    ):
        """
        Parse a raw pool orders response into typed order objects.

        tick_to_price and hex_to_decimal are injected so this class stays
        independent of any specific RPC formatting library.
        """
        if current_block_hash == self._current_block_hash:
            return  # No new block — nothing to update

        self._current_block_hash = current_block_hash
        self._current_block_number = current_block_number
        self._lp_open_orders = []
        ts = int(time.time())

        result = data.get("result", data)
        bids, asks = [], []

        for bid in result["limit_orders"]["bids"]:
            price = tick_to_price(bid["tick"], self._base_asset)
            amount = hex_to_decimal(bid["sell_amount"], "USDC") / price
            if amount == 0:
                continue
            order = LimitOrder(
                id=bid["id"],
                base_asset=self._base_asset,
                quote_asset="USDC",
                side=Side.BUY,
                price=price,
                amount=amount,
                lp_account=bid["lp"],
                timestamp=ts,
            )
            bids.append(order)
            if order.lp_account == self._lp_id:
                self._lp_open_orders.append(order)

        for ask in result["limit_orders"]["asks"]:
            price = tick_to_price(ask["tick"], self._base_asset)
            amount = hex_to_decimal(ask["sell_amount"], self._base_asset)
            if amount == 0:
                continue
            
            
            order = LimitOrder(
                id=ask["id"],
                base_asset=self._base_asset,
                quote_asset="USDC",
                side=Side.SELL,
                price=price,
                amount=amount,
                lp_account=ask["lp"],
                timestamp=ts,
            )
            asks.append(order)
            if order.lp_account == self._lp_id:
                self._lp_open_orders.append(order)

        for ro in result.get("range_orders", []):
            order = RangeOrder(
                id=ro["id"],
                base_asset=self._base_asset,
                quote_asset="USDC",
                lower_price=tick_to_price(ro["range"]["start"], self._base_asset),
                upper_price=tick_to_price(ro["range"]["end"], self._base_asset),
                order_type=RangeOrderType.LIQUIDITY,
                amount=ro["liquidity"],
                lp_account=ro["lp"],
                timestamp=ts,
            )
            self._range_orders.append(order)
            if order.lp_account == self._lp_id:
                self._lp_open_orders.append(order)

        self._bids = sorted(bids, key=lambda o: o.price, reverse=True)
        self._asks = sorted(asks, key=lambda o: o.price)
        self._top_bid = self._bids[0] if self._bids else None
        self._top_ask = self._asks[0] if self._asks else None

    def __str__(self) -> str:
        return (
            f"OrderBook({self._base_asset}/USDC | "
            f"top_bid={self._top_bid} | top_ask={self._top_ask})"
        )
