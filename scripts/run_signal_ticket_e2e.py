#!/usr/bin/env python3
"""E2E ticket harness — IFURI Signal tickets via LLM loop + schema gates."""
from __future__ import annotations

import json
import os
import sys
import time

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "urirun", "adapters", "python"))
_WORK = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
for _p in (_ROOT, _WORK):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("URIRUN_LLM_RUNTIME_ROOT", "/home/tom/github/if-uri/urirun-llm-runtime")
os.environ.setdefault("URIRUN_LENOVO_URL", "http://192.168.188.201:8765")

# --- gates -----------------------------------------------------------------

def gate_offline_schemas() -> dict:
    from urirun_runtime.ticket_llm_context import _load_offline_route_schemas
    routes = _load_offline_route_schemas("lenovo")
    uris = {r.get("uri") for r in routes}
    ok = "kvm://host/ui/command/type-verified" in uris
    return {"name": "offline_schemas", "ok": ok and len(routes) >= 10, "routes": len(routes)}


def gate_live_routes() -> dict:
    from urirun_runtime.ticket_llm_context import fetch_routes_from_node
    routes = fetch_routes_from_node("lenovo")
    uris = {r.get("uri") for r in routes}
    tv = next((r for r in routes if r.get("uri") == "kvm://host/ui/command/type-verified"), None)
    props = ((tv or {}).get("inputSchema") or {}).get("properties") or {}
    ok = bool(tv) and "x" in props and "text" in props
    return {"name": "live_routes", "ok": ok, "routes": len(routes), "type_verified": bool(tv)}


def gate_prompt_has_schemas() -> dict:
    from urirun_runtime.ticket_llm_context import build_first_system_prompt
    p = build_first_system_prompt(ticket={"id": "GATE", "name": "signal"}, node="lenovo")
    ok = "URI-PROCESY WĘZŁA" in p and "type-verified" in p and '"x"' in p
    return {"name": "prompt_schemas", "ok": ok, "chars": len(p)}


def gate_type_verified_exec() -> dict:
    """IFURI-059: type-verified with locate-derived x,y (no send)."""
    from urirun_connector_work.goal import (
        _node_run, _locate_match, KVM_URI_HOST, _ensure_gui_ready_for_signal,
    )

    node = "lenovo"
    host = KVM_URI_HOST
    ready = _ensure_gui_ready_for_signal(node, ticket="IFURI-059-gate")
    if not ready.get("ok"):
        return {"name": "type_verified_exec", "ok": False, "error": "signal not visible", "skipped": True}
    probe = "GATE"
    for label in ("Message", "Wiadomość", "Send a message"):
        match = _locate_match(node, label, min_conf=30)
        if match and match.get("center"):
            break
    else:
        match = None
    if not match or not match.get("center"):
        return {"name": "type_verified_exec", "ok": False, "error": "no composer locate"}
    cx, cy = match["center"]
    r = _node_run(
        node,
        f"kvm://{host}/ui/command/type-verified",
        {
            "text": probe,
            "x": int(cx),
            "y": int(cy),
            "submit": False,
            "draft_expect": probe,
        },
        timeout=45,
    )
    ok = bool(r.get("ok")) and bool(r.get("verified") or (r.get("draft") or {}).get("verified"))
    return {"name": "type_verified_exec", "ok": ok, "result": {k: r.get(k) for k in ("ok", "verified", "error", "submitted")}}


# --- ticket runs -----------------------------------------------------------

SIGNAL_TICKETS = [
    {"id": "IFURI-235", "name": "[SIGNAL-TEST-1]"},
    {"id": "IFURI-236", "name": "[SIGNAL-TEST-2]"},
    {"id": "IFURI-237", "name": "[SIGNAL-TEST-3]"},
]


def run_signal_ticket(ticket_id: str) -> dict:
    from urirun_connector_work import goal
    from urirun_connector_work.goal import _present_on_screen_tesseract
    from urirun_connector_work.signal_kvm import payload_from_ticket, DEFAULT_RECIPIENT

    t = {"id": ticket_id, "name": f"Signal test {ticket_id}", "labels": ["signal", "kvm", "lenovo"]}
    rec, _ = payload_from_ticket(t)
    rec = rec or DEFAULT_RECIPIENT
    msg = f"{ticket_id}-{int(time.time()) % 100000}"
    print(f"\n--- {ticket_id} → {rec!r}: {msg!r} ---", flush=True)
    r = goal.send_via_kvm(rec, msg, ticket=ticket_id, node="lenovo")
    tess = _present_on_screen_tesseract("lenovo", msg)
    ok = bool(r.get("ok")) and bool(r.get("verified"))
    return {
        "ticket": ticket_id,
        "ok": ok,
        "verified": r.get("verified"),
        "tesseract": tess,
        "status": r.get("status"),
        "plan_used": r.get("plan_used"),
        "message": msg,
    }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Signal ticket E2E harness")
    parser.add_argument("--ticket", help="Run only this ticket id (e.g. IFURI-237)")
    parser.add_argument("--skip-gates", action="store_true", help="Skip schema gates")
    args = parser.parse_args()

    os.environ.setdefault("URIRUN_LLM_RUNTIME_CONTROL", "1")
    os.environ.setdefault("URIRUN_LLM_SKIP_HUMAN_APPROVAL", "1")

    results: list[dict] = []
    if not args.skip_gates:
        print("=== GATES ===", flush=True)
        for fn in (gate_offline_schemas, gate_live_routes, gate_prompt_has_schemas, gate_type_verified_exec):
            g = fn()
            results.append(g)
            mark = "PASS" if g.get("ok") else "FAIL"
            print(f"  [{mark}] {g['name']}: {json.dumps({k: v for k, v in g.items() if k != 'name'}, default=str)[:200]}")

        required = [g for g in results if g["name"] != "type_verified_exec"]
        gates_ok = all(g.get("ok") for g in required)
        tv = next((g for g in results if g["name"] == "type_verified_exec"), {})
        if not tv.get("ok"):
            print(f"  [WARN] type_verified_exec: {tv.get('error', tv)}", flush=True)
        if not gates_ok:
            print("\nGates failed — fixing infra before tickets", flush=True)
    else:
        gates_ok = True

    tickets = [t for t in SIGNAL_TICKETS if not args.ticket or t["id"] == args.ticket]
    if args.ticket and not tickets:
        print(f"Unknown ticket: {args.ticket}", flush=True)
        return 2

    print("\n=== TICKETS (LLM loop) ===", flush=True)
    ticket_results = []
    for t in tickets:
        try:
            tr = run_signal_ticket(t["id"])
            ticket_results.append(tr)
            mark = "PASS" if tr.get("ok") else "FAIL"
            print(f"  [{mark}] {tr['ticket']}: verified={tr.get('verified')} tesseract={tr.get('tesseract')}")
        except Exception as exc:  # noqa: BLE001
            ticket_results.append({"ticket": t["id"], "ok": False, "error": str(exc)[:200]})
            print(f"  [FAIL] {t['id']}: {exc}")

    all_ok = gates_ok and all(tr.get("ok") for tr in ticket_results)
    summary = {"gates": results, "tickets": ticket_results, "all_ok": all_ok}
    print("\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2, default=str))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
