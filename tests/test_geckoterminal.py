from unittest.mock import MagicMock

from src.geckoterminal import GeckoTerminalClient, _parse_pool_to_token


def _make_pool(name, mint):
    return {
        "attributes": {"name": name},
        "relationships": {"base_token": {"data": {"id": f"solana_{mint}"}}},
    }


def test_parse_pool_to_token_extracts_symbol_and_mint():
    pool = _make_pool("PEPENOM / SOL", "EpEfnZxQyiBXppSKi8sncc8w4corn1UJbF9G91fQpump")

    token = _parse_pool_to_token(pool)

    assert token.symbol == "PEPENOM"
    assert token.mint == "EpEfnZxQyiBXppSKi8sncc8w4corn1UJbF9G91fQpump"


def test_parse_pool_to_token_handles_missing_name():
    pool = {
        "attributes": {},
        "relationships": {"base_token": {"data": {"id": "solana_SomeMint"}}},
    }

    token = _parse_pool_to_token(pool)

    assert token.symbol == "?"
    assert token.mint == "SomeMint"


def test_list_trending_and_new_tokens_merges_both_endpoints():
    client = GeckoTerminalClient.__new__(GeckoTerminalClient)
    client._rate_limiter = MagicMock()
    client._rate_limiter.wait = MagicMock()

    pool_a = _make_pool("AAA / SOL", "MintAAA")
    pool_b = _make_pool("BBB / SOL", "MintBBB")

    client._get_pools = MagicMock(side_effect=lambda endpoint, page: [pool_a] if endpoint == "trending_pools" and page == 1 else ([pool_b] if endpoint == "new_pools" and page == 1 else []))

    tokens = client.list_trending_and_new_tokens(pages=1)
    mints = {t.mint for t in tokens}

    assert mints == {"MintAAA", "MintBBB"}


def test_list_trending_and_new_tokens_survives_endpoint_failure():
    client = GeckoTerminalClient.__new__(GeckoTerminalClient)
    client._rate_limiter = MagicMock()
    client._rate_limiter.wait = MagicMock()
    client._get_pools = MagicMock(side_effect=Exception("boom"))

    tokens = client.list_trending_and_new_tokens(pages=1)

    assert tokens == []
