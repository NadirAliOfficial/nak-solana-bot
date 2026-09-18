import os

import pytest

from src.positions import PositionStore


@pytest.fixture
def store(tmp_path):
    db_path = os.path.join(tmp_path, "test.db")
    return PositionStore(db_path)


def test_open_and_get_position(store):
    position_id = store.open_position("MintABC123", "DOGE2", 0.001, 100000, 100.0)
    assert store.has_open_position("MintABC123") is True

    open_positions = store.get_open_positions()
    assert len(open_positions) == 1
    assert open_positions[0]["token_symbol"] == "DOGE2"
    assert open_positions[0]["id"] == position_id


def test_close_position_computes_pnl(store):
    position_id = store.open_position("MintXYZ789", "PEPE3", 0.0002, 500000, 100.0)
    store.close_position(position_id, 0.000216, "take_profit")

    assert store.has_open_position("MintXYZ789") is False
    closed = store.get_closed_positions()
    assert len(closed) == 1
    assert closed[0]["exit_reason"] == "take_profit"
    assert closed[0]["pnl_usd"] == pytest.approx(8.0)
    assert closed[0]["pnl_pct"] == pytest.approx(8.0)


def test_set_trigger_order_id(store):
    position_id = store.open_position("MintABC123", "DOGE2", 0.001, 100000, 100.0)
    store.set_trigger_order_id(position_id, "jupiter-order-1")

    position = store.get_position(position_id)
    assert position["trigger_order_id"] == "jupiter-order-1"


def test_no_duplicate_open_position_for_same_mint(store):
    store.open_position("MintSOL111", "WOJAK", 0.05, 2000, 100.0)
    assert store.has_open_position("MintSOL111") is True
    assert store.has_open_position("MintOther222") is False


def test_has_recent_position_cooldown(store):
    import time
    pid = store.open_position("MintCooldown", "COOL", 1.0, 100.0, 100.0)
    assert store.has_recent_position("MintCooldown", cooldown_seconds=1800) is True

    store.close_position(pid, 1.05, "take_profit")
    # Position is closed, but within 1800s cooldown
    assert store.has_recent_position("MintCooldown", cooldown_seconds=1800) is True

    # With a 0-second cooldown (in the past), exit_time >= now - 0 might be equal or false if sleep
    time.sleep(1)
    assert store.has_recent_position("MintCooldown", cooldown_seconds=0) is False




def test_closed_and_todays_stats(store):
    import time
    p1 = store.open_position("M1", "M1", 1.0, 100.0, 100.0)
    store.close_position(p1, 1.10, "take_profit")  # +$10

    p2 = store.open_position("M2", "M2", 1.0, 100.0, 100.0)
    store.close_position(p2, 0.95, "stop_loss")   # -$5

    stats = store.get_closed_stats()
    assert stats["total_count"] == 2
    assert stats["total_pnl"] == pytest.approx(5.0)
    assert stats["wins"] == 1

    todays = store.get_todays_stats(start_of_day_ts=time.time() - 3600)
    assert todays["today_count"] == 2
    assert todays["today_pnl"] == pytest.approx(5.0)
