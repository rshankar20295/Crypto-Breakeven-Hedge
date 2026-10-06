from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.models import CycleState, dec


class DecimalEncoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return str(obj)
        return super().default(obj)


class StateStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def _init(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cycles (
                    cycle_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    client_order_id TEXT PRIMARY KEY,
                    cycle_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()

    def save_cycle(self, cycle: CycleState) -> None:
        payload = json.dumps(asdict(cycle), cls=DecimalEncoder)
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO cycles (cycle_id, status, payload, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(cycle_id) DO UPDATE SET
                    status=excluded.status,
                    payload=excluded.payload,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (cycle.cycle_id, cycle.status, payload),
            )
            conn.commit()

    def load_cycle(self, cycle_id: str) -> CycleState | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT payload FROM cycles WHERE cycle_id = ?", (cycle_id,)).fetchone()
        if not row:
            return None
        return self._decode_cycle(json.loads(row[0]))

    def active_cycle(self, underlying: str) -> CycleState | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT payload FROM cycles
                WHERE status NOT IN ('settled', 'failed', 'closed')
                ORDER BY updated_at DESC LIMIT 1
                """
            ).fetchone()
        while row:
            cycle = self._decode_cycle(json.loads(row[0]))
            if cycle.underlying == underlying:
                return cycle
            with closing(self._connect()) as conn:
                row = conn.execute(
                    """
                    SELECT payload FROM cycles
                    WHERE status NOT IN ('settled', 'failed', 'closed') AND updated_at < (
                        SELECT updated_at FROM cycles WHERE cycle_id = ?
                    )
                    ORDER BY updated_at DESC LIMIT 1
                    """,
                    (cycle.cycle_id,),
                ).fetchone()
        return None

    def record_order(self, cycle_id: str, role: str, client_order_id: str, payload: dict[str, Any]) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO orders (client_order_id, cycle_id, role, payload, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(client_order_id) DO UPDATE SET
                    payload=excluded.payload,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (client_order_id, cycle_id, role, json.dumps(payload, cls=DecimalEncoder)),
            )
            conn.commit()

    def has_order(self, client_order_id: str) -> bool:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT 1 FROM orders WHERE client_order_id = ?", (client_order_id,)).fetchone()
        return row is not None

    @staticmethod
    def _decode_cycle(data: dict[str, Any]) -> CycleState:
        decimal_fields = {
            "call_strike",
            "put_strike",
            "premium_per_lot",
            "premium_multiplier",
            "target_max_profit",
            "actual_max_profit",
            "upper_breakeven",
            "lower_breakeven",
        }
        for field in decimal_fields:
            data[field] = dec(data[field])
        return CycleState(**data)

