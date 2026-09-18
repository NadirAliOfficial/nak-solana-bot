import base64
import time
from typing import Optional

import httpx
from solders.transaction import VersionedTransaction

from .logger import get_logger

logger = get_logger(__name__)

TRIGGER_BASE = "https://api.jup.ag/trigger/v2"


class JupiterTriggerClient:
    """Places take-profit/stop-loss as an on-chain OCO order executed by Jupiter's own
    keeper infrastructure, instead of relying solely on our own polling loop to catch
    the exit. Every call here is expected to be wrapped in try/except by the caller:
    if anything in this client fails (auth, vault, order placement), the caller falls
    back to the existing manage_open_positions() polling loop, so a bug here never
    removes the safety net - it only means we lose the faster on-chain exit for that
    position.
    """

    def __init__(self, rpc, keypair):
        self.rpc = rpc
        self.keypair = keypair
        self._http = httpx.Client(timeout=15.0)
        self._jwt: Optional[str] = None
        self._jwt_expiry = 0.0

    def _pubkey_str(self) -> str:
        return str(self.keypair.pubkey())

    def _authenticate(self) -> str:
        challenge_resp = self._http.post(
            f"{TRIGGER_BASE}/auth/challenge",
            json={"walletPubkey": self._pubkey_str(), "type": "message"},
        )
        challenge_resp.raise_for_status()
        challenge = challenge_resp.json()["challenge"]

        signature = self.keypair.sign_message(challenge.encode())

        verify_resp = self._http.post(
            f"{TRIGGER_BASE}/auth/verify",
            json={
                "walletPubkey": self._pubkey_str(),
                "type": "message",
                "signature": str(signature),
            },
        )
        verify_resp.raise_for_status()
        data = verify_resp.json()
        self._jwt = data["token"]
        self._jwt_expiry = time.time() + 23 * 3600  # 24h TTL, refresh an hour early
        return self._jwt

    def _auth_headers(self) -> dict:
        if not self._jwt or time.time() >= self._jwt_expiry:
            self._authenticate()
        return {"Authorization": f"Bearer {self._jwt}"}

    def _sign_and_send(self, tx_b64: str) -> str:
        raw_tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
        signed_tx = VersionedTransaction(raw_tx.message, [self.keypair])
        result = self.rpc.send_raw_transaction(bytes(signed_tx))
        return str(result.value)

    def place_oco_exit_order(
        self,
        token_mint: str,
        trade_currency_mint: str,
        quantity: float,
        token_decimals: int,
        tp_price_usd: float,
        sl_price_usd: float,
        slippage_bps: int,
        expires_in_seconds: int = 24 * 3600,
    ) -> Optional[str]:
        """Places an OCO order that sells `quantity` of token_mint for trade_currency_mint
        when price crosses tp_price_usd (up) or sl_price_usd (down). Whichever side fills
        first auto-cancels the other. Returns the Jupiter order id, or None on failure."""
        headers = self._auth_headers()
        amount_atomic = str(int(quantity * (10 ** token_decimals)))

        craft_resp = self._http.post(
            f"{TRIGGER_BASE}/deposit/craft",
            headers=headers,
            json={
                "inputMint": token_mint,
                "outputMint": trade_currency_mint,
                "userAddress": self._pubkey_str(),
                "amount": amount_atomic,
                "orderType": "price",
                "orderSubType": "oco",
            },
        )
        craft_resp.raise_for_status()
        craft = craft_resp.json()
        deposit_request_id = craft["requestId"]
        deposit_signed_tx = self._sign_and_send(craft["transaction"])

        order_resp = self._http.post(
            f"{TRIGGER_BASE}/orders/price",
            headers=headers,
            json={
                "orderType": "oco",
                "depositRequestId": deposit_request_id,
                "depositSignedTx": deposit_signed_tx,
                "userPubkey": self._pubkey_str(),
                "inputMint": token_mint,
                "inputAmount": amount_atomic,
                "outputMint": trade_currency_mint,
                "triggerMint": token_mint,
                "tpPriceUsd": tp_price_usd,
                "slPriceUsd": sl_price_usd,
                "slippageBps": slippage_bps,
                "expiresAt": int((time.time() + expires_in_seconds) * 1000),
            },
        )
        order_resp.raise_for_status()
        order = order_resp.json()
        logger.info(f"jupiter trigger OCO order placed for {token_mint[:8]}: id={order['id']}")
        return order["id"]

    def get_order_status(self, order_id: str) -> Optional[dict]:
        """Returns the order's history entry (state, fill price, etc.) or None if not found."""
        headers = self._auth_headers()
        resp = self._http.get(f"{TRIGGER_BASE}/orders/history", headers=headers, params={"orderId": order_id})
        resp.raise_for_status()
        orders = resp.json().get("orders", [])
        for o in orders:
            if o.get("id") == order_id:
                return o
        return None

    def cancel_order(self, order_id: str) -> bool:
        """Two-step cancel: initiate, then confirm with a signed withdrawal transaction
        that returns the deposited tokens to the wallet."""
        headers = self._auth_headers()
        cancel_resp = self._http.post(f"{TRIGGER_BASE}/orders/price/cancel/{order_id}", headers=headers)
        cancel_resp.raise_for_status()
        withdraw_tx_b64 = cancel_resp.json().get("transaction")
        if not withdraw_tx_b64:
            return True  # nothing to sign, already cancelled

        signed_withdraw_tx = self._sign_and_send(withdraw_tx_b64)
        confirm_resp = self._http.post(
            f"{TRIGGER_BASE}/orders/price/confirm-cancel/{order_id}",
            headers=headers,
            json={"signedTx": signed_withdraw_tx},
        )
        confirm_resp.raise_for_status()
        logger.info(f"jupiter trigger order cancelled: id={order_id}")
        return True
