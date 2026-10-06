# Crypto Breakeven Hedge Bot

Delta Exchange options bot for the strategy discussed:

- select call and put strikes by delta
- sell equal lots as a short strangle
- calculate quantity from a target max profit amount
- calculate breakevens from actual filled premiums
- buy the nearest call at the upper breakeven once
- buy the nearest put at the lower breakeven once
- persist state so restarts do not duplicate hedge orders
- mark the cycle settled at a configured expiry cutoff time
- use Delta public WebSocket for live price checks, with REST fallback

The bot starts in `DRY_RUN=true`. Keep it that way until the logs match your expected behavior.

## Strategy Example

If BTC is `100`, the selected options are:

```text
Sell 110 CE @ 6
Sell 90 PE @ 4
Premium per strangle = 10
Target max profit = 100
Quantity = 10 lots
```

Breakevens:

```text
Upper breakeven = 110 + 10 = 120
Lower breakeven = 90 - 10 = 80
```

If BTC touches `120`, the bot buys the available call strike nearest to `120` with the same quantity. If BTC touches `80`, it buys the available put strike nearest to `80` with the same quantity. Each side hedges only once.

## Configuration

Copy `.env.example` to `.env` and edit:

```text
DRY_RUN=true
UNDERLYING=BTC
ENTRY_TIME=09:30
EXPIRY_RANK=1
EXPIRY_CUTOFF_TIME=17:30
CALL_DELTA=0.10
PUT_DELTA=-0.10
TARGET_MAX_PROFIT_USD=100
```

For XAUT or another underlying, change:

```text
UNDERLYING=XAUT
EXPIRY_CUTOFF_TIME=21:30
```

When going live:

```text
DRY_RUN=false
DELTA_API_KEY=your_key
DELTA_API_SECRET=your_secret
```

## Run Locally

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python -m app.main
```

## Run With Docker

```bash
docker compose up --build
```

## Coolify

Create a new Coolify service from this GitHub repo.

Use Dockerfile deployment and set these environment variables in Coolify:

```text
DRY_RUN=true
DELTA_BASE_URL=https://api.india.delta.exchange
DELTA_PUBLIC_WS_URL=wss://public-socket.india.delta.exchange
DELTA_PRIVATE_WS_URL=wss://socket.india.delta.exchange
DELTA_API_KEY=
DELTA_API_SECRET=
UNDERLYING=BTC
QUOTE_CURRENCY=USD
ENTRY_TIME=09:30
TIMEZONE=Asia/Kolkata
EXPIRY_RANK=1
EXPIRY_CUTOFF_TIME=17:30
CALL_DELTA=0.10
PUT_DELTA=-0.10
TARGET_MAX_PROFIT_USD=100
```

Mount persistent storage for:

```text
/app/data
/app/logs
```

This matters because `/app/data/bot_state.sqlite3` prevents duplicate hedge orders after restart.

## Safety Notes

Market orders prioritize entry, not price. In a fast move, the hedge can fill with slippage. The bot retries until the hedge is confirmed, but it cannot guarantee the exact breakeven price.

If a hedge order fails because of insufficient funds or margin, the bot does not mark the hedge as complete. It logs `HEDGE_ORDER_FAILED` with `position_is_unhedged=true` and keeps retrying the same hedge side with the same `client_order_id`, so it does not intentionally duplicate the hedge. The open short strangle remains exposed until funds/margin are available or the exchange accepts and fills the hedge.

The bot considers an expiry cycle done at `EXPIRY_CUTOFF_TIME`. Use 24-hour time, so `17:30` means 5:30 PM. It does not try to manage old expired positions after that time.

## Log Examples

Normal monitoring:

```text
STATUS | cycle=BTC-06-10-2026-E0930 | spot=119.50 | pnl_est=64.20 | upper_be=120.00 | up_left=0.42% | lower_be=80.00 | down_left=33.05% | qty=10 | max_profit=100 | hedge_up=pending | hedge_down=pending
```

Hedge trigger:

```text
HEDGE_REQUIRED | side=upside | attempt=1 | action=buy | symbol=C-BTC-120-061026 | qty=10 | nearest_strike=120 | client_oid=HUBTC06102026E0930
```

Hedge problem:

```text
HEDGE_ORDER_FAILED | side=upside | attempt=3 | symbol=C-BTC-120-061026 | qty=10 | error=insufficient_margin | retry_in=2s | position_is_unhedged=true
```

