from urirun_connector_work import gates

def test_normal_ticket_runnable():
    t = {"id": "T1", "name": "refaktor helpera x", "labels": []}
    assert gates.runnable_gate(t)["runnable"] is True

def test_action_derivation():
    assert gates.action_of_ticket({"name": "[UNLOCK] pypi.publish", "labels": ["unlock"]}) == "pypi.publish"
    assert gates.action_of_ticket({"name": "Opublikuj post na LinkedIn", "labels": []}) == "linkedin.publish"
    assert gates.action_of_ticket({"name": "coś lokalnego", "labels": ["action:test.run"]}) == "test.run"

def test_human_pypi_in_delegation_becomes_runnable():
    # [UNLOCK] pypi.publish generated connector → twin approve_delegated → runnable
    t = {"id": "IFURI-170", "name": "[UNLOCK] pypi.publish urirun-connector-signal",
         "description": "publikacja urirun-connector-signal", "labels": ["unlock", "actor:human"]}
    g = gates.runnable_gate(t)
    assert g.get("delegation") == "approve_delegated" and g["runnable"] is True

def test_human_linkedin_stays_blocked():
    t = {"id": "IFURI-174", "name": "[UNLOCK] linkedin.publish post ifURI", "labels": ["unlock", "actor:human"]}
    g = gates.runnable_gate(t)
    assert g["runnable"] is False and g["blocked_by"] == "delegation"

def test_never_unblock_action_regates_even_if_ledger_says_unblocked(monkeypatch):
    # a human unblocked THIS ticket once — fine for a code ticket, but must NOT let an
    # irreversible publish action auto-continue forever (accidental one-off click ≠ standing grant)
    from urirun_connector_grants import unblock_ledger
    monkeypatch.setattr(unblock_ledger, "is_unblocked_for", lambda t: True)
    t = {"id": "IFURI-174", "name": "[UNLOCK] linkedin.publish post ifURI", "labels": ["unlock", "actor:human"]}
    g = gates.runnable_gate(t)
    assert g.get("human_unblocked") is not True
    assert g["runnable"] is False and g["blocked_by"] == "delegation"


def test_unblock_ledger_still_applies_to_non_irreversible_actions(monkeypatch):
    # sanity: the ledger bypass isn't disabled globally — only for the _NEVER_UNBLOCK action set
    from urirun_connector_grants import unblock_ledger
    monkeypatch.setattr(unblock_ledger, "is_unblocked_for", lambda t: True)
    t = {"id": "T-CODE", "name": "refactor helper x", "labels": []}
    g = gates.runnable_gate(t)
    assert g.get("human_unblocked") is True and g["runnable"] is True


def test_executor_for_known_action_uses_publish_script():
    argv = gates.executor_for("pypi.publish", root="/x/if-uri")
    assert argv is not None
    assert argv[-1] == "/x/if-uri/urirun/scripts/publish_release_chain.py"

def test_executor_for_unknown_action_is_none():
    assert gates.executor_for("linkedin.publish") is None

def test_node_offline_blocks(monkeypatch):
    monkeypatch.setattr(gates, "node_readiness", lambda n, timeout=3.0: {"node": n, "ready": False, "repair_allowed": False, "reason": "offline"})
    t = {"id": "N1", "name": "task na lenovo", "labels": ["node:lenovo"]}
    g = gates.runnable_gate(t)
    assert g["runnable"] is False and g["blocked_by"] == "readiness"
