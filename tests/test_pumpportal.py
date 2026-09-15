import json

from src.pumpportal import PumpPortalListener


def test_handle_message_calls_callback_with_token():
    received = []
    listener = PumpPortalListener(on_new_token=received.append)

    raw = json.dumps({"mint": "MintABC", "symbol": "ABC", "name": "A Coin", "txType": "create"})
    listener._handle_message(raw)

    assert len(received) == 1
    assert received[0].mint == "MintABC"
    assert received[0].symbol == "ABC"
    assert received[0].name == "A Coin"


def test_handle_message_ignores_non_token_messages():
    received = []
    listener = PumpPortalListener(on_new_token=received.append)

    listener._handle_message(json.dumps({"message": "Successfully subscribed to token creation events."}))

    assert received == []


def test_handle_message_defaults_missing_symbol_and_name():
    received = []
    listener = PumpPortalListener(on_new_token=received.append)

    listener._handle_message(json.dumps({"mint": "MintXYZ", "txType": "create"}))

    assert received[0].symbol == "?"
    assert received[0].name == ""


def test_handle_message_ignores_invalid_json():
    received = []
    listener = PumpPortalListener(on_new_token=received.append)

    listener._handle_message("not json")

    assert received == []


def test_handle_message_survives_callback_exception():
    def bad_callback(token):
        raise ValueError("boom")

    listener = PumpPortalListener(on_new_token=bad_callback)
    listener._handle_message(json.dumps({"mint": "MintABC", "symbol": "ABC"}))  # should not raise
