import time
from typing import Optional, Tuple

import httpx

from .logger import get_logger

logger = get_logger(__name__)

DEXSCREENER_TOKENS_API = "https://api.dexscreener.com/latest/dex/tokens"
CACHE_TTL_SECONDS = 60


class EntryQualityChecker:
    """Confirms a detected price pump reflects real buying interest rather than one
    whale moving a thin order book alone - a price spike with flat volume is easy to
    fake and easy to dump on the buyers who chase it. Separate from SafetyChecker:
    that module asks "can I get out", this one asks "is this move real".
    """

    def __init__(self, http: Optional[httpx.Client] = None):
        self._http = http or httpx.Client(timeout=10.0)
        self._cache = {}  # mint -> (checked_at, passed, reason)

    def get_volume_usd(self, mint: str) -> Optional[dict]:
        """Returns {"m5", "h1"} volume in USD for the token's highest-liquidity pair,
        or None if the lookup failed or the token has no pairs yet."""
        try:
            resp = self._http.get(f"{DEXSCREENER_TOKENS_API}/{mint}")
            if resp.status_code != 200:
                return None
            pairs = resp.json().get("pairs") or []
            if not pairs:
                return None
            best_pair = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0.0)
            volume = best_pair.get("volume") or {}
            return {"m5": volume.get("m5") or 0.0, "h1": volume.get("h1") or 0.0}
        except Exception as exc:
            logger.debug(f"volume lookup failed for {mint[:8]}: {exc}")
            return None

    def check(self, mint: str, config) -> Tuple[bool, str]:
        if not config.enable_volume_confirmation:
            return True, ""

        now = time.time()
        cached = self._cache.get(mint)
        if cached and now - cached[0] < CACHE_TTL_SECONDS:
            return cached[1], cached[2]

        result = self._run_check(mint, config)
        self._cache[mint] = (now, result[0], result[1])
        return result

    def _run_check(self, mint: str, config) -> Tuple[bool, str]:
        data = self.get_volume_usd(mint)
        if data is None:
            return False, "volume_unknown"

        if data["m5"] < config.min_recent_volume_usd:
            return False, f"volume ${data['m5']:.0f} < ${config.min_recent_volume_usd:.0f} (5m)"

        hourly_5m_rate = data["h1"] / 12.0
        if hourly_5m_rate > 0 and data["m5"] < hourly_5m_rate * config.volume_surge_multiplier:
            return False, (
                f"volume not surging (5m=${data['m5']:.0f} < "
                f"{config.volume_surge_multiplier}x hourly rate ${hourly_5m_rate:.0f})"
            )

        return True, ""
