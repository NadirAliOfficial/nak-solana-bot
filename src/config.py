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
    dry_run_paper_balance_usd: float = field(
        default_factory=lambda: float(os.getenv("DRY_RUN_PAPER_BALANCE_USD", "0"))
    )

    trade_currency: str = field(default_factory=lambda: os.getenv("TRADE_CURRENCY", "USDC"))
    position_size_usd: float = field(default_factory=lambda: float(os.getenv("POSITION_SIZE_USD", "100")))
    gas_reserve_sol: float = field(default_factory=lambda: float(os.getenv("GAS_RESERVE_SOL", "0.5")))
    slippage_bps: int = field(default_factory=lambda: int(os.getenv("SLIPPAGE_BPS", "300")))

    pump_window_minutes: int = field(default_factory=lambda: int(os.getenv("PUMP_WINDOW_MINUTES", "15")))
    pump_threshold_pct: float = field(default_factory=lambda: float(os.getenv("PUMP_THRESHOLD_PCT", "15")))
    take_profit_pct: float = field(default_factory=lambda: float(os.getenv("TAKE_PROFIT_PCT", "8")))
    stop_loss_pct: float = field(default_factory=lambda: float(os.getenv("STOP_LOSS_PCT", "3")))
    use_jupiter_trigger_orders: bool = field(default_factory=lambda: _bool("USE_JUPITER_TRIGGER_ORDERS", True))

    enable_safety_filters: bool = field(default_factory=lambda: _bool("ENABLE_SAFETY_FILTERS", True))
    min_liquidity_usd: float = field(default_factory=lambda: float(os.getenv("MIN_LIQUIDITY_USD", "5000")))
    require_mint_authority_revoked: bool = field(
        default_factory=lambda: _bool("REQUIRE_MINT_AUTHORITY_REVOKED", True)
    )
    require_freeze_authority_revoked: bool = field(
        default_factory=lambda: _bool("REQUIRE_FREEZE_AUTHORITY_REVOKED", True)
    )
    enable_rugcheck: bool = field(default_factory=lambda: _bool("ENABLE_RUGCHECK", True))
    min_lp_locked_pct: float = field(default_factory=lambda: float(os.getenv("MIN_LP_LOCKED_PCT", "50")))
    max_top_holder_pct: float = field(default_factory=lambda: float(os.getenv("MAX_TOP_HOLDER_PCT", "30")))

    daily_loss_limit_usd: float = field(default_factory=lambda: float(os.getenv("DAILY_LOSS_LIMIT_USD", "50")))
    losing_streak_count: int = field(default_factory=lambda: int(os.getenv("LOSING_STREAK_COUNT", "3")))
    losing_streak_cooldown_minutes: int = field(
        default_factory=lambda: int(os.getenv("LOSING_STREAK_COOLDOWN_MINUTES", "20"))
    )

    min_token_age_seconds: int = field(default_factory=lambda: int(os.getenv("MIN_TOKEN_AGE_SECONDS", "150")))

    enable_volume_confirmation: bool = field(default_factory=lambda: _bool("ENABLE_VOLUME_CONFIRMATION", True))
    min_recent_volume_usd: float = field(default_factory=lambda: float(os.getenv("MIN_RECENT_VOLUME_USD", "2000")))
    volume_surge_multiplier: float = field(
        default_factory=lambda: float(os.getenv("VOLUME_SURGE_MULTIPLIER", "1.5"))
    )

    enable_copycat_filter: bool = field(default_factory=lambda: _bool("ENABLE_COPYCAT_FILTER", True))
    copycat_similarity_threshold: float = field(
        default_factory=lambda: float(os.getenv("COPYCAT_SIMILARITY_THRESHOLD", "0.82"))
    )

    enable_conviction_sizing: bool = field(default_factory=lambda: _bool("ENABLE_CONVICTION_SIZING", True))

    enable_partial_exit: bool = field(default_factory=lambda: _bool("ENABLE_PARTIAL_EXIT", True))
    partial_exit_pct: float = field(default_factory=lambda: float(os.getenv("PARTIAL_EXIT_PCT", "50")))
    trailing_stop_bps: int = field(default_factory=lambda: int(os.getenv("TRAILING_STOP_BPS", "500")))

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
