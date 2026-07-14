"""E2E: policy∧pack∧proxy∧delegation∧readiness∧lock jako jeden system (sieć zmockowana)."""
import pytest
from urirun_connector_grants import packs, events, core as g
from urirun_connector_work import gates

@pytest.fixture
def cp(tmp_path, monkeypatch):
    monkeypatch.setattr(packs, "_ACTIVE", tmp_path/"active.json")
    monkeypatch.setattr(packs, "_KEYFILE", tmp_path/"key")
    monkeypatch.setattr(packs, "_SYNC", tmp_path/"sync.json")
    monkeypatch.setattr(events, "_TRACE", tmp_path/"trace.jsonl")
    monkeypatch.setattr(g, "_GRANTS", tmp_path/"grants.json")
    monkeypatch.setattr(g, "_UNLOCKS", tmp_path/"unlocks.json")
    monkeypatch.setattr(g, "_unlock_ticket", lambda p, e: None)
    monkeypatch.setattr(gates, "node_readiness", lambda n, timeout=3.0: {"node": n, "ready": False, "repair_allowed": False, "reason": "offline"})
    return tmp_path

def _gate(tid, name, labels, desc=""):
    return gates.runnable_gate({"id": tid, "name": name, "labels": labels, "description": desc})

def test_full_control_plane(cp):
    # 1. issue + sync
    iss = packs.issue("infra-proxy-test", ttl_minutes=120)
    assert iss["ok"] and iss["signed"]
    assert packs.sync()["ok"] and packs.verify()["valid"]
    # 2. gate: autonomiczna akcja w packu → runnable
    assert _gate("T2", "wygeneruj connector youtube", ["connector-gen"], "connector.generate")["runnable"]
    # 3. human pypi.publish generated connector → delegated → runnable
    r = _gate("T3", "[UNLOCK] pypi.publish urirun-connector-x", ["unlock", "actor:human"], "urirun-connector-x 0.1.0")
    assert r["runnable"] and r["delegation"] == "approve_delegated"
    # 4. human linkedin → escalate
    assert _gate("T4", "[UNLOCK] linkedin.publish", ["unlock", "actor:human"], "post")["runnable"] is False
    # 5. node offline → readiness block
    assert _gate("T5", "restart lenovo", ["node:lenovo"], "node.restart")["runnable"] is False
    # 6. hard-deny mimo packa
    assert packs.pack_allows("exfiltrate_secrets")["decision"] == "deny"
    # 7. trace pack_id
    events.append("ticket_claimed", ticket="T2")
    assert events.tail(3, event="ticket_claimed")[-1]["pack_id"] == iss["pack_id"]
    # 8. revoke → ekosystem stop
    packs.revoke()
    assert packs.active() is None and packs.pack_allows("pip.install")["decision"] == "no_pack"
