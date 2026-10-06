from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from app.config import Settings
from app.models import CycleState, OptionQuote
from app.state_store import StateStore
from app.strategy import BreakevenHedgeBot


class FakeClient:
    def __init__(self) -> None:
        self.orders: dict[str, dict] = {}
        self.placed: list[tuple[int, str, int, str, str]] = []
        self.fail_hedge = True

    def get_order_by_client_oid(self, oid: str) -> dict | None:
        return self.orders.get(oid)

    def place_order(self, product_id: int, side: str, size: int, client_order_id: str, order_type: str) -> dict:
        self.placed.append((product_id, side, size, client_order_id, order_type))
        if client_order_id.startswith("HU") and self.fail_hedge:
            raise RuntimeError("insufficient_margin")
        order = {
            "product_id": product_id,
            "size": size,
            "unfilled_size": 0,
            "side": side,
            "client_order_id": client_order_id,
            "state": "closed",
        }
        self.orders[client_order_id] = order
        return order

    def wait_for_fill(self, client_order_id: str, expected_size: int):
        return True, expected_size, Decimal("1")

    def get_option_chain(self, underlying: str, expiry_date: str):
        return [
            OptionQuote("C-BTC-120-061026", 120, "call_options", Decimal("120"), Decimal("0.5"), Decimal("1"), Decimal("2"), Decimal("1.5"), Decimal("120"), Decimal("1")),
            OptionQuote("P-BTC-80-061026", 80, "put_options", Decimal("80"), Decimal("-0.5"), Decimal("1"), Decimal("2"), Decimal("1.5"), Decimal("120"), Decimal("1")),
        ]

    def get_ticker(self, symbol: str):
        return {"quotes": {"best_ask": "1", "best_bid": "1"}, "mark_price": "1"}

    def get_spot_price(self, symbol: str):
        return Decimal("120")


def settings(path: str) -> Settings:
    return Settings(
        dry_run=False,
        base_url="https://example.test",
        public_ws_url="wss://example.test",
        private_ws_url="wss://example.test",
        api_key="key",
        api_secret="secret",
        underlying="BTC",
        quote_currency="USD",
        entry_time="09:30",
        timezone="Asia/Kolkata",
        expiry_rank=1,
        expiry_cutoff_time="17:30",
        call_delta=Decimal("0.1"),
        put_delta=Decimal("-0.1"),
        target_max_profit_usd=Decimal("100"),
        quantity_step=1,
        min_quantity=1,
        use_contract_value_multiplier=True,
        poll_interval_seconds=0.01,
        order_confirm_timeout_seconds=1,
        order_retry_seconds=0,
        entry_order_type="market_order",
        hedge_order_type="market_order",
        state_db_path=path,
        log_level="INFO",
    )


class StrategyTests(unittest.TestCase):
    def test_insufficient_margin_hedge_closes_existing_legs_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "state.sqlite3")
            store = StateStore(db_path)
            cycle = CycleState(
                cycle_id="BTC-06-10-2026-E0930",
                status="active",
                underlying="BTC",
                expiry_date="06-10-2026",
                quantity=10,
                call_symbol="C-BTC-110-061026",
                call_product_id=110,
                call_strike=Decimal("110"),
                put_symbol="P-BTC-90-061026",
                put_product_id=90,
                put_strike=Decimal("90"),
                premium_per_lot=Decimal("10"),
                premium_multiplier=Decimal("1"),
                target_max_profit=Decimal("100"),
                actual_max_profit=Decimal("100"),
                upper_breakeven=Decimal("120"),
                lower_breakeven=Decimal("80"),
                entry_time="2026-10-06T09:30:00+05:30",
                expiry_cutoff_time="17:30",
                call_entry_client_oid="ECBTC06102026E0930",
                put_entry_client_oid="EPBTC06102026E0930",
            )
            store.save_cycle(cycle)
            fake = FakeClient()
            bot = BreakevenHedgeBot(settings(db_path), fake, store)

            bot._hedge(cycle, side="upside")
            bot._emergency_close_cycle(store.load_cycle(cycle.cycle_id), reason="second_call_should_not_duplicate")

            pushed_oids = [item[3] for item in fake.placed]
            self.assertEqual(pushed_oids.count("CCBTC06102026E0930"), 1)
            self.assertEqual(pushed_oids.count("CPBTC06102026E0930"), 1)
            self.assertEqual(store.load_cycle(cycle.cycle_id).status, "closed")


if __name__ == "__main__":
    unittest.main()

