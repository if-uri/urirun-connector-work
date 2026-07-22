"""work:// lock+grant-aware lease control plane (IFURI-175/176)."""
from __future__ import annotations

import pytest
from urirun_work.locks import locks_conflict as shared_locks_conflict

from urirun_connector_work import core


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_STORE", tmp_path / "leases.json")
    # goal-agnostic tests must not see a live goal-delivery freeze (real current-goal.json)
    from urirun_connector_work import goal
    monkeypatch.delenv("CURRENT_GOAL", raising=False)
    monkeypatch.setattr(goal, "_GOAL_FILE", tmp_path / "no-goal.json")


# ── lock model ────────────────────────────────────────────────────────────────

def test_locks_conflict_ancestor_and_siblings():
    assert core.locks_conflict is shared_locks_conflict
    assert core.locks_conflict("repo:if-uri/urirun", "path:if-uri/urirun/adapters") is True
    assert core.locks_conflict("path:r/a", "path:r/a") is True
    assert core.locks_conflict("path:r/a", "path:r/b") is False
    assert core.locks_conflict("repo:if-uri/urirun", "repo:if-uri/urirun-flow") is False
    assert core.locks_conflict("ticket:lenovo", "node:lenovo") is False


def test_locks_for_ticket_files_heuristic_and_fallback():
    f = core.locks_for_ticket({"id": "T1", "files": ["adapters/x/core.py"]}, repo="if-uri/urirun")
    assert any(l.startswith("path:if-uri/urirun/adapters/x") for l in f)
    # heuristic: node label + sibling repo label
    h = core.locks_for_ticket({"id": "T2", "labels": ["node:lenovo", "kvm"]}, repo="if-uri/urirun")
    assert "node:lenovo" in h and "repo:if-uri/urirun-connector-kvm" in h
    # no files, no heuristic repo → coarse whole-repo fallback
    c = core.locks_for_ticket({"id": "T3"}, repo="if-uri/urirun")
    assert c == ["ticket:T3", "repo:if-uri/urirun"]


# ── 1-3, 4, 5/6: acquire / conflict / release / expiry ────────────────────────

def test_1_worker_gets_ticket_when_free():
    assert core.acquire("T1", "w1", ["path:r/a"])["granted"] is True


def test_2_second_worker_blocked_same_path():
    core.acquire("T1", "w1", ["path:r/a"])
    b = core.acquire("T2", "w2", ["path:r/a/sub"])
    assert b["granted"] is False and b["conflict"]["held_by"] == "w1"


def test_3_second_worker_gets_different_repo():
    core.acquire("T1", "w1", ["repo:if-uri/urirun"])
    assert core.acquire("T2", "w2", ["repo:if-uri/urirun-flow"])["granted"] is True


def test_4_release_frees_lock():
    core.acquire("T1", "w1", ["path:r/a"])
    assert core.acquire("T2", "w2", ["path:r/a"])["granted"] is False
    core.release(worker="w1")
    assert core.acquire("T2", "w2", ["path:r/a"])["granted"] is True


def test_5_6_expired_lease_returns_ticket_to_pool(monkeypatch):
    core.acquire("T1", "w1", ["path:r/a"], ttl_minutes=30)
    real = core.time.time
    monkeypatch.setattr(core.time, "time", lambda: real() + 4000)  # past TTL == dead worker
    assert core.acquire("T2", "w2", ["path:r/a"])["granted"] is True


# ── 7: human/grant-blocked not claimed ────────────────────────────────────────

def test_7_blocked_reason_human_and_grant():
    assert core.blocked_reason({"id": "T", "labels": ["actor:human"]})["reason"].startswith("actor:human")
    g = core.blocked_reason({"id": "T", "labels": ["needs-grant:node.lenovo.deploy"]})
    assert "missing grant" in g["reason"] and g["unlock_uri"].startswith("unlock://")
    assert core.blocked_reason({"id": "T", "name": "Zainstaluj signal-cli na lenovo"}) is not None
    assert core.blocked_reason({"id": "T", "name": "Refactor parser"}) is None


def test_7_claim_next_skips_blocked(monkeypatch):
    tickets = [
        {"id": "H1", "priority": "high", "files": [], "labels": ["actor:human"], "name": "link signal"},
        {"id": "C1", "priority": "high", "files": ["a.py"], "labels": [], "name": "code"},
    ]
    monkeypatch.setattr(core, "_open_tickets", lambda s, e, p: tickets)
    r = core.claim_next("w1")
    assert r["ticket"]["id"] == "C1"
    assert any(b["ticket"] == "H1" for b in r["blocked"])


# ── 8: lane respected ─────────────────────────────────────────────────────────

def test_8_claim_next_respects_lane(monkeypatch):
    tickets = [
        {"id": "F1", "priority": "high", "files": ["a.py"], "labels": ["lane:fleet"], "name": "x"},
        {"id": "S1", "priority": "high", "files": ["b.py"], "labels": ["lane:scheduler"], "name": "y"},
    ]
    monkeypatch.setattr(core, "_open_tickets", lambda s, e, p: tickets)
    r = core.claim_next("w1", lane="scheduler")
    assert r["ticket"]["id"] == "S1"  # F1 is off-lane


def test_8_unlaned_ticket_claimable_by_any_lane(monkeypatch):
    monkeypatch.setattr(core, "_open_tickets", lambda s, e, p:
                        [{"id": "U1", "priority": "high", "files": ["a.py"], "labels": [], "name": "x"}])
    assert core.claim_next("w1", lane="fleet")["ticket"]["id"] == "U1"


# ── 9: foreign release rejected + renew ownership ──────────────────────────────

def test_9_foreign_release_rejected():
    lease = core.acquire("T1", "w1", ["path:r/a"])["lease"]
    r = core.release(worker="w2", lease_id=lease["id"])
    assert r.get("rejected") is True and r["ok"] is False
    # owner can release
    assert core.release(worker="w1", lease_id=lease["id"])["released"] == 1


def test_renew_extends_and_rejects_foreign():
    lease = core.acquire("T1", "w1", ["path:r/a"], ttl_minutes=1)["lease"]
    r = core.renew(lease["id"], "w1", ttl_seconds=3600, status="running_tests")
    assert r["ok"] and r["lease"]["status"] == "running_tests" and r["lease"]["expires"] > lease["expires"]
    assert core.renew(lease["id"], "w2")["rejected"] is True


# ── scheduler self-distribution + contextmanager finally ──────────────────────

def test_claim_next_two_workers_parallel_on_disjoint_files(monkeypatch):
    tickets = [
        {"id": "A", "priority": "high", "files": ["x/a.py"], "labels": [], "name": "a"},
        {"id": "B", "priority": "high", "files": ["x/b.py"], "labels": [], "name": "b"},
    ]
    monkeypatch.setattr(core, "_open_tickets", lambda s, e, p: tickets)
    a = core.claim_next("w1")
    b = core.claim_next("w2")
    assert {a["ticket"]["id"], b["ticket"]["id"]} == {"A", "B"}  # parallel, no collision


def test_leased_contextmanager_releases_on_exception(monkeypatch):
    monkeypatch.setattr(core, "_open_tickets", lambda s, e, p:
                        [{"id": "A", "priority": "high", "files": ["a.py"], "labels": [], "name": "a"}])
    with pytest.raises(RuntimeError):
        with core.leased("w1") as claim:
            assert claim["ticket"]["id"] == "A"
            raise RuntimeError("work blew up")
    # lease must be gone despite the crash
    assert core.locks_query_list()["count"] == 0


def test_workers_status_and_locks_list():
    core.acquire("T1", "w1", ["path:r/a"])
    core.acquire("T2", "w2", ["path:r/b"])
    assert set(core.workers_query_status()["workers"]) == {"w1", "w2"}
    assert core.locks_query_list()["count"] == 2


# ── IFURI-170: run_action — the executor a claimed action-ticket was missing ──────────────

def test_run_action_known_action_runs_deterministic_script(monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        class R:
            returncode = 0
            stdout = "ok"
            stderr = ""
        return R()
    monkeypatch.setattr(core.subprocess, "run", fake_run)
    t = {"id": "IFURI-170", "name": "[UNLOCK] pypi.publish", "labels": ["unlock"]}
    res = core.run_action(t)
    assert res["ok"] and res["executor"] == "script" and res["executed_action"] == "pypi.publish"
    assert calls and calls[0][-1].endswith("publish_release_chain.py")
    assert "--publish" not in calls[0]  # plan-only unless explicitly requested


def test_run_action_publish_flag_appends_publish_argv(monkeypatch):
    calls = []
    monkeypatch.setattr(core.subprocess, "run", lambda argv, **kw: calls.append(argv) or type(
        "R", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    core.run_action({"id": "T", "name": "[UNLOCK] pypi.publish", "labels": ["unlock"]}, publish=True)
    assert calls[0][-1] == "--publish"


def test_run_action_unknown_action_falls_back_to_agent(monkeypatch):
    import types
    fake_agents = types.SimpleNamespace(task_run=lambda **kw: {"ok": True, "agent": "claude", **kw})
    monkeypatch.setitem(__import__("sys").modules, "urirun_connector_agents.core", fake_agents)
    monkeypatch.setitem(__import__("sys").modules, "urirun_connector_agents", types.SimpleNamespace(core=fake_agents))
    t = {"id": "T2", "name": "[UNLOCK] linkedin.publish", "labels": ["unlock"]}
    res = core.run_action(t)
    assert res["ok"] and res["executor"] == "agent" and res["executed_action"] == "linkedin.publish"


def test_run_action_no_derivable_action_fails():
    res = core.run_action({"id": "T3", "name": "coś lokalnego bez akcji", "labels": []})
    assert res["ok"] is False


def test_lease_command_run_action_requires_active_lease():
    res = core.lease_command_run_action(ticket="NOPE", worker="w1")
    assert res["ok"] is False and "lease" in res["error"]


def test_lease_command_run_action_with_lease_dispatches(monkeypatch):
    core.acquire("IFURI-170", "w1", ["ticket:IFURI-170"])
    monkeypatch.setattr(core, "_open_tickets", lambda *a, **kw: [
        {"id": "IFURI-170", "name": "[UNLOCK] pypi.publish", "labels": ["unlock"]}])
    monkeypatch.setattr(core.subprocess, "run", lambda argv, **kw: type(
        "R", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    res = core.lease_command_run_action(ticket="IFURI-170", worker="w1")
    assert res["ok"] and res["executed_action"] == "pypi.publish"
