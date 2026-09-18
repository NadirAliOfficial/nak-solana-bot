# Nak Solana Bot

Watches new token launches on Pump.fun and tokens trading on Raydium, buys any
token up 15% in the last 15 minutes, and exits at +8% take profit or -3% stop
loss. Same trigger and exit logic as the Coinbase momentum bot, applied to
Solana instead of a centralized exchange. Includes a live dashboard.

Pre-buy safety filters (liquidity floor, mint/freeze authority revocation) are
applied by default to cut the most common rug/honeypot patterns; see
"Safety filters" below. They reduce but do not eliminate rug pull and
honeypot risk — see the warnings below before running live.

## Strategy

- Buy trigger: price up `PUMP_THRESHOLD_PCT` (default 15%) over `PUMP_WINDOW_MINUTES` (default 15 min)
- Take profit: `TAKE_PROFIT_PCT` (default 8%)
- Stop loss: `STOP_LOSS_PCT` (default 3%)
- Position size: `POSITION_SIZE_USD` per token (default $100), multiple tokens can be open at once
- Only one open position per token mint at a time
- New tokens only become eligible once the bot has tracked them for a full
  `PUMP_WINDOW_MINUTES` — there is no historical candle data for a token that
  launched seconds ago

## Safety filters

Checked once per pump candidate, right before a buy (not against the whole
watchlist, so it stays cheap):

- **Liquidity floor** (`MIN_LIQUIDITY_USD`, default $5000): rejects the buy if
  DexScreener reports less pool liquidity than this, or reports none at all.
  Reduces slippage and exit risk on thin pools.
- **Mint authority revoked** (`REQUIRE_MINT_AUTHORITY_REVOKED`, default true):
  rejects the buy if the token creator can still mint new supply. An active
  mint authority means the creator can inflate supply and dump on holders at
  will.
- **Freeze authority revoked** (`REQUIRE_FREEZE_AUTHORITY_REVOKED`, default
  true): rejects the buy if the token creator can still freeze token
  accounts. This is the classic honeypot switch — an active freeze authority
  means the creator can block your sell outright, and the stop loss can't
  save you from that.

- **LP lock / top-holder concentration** (`ENABLE_RUGCHECK`, default true): queries
  RugCheck.xyz's free report API. Rejects if the token's highest-liquidity market
  has less than `MIN_LP_LOCKED_PCT` (default 50%) of its LP locked/burned, or if
  any single holder owns more than `MAX_TOP_HOLDER_PCT` (default 30%) of supply.
  RugCheck's indexer can lag on tokens seconds old; a lookup failure or unindexed
  token fails closed (`rugcheck_unknown`) rather than buying blind. Since a token
  isn't buy-eligible until it's been tracked for a full `PUMP_WINDOW_MINUTES`
  anyway, RugCheck has usually caught up by then in practice.

Set `ENABLE_SAFETY_FILTERS=false` to disable all of the above and restore the
old momentum-only behavior. These filters do not detect a creator dumping
their own token allocation, since that requires no special authority — they
only remove trades with an on-chain guarantee of no exit.

## Circuit breakers

Independent of the per-token safety filters above — these pause *all* new
buys regardless of which token is pumping:

- **Daily loss limit** (`DAILY_LOSS_LIMIT_USD`, default $50): once today's
  realized P&L drops to or below `-$50`, no new positions open until the next
  UTC day. Existing open positions still exit normally.
- **Losing-streak cooldown** (`LOSING_STREAK_COUNT`, default 3): after this
  many consecutive stop-losses in a row, new buys pause for
  `LOSING_STREAK_COOLDOWN_MINUTES` (default 20) from the most recent one. A
  win or take-profit resets the streak.

Set either count/limit to `0` to disable it.

## Entry quality and sizing

Beyond the pass/fail safety filters, these change *which* pumps get bought and
how much goes into each one:

- **Minimum token age** (`MIN_TOKEN_AGE_SECONDS`, default 150): skips a token
  even if it's already pumping until it's been tracked for at least this
  long. The highest rug density is the first couple minutes after launch —
  this lets it prove it survives a little before buying in.
- **Volume confirmation** (`ENABLE_VOLUME_CONFIRMATION`, default true): a
  price spike with flat volume is often one whale moving a thin order book
  alone, not real buying interest. Requires 5-minute volume (DexScreener) to
  clear `MIN_RECENT_VOLUME_USD` (default $2000) *and* be at least
  `VOLUME_SURGE_MULTIPLIER`x (default 1.5) the token's own hourly 5-minute
  average rate — a genuine acceleration, not ambient baseline trading.
- **Copycat filter** (`ENABLE_COPYCAT_FILTER`, default true): when a meme
  trend blows up, dozens of similarly-named copycat tokens can pump in the
  same cycle. Groups candidates by fuzzy symbol match
  (`COPYCAT_SIMILARITY_THRESHOLD`, default 0.82) and buys only the one with
  the most liquidity — the rest are a coin flip on which one sticks.
- **Conviction sizing** (`ENABLE_CONVICTION_SIZING`, default true): sizes a
  position up to 1.3x `POSITION_SIZE_USD` for a setup that clears the safety
  thresholds comfortably, and down to 0.7x for one that barely squeaks by,
  based on how much liquidity/LP-lock/holder-concentration margin it has
  over the configured minimums.

## Exits

- **Partial exit + trailing stop** (`ENABLE_PARTIAL_EXIT`, default true):
  instead of exiting the whole position at a fixed take-profit, sells
  `PARTIAL_EXIT_PCT` (default 50%) at the normal `TAKE_PROFIT_PCT`/
  `STOP_LOSS_PCT`, and lets the rest ride with a trailing stop
  (`TRAILING_STOP_BPS`, default 500 = 5% below the running peak) instead of
  capping it at the same fixed target as every other trade. A token that
  runs to +60% no longer exits at the same +8% as one that barely ticks up.
  The trailing leg uses Jupiter Trigger's native `single` order type with
  `trailingBps` when live; the polling fallback tracks its own peak price
  per position (`peak_price` in the DB) for the same behavior if the
  on-chain order can't be placed.
- Set `ENABLE_PARTIAL_EXIT=false` to restore the old all-or-nothing behavior
  (single OCO order per position, full take-profit/stop-loss).

## How it watches the market

Unlike Coinbase, Solana tokens have no ready-made candle API, especially for
tokens seconds old. Two discovery sources feed a single watchlist (tokens are
expired by age, not count, so each gets a fair 30-minute window):

- **PumpPortal** (`wss://pumpportal.fun/api/data`, free) streams brand new
  Pump.fun launches and Raydium migrations in real time. It only sees activity
  from the moment the bot connects, so it can't find tokens that already
  existed before that — pump.fun's own REST API (`frontend-api.pump.fun`) was
  retired and no longer works.
- **GeckoTerminal** (free, no API key) is polled every 90s for trending and
  newly listed pools across all of Solana (Raydium, pumpswap, Orca, Meteora),
  covering established tokens with renewed momentum that PumpPortal would
  never see.

The bot polls Jupiter's price API every 10s for whatever's on the combined
watchlist and builds its own rolling price history in memory; momentum is
computed from that accumulated history. The watchlist and price history are
also persisted to disk (`watchlist_state.json`) so a restart doesn't reset the
tracking window.

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

- **Filters reduce, not eliminate, risk.** The safety/entry-quality filters
  above catch the most common rug/honeypot patterns but do not simulate a
  sell before buying, and can't stop a creator from dumping their own token
  allocation (that requires no special authority to do).
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
