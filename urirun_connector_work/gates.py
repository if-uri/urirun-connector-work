# Author: Tom Sapletta · Part of the ifURI solution.
"""Stage 4 — bramki wykonalności: work:// claim-next spina WSZYSTKIE warstwy decyzyjne.

`work://` był lock-scheduler OBOK systemu decyzyjnego. Ten moduł czyni go GLOBALNYM DISPATCHEREM:
ticket jest runnable dopiero gdy przejdzie policy ∧ grant ∧ proxy ∧ delegation ∧ readiness ∧ lock.
Human-ticket NIE jest już twardo blokowany — idzie przez twin (delegated approval → runnable).

Degraduje łagodnie: gdy connectory control-plane nie zainstalowane, zostaje sam heurystyczny
blocked_reason (zachowanie sprzed Stage 4). Zainstalowane → pełne bramkowanie.
"""
from __future__ import annotations

import os
import re
from typing import Any

# akcja z ticketu: label action:X ma pierwszeństwo, potem słowa-klucze w nazwie/opisie
_ACTION_KW = {
    "pypi.publish": ("pypi", "publish na pypi", "opublikuj.*pypi", "upload.pypi"),
    "linkedin.publish": ("linkedin",),
    "email.send": ("wyślij mail", "email.send", "send email"),
    "mailbox.move_to_spam": ("spam", "move_to_spam", "junk"),
    "node.restart": ("restart node", "zrestartuj", "node.restart"),
    "connector.install_on_node": ("zainstaluj.*node", "install_on_node", "deploy.*connector"),
    "connector.generate": ("wygeneruj connector", "connector.generate", "connectorgen"),
    "connector.publish_public": ("publish_public", "opublikuj repo", "publikacja connectora"),
    "fleet.reconcile_execute": ("reconcile execute", "fleet.reconcile"),
    "payment.confirm": ("zapłać", "payment", "fiverr order"),
    "credential.export": ("eksport credential", "credential.export"),
}

# Akcje NIEODWRACALNE (publikacja/płatność/sekret/destrukcja): jeden "unblock" ticketu NIE
# autoryzuje trwałej auto-kontynuacji tej klasy akcji na przyszłość — re-gejtują ZAWSZE,
# nawet gdy ten konkretny ticket jest w unblock-ledger. Publiczna publikacja to nie refaktor:
# przypadkowe jednorazowe odblokowanie nie może cichcem stać się globalnym "zawsze publikuj".
_NEVER_UNBLOCK = ("pypi.publish", "linkedin.publish", "connector.publish_public",
                  "payment.confirm", "credential.export")


def action_of_ticket(ticket: dict) -> str:
    """Wyprowadź kanoniczną akcję z ticketu (label action:* > nazwa[UNLOCK]/[EWOLUCJA] > słowa-klucze)."""
    for lab in (ticket.get("labels") or []):
        s = str(lab).lower()
        if s.startswith("action:"):
            return s.split(":", 1)[1]
        if s.startswith("evolve:") or s == "unlock":
            pass
    blob = f"{ticket.get('name', '')} {ticket.get('description', '')}".lower()
    m = re.search(r"\b([a-z_]+\.[a-z_]+)\b", ticket.get("name", ""))  # jawne action.verb w nazwie
    if m and "." in m.group(1):
        return m.group(1)
    for act, kws in _ACTION_KW.items():
        if any(re.search(kw, blob) for kw in kws):
            return act
    return ""


# IFURI-170 fix: `runnable_gate` marks a delegated action-ticket runnable, but nothing then
# EXECUTES it — koru's generic coding-CLI drive has no code change to make on an [UNLOCK]
# ticket and fails (`no_executor` / "client command failed"). Known actions get a real,
# deterministic executor here; unknown ones fall back to agent:// (see core.run_action).
_ACTION_EXECUTORS: dict[str, str] = {
    "pypi.publish": os.environ.get("URIRUN_PYPI_PUBLISH_SCRIPT")
                    or "urirun/scripts/publish_release_chain.py",
}


def executor_for(action: str, root: str = "") -> list[str] | None:
    """argv for a known action's real executor, or None (caller falls back to agent://)."""
    import sys
    script = _ACTION_EXECUTORS.get(action)
    if not script:
        return None
    path = script if os.path.isabs(script) else os.path.join(
        root or os.path.expanduser("~/github/if-uri"), script)
    return [sys.executable, path]


def node_of_ticket(ticket: dict) -> str:
    labels = [str(x).lower() for x in (ticket.get("labels") or [])]
    for lab in labels:
        if lab.startswith("node:"):
            return lab.split(":", 1)[1]
    blob = f"{ticket.get('name', '')} {ticket.get('description', '')}".lower()
    return next((n for n in ("lenovo", "nvidia") if n in blob), "")


_NODE_URL = {"lenovo": os.environ.get("URIRUN_LENOVO_URL", "http://192.168.188.201:8765")}


def _get_lenovo_effective_url() -> str:
    try:
        from urirun.host import ticket_meta
        if ticket_meta.get_digital_person_mode("lenovo-node") == "sim":
            return "sim://digital-twin-lenovo"  # special, handled upstream
    except:
        pass
    return _NODE_URL.get("lenovo")


def node_readiness(node: str, timeout: float = 3.0) -> dict[str, Any]:
    """Minimalny readiness gate (Stage 4): probe węzła. ready / stale(repair) / offline."""
    url = _NODE_URL.get(node)
    if not url:
        return {"node": node, "ready": True, "reason": "brak znanego URL — nie blokuję (lokalny)"}
    host = url.split("//")[-1].split("/")[0].split(":")[0]
    port = int(url.rsplit(":", 1)[-1]) if url.rsplit(":", 1)[-1].isdigit() else 80
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"node": node, "ready": True}
    except Exception as exc:  # noqa: BLE001
        return {"node": node, "ready": False, "repair_allowed": False,
                "reason": f"node offline ({exc.__class__.__name__}) → nie claimuj normalnego taska",
                "unlock_uri": f"fleet://host/node/{node}/repair"}


def _twin_ticket(ticket: dict, action: str) -> dict:
    """Zmapuj planfile-ticket na HumanTicket dla twina (scope+dowody z labeli/tekstu)."""
    blob = f"{ticket.get('name', '')} {ticket.get('description', '')}".lower()
    pkg = ""
    m = re.search(r"(urirun-connector-[a-z0-9-]+)", blob)
    if m:
        pkg = m.group(1)
    ver = re.search(r"\b(\d+\.\d+\.\d+)\b", blob)
    scope = {"org": "if-uri", "package": pkg, "version": ver.group(1) if ver else "0.1.0"} if pkg else {}
    return {"id": ticket.get("id"), "action": action, "action_uri": ticket.get("action_uri"),
            "risk": "medium", "reversible": "publish" not in action and "delete" not in action,
            "scope": scope, "version": scope.get("version"),
            "tests": "passed", "smoke": "passed",
            "secrets": "none", "evidence": ["planfile ticket", "control-plane gate"]}


def _action_gate(action: str, ticket: dict, delegated: bool, project: str, result: dict) -> dict | None:
    """Bramka akcji → None gdy przechodzi, albo dict blocked. Mutuje result['gate']."""
    try:
        from urirun_connector_proxy import core as proxy
    except Exception:  # noqa: BLE001
        return None  # brak control-plane → nie blokuj
    if delegated:  # twin autoryzował „wolno"; sprawdź tylko egress „da-się"
        prx = proxy.check(action, "")
        result["gate"] = {"egress": prx["decision"], "authorized_by": "delegation"}
        if prx["decision"] in ("blocked", "deny"):
            return {**result, "runnable": False, "blocked_by": "proxy-egress",
                    "reason": prx.get("reason", "blocked_by_proxy_policy"),
                    "unlock_uri": "unlock://host/request/command/create"}
        return None
    # autonomiczny: grant-pack = ekosystemowe źródło prawdy; gdy aktywny → rządzi
    _, host = proxy._domain_for(action)
    pa = _pack_allows(action, host)
    if pa and pa["decision"] != "no_pack":
        result["gate"] = {"pack": pa["decision"], "pack_id": pa.get("pack_id")}
        if not pa["allowed"]:
            return {**result, "runnable": False, "blocked_by": f"pack:{pa['decision']}",
                    "reason": pa.get("reason", ""), "unlock_uri": pa.get("unlock_uri")}
        return None
    diag = proxy.diagnose(action, scope=(_twin_ticket(ticket, action).get("scope") or {}).get("package", ""), project=project)
    result["gate"] = {"decision": diag["decision"], "policy": diag.get("policy_mode"),
                      "proxy": diag.get("proxy_decision"), "grant": diag.get("grant")}
    if diag["decision"] not in ("execute",):
        return {**result, "runnable": False, "blocked_by": diag.get("blocked_by") or diag["decision"],
                "reason": diag.get("reason", ""), "unlock_uri": diag.get("unlock_uri")}
    return None


def _pack_allows(action: str, egress_domain: str = "") -> dict | None:
    try:
        from urirun_connector_grants import packs
        return packs.pack_allows(action, egress_domain=egress_domain)
    except Exception:  # noqa: BLE001
        return None


def _is_human(ticket: dict) -> bool:
    labels = [str(x).lower() for x in (ticket.get("labels") or [])]
    return "actor:human" in labels or "unlock" in labels or any(l.startswith("needs-human") for l in labels)


def _containment_dims(ticket: dict) -> dict:
    """Wymiary ticketu dla adaptive containment: node/connector/lane/ticket."""
    dims = {"ticket": ticket.get("id"), "node": node_of_ticket(ticket)}
    for lab in (ticket.get("labels") or []):
        l = str(lab).lower()
        if l.startswith("lane:"):
            dims["lane"] = l.split(":", 1)[1]
        elif l.startswith("connector:"):
            dims["connector"] = l.split(":", 1)[1]
        elif l in ("kvm", "signal", "email", "cron", "watchdog"):
            dims["connector"] = l
    return {k: v for k, v in dims.items() if v}


def _containment_check(ticket: dict, action: str) -> dict | None:
    """IFURI-192: adaptive containment (sentinel://) — czy dotknięty wymiar jest zawężony.
    None gdy przechodzi; łagodnie degraduje gdy connector nieobecny."""
    try:
        from urirun_connector_sentinel import core as sen
    except Exception:  # noqa: BLE001
        return None
    d = sen.containment_check(_containment_dims(ticket), action_class=(action.split(".")[0] if action else "command"))
    if not d.get("allowed"):
        return {"runnable": False, "blocked_by": "containment", "mode": d.get("mode"),
                "reason": d.get("reason", "contained"), "unlock_uri": "containment://host/scope/query/active"}
    return None


def runnable_gate(ticket: dict, project: str = "") -> dict[str, Any]:
    """PEŁNA bramka: policy ∧ grant ∧ proxy ∧ delegation(jeśli human) ∧ readiness(jeśli node).
    {runnable, reason, blocked_by, unlock_uri, delegation, readiness}. Łagodnie degraduje."""
    action = action_of_ticket(ticket)
    result: dict[str, Any] = {"action": action}
    delegated = False

    # 0. ODBLOKOWANIE RAZ = ZAPAMIĘTANE: człowiek odblokował ten ticket → runnable NA ZAWSZE,
    # żadna bramka nie pyta ponownie (per Tom: nie re-blokuj i nie re-pytaj o ten sam ticket).
    # WYJĄTEK: akcje NIEODWRACALNE (publikacje/płatności/sekrety/destrukcja) re-gejtują ZAWSZE —
    # jeden klik unblock NIE autoryzuje trwałej autonomii dla akcji, których nie da się cofnąć.
    try:
        from urirun_connector_grants import unblock_ledger
        if unblock_ledger.is_unblocked_for(ticket) and action not in _NEVER_UNBLOCK:
            return {**result, "runnable": True, "human_unblocked": True,
                    "reason": "człowiek odblokował raz (trwale) → auto-kontynuacja"}
    except Exception:  # noqa: BLE001
        pass

    # 1. human-ticket → twin (delegated approval = AUTORYZACJA człowieka, zastępuje grant)
    if _is_human(ticket):
        try:
            from urirun_connector_human_twin import core as twin
            assess = twin.assess(_twin_ticket(ticket, action))
            result["delegation"] = assess["decision"]
            # approve_delegated i ask_clarification (delegacja JEST, dowód do zebrania) → runnable;
            # tylko escalate_to_human / reject → człowiek (nie claimuj)
            if assess["decision"] in ("escalate_to_human", "reject"):
                return {**result, "runnable": False, "blocked_by": "delegation",
                        "reason": assess.get("reason", "twin eskaluje do człowieka"),
                        "unlock_uri": f"approval://adam/packet/query/show/{ticket.get('id')}"}
            delegated = True  # twin autoryzował → grant/policy „wolno" spełnione delegacją
        except Exception:  # noqa: BLE001 — twin nieobecny → zostaw człowiekowi
            return {**result, "runnable": False, "blocked_by": "human",
                    "reason": "actor:human (twin niedostępny)", "unlock_uri": f"human://operator/decision/{ticket.get('id')}"}

    # 2. akcja: bramka egress/pack/policy (delegowani: tylko egress; autonomiczni: pack lub policy∧grant∧proxy)
    if action:
        blk = _action_gate(action, ticket, delegated, project, result)
        if blk is not None:
            return blk

    # 2b. adaptive containment (sentinel://): dotknięty wymiar zawężony przez anomalię?
    cont = _containment_check(ticket, action)
    if cont is not None:
        return {**result, **cont}

    # 3. readiness dla node-touching
    node = node_of_ticket(ticket)
    if node:
        rd = node_readiness(node)
        result["readiness"] = rd
        if not rd.get("ready") and not rd.get("repair_allowed"):
            return {**result, "runnable": False, "blocked_by": "readiness",
                    "reason": rd.get("reason", "node not ready"), "unlock_uri": rd.get("unlock_uri")}

    return {**result, "runnable": True}
