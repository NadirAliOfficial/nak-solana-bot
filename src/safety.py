import time
from typing import Optional, Tuple

import httpx
from solders.pubkey import Pubkey

from .logger import get_logger

logger = get_logger(__name__)

DEXSCREENER_TOKENS_API = "https://api.dexscreener.com/latest/dex/tokens"
CACHE_TTL_SECONDS = 60  # a token's momentum can re-trigger multiple scan cycles; avoid re-checking every time


class SafetyChecker:
    """Pre-buy rug/honeypot filters, run only for tokens that already passed the
    momentum trigger (cheap to run there since it's a handful of candidates per
    cycle, not the whole watchlist).

    These catch the two most common Pump.fun rug patterns:
    - mint authority not revoked: creator can mint unlimited new supply and dump it
    - freeze authority not revoked: creator can freeze your tokens so you can't sell
      (the classic honeypot - stop loss can't save you if the sell itself is blocked)
    and a minimum liquidity floor to reduce slippage/exit risk on thin pools.

    None of this eliminates rug risk (a creator can still dump their own allocation
    even with authorities revoked) - it just removes the trades with an on-chain
    guarantee of no exit.
    """

    def __init__(self, rpc_client, http: Optional[httpx.Client] = None):
        self.rpc = rpc_client
        self._http = http or httpx.Client(timeout=10.0)
        self._cache = {}  # mint -> (checked_at, passed, reason)

    def get_liquidity_usd(self, mint: str) -> Optional[float]:
        try:
            resp = self._http.get(f"{DEXSCREENER_TOKENS_API}/{mint}")
            if resp.status_code != 200:
                return None
            pairs = resp.json().get("pairs") or []
            if not pairs:
                return None
            return max((p.get("liquidity", {}) or {}).get("usd") or 0.0 for p in pairs)
        except Exception as exc:
            logger.debug(f"liquidity lookup failed for {mint[:8]}: {exc}")
            return None

    def get_mint_authorities(self, mint: str) -> Tuple[Optional[str], Optional[str]]:
        """Returns (mint_authority, freeze_authority); None for either means revoked."""
        resp = self.rpc.get_account_info_json_parsed(Pubkey.from_string(mint))
        if not resp.value:
            raise ValueError(f"no account info for mint {mint}")
        info = resp.value.data.parsed["info"]
        return info.get("mintAuthority"), info.get("freezeAuthority")

    def check(self, mint: str, config) -> Tuple[bool, str]:
        """Returns (passed, reason). reason is empty on pass, otherwise explains the failure."""
        if not config.enable_safety_filters:
            return True, ""

        now = time.time()
        cached = self._cache.get(mint)
        if cached and now - cached[0] < CACHE_TTL_SECONDS:
            return cached[1], cached[2]

        result = self._run_checks(mint, config)
        self._cache[mint] = (now, result[0], result[1])
        return result

    def _run_checks(self, mint: str, config) -> Tuple[bool, str]:
        if config.min_liquidity_usd > 0:
            liquidity = self.get_liquidity_usd(mint)
            if liquidity is None:
                return False, "liquidity_unknown"
            if liquidity < config.min_liquidity_usd:
                return False, f"liquidity ${liquidity:.0f} < ${config.min_liquidity_usd:.0f}"

        if config.require_mint_authority_revoked or config.require_freeze_authority_revoked:
            try:
                mint_authority, freeze_authority = self.get_mint_authorities(mint)
            except Exception as exc:
                logger.debug(f"authority lookup failed for {mint[:8]}: {exc}")
                return False, "authority_unknown"

            if config.require_mint_authority_revoked and mint_authority is not None:
                return False, "mint_authority_not_revoked"

            if config.require_freeze_authority_revoked and freeze_authority is not None:
                return False, "freeze_authority_not_revoked"

        return True, ""
