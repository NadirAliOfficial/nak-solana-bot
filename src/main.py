import os
import threading
import time

from .config import Config
from .dashboard import create_app
from .geckoterminal import GeckoTerminalClient
from .geckoterminal import POLL_INTERVAL_SECONDS as GECKOTERMINAL_POLL_INTERVAL_SECONDS
from .logger import get_logger
from .market_state import MarketState
from .positions import PositionStore
from .pumpportal import PumpPortalListener
from .solana_client import SolanaClient
from .trader import Trader

logger = get_logger(__name__)


def _position_monitor_loop(trader: Trader, config: Config):
    while True:
        try:
            trader.manage_open_positions()
        except Exception as exc:
            logger.error(f"position monitor failed: {exc}")
        time.sleep(config.poll_interval_seconds)


def _geckoterminal_discovery_loop(trader: Trader, client: GeckoTerminalClient):
    while True:
        try:
            tokens = client.list_trending_and_new_tokens()
            for token in tokens:
                trader.add_discovered_token(token)
            logger.info(f"GeckoTerminal discovery: merged {len(tokens)} trending/new pool tokens")
        except Exception as exc:
            logger.error(f"GeckoTerminal discovery failed: {exc}")
        time.sleep(GECKOTERMINAL_POLL_INTERVAL_SECONDS)


def _market_scan_loop(trader: Trader, state_path: str):
    while True:
        try:
            watched = trader.scan_and_buy()
            logger.info(f"scan complete: watching {watched} tokens")
            trader.save_state(state_path)
        except Exception as exc:
            logger.error(f"market scan failed: {exc}")
        time.sleep(10)  # price sampling cadence; discovery itself is event-driven via PumpPortal


def main():
    config = Config()
    store = PositionStore(config.db_path)
    client = SolanaClient(config.rpc_url, config.private_key, config.trade_currency)
    market_state = MarketState()
    trader = Trader(client, config, store, market_state)

    state_path = os.path.join(os.path.dirname(config.db_path) or ".", "watchlist_state.json")
    trader.load_state(state_path)

    if config.dry_run:
        logger.info("Starting in DRY_RUN mode - no live swaps will be sent")
    else:
        logger.info("Starting in LIVE mode - real swaps will be sent")

    app = create_app(store, client, config, market_state)
    dashboard_thread = threading.Thread(
        target=lambda: app.run(host=config.dashboard_host, port=config.dashboard_port, use_reloader=False),
        daemon=True,
    )
    dashboard_thread.start()
    logger.info(f"Dashboard running on http://{config.dashboard_host}:{config.dashboard_port}")

    scan_thread = threading.Thread(target=_market_scan_loop, args=(trader, state_path), daemon=True)
    scan_thread.start()

    listener = PumpPortalListener(on_new_token=trader.add_discovered_token)
    listener.start()
    logger.info("PumpPortal listener started")

    gecko_client = GeckoTerminalClient()
    gecko_thread = threading.Thread(target=_geckoterminal_discovery_loop, args=(trader, gecko_client), daemon=True)
    gecko_thread.start()
    logger.info("GeckoTerminal discovery loop started")

    _position_monitor_loop(trader, config)


if __name__ == "__main__":
    main()
