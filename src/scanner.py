import difflib
import re
from typing import List, Tuple


def detect_pump(price_history: List[Tuple[float, float]], window_minutes: int, threshold_pct: float) -> Tuple[bool, float]:
    """price_history is a list of (timestamp, price) samples, ascending by timestamp.

    Unlike an exchange candle API, new Solana tokens have no history before the bot
    starts watching them, so a token only becomes eligible once it has been tracked
    for at least window_minutes.
    """
    if len(price_history) < 2:
        return False, 0.0

    latest_ts, latest_price = price_history[-1]
    window_start_ts = latest_ts - window_minutes * 60

    if price_history[0][0] > window_start_ts:
        return False, 0.0  # not tracked long enough yet

    baseline_price = price_history[0][1]
    for ts, price in price_history:
        if ts <= window_start_ts:
            baseline_price = price
        else:
            break

    if baseline_price <= 0:
        return False, 0.0

    pct_change = ((latest_price - baseline_price) / baseline_price) * 100
    return pct_change >= threshold_pct, pct_change


def _normalize_symbol(symbol: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (symbol or "").lower())


def is_copycat_symbol(a: str, b: str, threshold: float) -> bool:
    """True if two token symbols are similar enough to likely be copycats of the same
    meme trend (e.g. "GTA6" vs "GTA6INU"), after stripping case/punctuation. When a
    trend blows up, dozens of near-identical launches appear within minutes; only one
    of them is worth the risk.
    """
    na, nb = _normalize_symbol(a), _normalize_symbol(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= threshold
