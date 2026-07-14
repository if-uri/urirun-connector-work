"""IFURI-200: łańcuch native→agent://→human. Pinuje 10 przypadków z acceptance criteria."""
from urirun_connector_work import executor as ex

LEASE = {"id": "L1", "worker": "w1"}
T = {"id": "IFURI-200", "name": "test ticket"}

def _native_ok(t): return {"ok": True, "changed": "x"}
def _native_no_exec(t): return {"ok": False, "error": "client command failed"}
def _native_other(t): return {"ok": False, "error": "test failed 2/3"}

def test_1_native_success_agent_not_called(monkeypatch):
    monkeypatch.setattr(ex, "run_agent_executor", lambda *a, **k: (_ for _ in ()).throw(AssertionError("agent nie powinien być wołany")))
    r = ex.execute_ticket(T, LEASE, native_runner=_native_ok, mode="soft")
    assert r["ok"] and r["executor"] == "native"

def test_2_native_no_executor_calls_agent(monkeypatch):
    called = {}
    def _ag(*a, **k):
        called["y"] = 1
        return {"ok": True, "executor": "agent://claude"}
    monkeypatch.setattr(ex, "run_agent_executor", _ag)
    r = ex.execute_ticket(T, LEASE, native_runner=_native_no_exec, mode="soft")
    assert called and r["ok"] and r["executor"] == "agent://claude"

def test_3_agent_success_executor_marked(monkeypatch):
    monkeypatch.setattr(ex, "run_agent_executor", lambda *a, **k: {"ok": True, "executor": "agent://codex"})
    r = ex.execute_ticket(T, LEASE, native_runner=_native_no_exec, mode="soft")
    assert r["executor"] == "agent://codex" and r["executor_chain"]["agent"] == "success"

def test_4_agent_unavailable_human_escalation_no_retry(monkeypatch):
    monkeypatch.setattr(ex, "run_agent_executor", lambda *a, **k: {"ok": False, "category": "agent_missing"})
    r = ex.execute_ticket(T, LEASE, native_runner=_native_no_exec, mode="soft")
    assert r["ok"] is False and r["retry"] is False and r["actor"] == "human"
    assert r["executor_chain"]["human"] == "escalated retry=false"

def test_5_6_7_non_executor_error_no_agent(monkeypatch):
    monkeypatch.setattr(ex, "run_agent_executor", lambda *a, **k: (_ for _ in ()).throw(AssertionError("nie fallbackować nie-executorowego")))
    r = ex.execute_ticket(T, LEASE, native_runner=_native_other, mode="soft")
    assert r["ok"] is False and r["executor"] == "native" and "nie-executorowy" in r["note"]

def test_8_no_active_lease_agent_denied(monkeypatch):
    monkeypatch.setattr(ex, "_lease_active", lambda l: False)
    r = ex.run_agent_executor(T, {"id": "DEAD"}, project="/tmp")
    assert r["ok"] is False and r["denied"] and r["category"] == "no_lease"

def test_9_provenance_has_executor_chain(monkeypatch):
    monkeypatch.setattr(ex, "run_agent_executor", lambda *a, **k: {"ok": True, "executor": "agent://claude"})
    r = ex.execute_ticket(T, LEASE, native_runner=_native_no_exec, mode="soft")
    assert "executor_chain" in r and set(r["executor_chain"]) == {"native", "agent", "twin_human", "human"}

def test_10_shadow_measures_not_executes(monkeypatch):
    monkeypatch.setattr(ex, "agents_available", lambda: ["claude"])
    monkeypatch.setattr(ex, "run_agent_executor", lambda *a, **k: (_ for _ in ()).throw(AssertionError("shadow nie wykonuje")))
    r = ex.execute_ticket(T, LEASE, native_runner=_native_no_exec, mode="shadow")
    assert r["shadow"] and r["agent_available"] and r["executor_chain"]["agent"].startswith("would_try")

def test_is_no_executor_classification():
    assert ex.is_no_executor({"error": "no viable control route"})
    assert ex.is_no_executor({"category": "no_executor"})
    assert not ex.is_no_executor({"error": "missing_secret"})
    assert not ex.is_no_executor({"error": "blocked_by_proxy_policy"})

def test_off_mode_escalates(monkeypatch):
    r = ex.execute_ticket(T, LEASE, native_runner=_native_no_exec, mode="off")
    assert r["retry"] is False and r["actor"] == "human"
