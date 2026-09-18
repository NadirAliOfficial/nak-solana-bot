from unittest.mock import MagicMock, patch

from src.jupiter_trigger import JupiterTriggerClient


def _resp(json_data):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json.return_value = json_data
    return r


def _make_client():
    keypair = MagicMock()
    keypair.pubkey.return_value = "Wallet111"
    keypair.sign_message.return_value = "sig-bytes"
    rpc = MagicMock()
    rpc.send_raw_transaction.return_value = MagicMock(value="tx-sig")
    client = JupiterTriggerClient(rpc, keypair)
    client._http = MagicMock()
    return client


def test_authenticate_caches_jwt():
    client = _make_client()
    client._http.post.side_effect = [
        _resp({"challenge": "abc123"}),
        _resp({"token": "jwt-token"}),
    ]

    headers = client._auth_headers()

    assert headers == {"Authorization": "Bearer jwt-token"}
    assert client._http.post.call_count == 2

    # second call should reuse the cached JWT, not re-authenticate
    client._auth_headers()
    assert client._http.post.call_count == 2


@patch("src.jupiter_trigger.VersionedTransaction")
def test_place_oco_exit_order_returns_order_id(mock_vtx):
    client = _make_client()
    mock_vtx.from_bytes.return_value = MagicMock(message="msg")
    mock_vtx.return_value = MagicMock()

    client._http.post.side_effect = [
        _resp({"challenge": "abc123"}),
        _resp({"token": "jwt-token"}),
        _resp({"requestId": "req-1", "transaction": "dGVzdA=="}),
        _resp({"id": "order-1", "txSignature": "sig", "depositConfirmed": True}),
    ]

    order_id = client.place_oco_exit_order(
        token_mint="MintABC",
        trade_currency_mint="USDC",
        quantity=100.0,
        token_decimals=6,
        tp_price_usd=1.08,
        sl_price_usd=0.97,
        slippage_bps=300,
    )

    assert order_id == "order-1"


def test_get_order_status_finds_matching_order():
    client = _make_client()
    client._jwt = "jwt-token"
    client._jwt_expiry = 9999999999
    client._http.get.return_value = _resp({"orders": [{"id": "order-1", "state": "filled"}]})

    status = client.get_order_status("order-1")

    assert status["state"] == "filled"


def test_get_order_status_returns_none_when_not_found():
    client = _make_client()
    client._jwt = "jwt-token"
    client._jwt_expiry = 9999999999
    client._http.get.return_value = _resp({"orders": []})

    assert client.get_order_status("missing-order") is None
