import os

import pytest

from src.config import Config
from src.dashboard import _todays_pnl, create_app
from src.market_state import MarketState
from src.positions import PositionStore


class FakePriceClient:
    def __init__(self, prices, balance=None, sol_balance=None):
        self.prices = prices
        self.balance = balance
        self.sol_balance = sol_balance

    def get_prices_usd(self, mints):
        return {m: self.prices[m] for m in mints if m in self.prices}

    def get_trade_currency_balance_usd(self):
        return self.balance

    def get_sol_balance(self):
        return self.sol_balance


@pytest.fixture
def store(tmp_path):
    return PositionStore(os.path.join(tmp_path, "test.db"))


def test_index_renders_with_no_data(store):
    app = create_app(store, client=None, config=Config())
    resp = app.test_client().get("/")
    assert resp.status_code == 200
    assert b"Solana 369 System" in resp.data
    assert b"No open positions" in resp.data
    assert b"No closed trades yet" in resp.data


def test_index_renders_open_and_closed_positions(store):
    store.open_position("MintABC", "ABC", 1.0, 100.0, 100.0)
    pid = store.open_position("MintDEF", "DEF", 0.5, 200.0, 100.0)
    store.close_position(pid, 0.54, "take_profit")

    fake_client = FakePriceClient({"MintABC": 1.08})
    app = create_app(store, client=fake_client, config=Config())
    resp = app.test_client().get("/")

    assert resp.status_code == 200
    assert b"ABC" in resp.data
    assert b"DEF" in resp.data
    assert b"Take profit" in resp.data


def test_closed_table_labels_trailing_stop_distinctly_from_stop_loss(store):
    pid = store.open_position("MintABC", "ABC", 1.0, 100.0, 100.0, exit_style="trailing")
    store.close_position(pid, 0.97, "trailing_stop")
    pid2 = store.open_position("MintDEF", "DEF", 1.0, 100.0, 100.0)
    store.close_position(pid2, 0.97, "stop_loss")

    app = create_app(store, client=None, config=Config())
    resp = app.test_client().get("/")
    html = resp.data.decode("utf-8")

    assert "Trailing stop" in html
    assert "Stop loss" in html


def test_closed_table_colors_profitable_trailing_stop_as_positive(store):
    pid = store.open_position("MintABC", "ABC", 1.0, 100.0, 100.0, exit_style="trailing")
    store.close_position(pid, 1.05, "trailing_stop")  # exited above entry - a captured gain, not a loss

    app = create_app(store, client=None, config=Config())
    resp = app.test_client().get("/")
    html = resp.data.decode("utf-8")

    assert "Trailing stop" in html
    assert '<span class="tag tag-tp">' in html
    assert '<span class="tag tag-sl">' not in html


def test_api_render_returns_fragments(store):
    store.open_position("MintABC", "ABC", 1.0, 100.0, 100.0)
    app = create_app(store, client=None, config=Config())
    resp = app.test_client().get("/api/render")

    data = resp.get_json()
    assert "stats_html" in data
    assert "ABC" in data["open_table_html"]


def test_api_positions_unchanged(store):
    store.open_position("MintABC", "ABC", 1.0, 100.0, 100.0)
    app = create_app(store, client=None, config=Config())
    resp = app.test_client().get("/api/positions")

    data = resp.get_json()
    assert len(data["open"]) == 1
    assert data["open"][0]["token_symbol"] == "ABC"


def test_dashboard_requires_auth_when_configured(store):
    cfg = Config()
    cfg.dashboard_access_key = "secret123"
    app = create_app(store, client=None, config=cfg)
    client = app.test_client()

    resp = client.get("/")
    assert resp.status_code == 401

    resp = client.get("/?key=wrong")
    assert resp.status_code == 401

    resp = client.get("/?key=secret123")
    assert resp.status_code == 200
    assert resp.headers.get("Set-Cookie") and "key=secret123" in resp.headers["Set-Cookie"]

    # cookie set by the previous request lets a bare visit through without the URL key
    resp = client.get("/")
    assert resp.status_code == 200


def test_index_shows_balances(store):
    fake_client = FakePriceClient({}, balance=48.75, sol_balance=1.234)
    app = create_app(store, client=fake_client, config=Config())
    resp = app.test_client().get("/")

    assert b"48.75" in resp.data
    assert b"1.234 SOL" in resp.data


def test_index_shows_paper_balance_when_no_real_wallet(store):
    pid = store.open_position("MintX", "X", 1.0, 100.0, 100.0)
    store.close_position(pid, 1.08, "take_profit")  # +$8 realized

    fake_client = FakePriceClient({}, balance=None)
    config = Config()
    config.dry_run = True
    config.dry_run_paper_balance_usd = 300.0
    app = create_app(store, client=fake_client, config=config)
    resp = app.test_client().get("/")
    html = resp.data.decode("utf-8")

    assert "$308.00" in html  # 300 base + 8 realized pnl
    assert "unable to fetch balance" not in html


def test_index_shows_unable_to_fetch_when_no_wallet_and_no_paper_balance(store):
    fake_client = FakePriceClient({}, balance=None)
    config = Config()
    config.dry_run = True
    config.dry_run_paper_balance_usd = 0
    app = create_app(store, client=fake_client, config=config)
    resp = app.test_client().get("/")
    html = resp.data.decode("utf-8")

    assert "unable to fetch balance" in html


def test_top_movers_render_with_market_state(store):
    market_state = MarketState()
    market_state.update(
        top_movers=[
            {"mint": "MintABC1234567890", "symbol": "ABC", "pct_change": 16.5, "is_pump": True, "price": 1.2},
        ],
        tokens_watched=42,
        scan_seconds=3.4,
    )
    app = create_app(store, client=None, config=Config(), market_state=market_state)
    resp = app.test_client().get("/")

    assert b"ABC" in resp.data
    assert b"42 tokens watched" in resp.data


def test_todays_pnl_only_counts_trades_closed_today():
    import datetime as dt
    import time

    today_ts = int(time.time())
    yesterday_ts = int((dt.datetime.now() - dt.timedelta(days=1)).timestamp())

    closed = [
        {"exit_time": today_ts, "pnl_usd": 8.0},
        {"exit_time": yesterday_ts, "pnl_usd": 100.0},
    ]
    total, count = _todays_pnl(closed)
    assert count == 1
    assert total == 8.0


def test_copy_address_button_rendered_in_all_tables(store):
    store.open_position("MintOpen123456", "OPEN", 1.0, 100.0, 100.0)
    pid = store.open_position("MintClosed123456", "CLS", 0.5, 200.0, 100.0)
    store.close_position(pid, 0.54, "take_profit")

    market_state = MarketState()
    market_state.update(
        top_movers=[
            {"mint": "MintMover123456", "symbol": "MOVR", "pct_change": 20.0, "is_pump": True, "price": 1.5},
        ],
        tokens_watched=10,
        scan_seconds=1.2,
    )

    app = create_app(store, client=None, config=Config(), market_state=market_state)
    resp = app.test_client().get("/")
    html = resp.data.decode("utf-8")

    # verify open positions has copy button with mint and content_copy icon
    assert 'data-mint="MintOpen123456"' in html
    # verify closed positions has copy button with mint
    assert 'data-mint="MintClosed123456"' in html
    # verify top movers has copy button with mint
    assert 'data-mint="MintMover123456"' in html

    # verify copy button class, icon and script are included
    assert 'class="copy-btn"' in html
    assert "content_copy" in html
    assert "copyAddress" in html
