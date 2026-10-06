from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from app.config import Settings
from app.delta_client import DeltaClient
from app.market_data import LivePriceFeed
from app.models import CycleState, OptionQuote, dec
from app.state_store import StateStore

LOG = logging.getLogger(__name__)


class BreakevenHedgeBot:
    def __init__(self, settings: Settings, client: DeltaClient, store: StateStore, price_feed: LivePriceFeed | None = None):
        self.settings = settings
        self.client = client
        self.store = store
        self.price_feed = price_feed

    def run_forever(self) -> None:
        LOG.info("bot started dry_run=%s underlying=%s", self.settings.dry_run, self.settings.underlying_symbol)
        if self.price_feed:
            self.price_feed.start()
        while True:
            try:
                self.run_once()
            except Exception:
                LOG.exception("loop error")
            time.sleep(self.settings.poll_interval_seconds)

    def run_once(self) -> None:
        now = datetime.now(self.settings.tz)
        cycle = self.store.active_cycle(self.settings.underlying_symbol)

        if cycle and self._is_after_cutoff(now, cycle.expiry_date, cycle.expiry_cutoff_time):
            cycle.status = "settled"
            self.store.save_cycle(cycle)
            LOG.info("cycle %s marked settled at configured cutoff %s", cycle.cycle_id, cycle.expiry_cutoff_time)
            return

        if cycle and cycle.status in {"active", "hedging"}:
            self._monitor_cycle(cycle)
            return

        if self._is_entry_due(now):
            self._enter_new_cycle(now)
        else:
            LOG.info("waiting for entry time %s; now=%s", self.settings.entry_time, now.strftime("%H:%M:%S"))

    def _enter_new_cycle(self, now: datetime) -> None:
        expiry_date = self._select_expiry_date()
        cycle_id = f"{self.settings.underlying_symbol}-{expiry_date}-E{self.settings.entry_time.replace(':', '')}"
        existing = self.store.load_cycle(cycle_id)
        if existing and existing.status not in {"settled", "failed"}:
            LOG.info("cycle already exists and is not settled: %s", cycle_id)
            return

        chain = self.client.get_option_chain(self.settings.underlying_symbol, expiry_date)
        call = self._select_by_delta(chain, "call_options", self.settings.call_delta)
        put = self._select_by_delta(chain, "put_options", self.settings.put_delta)
        multiplier = self._premium_multiplier(call, put)
        premium_per_lot = (call.sell_price + put.sell_price) * multiplier
        quantity = self._quantity_for_target(premium_per_lot)
        estimated_profit = premium_per_lot * quantity

        call_oid = self._client_oid(cycle_id, "EC")
        put_oid = self._client_oid(cycle_id, "EP")
        LOG.info(
            "entry selected expiry=%s call=%s delta=%s bid=%s put=%s delta=%s bid=%s qty=%s target_profit=%s estimated_profit=%s",
            expiry_date,
            call.symbol,
            call.delta,
            call.sell_price,
            put.symbol,
            put.delta,
            put.sell_price,
            quantity,
            self.settings.target_max_profit_usd,
            estimated_profit,
        )

        call_order = self.client.place_order(call.product_id, "sell", quantity, call_oid, self.settings.entry_order_type)
        put_order = self.client.place_order(put.product_id, "sell", quantity, put_oid, self.settings.entry_order_type)
        self.store.record_order(cycle_id, "entry_call", call_oid, call_order)
        self.store.record_order(cycle_id, "entry_put", put_oid, put_order)

        call_filled, _, call_fill_price = self.client.wait_for_fill(call_oid, quantity)
        put_filled, _, put_fill_price = self.client.wait_for_fill(put_oid, quantity)
        if not call_filled or not put_filled:
            LOG.warning("entry not fully filled yet; will retry monitoring next loop")
            return

        if self.settings.dry_run:
            call_fill_price = call.sell_price
            put_fill_price = put.sell_price

        actual_premium_per_lot = (call_fill_price + put_fill_price) * multiplier
        upper_be = call.strike + actual_premium_per_lot
        lower_be = put.strike - actual_premium_per_lot
        cycle = CycleState(
            cycle_id=cycle_id,
            status="active",
            underlying=self.settings.underlying_symbol,
            expiry_date=expiry_date,
            quantity=quantity,
            call_symbol=call.symbol,
            call_product_id=call.product_id,
            call_strike=call.strike,
            put_symbol=put.symbol,
            put_product_id=put.product_id,
            put_strike=put.strike,
            premium_per_lot=actual_premium_per_lot,
            premium_multiplier=multiplier,
            target_max_profit=self.settings.target_max_profit_usd,
            actual_max_profit=actual_premium_per_lot * quantity,
            upper_breakeven=upper_be,
            lower_breakeven=lower_be,
            entry_time=now.isoformat(),
            expiry_cutoff_time=self.settings.expiry_cutoff_time,
            call_entry_client_oid=call_oid,
            put_entry_client_oid=put_oid,
        )
        self.store.save_cycle(cycle)
        LOG.info(
            "entry complete cycle=%s actual_max_profit=%s upper_be=%s lower_be=%s",
            cycle.cycle_id,
            cycle.actual_max_profit,
            cycle.upper_breakeven,
            cycle.lower_breakeven,
        )

    def _monitor_cycle(self, cycle: CycleState) -> None:
        spot = self._spot_price()
        up_left = self._percent_left(spot, cycle.upper_breakeven, upside=True)
        down_left = self._percent_left(spot, cycle.lower_breakeven, upside=False)
        pnl = self._estimate_current_pnl(cycle)
        LOG.info(
            "STATUS | cycle=%s | spot=%s | pnl_est=%s | upper_be=%s | up_left=%s%% | lower_be=%s | down_left=%s%% | "
            "qty=%s | max_profit=%s | hedge_up=%s | hedge_down=%s",
            cycle.cycle_id,
            spot,
            pnl,
            cycle.upper_breakeven,
            up_left,
            cycle.lower_breakeven,
            down_left,
            cycle.quantity,
            cycle.actual_max_profit,
            "done" if cycle.upside_hedged else "pending",
            "done" if cycle.downside_hedged else "pending",
        )

        if spot >= cycle.upper_breakeven and not cycle.upside_hedged:
            self._hedge(cycle, side="upside")
        if spot <= cycle.lower_breakeven and not cycle.downside_hedged:
            self._hedge(cycle, side="downside")

    def _hedge(self, cycle: CycleState, side: str) -> None:
        cycle.status = "hedging"
        self.store.save_cycle(cycle)
        chain = self.client.get_option_chain(cycle.underlying, cycle.expiry_date)
        if side == "upside":
            hedge = self._nearest_strike(chain, "call_options", cycle.upper_breakeven)
            oid = self._client_oid(cycle.cycle_id, "HU")
            role = "hedge_upside"
        else:
            hedge = self._nearest_strike(chain, "put_options", cycle.lower_breakeven)
            oid = self._client_oid(cycle.cycle_id, "HD")
            role = "hedge_downside"

        attempts = 0
        while True:
            attempts += 1
            order = self.client.get_order_by_client_oid(oid)
            if order:
                self.store.record_order(cycle.cycle_id, role, oid, order)
                size = int(order.get("size") or cycle.quantity)
                unfilled = int(order.get("unfilled_size") or 0)
                if size - unfilled >= cycle.quantity:
                    self._mark_hedged(cycle, side, hedge.symbol, oid)
                    return
            LOG.warning(
                "HEDGE_REQUIRED | side=%s | attempt=%s | action=buy | symbol=%s | qty=%s | nearest_strike=%s | client_oid=%s",
                side,
                attempts,
                hedge.symbol,
                cycle.quantity,
                hedge.strike,
                oid,
            )
            try:
                order = self.client.place_order(hedge.product_id, "buy", cycle.quantity, oid, self.settings.hedge_order_type)
                self.store.record_order(cycle.cycle_id, role, oid, order)
                filled, filled_size, _ = self.client.wait_for_fill(oid, cycle.quantity)
                if filled:
                    self._mark_hedged(cycle, side, hedge.symbol, oid)
                    return
                LOG.critical(
                    "HEDGE_NOT_FILLED | side=%s | filled=%s | required=%s | retry_in=%ss | position_is_unhedged=true",
                    side,
                    filled_size,
                    cycle.quantity,
                    self.settings.order_retry_seconds,
                )
            except Exception as exc:
                LOG.critical(
                    "HEDGE_ORDER_FAILED | side=%s | attempt=%s | symbol=%s | qty=%s | error=%s | retry_in=%ss | position_is_unhedged=true",
                    side,
                    attempts,
                    hedge.symbol,
                    cycle.quantity,
                    exc,
                    self.settings.order_retry_seconds,
                )
            time.sleep(self.settings.order_retry_seconds)

    def _mark_hedged(self, cycle: CycleState, side: str, symbol: str, oid: str) -> None:
        if side == "upside":
            cycle.upside_hedged = True
            cycle.upside_hedge_symbol = symbol
            cycle.upside_hedge_client_oid = oid
        else:
            cycle.downside_hedged = True
            cycle.downside_hedge_symbol = symbol
            cycle.downside_hedge_client_oid = oid
        cycle.status = "active"
        self.store.save_cycle(cycle)
        LOG.info("HEDGE_CONFIRMED | side=%s | symbol=%s | client_oid=%s", side, symbol, oid)

    def _select_expiry_date(self) -> str:
        products = self.client.get_products()
        expiries = sorted(
            {
                self._expiry_from_symbol(p["symbol"])
                for p in products
                if p.get("symbol", "").startswith(("C-", "P-"))
                and f"-{self.settings.underlying_symbol}-" in p.get("symbol", "")
            }
        )
        if len(expiries) < self.settings.expiry_rank:
            raise RuntimeError(f"Only found {len(expiries)} expiries for {self.settings.underlying_symbol}; need rank {self.settings.expiry_rank}.")
        return expiries[self.settings.expiry_rank - 1]

    @staticmethod
    def _expiry_from_symbol(symbol: str) -> str:
        raw = symbol.split("-")[-1]
        dt = datetime.strptime(raw, "%d%m%y")
        return dt.strftime("%d-%m-%Y")

    @staticmethod
    def _select_by_delta(chain: list[OptionQuote], contract_type: str, target: Decimal) -> OptionQuote:
        options = [q for q in chain if q.contract_type == contract_type]
        if not options:
            raise RuntimeError(f"No {contract_type} options found in selected chain.")
        return min(options, key=lambda q: abs(q.delta - target))

    @staticmethod
    def _nearest_strike(chain: list[OptionQuote], contract_type: str, target: Decimal) -> OptionQuote:
        options = [q for q in chain if q.contract_type == contract_type]
        if not options:
            raise RuntimeError(f"No {contract_type} options found for hedge.")
        return min(options, key=lambda q: (abs(q.strike - target), q.strike))

    def _premium_multiplier(self, call: OptionQuote, put: OptionQuote) -> Decimal:
        if not self.settings.use_contract_value_multiplier:
            return Decimal("1")
        values = [v for v in [call.contract_value, put.contract_value] if v > 0]
        return min(values) if values else Decimal("1")

    def _quantity_for_target(self, premium_per_lot: Decimal) -> int:
        if premium_per_lot <= 0:
            raise RuntimeError("Premium per lot is zero; cannot size position.")
        raw = self.settings.target_max_profit_usd / premium_per_lot
        step = self.settings.quantity_step
        floor_qty = max(self.settings.min_quantity, int(math.floor(float(raw) / step) * step))
        ceil_qty = max(self.settings.min_quantity, int(math.ceil(float(raw) / step) * step))
        candidates = sorted({floor_qty, ceil_qty})
        return min(candidates, key=lambda qty: abs((premium_per_lot * qty) - self.settings.target_max_profit_usd))

    def _estimate_current_pnl(self, cycle: CycleState) -> Decimal:
        try:
            symbols = [cycle.call_symbol, cycle.put_symbol]
            if cycle.upside_hedge_symbol:
                symbols.append(cycle.upside_hedge_symbol)
            if cycle.downside_hedge_symbol:
                symbols.append(cycle.downside_hedge_symbol)
            quotes = {s: self.client.get_ticker(s) for s in symbols}
            call_ask = dec((quotes[cycle.call_symbol].get("quotes") or {}).get("best_ask") or quotes[cycle.call_symbol].get("mark_price"))
            put_ask = dec((quotes[cycle.put_symbol].get("quotes") or {}).get("best_ask") or quotes[cycle.put_symbol].get("mark_price"))
            pnl = cycle.actual_max_profit - ((call_ask + put_ask) * cycle.quantity * cycle.premium_multiplier)
            if cycle.upside_hedge_symbol:
                buy_bid = dec((quotes[cycle.upside_hedge_symbol].get("quotes") or {}).get("best_bid") or quotes[cycle.upside_hedge_symbol].get("mark_price"))
                pnl += buy_bid * cycle.quantity * cycle.premium_multiplier
            if cycle.downside_hedge_symbol:
                buy_bid = dec((quotes[cycle.downside_hedge_symbol].get("quotes") or {}).get("best_bid") or quotes[cycle.downside_hedge_symbol].get("mark_price"))
                pnl += buy_bid * cycle.quantity * cycle.premium_multiplier
            return pnl.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        except Exception:
            LOG.exception("pnl estimate failed")
            return Decimal("0")

    def _spot_price(self) -> Decimal:
        if self.price_feed:
            price = self.price_feed.latest()
            if price is not None:
                return price
        return self.client.get_spot_price(self.settings.index_symbol)

    @staticmethod
    def _percent_left(spot: Decimal, breakeven: Decimal, upside: bool) -> Decimal:
        if spot <= 0:
            return Decimal("0")
        raw = ((breakeven - spot) / spot * Decimal("100")) if upside else ((spot - breakeven) / spot * Decimal("100"))
        return raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def _is_entry_due(self, now: datetime) -> bool:
        entry_hour, entry_minute = [int(x) for x in self.settings.entry_time.split(":")]
        entry = now.replace(hour=entry_hour, minute=entry_minute, second=0, microsecond=0)
        cutoff_hour, cutoff_minute = [int(x) for x in self.settings.expiry_cutoff_time.split(":")]
        cutoff = now.replace(hour=cutoff_hour, minute=cutoff_minute, second=0, microsecond=0)
        return entry <= now < cutoff

    def _is_after_cutoff(self, now: datetime, expiry_date: str, cutoff_str: str) -> bool:
        cutoff_hour, cutoff_minute = [int(x) for x in cutoff_str.split(":")]
        expiry = datetime.strptime(expiry_date, "%d-%m-%Y").replace(tzinfo=self.settings.tz)
        cutoff = expiry.replace(hour=cutoff_hour, minute=cutoff_minute, second=0, microsecond=0)
        return now >= cutoff

    @staticmethod
    def _client_oid(cycle_id: str, role: str) -> str:
        compact = cycle_id.replace("-", "").replace(":", "")
        return f"{role}{compact}"[:32]

