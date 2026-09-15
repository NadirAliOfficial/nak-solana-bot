import pytest

from src.risk import (
    pnl_pct,
    pnl_usd,
    should_stop_loss,
    should_take_profit,
    stop_loss_price,
    take_profit_price,
)


def test_take_profit_price():
    assert take_profit_price(1.0, 8) == 1.08


def test_stop_loss_price():
    assert stop_loss_price(1.0, 3) == 0.97


def test_should_take_profit_true_at_threshold():
    assert should_take_profit(1.08, 1.0, 8) is True


def test_should_take_profit_false_below_threshold():
    assert should_take_profit(1.07, 1.0, 8) is False


def test_should_stop_loss_true_at_threshold():
    assert should_stop_loss(0.97, 1.0, 3) is True


def test_should_stop_loss_false_above_threshold():
    assert should_stop_loss(0.98, 1.0, 3) is False


def test_pnl_usd_and_pct():
    assert pnl_usd(1.0, 1.08, 100) == pytest.approx(8.0)
    assert pnl_pct(1.0, 1.08) == pytest.approx(8.0)
