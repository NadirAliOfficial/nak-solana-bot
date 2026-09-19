from src.config import Config


def test_config_defaults(monkeypatch):
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.delenv("POSITION_SIZE_USD", raising=False)
    monkeypatch.delenv("DASHBOARD_HOST", raising=False)
    cfg = Config()
    assert cfg.dry_run is True
    assert cfg.position_size_usd == 100.0
    assert cfg.pump_threshold_pct == 15.0
    assert cfg.take_profit_pct == 9.0
    assert cfg.take_profit_pct_2 == 9.0
    assert cfg.enable_369_system is False
    assert cfg.enable_partial_exit is False
    assert cfg.stop_loss_pct == 3.0
    assert cfg.trade_currency == "USDC"
    assert cfg.dashboard_host == "127.0.0.1"


def test_config_reads_env_overrides(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("POSITION_SIZE_USD", "50")
    monkeypatch.setenv("TRADE_CURRENCY", "SOL")
    cfg = Config()
    assert cfg.dry_run is False
    assert cfg.position_size_usd == 50.0
    assert cfg.trade_currency == "SOL"
