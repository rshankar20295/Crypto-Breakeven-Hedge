from __future__ import annotations

import logging
import os

from app.config import load_settings
from app.delta_client import DeltaClient
from app.market_data import LivePriceFeed
from app.state_store import StateStore
from app.strategy import BreakevenHedgeBot


def setup_logging(level: str) -> None:
    os.makedirs("logs", exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("logs/bot.log", encoding="utf-8"),
        ],
    )


def main() -> None:
    settings = load_settings()
    setup_logging(settings.log_level)
    client = DeltaClient(settings)
    store = StateStore(settings.state_db_path)
    price_feed = LivePriceFeed(settings, settings.index_symbol)
    bot = BreakevenHedgeBot(settings, client, store, price_feed)
    bot.run_forever()


if __name__ == "__main__":
    main()

