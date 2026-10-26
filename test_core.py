import time
import pytest
from main import RingBuffer, MetricSample, SafetyGuard, AlertRule

def test_ring_buffer():
    buf = RingBuffer(capacity=3)
    s1 = MetricSample(10, 20, 30, 100, True)
    s2 = MetricSample(15, 25, 35, 100, True)
    
    buf.add(s1)
    buf.add(s2)
    snapshot = buf.snapshot()
    
    assert len(snapshot) == 2
    assert snapshot[0].cpu_pct == 10
    assert snapshot[1].cpu_pct == 15

def test_safety_guard():
    assert SafetyGuard.is_protected("system", 0) is True
    assert SafetyGuard.is_protected("explorer.exe", 1042) is True
    assert SafetyGuard.is_protected("notepad.exe", 5012) is False

def test_alert_rule_hysteresis():
    rule = AlertRule(metric='cpu', threshold=80.0, hold_s=1, cooldown_s=10)
    alerts_triggered = []

    def mock_notifier(title, msg):
        alerts_triggered.append((title, msg))

    # Sample below threshold
    s_low = MetricSample(50.0, 50.0, 50.0, 100, True)
    rule.evaluate(s_low, mock_notifier)
    assert len(alerts_triggered) == 0

    # Sample above threshold (First hit)
    s_high = MetricSample(90.0, 50.0, 50.0, 100, True)
    rule.evaluate(s_high, mock_notifier)
    # Should not trigger immediately because of hold duration
    assert len(alerts_triggered) == 0

    # Wait for hold duration to elapse
    time.sleep(1.1)
    rule.evaluate(s_high, mock_notifier)
    assert len(alerts_triggered) == 1