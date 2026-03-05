import datetime
from dataclasses import dataclass
from typing import List, Optional

from elysian_core.core.enums import Chain, CashflowSubType, TradeSubtype, Venue


# ── Helpers ───────────────────────────────────────────────────────────────────

def isotimestamp(dt: datetime.datetime) -> str:
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def format_amount_hex(amount: int) -> str:
    """Encode a signed integer as a 256-bit two's-complement hex string."""
    if amount >= 0:
        return hex(amount)
    nbits = 256
    return hex((amount + (1 << nbits)) % (1 << nbits))


# ── On-chain primitives ───────────────────────────────────────────────────────

@dataclass
class Coin:
    symbol: str
    address: str
    decimals: int

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "address": self.address, "decimals": self.decimals}


@dataclass
class Pool:
    address: str
    venue: Venue

    def to_dict(self) -> dict:
        return {"address": self.address, "venue": self.venue.value}



