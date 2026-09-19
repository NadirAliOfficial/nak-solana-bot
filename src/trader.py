import datetime
import json
import os
import threading
import time
from typing import Optional, Tuple

from .config import Config
from .entry_quality import EntryQualityChecker
from .logger import get_logger
from .positions import PositionStore
from .risk import should_stop_loss, should_take_profit, should_trailing_stop
from .safety import SafetyChecker, conviction_multiplier
from .scanner import detect_pump, is_copycat_symbol

logger = get_logger(__name__)

WATCHLIST_MAX_AGE_MULTIPLIER = 4  # keep a token this many times the pump window before giving up on it
WATCHLIST_HARD_CAP = 3000  # safety backstop if the discovery rate spikes; age-based expiry is the primary policy
JUPITER_BATCH_SIZE = 100
TOKEN_DECIMALS = 6  # standard for pump.fun tokens; verify per-mint before enabling live trading at scale


def _chunk(items, size):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _start_of_utc_day_ts() -> float:
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class Trader:
    def __init__(self, client, config: Config, store: PositionStore, market_state=None, trigger_client=None):
        self.client = client
        self.config = config
        self.store = store
        self.market_state = market_state
        self.trigger_client = trigger_client
        self.watchlist = {}  # mint -> {symbol, first_seen}
        self.price_history = {}  # mint -> [(ts, price), ...]
        self._history_lock = threading.Lock()
        self._watchlist_lock = threading.Lock()
        self.safety = SafetyChecker(client.rpc) if client is not None and hasattr(client, "rpc") else None
        self.entry_quality = EntryQualityChecker()

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
        """Snapshot the watchlist and price history to disk atomically so a restart (deploy,
        crash, reboot) doesn't reset the 15-minute momentum-tracking clock."""
        with self._watchlist_lock, self._history_lock:
            data = {"watchlist": self.watchlist, "price_history": self.price_history}
        tmp_path = f"{path}.tmp"
        try:
            with open(tmp_path, "w") as f:
                json.dump(data, f)
            os.replace(tmp_path, path)
        except Exception as exc:
            logger.warning(f"failed to save watchlist state: {exc}")
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

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

    def _buy_gate_status(self) -> Tuple[bool, str]:
        """Global checks that pause all new buys regardless of which token is pumping.
        Returns (blocked, reason)."""
        if self.config.daily_loss_limit_usd > 0:
            today_stats = self.store.get_todays_stats(_start_of_utc_day_ts())
            if today_stats["today_pnl"] <= -self.config.daily_loss_limit_usd:
                return True, f"daily loss limit hit (today P&L ${today_stats['today_pnl']:.2f})"

        if self.config.losing_streak_count > 0:
            streak = self.store.get_consecutive_losses()
            if streak >= self.config.losing_streak_count:
                last_loss_time = self.store.get_last_loss_exit_time()
                cooldown_seconds = self.config.losing_streak_cooldown_minutes * 60
                if last_loss_time and time.time() - last_loss_time < cooldown_seconds:
                    return True, f"{streak} consecutive losses - cooling down"

        return False, ""

    def _dedupe_copycats(self, candidates: list) -> list:
        """When a meme trend blows up, dozens of similarly-named copycat tokens can
        pump within the same cycle - only the one with the most liquidity is worth
        the risk. Groups candidates by fuzzy symbol match and keeps the highest-
        liquidity one per group, dropping the rest."""
        dropped = set()
        for i in range(len(candidates)):
            if candidates[i]["mint"] in dropped:
                continue
            group = [candidates[i]]
            for j in range(i + 1, len(candidates)):
                if candidates[j]["mint"] in dropped:
                    continue
                if is_copycat_symbol(
                    candidates[i]["symbol"], candidates[j]["symbol"], self.config.copycat_similarity_threshold
                ):
                    group.append(candidates[j])

            if len(group) < 2:
                continue

            for r in group:
                if "_liquidity_usd" not in r:
                    r["_liquidity_usd"] = (self.safety.get_liquidity_usd(r["mint"]) if self.safety else 0.0) or 0.0

            best = max(group, key=lambda r: r["_liquidity_usd"])
            for r in group:
                if r is not best:
                    dropped.add(r["mint"])
                    logger.info(
                        f"skipping buy for {r['symbol']} ({r['mint'][:8]}): copycat of "
                        f"{best['symbol']} ({best['mint'][:8]}) with less liquidity"
                    )

        return [r for r in candidates if r["mint"] not in dropped]

    def scan_and_buy(self) -> int:
        start = time.time()

        with self._watchlist_lock:
            mints = list(self.watchlist.keys())
            symbols = {m: v["symbol"] for m, v in self.watchlist.items()}
            first_seen_map = {m: v["first_seen"] for m, v in self.watchlist.items()}

        prices = self._fetch_all_prices(mints)

        results = []
        for mint, price in prices.items():
            history = self._update_history(mint, price)
            is_pump, pct_change = detect_pump(history, self.config.pump_window_minutes, self.config.pump_threshold_pct)
            symbol = symbols.get(mint, "?")
            results.append({"mint": mint, "symbol": symbol, "pct_change": pct_change, "is_pump": is_pump, "price": price})

        open_positions = self.store.get_open_positions()
        currently_exposed_usd = sum(p["usd_size"] for p in open_positions)

        available_funds_usd = None
        if self.config.dry_run and self.config.dry_run_paper_balance_usd > 0:
            # Lets dry run simulate a paper bankroll without needing a funded wallet -
            # still subject to the same real-balance-style constraint below, just against
            # a configured number instead of an on-chain lookup.
            available_funds_usd = self.config.dry_run_paper_balance_usd
        elif self.client is not None and hasattr(self.client, "get_tradable_balance_usd"):
            try:
                available_funds_usd = self.client.get_tradable_balance_usd(self.config.gas_reserve_sol)
            except Exception as exc:
                logger.warning(f"could not fetch tradable balance: {exc}")

        # In dry run mode, deduct cumulative paper P&L so simulated balance reflects real losses (no infinite reload)
        if available_funds_usd is not None and self.config.dry_run:
            closed_stats = self.store.get_closed_stats()
            total_realized_pnl = closed_stats.get("total_pnl", 0.0)
            available_funds_usd = max(0.0, available_funds_usd + total_realized_pnl)

        # Hard ceiling on $ risked per trade as a % of account bankroll - a rug pull can
        # wipe out a position entirely with no warning (liquidity metadata looks identical
        # for a token about to run and one about to be dumped into), so the only reliable
        # protection is capping how much any single trade can cost in the worst case,
        # independent of conviction sizing's upper bound. available_funds_usd is used as-is
        # (not summed with currently_exposed_usd): in dry run it's already the starting
        # bankroll before exposure is deducted below, and in live mode it's the wallet's
        # free cash - using it alone is a simpler, consistently-conservative basis than
        # trying to reconcile "total equity" across both modes' different semantics.
        max_risk_usd = None
        if available_funds_usd is not None and self.config.max_position_risk_pct > 0:
            max_risk_usd = available_funds_usd * self.config.max_position_risk_pct / 100

        # Sort so the highest momentum movers are evaluated and bought first
        results.sort(key=lambda x: x["pct_change"], reverse=True)

        buys_blocked, buys_blocked_reason = self._buy_gate_status()
        if buys_blocked:
            logger.info(f"no new buys this cycle: {buys_blocked_reason}")

        buy_candidates = [] if buys_blocked else [r for r in results if r["is_pump"]]
        if self.config.enable_copycat_filter:
            buy_candidates = self._dedupe_copycats(buy_candidates)

        for r in buy_candidates:
            token_age_seconds = time.time() - first_seen_map.get(r["mint"], 0)
            if token_age_seconds < self.config.min_token_age_seconds:
                continue

            cooldown_seconds = self.config.rebuy_cooldown_minutes * 60
            if self.store.has_recent_position(r["mint"], cooldown_seconds):
                continue

            open_count = len(open_positions)
            if open_count >= self.config.max_open_positions:
                logger.info(
                    f"skipping buy for {r['symbol']}: reached max open positions limit ({self.config.max_open_positions})"
                )
                break

            metrics = {}
            if self.safety is not None:
                passed, reason, metrics = self.safety.check(r["mint"], self.config)
                if not passed:
                    logger.info(f"skipping buy for {r['symbol']} ({r['mint'][:8]}): safety filter failed ({reason})")
                    continue

            passed, reason = self.entry_quality.check(r["mint"], self.config)
            if not passed:
                logger.info(f"skipping buy for {r['symbol']} ({r['mint'][:8]}): entry quality failed ({reason})")
                continue

            actual_size_usd = self.config.position_size_usd
            if self.config.enable_conviction_sizing:
                actual_size_usd *= conviction_multiplier(metrics, self.config)

            if max_risk_usd is not None and actual_size_usd > max_risk_usd:
                logger.info(
                    f"{r['symbol']} ({r['mint'][:8]}): capping size ${actual_size_usd:.2f} -> "
                    f"${max_risk_usd:.2f} ({self.config.max_position_risk_pct:.1f}% of bankroll)"
                )
                actual_size_usd = max_risk_usd

            # STRICT BALANCE CONSTRAINT: Even in dry run, you cannot buy more than available real balance
            if available_funds_usd is not None:
                free_capacity_usd = max(0.0, available_funds_usd - currently_exposed_usd)
                if free_capacity_usd < actual_size_usd:
                    logger.info(
                        f"skipping buy for {r['symbol']}: insufficient available funds (${free_capacity_usd:.2f} free < ${actual_size_usd:.2f} size | tradable: ${available_funds_usd:.2f}, exposed: ${currently_exposed_usd:.2f})"
                    )
                    break

            quantity = actual_size_usd / r["price"]
            logger.info(
                f"PUMP detected {r['symbol']} ({r['mint'][:8]}) +{r['pct_change']:.2f}% "
                f"-> buying ${actual_size_usd:.2f}"
            )

            buy_ok = True
            if not self.config.dry_run:
                try:
                    self.client.buy(r["mint"], actual_size_usd, r["price"], self.config.slippage_bps)
                except Exception as exc:
                    logger.error(f"buy failed for {r['symbol']} ({r['mint'][:8]}): {exc}")
                    buy_ok = False

            if buy_ok:
                partial_pct = self.config.partial_exit_pct / 100.0
                if self.config.enable_partial_exit and 0 < partial_pct < 1:
                    quantity_a, usd_a = quantity * partial_pct, actual_size_usd * partial_pct
                    if self.config.enable_369_system:
                        # Solana 369 System: -3% stop loss on both legs, first leg takes
                        # profit at take_profit_pct (6%), second leg at take_profit_pct_2
                        # (9%) - two fixed targets instead of letting the second leg trail.
                        leg_b = (quantity - quantity_a, actual_size_usd - usd_a, "oco", self.config.take_profit_pct_2)
                    else:
                        leg_b = (quantity - quantity_a, actual_size_usd - usd_a, "trailing", None)
                    legs = [(quantity_a, usd_a, "oco", self.config.take_profit_pct), leg_b]
                else:
                    legs = [(quantity, actual_size_usd, "oco", self.config.take_profit_pct)]

                currently_exposed_usd += actual_size_usd
                for leg_quantity, leg_usd, exit_style, leg_take_profit_pct in legs:
                    pos_id = self.store.open_position(
                        r["mint"],
                        r["symbol"],
                        r["price"],
                        leg_quantity,
                        leg_usd,
                        exit_style=exit_style,
                        take_profit_pct=leg_take_profit_pct,
                    )
                    open_positions.append({"id": pos_id, "token_mint": r["mint"], "usd_size": leg_usd})

                    if not self.config.dry_run and self.trigger_client is not None:
                        trade_mint = getattr(self.client, "trade_mint", None)
                        if trade_mint is not None:
                            try:
                                if exit_style == "trailing":
                                    order_id = self.trigger_client.place_trailing_stop_order(
                                        token_mint=r["mint"],
                                        trade_currency_mint=trade_mint,
                                        quantity=leg_quantity,
                                        token_decimals=TOKEN_DECIMALS,
                                        trailing_bps=self.config.trailing_stop_bps,
                                        slippage_bps=self.config.slippage_bps,
                                    )
                                else:
                                    tp_pct = (
                                        leg_take_profit_pct
                                        if leg_take_profit_pct is not None
                                        else self.config.take_profit_pct
                                    )
                                    order_id = self.trigger_client.place_oco_exit_order(
                                        token_mint=r["mint"],
                                        trade_currency_mint=trade_mint,
                                        quantity=leg_quantity,
                                        token_decimals=TOKEN_DECIMALS,
                                        tp_price_usd=r["price"] * (1 + tp_pct / 100),
                                        sl_price_usd=r["price"] * (1 - self.config.stop_loss_pct / 100),
                                        slippage_bps=self.config.slippage_bps,
                                    )
                                if order_id:
                                    self.store.set_trigger_order_id(pos_id, order_id)
                            except Exception as exc:
                                # Falls back to our own polling-based exit logic for this position -
                                # never leaves it unprotected, just loses the faster on-chain exit.
                                logger.warning(
                                    f"failed to place jupiter {exit_style} order for {r['symbol']} ({r['mint'][:8]}): {exc}"
                                )

        if self.market_state is not None:
            top_movers = sorted(results, key=lambda x: x["pct_change"], reverse=True)
            self.market_state.update(top_movers, len(mints), time.time() - start)

        return len(mints)

    def manage_open_positions(self) -> Tuple[bool, bool]:
        """Returns (has_open_positions, needs_fast_poll). needs_fast_poll is True while
        any position is younger than fast_poll_window_seconds - every rug pull observed
        so far hit within minutes of buying, so the caller should poll tighter during
        that window instead of the normal cadence."""
        open_positions = self.store.get_open_positions()
        if not open_positions:
            return False, False

        mints = [p["token_mint"] for p in open_positions]
        if self.client is not None and hasattr(self.client, "get_fast_prices_usd"):
            prices = self.client.get_fast_prices_usd(mints)
        else:
            prices = self._fetch_all_prices(mints)

        now = time.time()
        max_hold_seconds = self.config.max_position_hold_minutes * 60
        needs_fast_poll = False
        for position in open_positions:
            mint = position["token_mint"]
            entry_price = position["entry_price"]
            quantity = position["quantity"]
            pos_age = now - position["entry_time"]
            if pos_age < self.config.fast_poll_window_seconds:
                needs_fast_poll = True
            trigger_order_id = position["trigger_order_id"] if "trigger_order_id" in position.keys() else None
            exit_style = position["exit_style"] if "exit_style" in position.keys() else "oco"
            current_price = prices.get(mint)

            if trigger_order_id and not self.config.dry_run and self.trigger_client is not None:
                closed_by_trigger = self._check_trigger_order(
                    position, trigger_order_id, pos_age, max_hold_seconds, exit_style, current_price
                )
                if closed_by_trigger:
                    continue
                if closed_by_trigger is None:
                    # Order is still open on Jupiter's side and hasn't gone stale - their
                    # infrastructure is watching the price, so skip our own manual check.
                    continue
                # closed_by_trigger is False: status check failed (network error, unknown
                # order, etc.) - fall through to our own polling exit logic as a safety net.

            if current_price is None:
                # If price has been unavailable for dead_token_timeout_seconds, the token is
                # presumed dead/illiquid - a real sell would fail anyway, so this is the one
                # case marked closed without attempting one.
                if pos_age > self.config.dead_token_timeout_seconds:
                    logger.warning(
                        f"closing dead position for {position['token_symbol']} ({mint[:8]}): price unavailable after {int(pos_age/60)}m"
                    )
                    self.store.close_position(position["id"], 0.0, "dead_token")
                continue

            exit_reason = None
            if exit_style == "trailing":
                has_peak = "peak_price" in position.keys() and position["peak_price"] is not None
                peak_price = position["peak_price"] if has_peak else entry_price
                if current_price > peak_price:
                    peak_price = current_price
                    self.store.update_peak_price(position["id"], peak_price)

                if should_trailing_stop(current_price, peak_price, self.config.trailing_stop_bps):
                    exit_reason = "trailing_stop"
                elif pos_age > max_hold_seconds:
                    exit_reason = "stale_timeout"
            else:
                has_tp_override = "take_profit_pct" in position.keys() and position["take_profit_pct"] is not None
                take_profit_pct = position["take_profit_pct"] if has_tp_override else self.config.take_profit_pct
                if should_take_profit(current_price, entry_price, take_profit_pct):
                    exit_reason = "take_profit"
                elif should_stop_loss(current_price, entry_price, self.config.stop_loss_pct):
                    exit_reason = "stop_loss"
                elif pos_age > max_hold_seconds:
                    exit_reason = "stale_timeout"

            if exit_reason is None:
                continue

            pct = ((current_price - entry_price) / entry_price) * 100
            logger.info(
                f"REALTIME {exit_reason.upper()} {position['token_symbol']} ({mint[:8]}): {pct:+.2f}% "
                f"entry={entry_price} exit={current_price}"
            )

            sell_ok = True
            if not self.config.dry_run:
                try:
                    self.client.sell(mint, quantity, TOKEN_DECIMALS, self.config.slippage_bps)
                except Exception as exc:
                    logger.error(f"sell failed for {position['token_symbol']} ({mint[:8]}): {exc}")
                    sell_ok = False

            if sell_ok:
                self.store.close_position(position["id"], current_price, exit_reason)

        return True, needs_fast_poll

    def _check_trigger_order(
        self, position, trigger_order_id, pos_age, max_hold_seconds, exit_style="oco", current_price=None
    ) -> Optional[bool]:
        """Returns True if the position was closed here (order filled), None if the order
        is still open and not yet stale (nothing to do), or False if the caller should fall
        back to manual polling (status unknown, or the order went stale and was cancelled).

        OCO orders report separate tpState/slState; single/trailing orders report only
        orderState (no tp/sl split) - see Jupiter Trigger API docs."""
        try:
            order = self.trigger_client.get_order_status(trigger_order_id)
        except Exception as exc:
            logger.warning(f"failed to check trigger order {trigger_order_id}: {exc}")
            return False

        order_state = str((order or {}).get("orderState", "")).lower()

        if exit_style == "trailing":
            if order_state == "filled":
                exit_price = current_price or position["entry_price"]
                pct = ((exit_price - position["entry_price"]) / position["entry_price"]) * 100
                logger.info(
                    f"JUPITER TRAILING_STOP {position['token_symbol']} ({position['token_mint'][:8]}): "
                    f"{pct:+.2f}% entry={position['entry_price']} exit={exit_price}"
                )
                self.store.close_position(position["id"], exit_price, "trailing_stop")
                return True

            if order_state in ("cancelled", "expired", "failed", "closed"):
                return False

            if pos_age > max_hold_seconds:
                logger.info(
                    f"trailing-stop order for {position['token_symbol']} ({position['token_mint'][:8]}) still open "
                    f"past max hold time - cancelling to fall back to manual stale-timeout close"
                )
                try:
                    self.trigger_client.cancel_order(trigger_order_id)
                except Exception as exc:
                    logger.warning(f"failed to cancel stale trailing-stop order {trigger_order_id}: {exc}")
                return False

            return None

        tp_state = str((order or {}).get("tpState", "")).lower()
        sl_state = str((order or {}).get("slState", "")).lower()

        if tp_state == "filled" or sl_state == "filled":
            exit_reason = "take_profit" if tp_state == "filled" else "stop_loss"
            exit_price = order.get("tpPriceUsd") if exit_reason == "take_profit" else order.get("slPriceUsd")
            exit_price = exit_price or current_price or position["entry_price"]
            pct = ((exit_price - position["entry_price"]) / position["entry_price"]) * 100
            logger.info(
                f"JUPITER TRIGGER {exit_reason.upper()} {position['token_symbol']} ({position['token_mint'][:8]}): "
                f"{pct:+.2f}% entry={position['entry_price']} exit={exit_price}"
            )
            self.store.close_position(position["id"], exit_price, exit_reason)
            return True

        if order_state in ("cancelled", "expired", "failed", "closed"):
            return False  # nothing left protecting this position - fall back to manual polling

        if pos_age > max_hold_seconds:
            logger.info(
                f"trigger order for {position['token_symbol']} ({position['token_mint'][:8]}) still open past "
                f"max hold time - cancelling to fall back to manual stale-timeout close"
            )
            try:
                self.trigger_client.cancel_order(trigger_order_id)
            except Exception as exc:
                logger.warning(f"failed to cancel stale trigger order {trigger_order_id}: {exc}")
            return False

        return None  # still open, not stale - Jupiter is watching it, nothing to do here

    def run_cycle(self) -> None:
        self.manage_open_positions()
        scanned = self.scan_and_buy()
        open_count = len(self.store.get_open_positions())
        logger.info(f"cycle complete: watching {scanned} tokens, {open_count} open position(s)")
