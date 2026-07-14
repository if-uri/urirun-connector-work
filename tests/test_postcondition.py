"""IFURI-197/198: kontrakt weryfikacji — ok:true ≠ efekt; brak _meta = brak dowodu."""
from urirun_connector_work import postcondition as pc

_META = {"module": "kvm", "ranOn": "lenovo", "sha": "abc"}

def test_verified_when_effect_and_provenance():
    r = pc.classify({"ok": True, "effect": True, "_meta": _META})
    assert r["verdict"] == "verified" and not r["violations"]

def test_runtime_lies_ok_flagged():
    r = pc.classify({"ok": True, "effect": False, "_meta": _META})
    assert "runtime-lies-ok" in r["violations"] and r["verdict"] == "violation"

def test_effect_unknown_flagged():
    r = pc.classify({"ok": True, "_meta": _META})  # brak effect
    assert "effect-unknown" in r["violations"]

def test_no_provenance_flagged():
    r = pc.classify({"ok": True, "effect": True})  # brak _meta
    assert "no-provenance" in r["violations"]

def test_meta_in_value_envelope():
    r = pc.classify({"ok": True, "effect": True, "value": {"_meta": _META}})
    assert r["provenance"] is True and r["verdict"] == "verified"

def test_postcondition_check():
    assert "postcondition-failed" in pc.classify({"ok": True, "effect": True, "_meta": _META}, expect={"contains": "Junk"})["violations"]
    assert not pc.classify({"ok": True, "effect": True, "_meta": _META, "moved_to": "Junk"}, expect={"contains": "Junk"})["violations"]

def test_enforce_strict_downgrades_ok():
    r = pc.enforce({"ok": True, "effect": False, "_meta": _META}, strict=True)
    assert r["ok"] is False and r["verified"] is False and r["contract_violations"]

def test_enforce_soft_attaches_verification():
    r = pc.enforce({"ok": True, "effect": True, "_meta": _META})
    assert r["ok"] is True and r["_verification"]["verdict"] == "verified"

def test_non_mutating_skips_effect_requirement():
    r = pc.classify({"ok": True}, mutating=False)  # query: nie wymaga effect/prov
    assert r["verdict"] in ("unverified", "verified") and not r["violations"]
