"""Tests for global gate env parsing (imports gate_auth; requires streamlit)."""

import gate_auth as ga


def test_load_gate_password_map_complete(monkeypatch):
    monkeypatch.setenv("VINELEDGER_GATE_USER1_PASSWORD", "a")
    monkeypatch.setenv("VINELEDGER_GATE_USER2_PASSWORD", "b")
    monkeypatch.setenv("VINELEDGER_GATE_USER3_PASSWORD", "c")
    monkeypatch.setenv("VINELEDGER_GATE_USER4_PASSWORD", "d")
    monkeypatch.setenv("VINELEDGER_GATE_USER5_PASSWORD", "e")
    pw, missing = ga.load_gate_password_map()
    assert missing == []
    assert pw == {"user1": "a", "user2": "b", "user3": "c", "user4": "d", "user5": "e"}


def test_load_gate_password_map_missing(monkeypatch):
    for k in (
        "VINELEDGER_GATE_USER1_PASSWORD",
        "VINELEDGER_GATE_USER2_PASSWORD",
        "VINELEDGER_GATE_USER3_PASSWORD",
        "VINELEDGER_GATE_USER4_PASSWORD",
        "VINELEDGER_GATE_USER5_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)
    _, missing = ga.load_gate_password_map()
    assert len(missing) == 5


def test_gate_idle_seconds_default(monkeypatch):
    monkeypatch.delenv("VINELEDGER_GATE_IDLE_SECONDS", raising=False)
    assert ga.gate_idle_seconds() == 900


def test_gate_idle_seconds_custom(monkeypatch):
    monkeypatch.setenv("VINELEDGER_GATE_IDLE_SECONDS", "120")
    assert ga.gate_idle_seconds() == 120


def test_gate_idle_seconds_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("VINELEDGER_GATE_IDLE_SECONDS", "not-a-number")
    assert ga.gate_idle_seconds() == 900


def test_gate_idle_seconds_minimum(monkeypatch):
    monkeypatch.setenv("VINELEDGER_GATE_IDLE_SECONDS", "30")
    assert ga.gate_idle_seconds() == 60
