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


def test_no_duplicate_open_position_for_same_mint(store):
    store.open_position("MintSOL111", "WOJAK", 0.05, 2000, 100.0)
    assert store.has_open_position("MintSOL111") is True
    assert store.has_open_position("MintOther222") is False
