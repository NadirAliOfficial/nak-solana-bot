import pytest


@pytest.fixture(autouse=True)
def _no_real_dexscreener_rate_limit_sleeps(monkeypatch):
    """DEXSCREENER_RATE_LIMITER is a real, shared, process-wide RateLimiter used by
    solana_client/safety/entry_quality. Patch its wait() to a no-op for every test so
    the suite doesn't actually sleep on a shared rate budget across dozens of tests.
    Tests that want to assert wait() was called should patch the module-level name
    directly (e.g. patch("src.solana_client.DEXSCREENER_RATE_LIMITER")), which
    overrides this for the duration of that test.
    """
    from src.solana_client import DEXSCREENER_RATE_LIMITER

    monkeypatch.setattr(DEXSCREENER_RATE_LIMITER, "wait", lambda: None)
