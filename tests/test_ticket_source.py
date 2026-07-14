"""IFURI-199 — koru adoption of work://claim-next in 4 safe modes (off/shadow/soft/hard)."""
from __future__ import annotations

import json

import pytest

from urirun_connector_work import core, ticket_source as ts


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_STORE", tmp_path / "leases.json")
    monkeypatch.setattr(ts, "_LOG", tmp_path / "gate.jsonl")
    monkeypatch.delenv("KORU_WORK_GATE", raising=False)
    from urirun_connector_work import goal
    monkeypatch.delenv("CURRENT_GOAL", raising=False)
    monkeypatch.setattr(goal, "_GOAL_FILE", tmp_path / "no-goal.json")


def _legacy(tid="IFURI-033"):
    return lambda: {"id": tid}


def test_off_mode_is_legacy_no_lease(monkeypatch):
    monkeypatch.setenv("KORU_WORK_GATE", "off")
    item = ts.next_ticket("w1", legacy_next=_legacy())
    assert item["source"] == "legacy" and item["lease"] is None and item["ticket"]["id"] == "IFURI-033"


# ── 1. shadow logs the legacy-vs-claim-next comparison ────────────────────────

def test_shadow_mode_logs_divergence(monkeypatch, tmp_path):
    monkeypatch.setenv("KORU_WORK_GATE", "shadow")
    monkeypatch.setattr(core, "claim_next", lambda w, **k: {"ticket": {"id": "IFURI-177"}, "blocked": [{"ticket": "IFURI-033"}]})
    item = ts.next_ticket("w1", legacy_next=_legacy("IFURI-033"))
    # shadow still returns the legacy ticket (does not steer production)
    assert item["source"] == "legacy(shadow)" and item["ticket"]["id"] == "IFURI-033"
    ev = json.loads((tmp_path / "gate.jsonl").read_text().splitlines()[-1])
    assert ev["event"] == "koru.work_gate.shadow_compare"
    assert ev["legacy_ticket"] == "IFURI-033" and ev["work_ticket"] == "IFURI-177" and ev["divergence"] is True


# ── 2. soft prefers claim-next when work:// works ─────────────────────────────

def test_soft_mode_uses_claim_next(monkeypatch):
    monkeypatch.setenv("KORU_WORK_GATE", "soft")
    monkeypatch.setattr(core, "claim_next", lambda w, **k: {"ticket": {"id": "IFURI-177"}, "lease": {"id": "lease:w1:IFURI-177"}, "gate": {"policy": "ok"}})
    item = ts.next_ticket("w1", legacy_next=_legacy())
    assert item["source"] == "work://claim-next" and item["lease"]["id"] == "lease:w1:IFURI-177"
    assert item["gate"] == {"policy": "ok"}  # (8) /work sees the gate decision


def test_soft_falls_back_to_legacy_only_on_work_error(monkeypatch):
    monkeypatch.setenv("KORU_WORK_GATE", "soft")
    def _boom(w, **k):
        raise RuntimeError("work:// down")
    monkeypatch.setattr(core, "claim_next", _boom)
    item = ts.next_ticket("w1", legacy_next=_legacy())
    assert item["source"] == "legacy(degraded)"  # degraded fallback


def test_soft_idles_when_work_reachable_but_empty(monkeypatch):
    monkeypatch.setenv("KORU_WORK_GATE", "soft")
    monkeypatch.setattr(core, "claim_next", lambda w, **k: {"ticket": None, "blocked": []})
    # work:// works but nothing runnable → do NOT fall back to legacy (would defeat the gate)
    assert ts.next_ticket("w1", legacy_next=_legacy()) is None


# ── 3 + 9. hard: only claim-next; stop (no fallback) if work:// unavailable ────

def test_hard_never_starts_without_lease(monkeypatch):
    monkeypatch.setenv("KORU_WORK_GATE", "hard")
    monkeypatch.setattr(core, "claim_next", lambda w, **k: {"ticket": {"id": "X"}, "lease": None})  # no lease
    assert ts.next_ticket("w1", legacy_next=_legacy()) is None  # not started without a lease


def test_hard_stops_worker_when_work_unavailable(monkeypatch):
    monkeypatch.setenv("KORU_WORK_GATE", "hard")
    def _boom(w, **k):
        raise RuntimeError("work:// down")
    monkeypatch.setattr(core, "claim_next", _boom)
    with pytest.raises(ts.WorkGateUnavailable):
        ts.next_ticket("w1", legacy_next=_legacy())  # NO silent fallback in hard


# ── 4. frontier tickets are never claimed ─────────────────────────────────────

def test_4_frontier_not_claimed(monkeypatch):
    tickets = [
        {"id": "F1", "priority": "high", "files": ["a.py"], "labels": ["autonomy-frontier", "waiting:node"], "name": "frozen"},
        {"id": "C1", "priority": "high", "files": ["b.py"], "labels": [], "name": "code"},
    ]
    monkeypatch.setattr(core, "_open_tickets", lambda s, e, p: tickets)
    monkeypatch.setattr(core, "_gates", __import__("types").SimpleNamespace(runnable_gate=lambda t, p: {"runnable": True}), raising=False)
    # patch the lazy import inside claim_next
    import sys, types
    fake_gates = types.SimpleNamespace(runnable_gate=lambda t, p="": {"runnable": True, "action": "command"})
    monkeypatch.setitem(sys.modules, "urirun_connector_work.gates", fake_gates)
    r = core.claim_next("w1")
    assert r["ticket"]["id"] == "C1"  # F1 skipped as frozen
    assert any(b["ticket"] == "F1" and b["blocked_by"] == "autonomy-frontier" for b in r["blocked"])


# ── 6. release in finally is a no-op for legacy, releases a lease otherwise ────

def test_release_and_renew_no_lease_are_noops():
    assert ts.release({"ticket": {"id": "X"}, "lease": None}, "w1")["released"] == 0
    assert ts.renew({"lease": None}, "w1")["no_lease"] is True


def test_dry_run_reports_without_acquiring(monkeypatch):
    tickets = [{"id": "A", "priority": "high", "files": ["a.py"], "labels": [], "name": "a"}]
    monkeypatch.setattr(core, "_open_tickets", lambda s, e, p: tickets)
    import sys, types
    monkeypatch.setitem(sys.modules, "urirun_connector_work.gates",
                        types.SimpleNamespace(runnable_gate=lambda t, p="": {"runnable": True}))
    r = core.claim_next("w1", dry_run=True)
    assert r["would_claim"] is True and r["lease"] is None
    assert core.locks_query_list()["count"] == 0  # nothing reserved


# ── /work rollout observability ───────────────────────────────────────────────

def test_shadow_log_marks_legacy_frontier(monkeypatch, tmp_path):
    monkeypatch.setenv("KORU_WORK_GATE", "shadow")
    # work:// blocks the legacy pick as autonomy-frontier
    monkeypatch.setattr(core, "claim_next", lambda w, **k: {
        "ticket": {"id": "IFURI-CODE"}, "blocked": [{"ticket": "IFURI-033", "blocked_by": "autonomy-frontier"}]})
    ts.next_ticket("w1", legacy_next=lambda: {"id": "IFURI-033"})
    ev = json.loads((tmp_path / "gate.jsonl").read_text().splitlines()[-1])
    assert ev["legacy_is_frontier"] is True and ev["work_skipped_reason"] == "autonomy-frontier"


def test_rollout_status_reports_health(monkeypatch, tmp_path):
    monkeypatch.setenv("KORU_WORK_GATE", "shadow")
    monkeypatch.setattr(core, "claim_next", lambda w, **k: {
        "ticket": {"id": "IFURI-CODE"}, "blocked": [{"ticket": "IFURI-033", "blocked_by": "autonomy-frontier"}]})
    ts.next_ticket("w1", legacy_next=lambda: {"id": "IFURI-033"})
    st = ts.rollout_status()
    assert st["mode"] == "shadow" and st["divergences"] >= 1
    assert st["frontier_claims"] == 0 and st["gate_errors"] == 0 and st["healthy"] is True
    assert st["last_divergence"]["legacy_is_frontier"] is True
