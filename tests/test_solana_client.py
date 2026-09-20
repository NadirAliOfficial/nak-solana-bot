from unittest.mock import MagicMock, patch

from src.solana_client import SOL_MINT, USDC_MINT, SolanaClient


def _http_response(status_code=200, json_data=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    return resp


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
    client.get_sol_price_usd = MagicMock(return_value=97.08)
    client.swap = MagicMock(return_value="sig")

    client.buy("SomeMint", usd_amount=100.0, price_usd=0.01, slippage_bps=300)

    args = client.swap.call_args[0]
    amount_atomic = args[2]
    expected_sol = 100.0 / 97.08
    assert amount_atomic == int(expected_sol * 1_000_000_000)


def test_trade_currency_balance_usd_for_usdc_is_face_value():
    client = _make_client("USDC")
    client.get_trade_currency_balance = MagicMock(return_value=48.75)

    assert client.get_trade_currency_balance_usd() == 48.75


def test_trade_currency_balance_usd_for_sol_converts_through_price():
    client = _make_client("SOL")
    client.get_trade_currency_balance = MagicMock(return_value=3.09)
    client.get_sol_price_usd = MagicMock(return_value=97.08)

    assert client.get_trade_currency_balance_usd() == 3.09 * 97.08


def test_get_sol_price_usd_uses_jupiter_when_available():
    client = _make_client("SOL")
    client.get_prices_usd = MagicMock(return_value={SOL_MINT: 97.08})
    client._http = MagicMock()

    assert client.get_sol_price_usd() == 97.08
    client._http.get.assert_not_called()  # never fell back to Binance/CoinGecko


def test_get_sol_price_usd_falls_back_to_binance_when_jupiter_fails():
    client = _make_client("SOL")
    client.get_prices_usd = MagicMock(side_effect=Exception("jupiter down"))
    client._http = MagicMock()
    client._http.get = MagicMock(return_value=_http_response(200, {"price": "101.5"}))

    assert client.get_sol_price_usd() == 101.5


def test_get_sol_price_usd_falls_back_to_coingecko_when_jupiter_and_binance_fail():
    client = _make_client("SOL")
    client.get_prices_usd = MagicMock(side_effect=Exception("jupiter down"))
    client._http = MagicMock()
    client._http.get = MagicMock(
        side_effect=[
            Exception("binance down"),
            _http_response(200, {"solana": {"usd": 99.2}}),
        ]
    )

    assert client.get_sol_price_usd() == 99.2


def test_get_sol_price_usd_uses_cached_price_when_all_sources_fail():
    client = _make_client("SOL")
    client._last_sol_price = 88.0
    client.get_prices_usd = MagicMock(side_effect=Exception("jupiter down"))
    client._http = MagicMock()
    client._http.get = MagicMock(side_effect=Exception("network down"))

    assert client.get_sol_price_usd() == 88.0


def test_get_sol_price_usd_defaults_to_100_when_all_sources_fail_and_no_cache():
    client = _make_client("SOL")
    client.get_prices_usd = MagicMock(side_effect=Exception("jupiter down"))
    client._http = MagicMock()
    client._http.get = MagicMock(side_effect=Exception("network down"))

    assert client.get_sol_price_usd() == 100.0


def test_buy_sizing_for_sol_raises_if_price_unavailable():
    client = _make_client("SOL")
    client.get_sol_price_usd = MagicMock(return_value=0.0)
    client.swap = MagicMock(return_value="sig")

    try:
        client.buy("SomeMint", usd_amount=100.0, price_usd=0.01, slippage_bps=300)
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass


def test_get_fast_prices_usd_parses_dexscreener_response():
    client = _make_client("USDC")
    client._http = MagicMock()
    client._dexscreener_rate_limiter = MagicMock()
    client._http.get.return_value = _http_response(
        200,
        {"pairs": [{"baseToken": {"address": "MintABC"}, "priceUsd": "1.23"}]},
    )

    prices = client.get_fast_prices_usd(["MintABC"])

    assert prices == {"MintABC": 1.23}


def test_get_fast_prices_usd_rate_limits_every_chunk():
    client = _make_client("USDC")
    client._http = MagicMock()
    client._dexscreener_rate_limiter = MagicMock()
    client._http.get.return_value = _http_response(200, {"pairs": []})
    client.get_prices_usd = MagicMock(return_value={})  # avoid the fallback lookup path

    client.get_fast_prices_usd(["MintABC"])

    client._dexscreener_rate_limiter.wait.assert_called_once()


def test_get_fast_prices_usd_logs_warning_on_non_200_instead_of_silent_failure():
    client = _make_client("USDC")
    client._http = MagicMock()
    client._dexscreener_rate_limiter = MagicMock()
    client._http.get.return_value = _http_response(429, {})
    client.get_prices_usd = MagicMock(return_value={})  # fallback also finds nothing

    with patch("src.solana_client.logger") as mock_logger:
        prices = client.get_fast_prices_usd(["MintABC"])

    assert prices == {}
    mock_logger.warning.assert_called_once()
    assert "429" in mock_logger.warning.call_args[0][0]


def test_get_fast_prices_usd_falls_back_to_standard_lookup_when_missing():
    client = _make_client("USDC")
    client._http = MagicMock()
    client._dexscreener_rate_limiter = MagicMock()
    client._http.get.return_value = _http_response(200, {"pairs": []})
    client.get_prices_usd = MagicMock(return_value={"MintABC": 9.99})

    prices = client.get_fast_prices_usd(["MintABC"])

    assert prices == {"MintABC": 9.99}
    client.get_prices_usd.assert_called_once_with(["MintABC"])
