"""IFURI-199: brama pracy shadow/soft/hard — claim-next jako jedyna brama."""
from urirun_connector_work import work_gate, core

def test_hard_only_claim_next(monkeypatch):
    monkeypatch.setattr(core, "claim_next", lambda w, **k: {"ticket": {"id": "T1"}, "lease": {"id": "L1"}, "blocked": []})
    r = work_gate.next_work("w1", mode="hard")
    assert r["mode"] == "hard" and r["source"] == "claim-next" and r["ticket"]["id"] == "T1" and r["lease"]

def test_hard_no_lease_no_work(monkeypatch):
    monkeypatch.setattr(core, "claim_next", lambda w, **k: {"ticket": None, "blocked": [{"ticket": "X"}]})
    r = work_gate.next_work("w1", mode="hard")
    assert r["ticket"] is None and r["lease"] is None  # bez lease nie wolno pracować

def test_soft_prefers_claim_next(monkeypatch):
    monkeypatch.setattr(core, "claim_next", lambda w, **k: {"ticket": {"id": "C1"}, "lease": {"id": "L"}})
    assert work_gate.next_work("w1", mode="soft")["source"] == "claim-next"

def test_soft_fallback_legacy(monkeypatch):
    monkeypatch.setattr(core, "claim_next", lambda w, **k: {"ticket": None})
    monkeypatch.setattr(work_gate, "_legacy_next", lambda p: {"id": "LEG1"})
    r = work_gate.next_work("w1", mode="soft")
    assert r["source"] == "legacy-fallback" and r["ticket"]["id"] == "LEG1" and r["lease"] is None

def test_shadow_returns_legacy_logs_diff(monkeypatch):
    monkeypatch.setattr(work_gate, "_legacy_next", lambda p: {"id": "LEG"})
    monkeypatch.setattr(core, "claim_next", lambda w, **k: {"ticket": {"id": "CLAIM"}, "blocked": []})
    r = work_gate.next_work("w1", mode="shadow")
    assert r["source"] == "legacy" and r["ticket"]["id"] == "LEG" and r["shadow_would_claim"] == "CLAIM" and r["diff"] is True

def test_default_mode_is_hard(monkeypatch):
    monkeypatch.delenv("KORU_WORK_GATE", raising=False)
    from urirun_connector_work import ticket_source as ts
    assert ts.gate_mode() == "hard"
