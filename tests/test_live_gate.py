"""ALLOW_LIVE gate (Hard Rule #2): non-demo + no flag => refuse to start."""
from core.execution.live_gate import check_live_start, is_demo_server


def test_demo_servers_pass():
    assert is_demo_server("Exness-MT5Trial6")
    assert is_demo_server("MetaQuotes-Demo")
    assert check_live_start("Exness-MT5Trial6", allow_live=False).allowed


def test_real_server_refused_without_flag():
    d = check_live_start("Exness-MT5Real7", allow_live=False)
    assert not d.allowed
    assert "ALLOW_LIVE" in d.reason


def test_real_server_allowed_only_with_owner_flag():
    assert check_live_start("Exness-MT5Real7", allow_live=True).allowed


def test_unknown_server_is_not_demo():
    assert not is_demo_server("")
    assert not is_demo_server(None)
    assert not is_demo_server(12345)
    assert not check_live_start("", allow_live=False).allowed
    assert check_live_start("", allow_live=True).allowed
