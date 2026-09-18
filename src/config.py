import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    rpc_url: str = field(default_factory=lambda: os.getenv("SOLANA_RPC_URL", ""))
    private_key: str = field(default_factory=lambda: os.getenv("SOLANA_PRIVATE_KEY", ""))
    dry_run: bool = field(default_factory=lambda: _bool("DRY_RUN", True))

    trade_currency: str = field(default_factory=lambda: os.getenv("TRADE_CURRENCY", "USDC"))
    position_size_usd: float = field(default_factory=lambda: float(os.getenv("POSITION_SIZE_USD", "100")))
    gas_reserve_sol: float = field(default_factory=lambda: float(os.getenv("GAS_RESERVE_SOL", "0.5")))
    slippage_bps: int = field(default_factory=lambda: int(os.getenv("SLIPPAGE_BPS", "300")))

    pump_window_minutes: int = field(default_factory=lambda: int(os.getenv("PUMP_WINDOW_MINUTES", "15")))
    pump_threshold_pct: float = field(default_factory=lambda: float(os.getenv("PUMP_THRESHOLD_PCT", "15")))
    take_profit_pct: float = field(default_factory=lambda: float(os.getenv("TAKE_PROFIT_PCT", "8")))
    stop_loss_pct: float = field(default_factory=lambda: float(os.getenv("STOP_LOSS_PCT", "3")))

    rebuy_cooldown_minutes: int = field(default_factory=lambda: int(os.getenv("REBUY_COOLDOWN_MINUTES", "30")))
    max_position_hold_minutes: int = field(default_factory=lambda: int(os.getenv("MAX_POSITION_HOLD_MINUTES", "120")))
    max_open_positions: int = field(default_factory=lambda: int(os.getenv("MAX_OPEN_POSITIONS", "10")))
    scan_concurrency: int = field(default_factory=lambda: int(os.getenv("SCAN_CONCURRENCY", "8")))
    scan_rate_limit_per_second: float = field(
        default_factory=lambda: float(os.getenv("SCAN_RATE_LIMIT_PER_SECOND", "5"))
    )

    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", "positions.db"))
    dashboard_port: int = field(default_factory=lambda: int(os.getenv("DASHBOARD_PORT", "8090")))
    dashboard_host: str = field(default_factory=lambda: os.getenv("DASHBOARD_HOST", "127.0.0.1"))
    dashboard_access_key: str = field(default_factory=lambda: os.getenv("DASHBOARD_ACCESS_KEY", ""))
