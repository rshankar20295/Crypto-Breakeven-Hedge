from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _decimal(name: str, default: str) -> Decimal:
    return Decimal(os.getenv(name, default).strip())


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)).strip())


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)).strip())


@dataclass(frozen=True)
class Settings:
    dry_run: bool
    base_url: str
    public_ws_url: str
    private_ws_url: str
    api_key: str
    api_secret: str
    underlying: str
    quote_currency: str
    entry_time: str
    timezone: str
    expiry_rank: int
    expiry_cutoff_time: str
    call_delta: Decimal
    put_delta: Decimal
    target_max_profit_usd: Decimal
    quantity_step: int
    min_quantity: int
    use_contract_value_multiplier: bool
    poll_interval_seconds: float
    order_confirm_timeout_seconds: int
    order_retry_seconds: int
    entry_order_type: str
    hedge_order_type: str
    state_db_path: str
    log_level: str

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def underlying_symbol(self) -> str:
        return self.underlying.upper()

    @property
    def index_symbol(self) -> str:
        if self.underlying_symbol == "BTC" and self.quote_currency.upper() == "USD":
            return "BTCUSD"
        return f"{self.underlying_symbol}{self.quote_currency.upper()}"

    def validate(self) -> None:
        if self.expiry_rank < 1 or self.expiry_rank > 7:
            raise ValueError("EXPIRY_RANK must be between 1 and 7.")
        if self.call_delta <= 0:
            raise ValueError("CALL_DELTA must be positive.")
        if self.put_delta >= 0:
            raise ValueError("PUT_DELTA must be negative.")
        if self.target_max_profit_usd <= 0:
            raise ValueError("TARGET_MAX_PROFIT_USD must be positive.")
        if self.quantity_step < 1:
            raise ValueError("QUANTITY_STEP must be at least 1.")
        if self.min_quantity < 1:
            raise ValueError("MIN_QUANTITY must be at least 1.")
        if not self.dry_run and (not self.api_key or not self.api_secret):
            raise ValueError("DELTA_API_KEY and DELTA_API_SECRET are required when DRY_RUN=false.")


def load_settings() -> Settings:
    load_dotenv()
    settings = Settings(
        dry_run=_bool("DRY_RUN", True),
        base_url=os.getenv("DELTA_BASE_URL", "https://api.india.delta.exchange").rstrip("/"),
        public_ws_url=os.getenv("DELTA_PUBLIC_WS_URL", "wss://public-socket.india.delta.exchange"),
        private_ws_url=os.getenv("DELTA_PRIVATE_WS_URL", "wss://socket.india.delta.exchange"),
        api_key=os.getenv("DELTA_API_KEY", ""),
        api_secret=os.getenv("DELTA_API_SECRET", ""),
        underlying=os.getenv("UNDERLYING", "BTC"),
        quote_currency=os.getenv("QUOTE_CURRENCY", "USD"),
        entry_time=os.getenv("ENTRY_TIME", "09:30"),
        timezone=os.getenv("TIMEZONE", "Asia/Kolkata"),
        expiry_rank=_int("EXPIRY_RANK", 1),
        expiry_cutoff_time=os.getenv("EXPIRY_CUTOFF_TIME", "17:30"),
        call_delta=_decimal("CALL_DELTA", "0.10"),
        put_delta=_decimal("PUT_DELTA", "-0.10"),
        target_max_profit_usd=_decimal("TARGET_MAX_PROFIT_USD", "100"),
        quantity_step=_int("QUANTITY_STEP", 1),
        min_quantity=_int("MIN_QUANTITY", 1),
        use_contract_value_multiplier=_bool("USE_CONTRACT_VALUE_MULTIPLIER", True),
        poll_interval_seconds=_float("POLL_INTERVAL_SECONDS", 1.0),
        order_confirm_timeout_seconds=_int("ORDER_CONFIRM_TIMEOUT_SECONDS", 20),
        order_retry_seconds=_int("ORDER_RETRY_SECONDS", 2),
        entry_order_type=os.getenv("ENTRY_ORDER_TYPE", "market_order"),
        hedge_order_type=os.getenv("HEDGE_ORDER_TYPE", "market_order"),
        state_db_path=os.getenv("STATE_DB_PATH", "data/bot_state.sqlite3"),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )
    settings.validate()
    return settings

