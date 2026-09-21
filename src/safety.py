import time
from typing import Optional, Tuple

import httpx
from solders.pubkey import Pubkey

from .logger import get_logger
from .solana_client import DEXSCREENER_RATE_LIMITER

logger = get_logger(__name__)

DEXSCREENER_TOKENS_API = "https://api.dexscreener.com/latest/dex/tokens"
RUGCHECK_TOKENS_API = "https://api.rugcheck.xyz/v1/tokens"
JUPITER_PRICE_API = "https://api.jup.ag/price/v3"
CACHE_TTL_SECONDS = 60  # a token's momentum can re-trigger multiple scan cycles; avoid re-checking every time
CONVICTION_MIN_MULTIPLIER = 0.7
CONVICTION_MAX_MULTIPLIER = 1.3


def conviction_multiplier(metrics: dict, config) -> float:
    """Blends how comfortably a candidate cleared each safety threshold into a size
    multiplier - a setup that barely squeaks past the liquidity floor gets sized down,
    one that clears it well gets sized up. Clamped to +/-30% of the base size so one
    weak signal can't zero out a position and one strong signal can't overextend.
    Returns 1.0 (no adjustment) if no metrics are available to score.
    """
    scores = []

    if config.min_liquidity_usd > 0 and metrics.get("liquidity_usd") is not None:
        margin = metrics["liquidity_usd"] / config.min_liquidity_usd
        scores.append(min(2.0, margin) / 2.0)  # 0.0-1.0, saturates at 2x the floor

    if config.enable_rugcheck:
        if metrics.get("lp_locked_pct") is not None:
            scores.append(min(100.0, metrics["lp_locked_pct"]) / 100.0)
        if metrics.get("top_holder_pct") is not None and config.max_top_holder_pct > 0:
            headroom = 1.0 - (metrics["top_holder_pct"] / config.max_top_holder_pct)
            scores.append(max(0.0, min(1.0, headroom)))

    if not scores:
        return 1.0

    avg = sum(scores) / len(scores)
    return CONVICTION_MIN_MULTIPLIER + avg * (CONVICTION_MAX_MULTIPLIER - CONVICTION_MIN_MULTIPLIER)


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
        """Jupiter's price API is tried first - it returns pool liquidity for any mint
        it can price, including brand-new pump.fun/launchpad tokens seconds after
        launch, well before DexScreener has indexed a pair for them. DexScreener is the
        fallback for anything Jupiter doesn't have priced."""
        jupiter_liquidity = self._get_jupiter_liquidity_usd(mint)
        if jupiter_liquidity is not None:
            return jupiter_liquidity
        return self._get_dexscreener_liquidity_usd(mint)

    def _get_jupiter_liquidity_usd(self, mint: str) -> Optional[float]:
        try:
            resp = self._http.get(JUPITER_PRICE_API, params={"ids": mint})
            if resp.status_code != 200:
                return None
            entry = resp.json().get(mint)
            if not entry or entry.get("liquidity") is None:
                return None
            return float(entry["liquidity"])
        except Exception as exc:
            logger.debug(f"jupiter liquidity lookup failed for {mint[:8]}: {exc}")
            return None

    def _get_dexscreener_liquidity_usd(self, mint: str) -> Optional[float]:
        DEXSCREENER_RATE_LIMITER.wait()
        try:
            resp = self._http.get(f"{DEXSCREENER_TOKENS_API}/{mint}")
            if resp.status_code != 200:
                logger.debug(f"dexscreener liquidity lookup returned {resp.status_code} for {mint[:8]}")
                return None
            pairs = resp.json().get("pairs") or []
            if not pairs:
                return None
            return max((p.get("liquidity", {}) or {}).get("usd") or 0.0 for p in pairs)
        except Exception as exc:
            logger.debug(f"dexscreener liquidity lookup failed for {mint[:8]}: {exc}")
            return None

    def get_rugcheck_data(self, mint: str) -> Optional[dict]:
        """Returns {"lp_locked_pct", "top_holder_pct"} from RugCheck's free report
        endpoint, or None if the lookup failed or the token isn't indexed yet.

        lp_locked_pct comes from whichever market has the most USD liquidity - most
        tokens have dozens of tiny/irrelevant markets alongside the one that matters.
        top_holder_pct is the largest single holder's share of supply across the
        token's full holder list (RugCheck does not separate LP/burn addresses out
        of this list, so a legitimate large LP position can itself trip this check).
        """
        try:
            resp = self._http.get(f"{RUGCHECK_TOKENS_API}/{mint}/report")
            if resp.status_code != 200:
                return None
            data = resp.json()

            markets = data.get("markets") or []
            lp_locked_pct = 0.0
            if markets:
                def market_liquidity_usd(m):
                    lp = m.get("lp") or {}
                    return (lp.get("baseUSD") or 0.0) + (lp.get("quoteUSD") or 0.0)

                best_market = max(markets, key=market_liquidity_usd)
                lp_locked_pct = (best_market.get("lp") or {}).get("lpLockedPct") or 0.0

            top_holders = data.get("topHolders") or []
            top_holder_pct = max((h.get("pct") or 0.0) for h in top_holders) if top_holders else 0.0

            return {"lp_locked_pct": lp_locked_pct, "top_holder_pct": top_holder_pct}
        except Exception as exc:
            logger.debug(f"rugcheck lookup failed for {mint[:8]}: {exc}")
            return None

    def get_mint_authorities(self, mint: str) -> Tuple[Optional[str], Optional[str]]:
        """Returns (mint_authority, freeze_authority); None for either means revoked."""
        resp = self.rpc.get_account_info_json_parsed(Pubkey.from_string(mint))
        if not resp.value:
            raise ValueError(f"no account info for mint {mint}")
        info = resp.value.data.parsed["info"]
        return info.get("mintAuthority"), info.get("freezeAuthority")

    def check(self, mint: str, config) -> Tuple[bool, str, dict]:
        """Returns (passed, reason, metrics). reason is empty on pass, otherwise
        explains the failure. metrics carries whatever raw values were fetched along
        the way (liquidity_usd, lp_locked_pct, top_holder_pct) so callers can use them
        for conviction-based position sizing without a second round of API calls."""
        if not config.enable_safety_filters:
            return True, "", {}

        now = time.time()
        cached = self._cache.get(mint)
        if cached and now - cached[0] < CACHE_TTL_SECONDS:
            return cached[1], cached[2], cached[3]

        result = self._run_checks(mint, config)
        self._cache[mint] = (now, result[0], result[1], result[2])
        return result

    def _run_checks(self, mint: str, config) -> Tuple[bool, str, dict]:
        metrics = {}

        if config.min_liquidity_usd > 0:
            liquidity = self.get_liquidity_usd(mint)
            if liquidity is None:
                return False, "liquidity_unknown", metrics
            metrics["liquidity_usd"] = liquidity
            if liquidity < config.min_liquidity_usd:
                return False, f"liquidity ${liquidity:.0f} < ${config.min_liquidity_usd:.0f}", metrics

        if config.require_mint_authority_revoked or config.require_freeze_authority_revoked:
            try:
                mint_authority, freeze_authority = self.get_mint_authorities(mint)
            except Exception as exc:
                logger.debug(f"authority lookup failed for {mint[:8]}: {exc}")
                return False, "authority_unknown", metrics

            if config.require_mint_authority_revoked and mint_authority is not None:
                return False, "mint_authority_not_revoked", metrics

            if config.require_freeze_authority_revoked and freeze_authority is not None:
                return False, "freeze_authority_not_revoked", metrics

        if config.enable_rugcheck:
            rugcheck = self.get_rugcheck_data(mint)
            if rugcheck is None:
                return False, "rugcheck_unknown", metrics
            metrics["lp_locked_pct"] = rugcheck["lp_locked_pct"]
            metrics["top_holder_pct"] = rugcheck["top_holder_pct"]
            if rugcheck["lp_locked_pct"] < config.min_lp_locked_pct:
                return False, f"lp_locked {rugcheck['lp_locked_pct']:.0f}% < {config.min_lp_locked_pct:.0f}%", metrics
            if rugcheck["top_holder_pct"] > config.max_top_holder_pct:
                return False, f"top_holder {rugcheck['top_holder_pct']:.1f}% > {config.max_top_holder_pct:.1f}%", metrics

        return True, "", metrics
