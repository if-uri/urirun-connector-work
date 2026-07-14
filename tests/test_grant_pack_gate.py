"""Contract seam: grant-pack (urirun-connector-grants) ↔ work:// claim-next gate.

Two pieces were built in parallel — grant-packs (grants.packs) and the runnable-gate
(work.gates). This test pins their contract end-to-end so a future edit to either side can't
silently break the ecosystem authorization: issue a pack → the gate allows a pack-allowed
action and gates a pack-gated one → revoke (kill-switch) → the pack no longer authorizes.
"""
from __future__ import annotations

import pytest

pytest.importorskip("urirun_connector_grants")
from urirun_connector_grants import packs  # noqa: E402
from urirun_connector_work import gates  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_pack_store(tmp_path, monkeypatch):
    monkeypatch.setattr(packs, "_ACTIVE", tmp_path / "active.json")
    monkeypatch.setattr(packs, "_KEYFILE", tmp_path / "issuer.key")
    yield


def _ticket(action: str) -> dict:
    return {"id": f"T-{action}", "priority": "high", "files": ["a.py"],
            "labels": [f"needs-grant:{action}"], "name": f"do {action}", "blocked_by": []}


def test_infra_pack_allows_pip_install_and_gates_pypi_publish():
    if "infra-proxy-test" not in packs._profiles():
        pytest.skip("grant_packs.yaml profiles not installed")
    packs.issue("infra-proxy-test", ttl_minutes=60, targets=["koru-worker-*", "host"])

    allow = gates.runnable_gate(_ticket("pip.install"))
    assert allow.get("runnable") is True

    gated = gates.runnable_gate(_ticket("pypi.publish"))
    assert gated.get("runnable") is False  # pack gates publish → not runnable without unlock


def test_hard_denied_action_never_allowed_even_under_breakglass():
    if "dev-breakglass" not in packs._profiles():
        pytest.skip("profiles not installed")
    packs.issue("dev-breakglass", ttl_minutes=60, targets=["koru-worker-*", "host"])
    # the worst classes stay denied even under a near-everything break-glass pack
    assert packs.pack_allows("exfiltrate_secrets", agent="koru-worker-1")["allowed"] is False
    assert packs.pack_allows("credential.export", agent="koru-worker-1")["allowed"] is False


def test_revoke_is_a_kill_switch():
    if "infra-proxy-test" not in packs._profiles():
        pytest.skip("profiles not installed")
    r = packs.issue("infra-proxy-test", ttl_minutes=60, targets=["koru-worker-*"])
    assert packs.active() is not None
    packs.revoke(r["pack_id"])
    assert packs.active() is None  # ecosystem loses the pack's authorization at once


def test_containment_narrows_runnable_gate(monkeypatch, tmp_path):
    """IFURI-192: an active containment on a ticket's dimension makes runnable_gate block it."""
    sen = pytest.importorskip("urirun_connector_sentinel").core
    monkeypatch.setattr(sen, "_CONT", tmp_path / "cont.json")
    monkeypatch.setattr(sen, "_TRUST", tmp_path / "trust.json")
    tk = {"id": "T-K", "priority": "normal", "files": ["a.py"], "labels": ["kvm"], "name": "kvm work", "blocked_by": []}
    # no containment → gate does not block on containment
    assert gates._containment_check(tk, "command") is None
    # quarantine the kvm connector → the ticket's dimension is contained
    sen.containment_apply({"connector": "kvm"}, "quarantine", reason="kvm drift")
    blk = gates._containment_check(tk, "command")
    assert blk is not None and blk["blocked_by"] == "containment"
