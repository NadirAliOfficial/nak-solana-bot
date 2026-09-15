from src.scanner import detect_pump


def make_history(prices, start=1_000_000, step=60):
    return [(start + i * step, p) for i, p in enumerate(prices)]


def test_detect_pump_triggers_on_15_percent_rise():
    history = make_history([1.0] * 15 + [1.16])
    is_pump, pct = detect_pump(history, window_minutes=15, threshold_pct=15.0)
    assert is_pump is True
    assert pct >= 15.0


def test_detect_pump_does_not_trigger_below_threshold():
    history = make_history([1.0] * 15 + [1.10])
    is_pump, pct = detect_pump(history, window_minutes=15, threshold_pct=15.0)
    assert is_pump is False
    assert pct < 15.0


def test_detect_pump_not_tracked_long_enough_yet():
    history = make_history([1.0, 1.20])  # only 2 samples, far short of 15 minutes
    is_pump, pct = detect_pump(history, window_minutes=15, threshold_pct=15.0)
    assert is_pump is False
    assert pct == 0.0


def test_detect_pump_falling_price():
    history = make_history([1.0] * 15 + [0.9])
    is_pump, pct = detect_pump(history, window_minutes=15, threshold_pct=15.0)
    assert is_pump is False
    assert pct < 0


def test_detect_pump_insufficient_samples():
    history = make_history([1.0])
    is_pump, pct = detect_pump(history, window_minutes=15, threshold_pct=15.0)
    assert is_pump is False
    assert pct == 0.0
