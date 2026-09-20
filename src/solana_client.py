import base64
import threading
import time
from typing import Dict, List, Optional

import httpx
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solana.rpc.api import Client as SolanaRpcClient

from .logger import get_logger

logger = get_logger(__name__)

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_DECIMALS = 9
USDC_DECIMALS = 6

JUPITER_PRICE_API = "https://api.jup.ag/price/v3"
JUPITER_QUOTE_API = "https://api.jup.ag/swap/v1/quote"
JUPITER_SWAP_API = "https://api.jup.ag/swap/v1/swap"


class Token:
    def __init__(self, mint: str, symbol: str, name: str = ""):
        self.mint = mint
        self.symbol = symbol
        self.name = name


class RateLimiter:
    def __init__(self, max_per_second: float):
        self._interval = 1.0 / max_per_second
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


class SolanaClient:
    def __init__(self, rpc_url: str, private_key_b58: str, trade_currency: str = "USDC"):
        self.rpc = SolanaRpcClient(rpc_url)
        self.keypair = Keypair.from_base58_string(private_key_b58) if private_key_b58 else None
        self.trade_mint = USDC_MINT if trade_currency.upper() == "USDC" else SOL_MINT
        self.trade_mint_decimals = USDC_DECIMALS if self.trade_mint == USDC_MINT else SOL_DECIMALS
        self._http = httpx.Client(timeout=10.0)
        self._price_rate_limiter = RateLimiter(3)  # keep well under Jupiter's public rate limit
        self._dexscreener_rate_limiter = RateLimiter(4)  # keep well under DexScreener's public rate limit

    def get_prices_usd(self, mints: List[str]) -> Dict[str, float]:
        if not mints:
            return {}

        data = None
        for attempt in range(3):
            self._price_rate_limiter.wait()
            try:
                resp = self._http.get(JUPITER_PRICE_API, params={"ids": ",".join(mints)})
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception:
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
        prices = {}
        if data:
            for mint, entry in data.items():
                if entry and entry.get("usdPrice"):
                    try:
                        prices[mint] = float(entry["usdPrice"])
                    except (ValueError, TypeError):
                        pass

        # Fallback to DexScreener if any tokens are missing or Jupiter was rate-limited (HTTP 429)
        missing = [m for m in mints if m not in prices]
        if missing:
            for i in range(0, len(missing), 30):
                chunk = missing[i : i + 30]
                try:
                    resp = self._http.get(
                        f"https://api.dexscreener.com/latest/dex/tokens/{','.join(chunk)}"
                    )
                    if resp.status_code == 200:
                        pairs = resp.json().get("pairs") or []
                        for p in pairs:
                            base_addr = p.get("baseToken", {}).get("address")
                            price_str = p.get("priceUsd")
                            if base_addr and price_str and base_addr not in prices:
                                try:
                                    prices[base_addr] = float(price_str)
                                except (ValueError, TypeError):
                                    pass
                except Exception as exc:
                    logger.debug(f"dexscreener fallback error: {exc}")

        return prices

    def get_fast_prices_usd(self, mints: List[str]) -> Dict[str, float]:
        """Sub-200ms ultra-fast price lookup specifically for open positions via DexScreener.

        Rate-limited: the fast-poll loop can call this every 0.15s while a position is
        young, and an unthrottled call was found hammering DexScreener into silent
        rate-limit failures - which get misread as "no price data" and, after enough
        consecutive misses, written off as a dead/rugged token. Several tokens marked
        dead this way turned out to still have real liquidity on DexScreener hours
        later, meaning the position was very likely still sellable the whole time.
        """
        if not mints:
            return {}
        prices = {}
        for i in range(0, len(mints), 30):
            chunk = mints[i : i + 30]
            self._dexscreener_rate_limiter.wait()
            try:
                resp = self._http.get(
                    f"https://api.dexscreener.com/latest/dex/tokens/{','.join(chunk)}"
                )
                if resp.status_code == 200:
                    pairs = resp.json().get("pairs") or []
                    for p in pairs:
                        base_addr = p.get("baseToken", {}).get("address")
                        price_str = p.get("priceUsd")
                        if base_addr and price_str and base_addr not in prices:
                            try:
                                prices[base_addr] = float(price_str)
                            except (ValueError, TypeError):
                                pass
                else:
                    logger.warning(
                        f"dexscreener fast price lookup returned {resp.status_code} for "
                        f"{len(chunk)} mint(s) - likely rate limited, not necessarily no data"
                    )
            except Exception as exc:
                logger.debug(f"dexscreener fast price error: {exc}")

        # If any open position wasn't on DexScreener yet, try standard lookup
        missing = [m for m in mints if m not in prices]
        if missing:
            prices.update(self.get_prices_usd(missing))
        return prices

    def get_sol_balance(self) -> float:
        resp = self.rpc.get_balance(self.keypair.pubkey())
        return resp.value / 1_000_000_000

    def get_token_balance(self, mint: str) -> float:
        from solana.rpc.types import TokenAccountOpts

        resp = self.rpc.get_token_accounts_by_owner_json_parsed(
            self.keypair.pubkey(), TokenAccountOpts(mint=Pubkey.from_string(mint))
        )
        if not resp.value:
            return 0.0
        info = resp.value[0].account.data.parsed["info"]
        return float(info["tokenAmount"]["uiAmount"] or 0.0)

    def get_sol_price_usd(self) -> float:
        try:
            prices = self.get_prices_usd([SOL_MINT])
            if prices.get(SOL_MINT):
                self._last_sol_price = prices[SOL_MINT]
                return self._last_sol_price
        except Exception:
            pass

        try:
            resp = self._http.get("https://api.binance.com/api/v3/ticker/price?symbol=SOLUSDT", timeout=5.0)
            if resp.status_code == 200:
                p = float(resp.json().get("price", 0.0))
                if p > 0:
                    self._last_sol_price = p
                    return self._last_sol_price
        except Exception:
            pass

        try:
            resp = self._http.get(
                "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd", timeout=5.0
            )
            if resp.status_code == 200:
                p = float(resp.json().get("solana", {}).get("usd", 0.0))
                if p > 0:
                    self._last_sol_price = p
                    return self._last_sol_price
        except Exception:
            pass

        return getattr(self, "_last_sol_price", 100.0)

    def get_trade_currency_balance(self) -> float:
        if self.trade_mint == SOL_MINT:
            return self.get_sol_balance()
        return self.get_token_balance(self.trade_mint)

    def get_trade_currency_balance_usd(self) -> float:
        balance = self.get_trade_currency_balance()
        if self.trade_mint == USDC_MINT:
            return balance  # USDC is ~$1
        price = self.get_sol_price_usd()
        return balance * price

    def get_tradable_balance_usd(self, gas_reserve_sol: float = 0.5) -> float:
        if self.trade_mint == USDC_MINT:
            return self.get_token_balance(USDC_MINT)
        total_sol = self.get_sol_balance()
        tradable_sol = max(0.0, total_sol - gas_reserve_sol)
        price = self.get_sol_price_usd()
        return tradable_sol * price

    def get_wallet_address(self) -> Optional[str]:
        return str(self.keypair.pubkey()) if self.keypair else None

    def _get_quote(self, input_mint: str, output_mint: str, amount: int, slippage_bps: int) -> dict:
        last_exc = None
        for attempt in range(3):
            self._price_rate_limiter.wait()
            try:
                resp = self._http.get(
                    JUPITER_QUOTE_API,
                    params={
                        "inputMint": input_mint,
                        "outputMint": output_mint,
                        "amount": amount,
                        "slippageBps": slippage_bps,
                    },
                )
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(1.0 * (attempt + 1))
        raise last_exc

    def swap(self, input_mint: str, output_mint: str, amount: int, slippage_bps: int) -> str:
        quote = self._get_quote(input_mint, output_mint, amount, slippage_bps)

        self._price_rate_limiter.wait()
        swap_resp = self._http.post(
            JUPITER_SWAP_API,
            json={
                "quoteResponse": quote,
                "userPublicKey": str(self.keypair.pubkey()),
                "wrapAndUnwrapSol": True,
            },
        )
        swap_resp.raise_for_status()
        swap_tx_b64 = swap_resp.json()["swapTransaction"]

        raw_tx = VersionedTransaction.from_bytes(base64.b64decode(swap_tx_b64))
        signed_tx = VersionedTransaction(raw_tx.message, [self.keypair])

        result = self.rpc.send_raw_transaction(bytes(signed_tx))
        signature = str(result.value)
        logger.info(f"swap sent: {input_mint[:6]}->{output_mint[:6]} amount={amount} sig={signature}")
        return signature

    def buy(self, token_mint: str, usd_amount: float, price_usd: float, slippage_bps: int) -> str:
        if self.trade_mint == USDC_MINT:
            trade_currency_amount = usd_amount  # USDC is ~$1
        else:
            trade_mint_price = self.get_sol_price_usd()
            if not trade_mint_price:
                raise RuntimeError(f"could not price trade currency {self.trade_mint} for buy sizing")
            trade_currency_amount = usd_amount / trade_mint_price

        amount_atomic = int(trade_currency_amount * (10 ** self.trade_mint_decimals))
        return self.swap(self.trade_mint, token_mint, amount_atomic, slippage_bps)

    def sell(self, token_mint: str, quantity: float, token_decimals: int, slippage_bps: int) -> str:
        amount_atomic = int(quantity * (10 ** token_decimals))
        return self.swap(token_mint, self.trade_mint, amount_atomic, slippage_bps)
