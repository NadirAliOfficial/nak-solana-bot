from types import SimpleNamespace
from unittest.mock import MagicMock

from src.safety import SafetyChecker, conviction_multiplier


def _config(**overrides):
    base = dict(
        enable_safety_filters=True,
        min_liquidity_usd=5000,
        require_mint_authority_revoked=True,
        require_freeze_authority_revoked=True,
        enable_rugcheck=False,
        min_lp_locked_pct=50,
        max_top_holder_pct=30,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _http_response(status_code=200, json_data=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    return resp


def _make_checker():
    checker = SafetyChecker(rpc_client=MagicMock())
    checker._http = MagicMock()
    return checker


def test_check_disabled_always_passes():
    checker = _make_checker()
    passed, reason, metrics = checker.check("SomeMint", _config(enable_safety_filters=False))
    assert passed is True
    assert reason == ""


def test_check_fails_on_low_liquidity():
    checker = _make_checker()
    checker.get_liquidity_usd = MagicMock(return_value=1000.0)

    passed, reason, metrics = checker.check("SomeMint", _config())

    assert passed is False
    assert "liquidity" in reason


def test_check_fails_on_unknown_liquidity():
    checker = _make_checker()
    checker.get_liquidity_usd = MagicMock(return_value=None)

    passed, reason, metrics = checker.check("SomeMint", _config())

    assert passed is False
    assert reason == "liquidity_unknown"


def test_check_fails_on_active_mint_authority():
    checker = _make_checker()
    checker.get_liquidity_usd = MagicMock(return_value=10000.0)
    checker.get_mint_authorities = MagicMock(return_value=("SomeAuthority", None))

    passed, reason, metrics = checker.check("SomeMint", _config())

    assert passed is False
    assert reason == "mint_authority_not_revoked"


def test_check_fails_on_active_freeze_authority():
    checker = _make_checker()
    checker.get_liquidity_usd = MagicMock(return_value=10000.0)
    checker.get_mint_authorities = MagicMock(return_value=(None, "SomeAuthority"))

    passed, reason, metrics = checker.check("SomeMint", _config())

    assert passed is False
    assert reason == "freeze_authority_not_revoked"


def test_check_passes_when_all_conditions_met():
    checker = _make_checker()
    checker.get_liquidity_usd = MagicMock(return_value=10000.0)
    checker.get_mint_authorities = MagicMock(return_value=(None, None))

    passed, reason, metrics = checker.check("SomeMint", _config())

    assert passed is True
    assert reason == ""
    assert metrics == {"liquidity_usd": 10000.0}


def test_check_result_is_cached_within_ttl():
    checker = _make_checker()
    checker.get_liquidity_usd = MagicMock(return_value=10000.0)
    checker.get_mint_authorities = MagicMock(return_value=(None, None))

    checker.check("SomeMint", _config())
    checker.check("SomeMint", _config())

    checker.get_liquidity_usd.assert_called_once()
    checker.get_mint_authorities.assert_called_once()


def test_check_skips_authority_lookup_when_not_required():
    checker = _make_checker()
    checker.get_liquidity_usd = MagicMock(return_value=10000.0)
    checker.get_mint_authorities = MagicMock()

    passed, reason, metrics = checker.check(
        "SomeMint",
        _config(require_mint_authority_revoked=False, require_freeze_authority_revoked=False),
    )

    assert passed is True
    checker.get_mint_authorities.assert_not_called()


def test_check_fails_on_low_lp_locked_pct():
    checker = _make_checker()
    checker.get_liquidity_usd = MagicMock(return_value=10000.0)
    checker.get_mint_authorities = MagicMock(return_value=(None, None))
    checker.get_rugcheck_data = MagicMock(return_value={"lp_locked_pct": 10.0, "top_holder_pct": 5.0})

    passed, reason, metrics = checker.check("SomeMint", _config(enable_rugcheck=True))

    assert passed is False
    assert "lp_locked" in reason


def test_check_fails_on_high_top_holder_pct():
    checker = _make_checker()
    checker.get_liquidity_usd = MagicMock(return_value=10000.0)
    checker.get_mint_authorities = MagicMock(return_value=(None, None))
    checker.get_rugcheck_data = MagicMock(return_value={"lp_locked_pct": 90.0, "top_holder_pct": 45.0})

    passed, reason, metrics = checker.check("SomeMint", _config(enable_rugcheck=True))

    assert passed is False
    assert "top_holder" in reason


def test_check_fails_when_rugcheck_lookup_fails():
    checker = _make_checker()
    checker.get_liquidity_usd = MagicMock(return_value=10000.0)
    checker.get_mint_authorities = MagicMock(return_value=(None, None))
    checker.get_rugcheck_data = MagicMock(return_value=None)

    passed, reason, metrics = checker.check("SomeMint", _config(enable_rugcheck=True))

    assert passed is False
    assert reason == "rugcheck_unknown"


def test_check_passes_rugcheck_when_thresholds_met():
    checker = _make_checker()
    checker.get_liquidity_usd = MagicMock(return_value=10000.0)
    checker.get_mint_authorities = MagicMock(return_value=(None, None))
    checker.get_rugcheck_data = MagicMock(return_value={"lp_locked_pct": 90.0, "top_holder_pct": 10.0})

    passed, reason, metrics = checker.check("SomeMint", _config(enable_rugcheck=True))

    assert passed is True
    assert reason == ""
    assert metrics["lp_locked_pct"] == 90.0
    assert metrics["top_holder_pct"] == 10.0


def test_get_rugcheck_data_picks_highest_liquidity_market():
    checker = _make_checker()
    checker._http.get.return_value = _http_response(
        200,
        {
            "markets": [
                {"lp": {"baseUSD": 100.0, "quoteUSD": 100.0, "lpLockedPct": 10.0}},
                {"lp": {"baseUSD": 50000.0, "quoteUSD": 50000.0, "lpLockedPct": 95.0}},
            ],
            "topHolders": [{"pct": 3.0}, {"pct": 12.5}],
        },
    )

    data = checker.get_rugcheck_data("SomeMint")

    assert data == {"lp_locked_pct": 95.0, "top_holder_pct": 12.5}


def test_get_rugcheck_data_returns_none_on_http_error():
    checker = _make_checker()
    checker._http.get.side_effect = Exception("network down")

    assert checker.get_rugcheck_data("SomeMint") is None


def test_get_rugcheck_data_returns_none_on_non_200():
    checker = _make_checker()
    checker._http.get.return_value = _http_response(404, {})

    assert checker.get_rugcheck_data("SomeMint") is None


def test_conviction_multiplier_returns_1_when_no_metrics():
    assert conviction_multiplier({}, _config()) == 1.0


def test_conviction_multiplier_scales_up_for_strong_liquidity_margin():
    cfg = _config(enable_rugcheck=False)
    at_floor = conviction_multiplier({"liquidity_usd": 5000.0}, cfg)
    well_above_floor = conviction_multiplier({"liquidity_usd": 50000.0}, cfg)

    assert well_above_floor > at_floor


def test_conviction_multiplier_is_clamped_between_bounds():
    cfg = _config(enable_rugcheck=False)
    low = conviction_multiplier({"liquidity_usd": 1.0}, cfg)
    high = conviction_multiplier({"liquidity_usd": 10_000_000.0}, cfg)

    assert 0.7 <= low <= 1.3
    assert 0.7 <= high <= 1.3


def test_conviction_multiplier_incorporates_rugcheck_metrics():
    cfg = _config(enable_rugcheck=True, min_liquidity_usd=0)
    strong_setup = conviction_multiplier({"lp_locked_pct": 100.0, "top_holder_pct": 5.0}, cfg)
    marginal_setup = conviction_multiplier({"lp_locked_pct": 50.0, "top_holder_pct": 29.0}, cfg)

    assert strong_setup > marginal_setup


def test_get_liquidity_usd_returns_max_across_pairs():
    checker = _make_checker()
    checker._http.get.return_value = _http_response(
        200,
        {
            "pairs": [
                {"liquidity": {"usd": 2000}},
                {"liquidity": {"usd": 9000}},
            ]
        },
    )

    assert checker.get_liquidity_usd("SomeMint") == 9000


def test_get_liquidity_usd_returns_none_when_no_pairs():
    checker = _make_checker()
    checker._http.get.return_value = _http_response(200, {"pairs": []})

    assert checker.get_liquidity_usd("SomeMint") is None


def test_get_liquidity_usd_returns_none_on_http_error():
    checker = _make_checker()
    checker._http.get.side_effect = Exception("network down")

    assert checker.get_liquidity_usd("SomeMint") is None


VALID_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"  # well-formed base58 pubkey; USDC mint, value unused


def test_get_mint_authorities_parses_response():
    checker = _make_checker()
    account_info = MagicMock()
    account_info.value.data.parsed = {"info": {"mintAuthority": "Auth1", "freezeAuthority": None}}
    checker.rpc.get_account_info_json_parsed.return_value = account_info

    mint_authority, freeze_authority = checker.get_mint_authorities(VALID_MINT)

    assert mint_authority == "Auth1"
    assert freeze_authority is None


def test_get_mint_authorities_raises_when_account_missing():
    checker = _make_checker()
    account_info = MagicMock()
    account_info.value = None
    checker.rpc.get_account_info_json_parsed.return_value = account_info

    try:
        checker.get_mint_authorities(VALID_MINT)
        assert False, "expected ValueError"
    except ValueError:
        pass
