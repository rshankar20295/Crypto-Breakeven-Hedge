from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import requests

from app.config import Settings
from app.models import OptionQuote, dec

LOG = logging.getLogger(__name__)


class DeltaClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "crypto-breakeven-hedge-bot/1.0",
            }
        )

    def _signature(self, method: str, path: str, query_string: str, body: str, timestamp: str) -> str:
        message = method.upper() + timestamp + path + query_string + body
        return hmac.new(
            self.settings.api_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _headers(self, method: str, path: str, query_string: str, body: str) -> dict[str, str]:
        timestamp = str(int(time.time()))
        return {
            "api-key": self.settings.api_key,
            "timestamp": timestamp,
            "signature": self._signature(method, path, query_string, body, timestamp),
        }

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        auth: bool = False,
    ) -> Any:
        params = params or {}
        body = body or {}
        query_string = f"?{urlencode(params)}" if params else ""
        payload = json.dumps(body, separators=(",", ":")) if body else ""
        headers = self._headers(method, path, query_string, payload) if auth else {}
        response = self.session.request(
            method,
            f"{self.settings.base_url}{path}",
            params=params,
            data=payload if payload else None,
            headers=headers,
            timeout=(3, 20),
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("success", False):
            raise RuntimeError(f"Delta API error for {method} {path}: {data}")
        return data.get("result")

    def get_products(self, contract_types: str = "call_options,put_options", states: str = "live") -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        params: dict[str, Any] = {"contract_types": contract_types, "states": states, "page_size": 1000}
        while True:
            data = self.session.get(
                f"{self.settings.base_url}/v2/products",
                params=params,
                headers={"Accept": "application/json"},
                timeout=(3, 20),
            )
            data.raise_for_status()
            parsed = data.json()
            if not parsed.get("success", False):
                raise RuntimeError(f"Delta products error: {parsed}")
            chunk = parsed.get("result") or []
            if isinstance(chunk, dict):
                chunk = [chunk]
            result.extend(chunk)
            meta = parsed.get("meta") or {}
            after = meta.get("after")
            if not after:
                break
            params["after"] = after
        return result

    def get_option_chain(self, underlying: str, expiry_date: str) -> list[OptionQuote]:
        params = {
            "contract_types": "call_options,put_options",
            "underlying_asset_symbols": underlying,
            "expiry_date": expiry_date,
        }
        rows = self._request("GET", "/v2/tickers", params=params)
        return [self._quote_from_ticker(row) for row in rows or []]

    def get_ticker(self, symbol: str) -> dict[str, Any]:
        return self._request("GET", f"/v2/tickers/{symbol}")

    def get_spot_price(self, symbol: str) -> Decimal:
        ticker = self.get_ticker(symbol)
        return dec(ticker.get("spot_price") or ticker.get("close") or ticker.get("mark_price"))

    def place_order(self, product_id: int, side: str, size: int, client_order_id: str, order_type: str) -> dict[str, Any]:
        body = {
            "product_id": product_id,
            "size": int(size),
            "side": side,
            "order_type": order_type,
            "client_order_id": client_order_id[:32],
        }
        LOG.info("placing %s %s order: product_id=%s size=%s client_oid=%s", side, order_type, product_id, size, client_order_id[:32])
        if self.settings.dry_run:
            return {
                "id": int(time.time() * 1000),
                "product_id": product_id,
                "size": size,
                "unfilled_size": 0,
                "side": side,
                "order_type": order_type,
                "client_order_id": client_order_id[:32],
                "average_fill_price": "0",
                "state": "closed",
            }
        return self._request("POST", "/v2/orders", body=body, auth=True)

    def get_order_by_client_oid(self, client_order_id: str) -> dict[str, Any] | None:
        if self.settings.dry_run:
            return None
        try:
            return self._request("GET", f"/v2/orders/client_order_id/{client_order_id[:32]}", auth=True)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return None
            raise

    def wait_for_fill(self, client_order_id: str, expected_size: int) -> tuple[bool, int, Decimal]:
        if self.settings.dry_run:
            return True, expected_size, Decimal("0")

        deadline = time.monotonic() + self.settings.order_confirm_timeout_seconds
        last_filled = 0
        avg_price = Decimal("0")
        while time.monotonic() < deadline:
            order = self.get_order_by_client_oid(client_order_id)
            if order:
                size = int(order.get("size") or 0)
                unfilled = int(order.get("unfilled_size") or 0)
                last_filled = max(0, size - unfilled)
                avg_price = dec(order.get("average_fill_price"))
                if last_filled >= expected_size:
                    return True, last_filled, avg_price
            time.sleep(1)
        return False, last_filled, avg_price

    @staticmethod
    def _quote_from_ticker(row: dict[str, Any]) -> OptionQuote:
        quotes = row.get("quotes") or {}
        greeks = row.get("greeks") or {}
        return OptionQuote(
            symbol=str(row.get("symbol")),
            product_id=int(row.get("product_id")),
            contract_type=str(row.get("contract_type")),
            strike=dec(row.get("strike_price")),
            delta=dec(greeks.get("delta")),
            best_bid=dec(quotes.get("best_bid")),
            best_ask=dec(quotes.get("best_ask")),
            mark_price=dec(row.get("mark_price")),
            spot_price=dec(row.get("spot_price")),
            contract_value=dec(row.get("contract_value"), "1"),
        )

