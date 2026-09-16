import threading
import time

import httpx

from .logger import get_logger
from .solana_client import Token

logger = get_logger(__name__)

GECKOTERMINAL_API_BASE = "https://api.geckoterminal.com/api/v2"
RATE_LIMIT_PER_MINUTE = 7  # documented limit is 10/min; stay under it with margin
PAGES_PER_POLL = 6  # trending + new pools pages fetched each cycle, within the rate limit
POLL_INTERVAL_SECONDS = 15  # small buffer between passes; the rate limiter itself paces the calls


class RateLimiter:
    def __init__(self, max_per_minute: float):
        self._interval = 60.0 / max_per_minute
        self._lock = threading.Lock()
        self._next_slot = 0.0

    def wait(self):
        with self._lock:
            now = time.time()
            start = max(now, self._next_slot)
            self._next_slot = start + self._interval
            delay = start - now
        if delay > 0:
            time.sleep(delay)


def _parse_pool_to_token(pool: dict) -> Token:
    attrs = pool.get("attributes", {})
    name = attrs.get("name", "") or ""
    symbol = name.split("/")[0].strip() if "/" in name else (name or "?")

    base_token_id = pool.get("relationships", {}).get("base_token", {}).get("data", {}).get("id", "")
    mint = base_token_id.split("_", 1)[1] if "_" in base_token_id else base_token_id

    return Token(mint=mint, symbol=symbol or "?", name=name)


class GeckoTerminalClient:
    """Covers the broader Solana token universe (trending + newly listed pools across
    Raydium, pumpswap, Orca, Meteora, etc.), complementing PumpPortal's real-time
    firehose which only sees activity from the moment this bot started listening.
    """

    def __init__(self):
        self._http = httpx.Client(timeout=10.0)
        self._rate_limiter = RateLimiter(RATE_LIMIT_PER_MINUTE)

    def _get_pools(self, endpoint: str, page: int) -> list:
        last_exc = None
        for attempt in range(3):
            self._rate_limiter.wait()
            try:
                resp = self._http.get(
                    f"{GECKOTERMINAL_API_BASE}/networks/solana/{endpoint}",
                    params={"page": page},
                    headers={"Accept": "application/json"},
                )
                resp.raise_for_status()
                return resp.json().get("data", [])
            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
        raise last_exc

    def list_trending_and_new_tokens(self, pages: int = PAGES_PER_POLL) -> list:
        tokens = []
        for endpoint in ("trending_pools", "new_pools"):
            for page in range(1, pages + 1):
                try:
                    pools = self._get_pools(endpoint, page)
                except Exception as exc:
                    logger.warning(f"GeckoTerminal {endpoint} page {page} failed: {exc}")
                    continue
                for pool in pools:
                    try:
                        tokens.append(_parse_pool_to_token(pool))
                    except Exception:
                        continue
        return tokens
