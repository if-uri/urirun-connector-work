"""Full worker cycle — the seam between the two parallel-built halves:
  IFURI-199 selection  (ticket_source: claim-next = correct WORK selection)
  IFURI-200 execution  (executor:      native → agent:// = correct EXECUTION)
This pins their contract so an edit to either side can't silently break the loop.
"""
from __future__ import annotations

import pytest

from urirun_connector_work import core, executor as ex, ticket_source as ts


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_STORE", tmp_path / "leases.json")
    monkeypatch.setattr(ts, "_LOG", tmp_path / "gate.jsonl")
    monkeypatch.delenv("KORU_WORK_GATE", raising=False)
    monkeypatch.delenv("KORU_EXECUTOR_FALLBACK", raising=False)
    from urirun_connector_work import goal
    monkeypatch.delenv("CURRENT_GOAL", raising=False)
    monkeypatch.setattr(goal, "_GOAL_FILE", tmp_path / "no-goal.json")


def test_full_cycle_claim_native_no_executor_agent_succeeds_then_release(monkeypatch):
    monkeypatch.setenv("KORU_WORK_GATE", "soft")
    monkeypatch.setenv("KORU_EXECUTOR_FALLBACK", "soft")
    # 1. SELECTION: claim-next hands a code ticket + a real lease
    core.acquire("IFURI-CODE", "w1", ["ticket:IFURI-CODE"])
    lease = {"id": "lease:w1:IFURI-CODE", "worker": "w1"}
    monkeypatch.setattr(core, "claim_next", lambda w, **k: {"ticket": {"id": "IFURI-CODE"}, "lease": lease, "gate": {"policy": "ok"}})
    item = ts.next_ticket("w1", legacy_next=lambda: {"id": "X"})
    assert item["source"] == "work://claim-next" and item["lease"]["id"] == "lease:w1:IFURI-CODE"

    # 2. EXECUTION: native koru drive dies no_executor → agent:// takes over
    monkeypatch.setattr(ex, "run_agent_executor",
                        lambda t, l, **k: {"ok": True, "executor": "agent://host/task/command/run", "agent": "claude"})
    res = ex.execute_ticket(item["ticket"], item["lease"], mode="soft",
                            native_runner=lambda t: {"ok": False, "category": "no_executor", "error": "client command failed"})
    assert res["ok"] and res["executor"].startswith("agent://")
    assert res["executor_chain"]["native"] == "failed no_executor"
    assert res["executor_chain"]["agent"] == "success"

    # 3. RELEASE in finally
    rel = ts.release(item, "w1")
    assert rel["ok"] and rel["released"] == 1
    assert core.locks_query_list()["count"] == 0


def test_native_success_never_reaches_agent(monkeypatch):
    called = []
    monkeypatch.setattr(ex, "run_agent_executor", lambda t, l, **k: called.append(1) or {"ok": True})
    res = ex.execute_ticket({"id": "C1"}, {"id": "L1", "worker": "w1"}, mode="soft",
                            native_runner=lambda t: {"ok": True, "result": "done"})
    assert res["executor"] == "native" and res["ok"] and not called


def test_frontier_style_block_never_reaches_agent(monkeypatch):
    """A non-executor failure (waiting_node/proxy_blocked/test-fail) must NOT fall to agent://."""
    called = []
    monkeypatch.setattr(ex, "run_agent_executor", lambda t, l, **k: called.append(1) or {"ok": True})
    res = ex.execute_ticket({"id": "IFURI-033", "labels": ["autonomy-frontier", "waiting:node"]},
                            {"id": "L", "worker": "w1"}, mode="soft",
                            native_runner=lambda t: {"ok": False, "category": "waiting_node", "reason": "node offline"})
    assert res["ok"] is False and not called            # agent never tried
    assert res["executor_chain"]["agent"] is None        # not a no_executor failure


def test_agent_unavailable_escalates_human_retry_false_stops_churn(monkeypatch):
    monkeypatch.setattr(ex, "run_agent_executor", lambda t, l, **k: {"ok": False, "category": "no_executor"})
    res = ex.execute_ticket({"id": "C1"}, {"id": "L1", "worker": "w1"}, mode="hard",
                            native_runner=lambda t: {"ok": False, "category": "no_executor"})
    assert res["ok"] is False and res.get("retry") is False and res.get("actor") == "human"
    # retry=false is the signal that stops the watchdog re-driving → no NORA/DIAGNOZA churn
