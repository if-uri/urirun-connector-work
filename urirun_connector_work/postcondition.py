# Author: Tom Sapletta · Part of the ifURI solution.
"""IFURI-197/198 — kontrakt weryfikacji: `ok:true` ≠ efekt osiągnięty; brak `_meta` = brak dowodu.

Fundament autonomii: agent NIE może wierzyć w fałszywe sukcesy. Każdy wynik `command` (mutujący)
MUSI nieść dowód: `effect` (czy skutek zaszedł), `verified` (czy potwierdzony), `_meta.provenance`
(co/gdzie realnie wykonało). Bez tego runtime „kłamie ok" i agent działa na fałszywej przesłance.

  classify() — werdykt wg kontraktu (verified / unverified / violation) + lista naruszeń.
  enforce()  — dokleja _verification; strict=True degraduje ok→false przy naruszeniu (twardy gate).
"""
from __future__ import annotations

from typing import Any

# naruszenia kontraktu
_RUNTIME_LIES = "runtime-lies-ok"     # ok:true ale effect:false
_EFFECT_UNKNOWN = "effect-unknown"    # ok:true bez flagi effect (niezweryfikowany skutek)
_NO_PROVENANCE = "no-provenance"      # brak _meta (brak dowodu co wykonało)
_POSTCOND_FAILED = "postcondition-failed"


def _extract_meta(result: dict) -> dict | None:
    """Znajdź _meta w kopercie lub w value (runtime może pakować efekt w value)."""
    m = result.get("_meta")
    if isinstance(m, dict):
        return m
    val = result.get("value")
    if isinstance(val, dict) and isinstance(val.get("_meta"), dict):
        return val["_meta"]
    return None


def _has_provenance(meta: dict | None) -> bool:
    return bool(meta and any(meta.get(k) for k in ("module", "version", "source", "ranOn", "sha", "file")))


def _check_postcondition(result: dict, expect: Any) -> bool:
    """Prosty postcondition: expect jako {field: value} lub {'contains': text} nad wynikiem."""
    if isinstance(expect, dict):
        if "contains" in expect:
            return str(expect["contains"]).lower() in str(result).lower()
        return all(str(result.get(k)) == str(v) for k, v in expect.items())
    return bool(expect)


def classify(result: dict | None, uri: str = "", expect: Any = None, mutating: bool = True) -> dict[str, Any]:
    """Werdykt kontraktu weryfikacji dla wyniku command. Read-only, nie rzuca."""
    r = result or {}
    ok = r.get("ok")
    effect = r.get("effect")
    verified = r.get("verified")
    meta = _extract_meta(r)
    has_prov = _has_provenance(meta)
    violations: list[str] = []

    if mutating:
        if ok and effect is False:
            violations.append(_RUNTIME_LIES)
        elif ok and effect is None:
            violations.append(_EFFECT_UNKNOWN)
        if not has_prov:
            violations.append(_NO_PROVENANCE)
    if expect is not None and not _check_postcondition(r, expect):
        violations.append(_POSTCOND_FAILED)

    if violations:
        verdict = "violation"
    elif ok and effect and has_prov:
        verdict = "verified"
    else:
        verdict = "unverified"
    return {"uri": uri, "ok": ok, "effect": effect, "verified": verified, "provenance": has_prov,
            "violations": violations, "verdict": verdict}


def enforce(result: dict | None, uri: str = "", expect: Any = None, *, strict: bool = False,
            mutating: bool = True) -> dict[str, Any]:
    """Dokleja `_verification`. strict=True: naruszenie → ok:false (twardy gate — agent nie ufa)."""
    r = dict(result or {})
    c = classify(r, uri, expect, mutating)
    r["_verification"] = c
    if strict and c["violations"]:
        r["ok"] = False
        r["verified"] = False
        r["contract_violations"] = c["violations"]
    return r
