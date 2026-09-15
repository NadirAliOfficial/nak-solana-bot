# Solana Meme Momentum Bot

Watches new token launches on Pump.fun and tokens trading on Raydium, buys any
token up 15% in the last 15 minutes, and exits at +8% take profit or -3% stop
loss. Same trigger and exit logic as the Coinbase momentum bot, applied to
Solana instead of a centralized exchange. Includes a live dashboard.

No safety filters (liquidity, holder count, mint/freeze authority, honeypot
detection) are applied by design, per current scope. New token launches carry
a high rug pull and honeypot risk; see the warnings below before running live.

## Strategy

- Buy trigger: price up `PUMP_THRESHOLD_PCT` (default 15%) over `PUMP_WINDOW_MINUTES` (default 15 min)
- Take profit: `TAKE_PROFIT_PCT` (default 8%)
- Stop loss: `STOP_LOSS_PCT` (default 3%)
- Position size: `POSITION_SIZE_USD` per token (default $100), multiple tokens can be open at once
- Only one open position per token mint at a time
- New tokens only become eligible once the bot has tracked them for a full
  `PUMP_WINDOW_MINUTES` — there is no historical candle data for a token that
  launched seconds ago

## How it watches the market

Unlike Coinbase, Solana tokens have no ready-made candle API, especially for
tokens seconds old. New launches and Raydium migrations stream in continuously
over PumpPortal's free WebSocket feed (`wss://pumpportal.fun/api/data`) and
feed a watchlist (capped at 300 tokens); pump.fun's own REST API
(`frontend-api.pump.fun`) was retired and no longer works. Separately, the bot
polls Jupiter's price API every 10s for whatever's on the watchlist and builds
its own rolling price history in memory. Momentum is computed from that
accumulated history.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:
- `SOLANA_RPC_URL`: a paid RPC endpoint (Helius or QuickNode). Public RPC is
  too rate limited for real-time trading.
- `SOLANA_PRIVATE_KEY`: base58-encoded private key of a **dedicated burner
  wallet**, funded only with what you're willing to trade plus
  `GAS_RESERVE_SOL` for fees. Never use a main wallet.
- `TRADE_CURRENCY`: `USDC` or `SOL`, the asset positions are bought/sold against.

Leave `DRY_RUN=true` for your first run. In dry run, the bot watches real
market data and simulates buys/sells without sending real swaps. Set
`DRY_RUN=false` only once you've confirmed the behavior looks right and you
understand the risks below.

## Run

```bash
source .venv/bin/activate
python -m src.main
```

Dashboard: http://localhost:8090 (port configurable via `DASHBOARD_PORT`; avoid the 5060-5061 range, browsers block those as unsafe SIP ports)

## Tests

```bash
source .venv/bin/activate
pytest
```

## Risk warnings

- **No safety filtering.** The bot buys purely on price momentum. It does not
  check liquidity, holder concentration, mint/freeze authority, or simulate a
  sell before buying.
- **Honeypots.** Some tokens block selling entirely once you've bought in —
  the stop loss cannot force an exit if the token's contract won't let you
  sell. This bot has no way to detect that in advance under current scope.
- **Rug pulls.** Most Pump.fun launches are abandoned or drained by their
  creator within minutes. Buying on momentum alone means catching a lot of
  these.
- **Slippage on thin liquidity.** New tokens can have very little liquidity;
  a $100 buy can itself move the price significantly, and `SLIPPAGE_BPS`
  should be set with that in mind.
- This bot does not guarantee profits and can lose the full amount allocated
  to a position, including cases where a position can't be exited at all.

## Notes

- `SOLANA_RPC_URL`, `SOLANA_PRIVATE_KEY`, and the Jupiter/Pump.fun API
  integrations in `src/solana_client.py` are built against currently
  documented public endpoints but have not been exercised against a funded
  wallet yet — verify swap execution end to end with a small amount before
  trusting it with real capital.
- Token balances assume 6 decimals (the common Pump.fun convention); confirm
  per-mint decimals before scaling position sizes up.
