import json
import os
import time
from unittest.mock import MagicMock

import pytest

from src.config import Config
from src.market_state import MarketState
from src.positions import PositionStore
from src.solana_client import Token
from src.trader import Trader


class FakeClient:
    def __init__(self, prices):
        self.prices = dict(prices)  # mint -> price, mutable across cycles in tests
        self.buys = []
        self.sells = []

    def get_prices_usd(self, mints):
        return {m: self.prices[m] for m in mints if m in self.prices}

    def buy(self, token_mint, usd_amount, price_usd, slippage_bps):
        self.buys.append((token_mint, usd_amount))
        return "buy-sig"

    def sell(self, token_mint, quantity, token_decimals, slippage_bps):
        self.sells.append((token_mint, quantity))
        return "sell-sig"


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.dry_run = True
    cfg.db_path = os.path.join(tmp_path, "test.db")
    cfg.pump_window_minutes = 15
    cfg.pump_threshold_pct = 15.0
    cfg.take_profit_pct = 8.0
    cfg.stop_loss_pct = 3.0
    cfg.position_size_usd = 100.0
    cfg.min_token_age_seconds = 0
    cfg.enable_volume_confirmation = False
    cfg.max_position_risk_pct = 0
    return cfg


def _prime_history(trader, mint, prices, window_seconds=900):
    now = time.time()
    n = len(prices)
    step = window_seconds / max(n - 1, 1)
    with trader._history_lock:
        trader.price_history[mint] = [(now - window_seconds + i * step, p) for i, p in enumerate(prices)]


def test_scan_and_buy_opens_position_on_pump(config):
    store = PositionStore(config.db_path)
    client = FakeClient({"MintABC": 1.0})
    trader = Trader(client, config, store)
    trader.add_discovered_token(Token(mint="MintABC", symbol="ABC"))
    _prime_history(trader, "MintABC", [1.0] * 15)
    client.prices["MintABC"] = 1.20  # next observed price triggers the pump

    trader.scan_and_buy()

    assert store.has_open_position("MintABC") is True
    assert client.buys == []  # dry run: no live swap sent


def test_scan_and_buy_skips_buy_when_safety_filter_fails(config):
    store = PositionStore(config.db_path)
    client = FakeClient({"MintABC": 1.0})
    trader = Trader(client, config, store)
    trader.safety = MagicMock()
    trader.safety.check.return_value = (False, "liquidity_unknown", {})
    trader.add_discovered_token(Token(mint="MintABC", symbol="ABC"))
    _prime_history(trader, "MintABC", [1.0] * 15)
    client.prices["MintABC"] = 1.20

    trader.scan_and_buy()

    assert store.has_open_position("MintABC") is False
    trader.safety.check.assert_called_once_with("MintABC", config)


def test_scan_and_buy_buys_when_safety_filter_passes(config):
    store = PositionStore(config.db_path)
    client = FakeClient({"MintABC": 1.0})
    trader = Trader(client, config, store)
    trader.safety = MagicMock()
    trader.safety.check.return_value = (True, "", {})
    trader.add_discovered_token(Token(mint="MintABC", symbol="ABC"))
    _prime_history(trader, "MintABC", [1.0] * 15)
    client.prices["MintABC"] = 1.20

    trader.scan_and_buy()

    assert store.has_open_position("MintABC") is True


def test_scan_and_buy_caps_size_by_max_position_risk_pct(config):
    store = PositionStore(config.db_path)
    client = FakeClient({"MintABC": 1.0})
    config.dry_run_paper_balance_usd = 300.0
    config.max_position_risk_pct = 2.0  # max $6 on a $300 bankroll
    config.enable_conviction_sizing = False
    config.enable_partial_exit = False
    trader = Trader(client, config, store)
    trader.add_discovered_token(Token(mint="MintABC", symbol="ABC"))
    _prime_history(trader, "MintABC", [1.0] * 15)
    client.prices["MintABC"] = 1.20

    trader.scan_and_buy()

    position = store.get_open_positions()[0]
    assert position["usd_size"] == pytest.approx(6.0)


def test_scan_and_buy_does_not_cap_size_when_max_position_risk_pct_disabled(config):
    store = PositionStore(config.db_path)
    client = FakeClient({"MintABC": 1.0})
    config.dry_run_paper_balance_usd = 300.0
    config.max_position_risk_pct = 0  # disabled
    config.enable_conviction_sizing = False
    config.enable_partial_exit = False
    trader = Trader(client, config, store)
    trader.add_discovered_token(Token(mint="MintABC", symbol="ABC"))
    _prime_history(trader, "MintABC", [1.0] * 15)
    client.prices["MintABC"] = 1.20

    trader.scan_and_buy()

    position = store.get_open_positions()[0]
    assert position["usd_size"] == pytest.approx(config.position_size_usd)


def test_scan_and_buy_respects_dry_run_paper_balance_cap(config):
    store = PositionStore(config.db_path)
    client = FakeClient({"MintA": 1.0, "MintB": 1.0, "MintC": 1.0})
    config.dry_run_paper_balance_usd = 150.0
    config.position_size_usd = 100.0
    config.enable_partial_exit = False
    config.max_open_positions = 10
    trader = Trader(client, config, store)
    for mint in ("MintA", "MintB", "MintC"):
        trader.add_discovered_token(Token(mint=mint, symbol=mint))
        _prime_history(trader, mint, [1.0] * 15)
        client.prices[mint] = 1.20

    trader.scan_and_buy()

    # $150 paper balance covers one $100 position but not a second
    assert len(store.get_open_positions()) == 1


def test_scan_and_buy_paper_balance_allows_multiple_positions_within_cap(config):
    store = PositionStore(config.db_path)
    client = FakeClient({"MintA": 1.0, "MintB": 1.0})
    config.dry_run_paper_balance_usd = 300.0
    config.position_size_usd = 100.0
    config.enable_partial_exit = False
    trader = Trader(client, config, store)
    for mint in ("MintA", "MintB"):
        trader.add_discovered_token(Token(mint=mint, symbol=mint))
        _prime_history(trader, mint, [1.0] * 15)
        client.prices[mint] = 1.20

    trader.scan_and_buy()

    assert len(store.get_open_positions()) == 2


def test_scan_and_buy_skips_token_younger_than_min_age(config):
    store = PositionStore(config.db_path)
    client = FakeClient({"MintABC": 1.0})
    config.min_token_age_seconds = 300
    trader = Trader(client, config, store)
    trader.add_discovered_token(Token(mint="MintABC", symbol="ABC"))  # first_seen = now
    _prime_history(trader, "MintABC", [1.0] * 15)
    client.prices["MintABC"] = 1.20

    trader.scan_and_buy()

    assert store.has_open_position("MintABC") is False


def test_scan_and_buy_allows_token_older_than_min_age(config):
    store = PositionStore(config.db_path)
    client = FakeClient({"MintABC": 1.0})
    config.min_token_age_seconds = 60
    trader = Trader(client, config, store)
    trader.add_discovered_token(Token(mint="MintABC", symbol="ABC"))
    trader.watchlist["MintABC"]["first_seen"] = time.time() - 120  # older than the min-age threshold
    _prime_history(trader, "MintABC", [1.0] * 15)
    client.prices["MintABC"] = 1.20

    trader.scan_and_buy()

    assert store.has_open_position("MintABC") is True


def test_scan_and_buy_skips_when_volume_confirmation_fails(config):
    store = PositionStore(config.db_path)
    client = FakeClient({"MintABC": 1.0})
    config.enable_volume_confirmation = True
    trader = Trader(client, config, store)
    trader.entry_quality = MagicMock()
    trader.entry_quality.check.return_value = (False, "volume_unknown")
    trader.add_discovered_token(Token(mint="MintABC", symbol="ABC"))
    _prime_history(trader, "MintABC", [1.0] * 15)
    client.prices["MintABC"] = 1.20

    trader.scan_and_buy()

    assert store.has_open_position("MintABC") is False


def test_scan_and_buy_buys_when_volume_confirmation_passes(config):
    store = PositionStore(config.db_path)
    client = FakeClient({"MintABC": 1.0})
    config.enable_volume_confirmation = True
    trader = Trader(client, config, store)
    trader.entry_quality = MagicMock()
    trader.entry_quality.check.return_value = (True, "")
    trader.add_discovered_token(Token(mint="MintABC", symbol="ABC"))
    _prime_history(trader, "MintABC", [1.0] * 15)
    client.prices["MintABC"] = 1.20

    trader.scan_and_buy()

    assert store.has_open_position("MintABC") is True


def test_dedupe_copycats_keeps_highest_liquidity(config):
    store = PositionStore(config.db_path)
    client = FakeClient({})
    trader = Trader(client, config, store)
    trader.safety = MagicMock()
    trader.safety.get_liquidity_usd.side_effect = lambda mint: {"MintA": 5000.0, "MintB": 50000.0}[mint]

    candidates = [
        {"mint": "MintA", "symbol": "GTA6", "pct_change": 20.0, "is_pump": True, "price": 1.0},
        {"mint": "MintB", "symbol": "GTA6", "pct_change": 18.0, "is_pump": True, "price": 1.0},
    ]

    result = trader._dedupe_copycats(candidates)

    assert [r["mint"] for r in result] == ["MintB"]


def test_dedupe_copycats_leaves_unrelated_symbols_alone(config):
    store = PositionStore(config.db_path)
    client = FakeClient({})
    trader = Trader(client, config, store)

    candidates = [
        {"mint": "MintA", "symbol": "DOGE", "pct_change": 20.0, "is_pump": True, "price": 1.0},
        {"mint": "MintB", "symbol": "PEPE", "pct_change": 18.0, "is_pump": True, "price": 1.0},
    ]

    result = trader._dedupe_copycats(candidates)

    assert len(result) == 2


def test_scan_and_buy_only_buys_best_liquidity_copycat(config):
    store = PositionStore(config.db_path)
    client = FakeClient({"MintA": 1.0, "MintB": 1.0})
    trader = Trader(client, config, store)
    trader.safety = MagicMock()
    trader.safety.check.return_value = (True, "", {})
    trader.safety.get_liquidity_usd.side_effect = lambda mint: {"MintA": 5000.0, "MintB": 50000.0}[mint]
    trader.add_discovered_token(Token(mint="MintA", symbol="GTA6"))
    trader.add_discovered_token(Token(mint="MintB", symbol="GTA6"))
    _prime_history(trader, "MintA", [1.0] * 15)
    _prime_history(trader, "MintB", [1.0] * 15)
    client.prices["MintA"] = 1.20
    client.prices["MintB"] = 1.20

    trader.scan_and_buy()

    assert store.has_open_position("MintA") is False
    assert store.has_open_position("MintB") is True


def test_scan_and_buy_sizes_position_by_conviction(config):
    store = PositionStore(config.db_path)
    client = FakeClient({"MintABC": 1.0})
    config.enable_conviction_sizing = True
    config.enable_partial_exit = False
    trader = Trader(client, config, store)
    trader.safety = MagicMock()
    trader.safety.check.return_value = (True, "", {"liquidity_usd": 50000.0})
    trader.add_discovered_token(Token(mint="MintABC", symbol="ABC"))
    _prime_history(trader, "MintABC", [1.0] * 15)
    client.prices["MintABC"] = 1.20

    trader.scan_and_buy()

    position = store.get_open_positions()[0]
    assert position["usd_size"] > config.position_size_usd  # comfortable liquidity margin sizes up


def test_scan_and_buy_splits_into_oco_and_trailing_legs_when_partial_exit_enabled(config):
    store = PositionStore(config.db_path)
    client = FakeClient({"MintABC": 1.0})
    config.enable_partial_exit = True
    config.partial_exit_pct = 50
    trader = Trader(client, config, store)
    trader.add_discovered_token(Token(mint="MintABC", symbol="ABC"))
    _prime_history(trader, "MintABC", [1.0] * 15)
    client.prices["MintABC"] = 1.20

    trader.scan_and_buy()

    positions = store.get_open_positions()
    assert len(positions) == 2
    assert sorted(p["exit_style"] for p in positions) == ["oco", "trailing"]
    assert sum(p["usd_size"] for p in positions) == pytest.approx(config.position_size_usd)


def test_scan_and_buy_keeps_single_position_when_partial_exit_disabled(config):
    store = PositionStore(config.db_path)
    client = FakeClient({"MintABC": 1.0})
    config.enable_partial_exit = False
    trader = Trader(client, config, store)
    trader.add_discovered_token(Token(mint="MintABC", symbol="ABC"))
    _prime_history(trader, "MintABC", [1.0] * 15)
    client.prices["MintABC"] = 1.20

    trader.scan_and_buy()

    positions = store.get_open_positions()
    assert len(positions) == 1
    assert positions[0]["exit_style"] == "oco"


def test_manage_open_positions_closes_on_trailing_stop(config):
    store = PositionStore(config.db_path)
    store.open_position("MintABC", "ABC", 1.0, 100.0, 100.0, exit_style="trailing")
    config.trailing_stop_bps = 500
    client = FakeClient({"MintABC": 1.5})
    trader = Trader(client, config, store)
    trader.manage_open_positions()  # observes the peak at 1.5

    client.prices["MintABC"] = 1.5 * 0.94  # 6% down from peak, past the 5% trailing distance
    trader.manage_open_positions()

    assert store.has_open_position("MintABC") is False
    closed = store.get_closed_positions()
    assert closed[0]["exit_reason"] == "trailing_stop"


def test_manage_open_positions_updates_peak_price_as_price_rises(config):
    store = PositionStore(config.db_path)
    pid = store.open_position("MintABC", "ABC", 1.0, 100.0, 100.0, exit_style="trailing")
    client = FakeClient({"MintABC": 1.3})
    trader = Trader(client, config, store)

    trader.manage_open_positions()

    position = store.get_position(pid)
    assert position["peak_price"] == pytest.approx(1.3)


def test_manage_open_positions_holds_trailing_position_within_trailing_distance(config):
    store = PositionStore(config.db_path)
    store.open_position("MintABC", "ABC", 1.0, 100.0, 100.0, exit_style="trailing")
    config.trailing_stop_bps = 500
    client = FakeClient({"MintABC": 1.5})
    trader = Trader(client, config, store)
    trader.manage_open_positions()  # sets peak to 1.5

    client.prices["MintABC"] = 1.47  # within the 5% trailing distance from peak
    trader.manage_open_positions()

    assert store.has_open_position("MintABC") is True


def test_manage_open_positions_closes_on_filled_trailing_trigger_order(config):
    store = PositionStore(config.db_path)
    pid = store.open_position("MintABC", "ABC", 1.0, 100.0, 100.0, exit_style="trailing")
    store.set_trigger_order_id(pid, "order-MintABC")
    client = FakeClient({"MintABC": 1.45})
    trigger_client = FakeTriggerClient()
    trigger_client.set_status("order-MintABC", "filled")
    config.dry_run = False
    trader = Trader(client, config, store, trigger_client=trigger_client)

    trader.manage_open_positions()

    assert store.has_open_position("MintABC") is False
    closed = store.get_closed_positions()
    assert closed[0]["exit_reason"] == "trailing_stop"
    assert closed[0]["exit_price"] == pytest.approx(1.45)


def test_scan_and_buy_skips_all_buys_when_daily_loss_limit_hit(config):
    store = PositionStore(config.db_path)
    pid = store.open_position("MintLoss", "LOSS", 1.0, 100.0, 100.0)
    store.close_position(pid, 0.4, "stop_loss")  # -$60 realized loss today
    config.daily_loss_limit_usd = 50.0
    client = FakeClient({"MintABC": 1.0})
    trader = Trader(client, config, store)
    trader.add_discovered_token(Token(mint="MintABC", symbol="ABC"))
    _prime_history(trader, "MintABC", [1.0] * 15)
    client.prices["MintABC"] = 1.20

    trader.scan_and_buy()

    assert store.has_open_position("MintABC") is False


def test_scan_and_buy_skips_all_buys_during_losing_streak_cooldown(config):
    store = PositionStore(config.db_path)
    for i in range(3):
        pid = store.open_position(f"MintLoss{i}", "LOSS", 1.0, 100.0, 100.0)
        store.close_position(pid, 0.97, "stop_loss")
    config.losing_streak_count = 3
    config.losing_streak_cooldown_minutes = 20
    client = FakeClient({"MintABC": 1.0})
    trader = Trader(client, config, store)
    trader.add_discovered_token(Token(mint="MintABC", symbol="ABC"))
    _prime_history(trader, "MintABC", [1.0] * 15)
    client.prices["MintABC"] = 1.20

    trader.scan_and_buy()

    assert store.has_open_position("MintABC") is False


def test_scan_and_buy_allows_buys_when_losing_streak_below_threshold(config):
    store = PositionStore(config.db_path)
    for i in range(2):
        pid = store.open_position(f"MintLoss{i}", "LOSS", 1.0, 100.0, 100.0)
        store.close_position(pid, 0.97, "stop_loss")
    config.losing_streak_count = 3
    client = FakeClient({"MintABC": 1.0})
    trader = Trader(client, config, store)
    trader.add_discovered_token(Token(mint="MintABC", symbol="ABC"))
    _prime_history(trader, "MintABC", [1.0] * 15)
    client.prices["MintABC"] = 1.20

    trader.scan_and_buy()

    assert store.has_open_position("MintABC") is True


def test_scan_and_buy_skips_existing_position(config):
    store = PositionStore(config.db_path)
    client = FakeClient({"MintABC": 1.0})
    trader = Trader(client, config, store)
    trader.add_discovered_token(Token(mint="MintABC", symbol="ABC"))
    _prime_history(trader, "MintABC", [1.0] * 15)
    client.prices["MintABC"] = 1.20

    trader.scan_and_buy()
    first_count = len(store.get_open_positions())
    trader.scan_and_buy()
    second_count = len(store.get_open_positions())

    assert first_count > 0
    assert second_count == first_count  # second scan didn't add any more positions for the same mint


def test_add_discovered_token_ignores_duplicates(config):
    store = PositionStore(config.db_path)
    client = FakeClient({})
    trader = Trader(client, config, store)

    trader.add_discovered_token(Token(mint="MintABC", symbol="ABC"))
    first_seen = trader.watchlist["MintABC"]["first_seen"]
    trader.add_discovered_token(Token(mint="MintABC", symbol="ABC"))

    assert len(trader.watchlist) == 1
    assert trader.watchlist["MintABC"]["first_seen"] == first_seen


def test_add_discovered_token_expires_by_age_not_count(config):
    # pump_window_minutes=15 -> max age is 60 minutes (4x multiplier)
    store = PositionStore(config.db_path)
    client = FakeClient({})
    trader = Trader(client, config, store)

    old_mint = "MintOld"
    trader.watchlist[old_mint] = {"symbol": "OLD", "first_seen": time.time() - 61 * 60}
    trader.price_history[old_mint] = [(time.time() - 61 * 60, 1.0)]

    trader.add_discovered_token(Token(mint="MintNew", symbol="NEW"))

    assert old_mint not in trader.watchlist
    assert old_mint not in trader.price_history
    assert "MintNew" in trader.watchlist


def test_add_discovered_token_keeps_recent_token_even_when_many_others_added(config):
    store = PositionStore(config.db_path)
    client = FakeClient({})
    trader = Trader(client, config, store)

    trader.add_discovered_token(Token(mint="MintFirst", symbol="FIRST"))
    for i in range(50):
        trader.add_discovered_token(Token(mint=f"MintFlood{i}", symbol="FLOOD"))

    # still well within the window, should not have been evicted by the flood of new arrivals
    assert "MintFirst" in trader.watchlist


def test_manage_open_positions_closes_on_take_profit(config):
    store = PositionStore(config.db_path)
    store.open_position("MintABC", "ABC", 1.0, 100.0, 100.0)
    client = FakeClient({"MintABC": 1.09})
    trader = Trader(client, config, store)

    trader.manage_open_positions()

    assert store.has_open_position("MintABC") is False
    closed = store.get_closed_positions()
    assert closed[0]["exit_reason"] == "take_profit"


def test_manage_open_positions_closes_on_stop_loss(config):
    store = PositionStore(config.db_path)
    store.open_position("MintXYZ", "XYZ", 1.0, 100.0, 100.0)
    client = FakeClient({"MintXYZ": 0.96})
    trader = Trader(client, config, store)

    trader.manage_open_positions()

    assert store.has_open_position("MintXYZ") is False
    closed = store.get_closed_positions()
    assert closed[0]["exit_reason"] == "stop_loss"


def test_manage_open_positions_sells_before_closing_stale_position(config):
    import sqlite3

    store = PositionStore(config.db_path)
    config.max_position_hold_minutes = 1
    pid = store.open_position("MintOld", "OLD", 1.0, 100.0, 100.0)

    # Backdate entry_time so the position is older than the 1-minute hold limit,
    # but keep the price flat so neither take_profit nor stop_loss would trigger.
    conn = sqlite3.connect(config.db_path)
    conn.execute("UPDATE positions SET entry_time = ? WHERE id = ?", (int(time.time()) - 120, pid))
    conn.commit()
    conn.close()

    client = FakeClient({"MintOld": 1.0})
    config.dry_run = False  # must confirm a real sell is attempted, not skipped
    trader = Trader(client, config, store)

    trader.manage_open_positions()

    assert client.sells == [("MintOld", 100.0)]  # a real sell was attempted before closing
    assert store.has_open_position("MintOld") is False
    closed = store.get_closed_positions()
    assert closed[0]["exit_reason"] == "stale_timeout"
    assert closed[0]["exit_price"] == 1.0  # real current price recorded, not a hardcoded 0.0/-100%


class FakeTriggerClient:
    def __init__(self):
        self.placed = []
        self.cancelled = []
        self._status = {}

    def place_oco_exit_order(self, token_mint, trade_currency_mint, quantity, token_decimals, tp_price_usd, sl_price_usd, slippage_bps):
        order_id = f"order-{token_mint}"
        self.placed.append((token_mint, quantity, tp_price_usd, sl_price_usd))
        return order_id

    def set_status(self, order_id, order_state, tp_state="open", sl_state="open", tp_price_usd=None, sl_price_usd=None):
        self._status[order_id] = {
            "id": order_id,
            "orderState": order_state,
            "tpState": tp_state,
            "slState": sl_state,
            "tpPriceUsd": tp_price_usd,
            "slPriceUsd": sl_price_usd,
        }

    def get_order_status(self, order_id):
        return self._status.get(order_id)

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return True


def test_scan_and_buy_places_trigger_order_in_live_mode(config):
    store = PositionStore(config.db_path)
    client = FakeClient({"MintABC": 1.0})
    client.trade_mint = "USDC_MINT"
    trigger_client = FakeTriggerClient()
    config.dry_run = False
    trader = Trader(client, config, store, trigger_client=trigger_client)
    trader.add_discovered_token(Token(mint="MintABC", symbol="ABC"))
    _prime_history(trader, "MintABC", [1.0] * 15)
    client.prices["MintABC"] = 1.20

    trader.scan_and_buy()

    assert client.buys == [("MintABC", 100.0)]
    assert len(trigger_client.placed) == 1
    mint, quantity, tp, sl = trigger_client.placed[0]
    assert mint == "MintABC"
    assert tp == pytest.approx(1.20 * 1.08)
    assert sl == pytest.approx(1.20 * 0.97)

    position = store.get_open_positions()[0]
    assert position["trigger_order_id"] == "order-MintABC"


def test_manage_open_positions_closes_on_filled_trigger_order(config):
    store = PositionStore(config.db_path)
    pid = store.open_position("MintABC", "ABC", 1.0, 100.0, 100.0)
    store.set_trigger_order_id(pid, "order-MintABC")
    client = FakeClient({"MintABC": 1.08})
    trigger_client = FakeTriggerClient()
    trigger_client.set_status("order-MintABC", "open", tp_state="filled", tp_price_usd=1.08)
    config.dry_run = False
    trader = Trader(client, config, store, trigger_client=trigger_client)

    trader.manage_open_positions()

    assert store.has_open_position("MintABC") is False
    closed = store.get_closed_positions()
    assert closed[0]["exit_reason"] == "take_profit"
    assert closed[0]["exit_price"] == 1.08
    assert client.sells == []  # Jupiter executed the swap, we never sent our own


def test_manage_open_positions_skips_manual_check_while_trigger_order_open(config):
    store = PositionStore(config.db_path)
    pid = store.open_position("MintABC", "ABC", 1.0, 100.0, 100.0)
    store.set_trigger_order_id(pid, "order-MintABC")
    client = FakeClient({"MintABC": 0.50})  # would trip manual stop loss if we checked it
    trigger_client = FakeTriggerClient()
    trigger_client.set_status("order-MintABC", "open")
    config.dry_run = False
    trader = Trader(client, config, store, trigger_client=trigger_client)

    trader.manage_open_positions()

    assert store.has_open_position("MintABC") is True
    assert client.sells == []


def test_manage_open_positions_cancels_stale_trigger_order_and_falls_back_to_manual_sell(config):
    import sqlite3

    store = PositionStore(config.db_path)
    config.max_position_hold_minutes = 1
    pid = store.open_position("MintABC", "ABC", 1.0, 100.0, 100.0)
    store.set_trigger_order_id(pid, "order-MintABC")
    conn = sqlite3.connect(config.db_path)
    conn.execute("UPDATE positions SET entry_time = ? WHERE id = ?", (int(time.time()) - 120, pid))
    conn.commit()
    conn.close()

    client = FakeClient({"MintABC": 1.0})
    trigger_client = FakeTriggerClient()
    trigger_client.set_status("order-MintABC", "open")
    config.dry_run = False
    trader = Trader(client, config, store, trigger_client=trigger_client)

    trader.manage_open_positions()

    assert trigger_client.cancelled == ["order-MintABC"]
    assert client.sells == [("MintABC", 100.0)]
    closed = store.get_closed_positions()
    assert closed[0]["exit_reason"] == "stale_timeout"


def test_manage_open_positions_holds_when_within_range(config):
    store = PositionStore(config.db_path)
    store.open_position("MintSOL", "SOL2", 1.0, 100.0, 100.0)
    client = FakeClient({"MintSOL": 1.02})
    trader = Trader(client, config, store)

    trader.manage_open_positions()

    assert store.has_open_position("MintSOL") is True


def test_manage_open_positions_returns_false_false_when_no_positions(config):
    store = PositionStore(config.db_path)
    client = FakeClient({})
    trader = Trader(client, config, store)

    has_open, needs_fast_poll = trader.manage_open_positions()

    assert has_open is False
    assert needs_fast_poll is False


def test_manage_open_positions_needs_fast_poll_for_young_position(config):
    store = PositionStore(config.db_path)
    store.open_position("MintABC", "ABC", 1.0, 100.0, 100.0)
    config.fast_poll_window_seconds = 120
    client = FakeClient({"MintABC": 1.0})
    trader = Trader(client, config, store)

    has_open, needs_fast_poll = trader.manage_open_positions()

    assert has_open is True
    assert needs_fast_poll is True


def test_manage_open_positions_no_fast_poll_for_older_position(config):
    import sqlite3

    store = PositionStore(config.db_path)
    pid = store.open_position("MintABC", "ABC", 1.0, 100.0, 100.0)
    config.fast_poll_window_seconds = 120
    conn = sqlite3.connect(config.db_path)
    conn.execute("UPDATE positions SET entry_time = ? WHERE id = ?", (int(time.time()) - 300, pid))
    conn.commit()
    conn.close()
    client = FakeClient({"MintABC": 1.0})
    trader = Trader(client, config, store)

    has_open, needs_fast_poll = trader.manage_open_positions()

    assert has_open is True
    assert needs_fast_poll is False


def test_scan_and_buy_updates_market_state(config):
    store = PositionStore(config.db_path)
    client = FakeClient({"MintABC": 1.0, "MintDEF": 1.0})
    market_state = MarketState()
    trader = Trader(client, config, store, market_state)
    trader.add_discovered_token(Token(mint="MintABC", symbol="ABC"))
    trader.add_discovered_token(Token(mint="MintDEF", symbol="DEF"))
    _prime_history(trader, "MintABC", [1.0] * 15)
    _prime_history(trader, "MintDEF", [1.0] * 15)
    client.prices["MintABC"] = 1.20
    client.prices["MintDEF"] = 1.02

    trader.scan_and_buy()
    snapshot = market_state.snapshot()

    assert snapshot["tokens_watched"] == 2
    top_symbols = [m["symbol"] for m in snapshot["top_movers"]]
    assert top_symbols[0] == "ABC"


def test_save_and_load_state_round_trips(config, tmp_path):
    store = PositionStore(config.db_path)
    client = FakeClient({})
    trader = Trader(client, config, store)
    trader.add_discovered_token(Token(mint="MintABC", symbol="ABC"))
    _prime_history(trader, "MintABC", [1.0, 1.05, 1.10])

    state_path = os.path.join(tmp_path, "state.json")
    trader.save_state(state_path)

    restored = Trader(client, config, store)
    restored.load_state(state_path)

    assert "MintABC" in restored.watchlist
    assert restored.watchlist["MintABC"]["symbol"] == "ABC"
    assert len(restored.price_history["MintABC"]) == 3
    assert restored.price_history["MintABC"][-1][1] == 1.10


def test_load_state_drops_entries_older_than_max_age(config, tmp_path):
    state_path = os.path.join(tmp_path, "state.json")
    stale_ts = time.time() - (config.pump_window_minutes * 60 * 4) - 60  # past the 4x max-age window
    with open(state_path, "w") as f:
        json.dump(
            {
                "watchlist": {"MintOld": {"symbol": "OLD", "first_seen": stale_ts}},
                "price_history": {"MintOld": [[stale_ts, 1.0]]},
            },
            f,
        )

    store = PositionStore(config.db_path)
    client = FakeClient({})
    trader = Trader(client, config, store)
    trader.load_state(state_path)

    assert "MintOld" not in trader.watchlist
    assert "MintOld" not in trader.price_history


def test_load_state_missing_file_is_a_noop(config):
    store = PositionStore(config.db_path)
    client = FakeClient({})
    trader = Trader(client, config, store)

    trader.load_state("/nonexistent/path/state.json")  # should not raise

    assert trader.watchlist == {}
