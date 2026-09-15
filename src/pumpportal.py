import asyncio
import json
import ssl
import threading

import certifi
import websockets

from .logger import get_logger
from .solana_client import Token

logger = get_logger(__name__)

PUMPPORTAL_WS_URL = "wss://pumpportal.fun/api/data"
RECONNECT_DELAY_SECONDS = 5


class PumpPortalListener:
    """Streams new Pump.fun launches and Raydium migrations in real time.

    Pump.fun's own REST API (frontend-api.pump.fun) was retired; PumpPortal's
    free WebSocket feed is the current standard replacement for bots.
    """

    def __init__(self, on_new_token):
        self._on_new_token = on_new_token
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        asyncio.run(self._listen_forever())

    async def _listen_forever(self):
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        while True:
            try:
                async with websockets.connect(PUMPPORTAL_WS_URL, ssl=ssl_context) as ws:
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    await ws.send(json.dumps({"method": "subscribeMigration"}))
                    logger.info("connected to PumpPortal new-token/migration stream")
                    async for message in ws:
                        self._handle_message(message)
            except Exception as exc:
                logger.warning(f"PumpPortal connection lost, reconnecting: {exc}")
                await asyncio.sleep(RECONNECT_DELAY_SECONDS)

    def _handle_message(self, raw_message):
        try:
            data = json.loads(raw_message)
        except Exception:
            return

        mint = data.get("mint")
        if not mint:
            return

        token = Token(mint=mint, symbol=data.get("symbol") or "?", name=data.get("name") or "")
        try:
            self._on_new_token(token)
        except Exception as exc:
            logger.warning(f"on_new_token callback failed: {exc}")
