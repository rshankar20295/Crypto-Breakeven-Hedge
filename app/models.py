from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


def dec(value: Any, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    return Decimal(str(value))


@dataclass(frozen=True)
class OptionQuote:
    symbol: str
    product_id: int
    contract_type: str
    strike: Decimal
    delta: Decimal
    best_bid: Decimal
    best_ask: Decimal
    mark_price: Decimal
    spot_price: Decimal
    contract_value: Decimal

    @property
    def sell_price(self) -> Decimal:
        return self.best_bid if self.best_bid > 0 else self.mark_price

    @property
    def buy_price(self) -> Decimal:
        return self.best_ask if self.best_ask > 0 else self.mark_price


@dataclass
class CycleState:
    cycle_id: str
    status: str
    underlying: str
    expiry_date: str
    quantity: int
    call_symbol: str
    call_product_id: int
    call_strike: Decimal
    put_symbol: str
    put_product_id: int
    put_strike: Decimal
    premium_per_lot: Decimal
    premium_multiplier: Decimal
    target_max_profit: Decimal
    actual_max_profit: Decimal
    upper_breakeven: Decimal
    lower_breakeven: Decimal
    entry_time: str
    expiry_cutoff_time: str
    call_entry_client_oid: str
    put_entry_client_oid: str
    upside_hedged: bool = False
    downside_hedged: bool = False
    upside_hedge_symbol: str = ""
    upside_hedge_client_oid: str = ""
    downside_hedge_symbol: str = ""
    downside_hedge_client_oid: str = ""

