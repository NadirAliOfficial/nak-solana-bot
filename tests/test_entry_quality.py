from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.entry_quality import EntryQualityChecker


def _config(**overrides):
    base = dict(
        enable_volume_confirmation=True,
        min_recent_volume_usd=2000.0,
        volume_surge_multiplier=1.5,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _http_response(status_code=200, json_data=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    return resp


def _make_checker():
    checker = EntryQualityChecker()
    checker._http = MagicMock()
    return checker


def test_check_disabled_always_passes():
    checker = _make_checker()
    passed, reason = checker.check("SomeMint", _config(enable_volume_confirmation=False))

    assert passed is True
    assert reason == ""


def test_check_fails_when_volume_lookup_fails():
    checker = _make_checker()
    checker.get_volume_usd = MagicMock(return_value=None)

    passed, reason = checker.check("SomeMint", _config())

    assert passed is False
    assert reason == "volume_unknown"


def test_check_fails_on_low_absolute_volume():
    checker = _make_checker()
    checker.get_volume_usd = MagicMock(return_value={"m5": 500.0, "h1": 6000.0})

    passed, reason = checker.check("SomeMint", _config(min_recent_volume_usd=2000.0))

    assert passed is False
    assert "volume" in reason


def test_check_fails_when_volume_not_surging():
    checker = _make_checker()
    # h1=30000 implies a steady 2500/5min baseline rate; m5=3000 clears the absolute
    # floor but isn't 2x that baseline, so it reads as ambient volume, not a surge
    checker.get_volume_usd = MagicMock(return_value={"m5": 3000.0, "h1": 30000.0})

    passed, reason = checker.check("SomeMint", _config(min_recent_volume_usd=1000.0, volume_surge_multiplier=2.0))

    assert passed is False
    assert "surging" in reason


def test_check_passes_on_strong_surging_volume():
    checker = _make_checker()
    checker.get_volume_usd = MagicMock(return_value={"m5": 5000.0, "h1": 6000.0})

    passed, reason = checker.check("SomeMint", _config(min_recent_volume_usd=1000.0, volume_surge_multiplier=1.5))

    assert passed is True
    assert reason == ""


def test_check_result_is_cached_within_ttl():
    checker = _make_checker()
    checker.get_volume_usd = MagicMock(return_value={"m5": 5000.0, "h1": 6000.0})

    checker.check("SomeMint", _config())
    checker.check("SomeMint", _config())

    checker.get_volume_usd.assert_called_once()


def test_get_volume_usd_picks_highest_liquidity_pair():
    checker = _make_checker()
    checker._http.get.return_value = _http_response(
        200,
        {
            "pairs": [
                {"liquidity": {"usd": 100.0}, "volume": {"m5": 10.0, "h1": 20.0}},
                {"liquidity": {"usd": 90000.0}, "volume": {"m5": 5000.0, "h1": 6000.0}},
            ]
        },
    )

    data = checker.get_volume_usd("SomeMint")

    assert data == {"m5": 5000.0, "h1": 6000.0}


def test_get_volume_usd_uses_shared_rate_limiter():
    checker = _make_checker()
    checker._http.get.return_value = _http_response(200, {"pairs": []})

    with patch("src.entry_quality.DEXSCREENER_RATE_LIMITER") as mock_limiter:
        checker.get_volume_usd("SomeMint")

    mock_limiter.wait.assert_called_once()


def test_get_volume_usd_returns_none_when_no_pairs():
    checker = _make_checker()
    checker._http.get.return_value = _http_response(200, {"pairs": []})

    assert checker.get_volume_usd("SomeMint") is None


def test_get_volume_usd_returns_none_on_http_error():
    checker = _make_checker()
    checker._http.get.side_effect = Exception("network down")

    assert checker.get_volume_usd("SomeMint") is None


def test_get_volume_usd_returns_none_on_non_200():
    checker = _make_checker()
    checker._http.get.return_value = _http_response(404, {})

    assert checker.get_volume_usd("SomeMint") is None
