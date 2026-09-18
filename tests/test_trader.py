import json
import os
import time

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

    assert first_count == 1
    assert second_count == 1


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


def test_manage_open_positions_holds_when_within_range(config):
    store = PositionStore(config.db_path)
    store.open_position("MintSOL", "SOL2", 1.0, 100.0, 100.0)
    client = FakeClient({"MintSOL": 1.02})
    trader = Trader(client, config, store)

    trader.manage_open_positions()

    assert store.has_open_position("MintSOL") is True


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
