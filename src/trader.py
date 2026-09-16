import json
import threading
import time

from .config import Config
from .logger import get_logger
from .positions import PositionStore
from .risk import should_stop_loss, should_take_profit
from .scanner import detect_pump

logger = get_logger(__name__)

TOP_MOVERS_LIMIT = 20
WATCHLIST_MAX_AGE_MULTIPLIER = 2  # keep a token this many times the pump window before giving up on it
WATCHLIST_HARD_CAP = 1500  # safety backstop if the discovery rate spikes; age-based expiry is the primary policy
JUPITER_BATCH_SIZE = 100
TOKEN_DECIMALS = 6  # standard for pump.fun tokens; verify per-mint before enabling live trading at scale


def _chunk(items, size):
    for i in range(0, len(items), size):
        yield items[i : i + size]


class Trader:
    def __init__(self, client, config: Config, store: PositionStore, market_state=None):
        self.client = client
        self.config = config
        self.store = store
        self.market_state = market_state
        self.watchlist = {}  # mint -> {symbol, first_seen}
        self.price_history = {}  # mint -> [(ts, price), ...]
        self._history_lock = threading.Lock()
        self._watchlist_lock = threading.Lock()

    def add_discovered_token(self, token) -> None:
        """Called from the PumpPortal listener thread as new tokens/migrations stream in.

        Tokens are expired by age, not by watchlist size: pump.fun launches far more
        than a few hundred tokens within any 15-minute window, so a count-based cap
        would evict tokens before they ever reach the full pump window needed to
        measure momentum. Age-based expiry guarantees every token gets a fair shot.
        """
        now = time.time()
        max_age_seconds = self.config.pump_window_minutes * 60 * WATCHLIST_MAX_AGE_MULTIPLIER
        with self._watchlist_lock:
            if token.mint not in self.watchlist:
                self.watchlist[token.mint] = {"symbol": token.symbol, "first_seen": now}

            cutoff = now - max_age_seconds
            self.watchlist = {m: v for m, v in self.watchlist.items() if v["first_seen"] >= cutoff}

            if len(self.watchlist) > WATCHLIST_HARD_CAP:
                ordered = sorted(self.watchlist.items(), key=lambda kv: kv[1]["first_seen"], reverse=True)
                self.watchlist = dict(ordered[:WATCHLIST_HARD_CAP])

            active_mints = set(self.watchlist.keys())

        with self._history_lock:
            self.price_history = {m: h for m, h in self.price_history.items() if m in active_mints}

    def save_state(self, path: str) -> None:
        """Snapshot the watchlist and price history to disk so a restart (deploy,
        crash, reboot) doesn't reset the 15-minute momentum-tracking clock."""
        with self._watchlist_lock, self._history_lock:
            data = {"watchlist": self.watchlist, "price_history": self.price_history}
        try:
            with open(path, "w") as f:
                json.dump(data, f)
        except Exception as exc:
            logger.warning(f"failed to save watchlist state: {exc}")

    def load_state(self, path: str) -> None:
        try:
            with open(path) as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except Exception as exc:
            logger.warning(f"failed to load watchlist state: {exc}")
            return

        now = time.time()
        max_age_seconds = self.config.pump_window_minutes * 60 * WATCHLIST_MAX_AGE_MULTIPLIER
        cutoff = now - max_age_seconds

        with self._watchlist_lock:
            self.watchlist = {
                m: v for m, v in data.get("watchlist", {}).items() if v.get("first_seen", 0) >= cutoff
            }
            active_mints = set(self.watchlist.keys())
        with self._history_lock:
            self.price_history = {
                mint: [tuple(sample) for sample in history]
                for mint, history in data.get("price_history", {}).items()
                if mint in active_mints
            }
        logger.info(f"restored {len(self.watchlist)} watched tokens from disk")

    def _fetch_all_prices(self, mints):
        prices = {}
        for batch in _chunk(mints, JUPITER_BATCH_SIZE):
            try:
                prices.update(self.client.get_prices_usd(batch))
            except Exception as exc:
                logger.warning(f"failed to fetch price batch: {exc}")
        return prices

    def _update_history(self, mint, price):
        now = time.time()
        cutoff = now - self.config.pump_window_minutes * 60 - 60
        with self._history_lock:
            history = self.price_history.setdefault(mint, [])
            history.append((now, price))
            trimmed = [(t, p) for t, p in history if t >= cutoff]
            self.price_history[mint] = trimmed
            return list(trimmed)

    def scan_and_buy(self) -> int:
        start = time.time()

        with self._watchlist_lock:
            mints = list(self.watchlist.keys())
            symbols = {m: v["symbol"] for m, v in self.watchlist.items()}

        prices = self._fetch_all_prices(mints)

        results = []
        for mint, price in prices.items():
            history = self._update_history(mint, price)
            is_pump, pct_change = detect_pump(history, self.config.pump_window_minutes, self.config.pump_threshold_pct)
            symbol = symbols.get(mint, "?")
            results.append({"mint": mint, "symbol": symbol, "pct_change": pct_change, "is_pump": is_pump, "price": price})

        for r in results:
            if not r["is_pump"] or self.store.has_open_position(r["mint"]):
                continue

            quantity = self.config.position_size_usd / r["price"]
            logger.info(
                f"PUMP detected {r['symbol']} ({r['mint'][:8]}) +{r['pct_change']:.2f}% "
                f"-> buying ${self.config.position_size_usd:.2f}"
            )

            if not self.config.dry_run:
                self.client.buy(r["mint"], self.config.position_size_usd, r["price"], self.config.slippage_bps)

            self.store.open_position(r["mint"], r["symbol"], r["price"], quantity, self.config.position_size_usd)

        if self.market_state is not None:
            top_movers = sorted(results, key=lambda x: x["pct_change"], reverse=True)[:TOP_MOVERS_LIMIT]
            self.market_state.update(top_movers, len(mints), time.time() - start)

        return len(mints)

    def manage_open_positions(self) -> None:
        open_positions = self.store.get_open_positions()
        if not open_positions:
            return

        mints = [p["token_mint"] for p in open_positions]
        prices = self._fetch_all_prices(mints)

        for position in open_positions:
            mint = position["token_mint"]
            entry_price = position["entry_price"]
            quantity = position["quantity"]

            current_price = prices.get(mint)
            if current_price is None:
                logger.warning(f"failed to fetch price for {position['token_symbol']} ({mint[:8]})")
                continue

            exit_reason = None
            if should_take_profit(current_price, entry_price, self.config.take_profit_pct):
                exit_reason = "take_profit"
            elif should_stop_loss(current_price, entry_price, self.config.stop_loss_pct):
                exit_reason = "stop_loss"

            if exit_reason is None:
                continue

            logger.info(
                f"{exit_reason.upper()} {position['token_symbol']} ({mint[:8]}) "
                f"entry={entry_price} exit={current_price}"
            )

            if not self.config.dry_run:
                self.client.sell(mint, quantity, TOKEN_DECIMALS, self.config.slippage_bps)

            self.store.close_position(position["id"], current_price, exit_reason)

    def run_cycle(self) -> None:
        self.manage_open_positions()
        scanned = self.scan_and_buy()
        open_count = len(self.store.get_open_positions())
        logger.info(f"cycle complete: watching {scanned} tokens, {open_count} open position(s)")
