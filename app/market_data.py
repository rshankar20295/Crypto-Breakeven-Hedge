from __future__ import annotations

import json
import logging
import threading
import time
from decimal import Decimal

try:
    import websocket
except ImportError:  # pragma: no cover - only used when dependencies are not installed locally.
    websocket = None

from app.config import Settings
from app.models import dec

LOG = logging.getLogger(__name__)


class LivePriceFeed:
    def __init__(self, settings: Settings, symbol: str):
        self.settings = settings
        self.symbol = symbol
        self._price: Decimal | None = None
        self._updated_at = 0.0
        self._started = False
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._started:
            return
        if websocket is None:
            LOG.warning("websocket-client is not installed; using REST price fallback only")
            return
        self._started = True
        thread = threading.Thread(target=self._run_forever, name="delta-price-feed", daemon=True)
        thread.start()

    def latest(self, max_age_seconds: float = 3.0) -> Decimal | None:
        with self._lock:
            if self._price is None:
                return None
            if time.monotonic() - self._updated_at > max_age_seconds:
                return None
            return self._price

    def _run_forever(self) -> None:
        while True:
            try:
                self._connect_once()
            except Exception:
                LOG.exception("public websocket failed; reconnecting")
            time.sleep(2)

    def _connect_once(self) -> None:
        def on_open(ws: websocket.WebSocketApp) -> None:
            LOG.info("public websocket connected for %s", self.symbol)
            ws.send(
                json.dumps(
                    {
                        "type": "subscribe",
                        "payload": {
                            "channels": [
                                {
                                    "name": "ob_l1",
                                    "symbols": [self.symbol],
                                }
                            ]
                        },
                    }
                )
            )

        def on_message(_: websocket.WebSocketApp, message: str) -> None:
            payload = json.loads(message)
            if payload.get("type") != "ob_l1":
                return
            best_bid = dec(payload.get("bp"))
            best_ask = dec(payload.get("ap"))
            price = (best_bid + best_ask) / Decimal("2") if best_bid > 0 and best_ask > 0 else best_bid or best_ask
            if price <= 0:
                return
            with self._lock:
                self._price = price
                self._updated_at = time.monotonic()

        def on_error(_: websocket.WebSocketApp, error: Exception) -> None:
            LOG.warning("public websocket error: %s", error)

        def on_close(_: websocket.WebSocketApp, code: int, reason: str) -> None:
            LOG.warning("public websocket closed code=%s reason=%s", code, reason)

        ws = websocket.WebSocketApp(
            self.settings.public_ws_url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        ws.run_forever(ping_interval=30, ping_timeout=5)

