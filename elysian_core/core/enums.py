from enum import Enum
from typing import Dict

# ── Order / Execution ─────────────────────────────────────────────────────────

class Side(Enum):
    BUY = "Buy"
    SELL = "Sell"

    def opposite(self) -> "Side":
        return Side.SELL if self == Side.BUY else Side.BUY


class AssetType(Enum):
    SPOT= "Spot"
    PERPETUAL = "Perpetuals"
    
    
class OrderType(Enum):
    LIMIT = "Limit"
    MARKET = "Market"
    RANGE = "Range"          # AMM / LP range order
    OTHERS = "OTHERS"
    


class OrderStatus(Enum):
    PENDING = "Pending"
    OPEN = "Open"
    PARTIALLY_FILLED = "PartiallyFilled"
    FILLED = "Filled"
    CANCELLED = "Cancelled"
    REJECTED = "Rejected"

    def is_active(self) -> bool:
        return self in (OrderStatus.PENDING, OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED)


    # Binance sends uppercase "BUY"/"SELL"; map to our Side enum.
_BINANCE_SIDE: Dict[str, Side] = {
        "BUY": Side.BUY,
        "SELL": Side.SELL,
    }

    # Binance order status strings → OrderStatus enum.
_BINANCE_STATUS: Dict[str, OrderStatus] = {
        "NEW": OrderStatus.OPEN,
        "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
        "FILLED": OrderStatus.FILLED,
        "CANCELED": OrderStatus.CANCELLED,
        "PENDING_CANCEL": OrderStatus.PENDING,
        "REJECTED": OrderStatus.REJECTED,
        "EXPIRED": OrderStatus.CANCELLED,
    }


# ── Venues / Chains ───────────────────────────────────────────────────────────

class Venue(Enum):
    BINANCE = "Binance"
    BYBIT = "Bybit"
    OKX = "OKX"
    CETUS = "Cetus Protocol"
    TURBOS = "Turbos"
    DEEPBOOK = "Deepbook"
    ASTER = "Aster"


class Chain(Enum):
    SUI = "sui"
    TON = "ton"
    ETHEREUM = "ethereum"
    BITCOIN = "bitcoin"
    SOLANA = "solana"

    def to_dict(self) -> dict:
        return {"chain": self.value}


# ── Trade Capture ─────────────────────────────────────────────────────────────

class TradeType(Enum):
    REAL = "Real"
    SIMULATED = "Simulated"


class TradeSubtype(Enum):
    SWAP = "Swap"


class CashflowSubType(Enum):
    SWAP_IN = "TransferIn"
    SWAP_OUT = "TransferOut"
    GAS_FEE = "GasFee"
    FEE_REBATE = "FeeRebate"


# ── DEX / On-chain ────────────────────────────────────────────────────────────

class SwapType(Enum):
    BLUEFIN = "Bluefin"
    CETUS = "Cetus"
    FLOW_X = "FlowX"
    TURBOS = "Turbos"


class WalletType(Enum):
    WALLET = "sui_wallet"
    CEX = "cex"


# ── Chainflip / LP Protocol (legacy) ──────────────────────────────────────────

class RangeOrderType(Enum):
    LIQUIDITY = 1
    ASSET = 2


class APICommands(Enum):
    Empty = 0
    Register = 1
    LiquidityDeposit = 2
    RegisterWithdrawalAddress = 3
    WithdrawAsset = 4
    AssetBalances = 5
    SetRangeOrderByLiquidity = 8
    SetRangeOrderByAmounts = 9
    UpdateLimitOrder = 10
    SetLimitOrder = 11
    GetOpenSwapChannels = 12
    CancelAllOrders = 13


class RPCCommands(Enum):
    Empty = 0
    AccountInfo = 1
    RequiredRatioForRangeOrder = 3
    PoolInfo = 4
    PoolDepth = 5
    PoolLiquidity = 6
    PoolOrders = 7
    PoolRangeOrdersLiquidityValue = 8
    PoolScheduledSwaps = 9
    AccountAssetBalances = 10


class StreamCommands(Enum):
    Empty = 0
    SubscribePoolPrice = 1
    SubscribePreWitnessedSwaps = 2


class NetworkStatus(Enum):
    STOPPED = 0
    NOT_CONNECTED = 1
    CONNECTED = 2
