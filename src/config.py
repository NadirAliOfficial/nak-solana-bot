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
    take_profit_pct: float = field(default_factory=lambda: float(os.getenv("TAKE_PROFIT_PCT", "9")))
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
    max_position_risk_pct: float = field(default_factory=lambda: float(os.getenv("MAX_POSITION_RISK_PCT", "2.0")))

    fast_poll_window_seconds: int = field(default_factory=lambda: int(os.getenv("FAST_POLL_WINDOW_SECONDS", "120")))
    fast_poll_interval_seconds: float = field(
        default_factory=lambda: float(os.getenv("FAST_POLL_INTERVAL_SECONDS", "0.15"))
    )

    # How long a position can go with no price data before it's written off as dead
    # (illiquid/abandoned) and closed as a full loss. Was hardcoded to 1800s (30min) -
    # that's 30 minutes of a stop loss having zero chance to fire since there's no price
    # to check against. Shortened so a real rug gets flagged and stops eating polling
    # cycles faster, without being so short that a brief API hiccup counts as dead.
    dead_token_timeout_seconds: int = field(
        default_factory=lambda: int(os.getenv("DEAD_TOKEN_TIMEOUT_SECONDS", "300"))
    )

    # Rejects a single price reading implying more than this multiple of a move from
    # entry (up or down) as an API glitch rather than a real price. The most extreme
    # real move observed in testing was ~3.8x; 50x leaves huge headroom above any real
    # case while catching garbage readings (observed: a 5000x bad tick). Set to 0 to
    # disable (trust every reading as-is).
    max_price_jump_multiple: float = field(
        default_factory=lambda: float(os.getenv("MAX_PRICE_JUMP_MULTIPLE", "50"))
    )

    enable_partial_exit: bool = field(default_factory=lambda: _bool("ENABLE_PARTIAL_EXIT", False))
    partial_exit_pct: float = field(default_factory=lambda: float(os.getenv("PARTIAL_EXIT_PCT", "50")))
    trailing_stop_bps: int = field(default_factory=lambda: int(os.getenv("TRAILING_STOP_BPS", "500")))

    # "Solana 369 System": -3% stop loss (stop_loss_pct above), first leg takes profit at
    # +6%, second leg at +9%, instead of the second leg trailing. Overrides the trailing
    # leg from ENABLE_PARTIAL_EXIT when enabled.
    enable_369_system: bool = field(default_factory=lambda: _bool("ENABLE_369_SYSTEM", False))
    take_profit_pct_2: float = field(default_factory=lambda: float(os.getenv("TAKE_PROFIT_PCT_2", "9")))

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
