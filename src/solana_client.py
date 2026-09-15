import base64
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

PUMPFUN_API_BASE = "https://frontend-api.pump.fun"
JUPITER_PRICE_API = "https://api.jup.ag/price/v3"
JUPITER_QUOTE_API = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_API = "https://quote-api.jup.ag/v6/swap"


class Token:
    def __init__(self, mint: str, symbol: str, name: str = ""):
        self.mint = mint
        self.symbol = symbol
        self.name = name


class SolanaClient:
    def __init__(self, rpc_url: str, private_key_b58: str, trade_currency: str = "USDC"):
        self.rpc = SolanaRpcClient(rpc_url)
        self.keypair = Keypair.from_base58_string(private_key_b58) if private_key_b58 else None
        self.trade_mint = USDC_MINT if trade_currency.upper() == "USDC" else SOL_MINT
        self._http = httpx.Client(timeout=10.0)

    def list_new_pumpfun_tokens(self, limit: int = 50) -> List[Token]:
        resp = self._http.get(
            f"{PUMPFUN_API_BASE}/coins",
            params={"offset": 0, "limit": limit, "sort": "created_timestamp", "order": "DESC"},
        )
        resp.raise_for_status()
        data = resp.json()
        return [Token(mint=c["mint"], symbol=c.get("symbol", "?"), name=c.get("name", "")) for c in data]

    def get_prices_usd(self, mints: List[str]) -> Dict[str, float]:
        if not mints:
            return {}
        resp = self._http.get(JUPITER_PRICE_API, params={"ids": ",".join(mints)})
        resp.raise_for_status()
        data = resp.json()
        prices = {}
        for mint, entry in data.items():
            if entry and entry.get("usdPrice"):
                prices[mint] = float(entry["usdPrice"])
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

    def get_trade_currency_balance(self) -> float:
        return self.get_token_balance(self.trade_mint)

    def get_wallet_address(self) -> Optional[str]:
        return str(self.keypair.pubkey()) if self.keypair else None

    def _get_quote(self, input_mint: str, output_mint: str, amount: int, slippage_bps: int) -> dict:
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

    def swap(self, input_mint: str, output_mint: str, amount: int, slippage_bps: int) -> str:
        quote = self._get_quote(input_mint, output_mint, amount, slippage_bps)

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
        # trade_mint is assumed to be a stable-priced asset (USDC); amount is in that token's atomic units (6 decimals)
        amount_atomic = int(usd_amount * 1_000_000)
        return self.swap(self.trade_mint, token_mint, amount_atomic, slippage_bps)

    def sell(self, token_mint: str, quantity: float, token_decimals: int, slippage_bps: int) -> str:
        amount_atomic = int(quantity * (10 ** token_decimals))
        return self.swap(token_mint, self.trade_mint, amount_atomic, slippage_bps)
