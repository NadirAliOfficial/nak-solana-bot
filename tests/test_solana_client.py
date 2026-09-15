from unittest.mock import MagicMock

from src.solana_client import SOL_MINT, USDC_MINT, SolanaClient


def _make_client(trade_currency):
    client = SolanaClient.__new__(SolanaClient)
    client.trade_mint = USDC_MINT if trade_currency == "USDC" else SOL_MINT
    client.trade_mint_decimals = 6 if trade_currency == "USDC" else 9
    return client


def test_get_trade_currency_balance_uses_native_sol_for_sol():
    client = _make_client("SOL")
    client.get_sol_balance = MagicMock(return_value=3.09)
    client.get_token_balance = MagicMock(return_value=0.0)

    assert client.get_trade_currency_balance() == 3.09
    client.get_token_balance.assert_not_called()


def test_get_trade_currency_balance_uses_token_account_for_usdc():
    client = _make_client("USDC")
    client.get_token_balance = MagicMock(return_value=48.75)

    assert client.get_trade_currency_balance() == 48.75
    client.get_token_balance.assert_called_once_with(USDC_MINT)


def test_buy_sizing_for_usdc_assumes_one_dollar_peg():
    client = _make_client("USDC")
    client.swap = MagicMock(return_value="sig")

    client.buy("SomeMint", usd_amount=100.0, price_usd=0.01, slippage_bps=300)

    args = client.swap.call_args[0]
    amount_atomic = args[2]
    assert amount_atomic == 100_000_000  # 100 USDC at 6 decimals


def test_buy_sizing_for_sol_converts_through_live_price():
    client = _make_client("SOL")
    client.get_prices_usd = MagicMock(return_value={SOL_MINT: 97.08})
    client.swap = MagicMock(return_value="sig")

    client.buy("SomeMint", usd_amount=100.0, price_usd=0.01, slippage_bps=300)

    args = client.swap.call_args[0]
    amount_atomic = args[2]
    expected_sol = 100.0 / 97.08
    assert amount_atomic == int(expected_sol * 1_000_000_000)


def test_buy_sizing_for_sol_raises_if_price_unavailable():
    client = _make_client("SOL")
    client.get_prices_usd = MagicMock(return_value={})
    client.swap = MagicMock(return_value="sig")

    try:
        client.buy("SomeMint", usd_amount=100.0, price_usd=0.01, slippage_bps=300)
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass
