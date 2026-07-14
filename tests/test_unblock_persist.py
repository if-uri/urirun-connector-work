"""ODBLOKUJ RAZ = ZAPAMIĘTANE: gate honoruje trwałe odblokowanie człowieka (nie re-pyta)."""
from urirun_connector_grants import unblock_ledger as ul
from urirun_connector_work import gates

def test_unblock_makes_ticket_runnable_forever(tmp_path, monkeypatch):
    monkeypatch.setattr(ul, "_LEDGER", tmp_path / "ledger.json")
    # non-irreversible action: ledger "unblock once" IS allowed to make it runnable forever
    t = {"id": "IFURI-043", "name": "[UNLOCK] node.restart lenovo", "labels": ["unlock", "actor:human"]}
    # przed odblokowaniem: human-ticket bez delegacji → NIE runnable
    g1 = gates.runnable_gate(t)
    # człowiek odblokowuje RAZ
    ul.record_unblock("IFURI-043", by="human", note="odblokowane w /work")
    g2 = gates.runnable_gate(t)
    assert g2["runnable"] is True and g2["human_unblocked"] is True
    # i pamiętane trwale (nowy odczyt)
    assert ul.is_unblocked("IFURI-043") is True

def test_unblock_never_forever_for_irreversible_publish(tmp_path, monkeypatch):
    # publikacja (pypi.publish) to klasa _NEVER_UNBLOCK: jedno odblokowanie NIE staje się trwałym
    # grantem — musi re-gejtować (delegacja/twin) na każdym kolejnym runie, nawet gdy ledger mówi unblocked
    monkeypatch.setattr(ul, "_LEDGER", tmp_path / "ledger2.json")
    t = {"id": "IFURI-999", "name": "[UNLOCK] pypi.publish urirun-connector-x", "labels": ["unlock", "actor:human"]}
    ul.record_unblock("IFURI-999", by="human", note="odblokowane w /work")
    assert ul.is_unblocked("IFURI-999") is True  # ledger record IS persisted...
    g = gates.runnable_gate(t)
    assert g.get("human_unblocked") is not True  # ...but the gate refuses to treat it as a standing grant

def test_revoke_unblock(tmp_path, monkeypatch):
    monkeypatch.setattr(ul, "_LEDGER", tmp_path / "l.json")
    ul.record_unblock("X-1", by="human")
    assert ul.is_unblocked("X-1")
    ul.revoke_unblock("X-1")
    assert not ul.is_unblocked("X-1")

def test_not_unblocked_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(ul, "_LEDGER", tmp_path / "l2.json")
    assert ul.is_unblocked("NEVER") is False


def test_unblock_by_wait_gate_passes_second_ticket(tmp_path, monkeypatch):
    """Odblokowanie IFURI-A (wait-gate:waiting_node) przepuszcza IFURI-B (waiting:node) bez kolejnego kliku."""
    monkeypatch.setattr(ul, "_LEDGER", tmp_path / "ledger-type.json")
    t_a = {"id": "IFURI-A", "name": "[GATE] waiting_node — czekanie",
           "labels": ["wait-gate:waiting_node", "actor:human"]}
    t_b = {"id": "IFURI-B", "name": "Wdróż connector na lenovo", "labels": ["waiting:node", "node:lenovo"]}
    assert ul.is_unblocked_for(t_b) is False
    ul.record_unblock("IFURI-A", ticket=t_a, by="human", note="odblokowane w /work")
    g = gates.runnable_gate(t_b)
    assert g["runnable"] is True and g.get("human_unblocked") is True
    assert ul.is_unblocked_for(t_b) is True


def test_unblock_by_action_passes_second_ticket(tmp_path, monkeypatch):
    """Odblokowanie pierwszego node.restart przepuszcza kolejny ticket z tą samą akcją."""
    monkeypatch.setattr(ul, "_LEDGER", tmp_path / "ledger-action.json")
    t_a = {"id": "IFURI-210", "name": "[UNLOCK] node.restart lenovo", "labels": ["unlock", "actor:human"]}
    t_b = {"id": "IFURI-230", "name": "[UNLOCK] node.restart lenovo", "labels": ["unlock", "actor:human"]}
    ul.record_unblock("IFURI-210", ticket=t_a, by="human")
    g = gates.runnable_gate(t_b)
    assert g["runnable"] is True and g.get("human_unblocked") is True


def test_decision_keys_include_wait_aliases():
    keys = ul.decision_keys({"id": "X", "labels": ["wait-gate:waiting_node"]})
    assert "wait-gate:waiting_node" in keys
    assert "waiting:node" in keys


def test_list_type_grants_excludes_ticket_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(ul, "_LEDGER", tmp_path / "board.json")
    ul.record_unblock("IFURI-210", ticket={"id": "IFURI-210", "labels": ["wait-gate:waiting_node"]})
    types = ul.list_type_grants()
    tickets = ul.list_ticket_grants()
    assert any(r.get("key") == "waiting:node" for r in types)
    assert not any(r.get("key") == "IFURI-210" for r in types)
    assert any(r.get("ticket") == "IFURI-210" for r in tickets)
