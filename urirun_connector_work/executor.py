# Author: Tom Sapletta · Part of the ifURI solution.
"""IFURI-200 — łańcuch wykonawców: native koru → agent:// → twin-human (kvm) → human (retry=false).

`claim-next` mówi CO wolno wziąć; TO mówi CZYM wykonać, gdy natywny drive koru pada (no_executor).
agent:// jest WYKONAWCĄ, nie drugim plannerem — wykonuje TYLKO ticket, który już przeszedł bramki.

Dla ticketów z labelami kvm/lenovo/signal-gui lub "na lenovo" / deliver_signal:
  → delegacja do urirun-twin-human (act_as_human + kvm click/type + unblock).
  Dzięki temu koru autonomicznie obsługuje akcje desktopowe (Signal na lenovo) zamiast padać na chat-drive.

Klasyfikacja krytyczna: tylko błędy wykonawcy (no_executor/client-command-failed/drive_failed) idą do
agent:// / twin. Frontier/... to NIE problem wykonawcy.

Tryby (KORU_EXECUTOR_FALLBACK): off · shadow · soft · hard.
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable

from . import core as _work

_MODES = ("off", "shadow", "soft", "hard")
_NO_EXECUTOR = ("no_executor", "client command failed", "drive_failed",
                "no viable control route", "executor unavailable", "native ide unavailable")
_DEFAULT_AGENTS = ("claude", "codex", "aider", "gemini")

from .signal_kvm import KVM_DELEGATION_LABELS, KVM_HUMAN_KEYWORDS


def is_no_executor(result: dict | None) -> bool:
    """Czy porażka to problem WYKONAWCY (→ agent://), a nie frontier/secret/proxy (→ upstream)."""
    r = result or {}
    if str(r.get("category") or "").lower() == "no_executor":
        return True
    msg = str(r.get("error") or r.get("message") or r.get("reason") or "").lower()
    return any(x in msg for x in _NO_EXECUTOR)


def _is_kvm_or_human_desktop_ticket(ticket: dict) -> bool:
    """Tickets that should be executed by urirun-twin-human (KVM on lenovo etc) instead of chat/agent."""
    labels = [str(x).lower() for x in (ticket.get("labels") or [])]
    name = (ticket.get("name") or "").lower()
    desc = (ticket.get("description") or "").lower()
    text = f"{name} {desc}"
    executor = ticket.get("executor") or {}
    if isinstance(executor, dict) and str(executor.get("kind", "")).lower() == "human":
        return True
    if any(k in labels for k in KVM_DELEGATION_LABELS):
        return True
    if any(kw in text for kw in KVM_HUMAN_KEYWORDS):
        return True
    handler = str(executor.get("handler", "")).lower() if isinstance(executor, dict) else ""
    if "deliver_signal" in handler or "kvm" in handler:
        return True
    return False


def agents_available() -> list[str]:
    try:
        from urirun_connector_agents import core as ag
        return [k for k, v in ag._available().items() if not (ag._ADAPTERS.get(k) or {}).get("gui")]
    except Exception:  # noqa: BLE001
        return []


def _lease_active(lease: dict | None) -> bool:
    lid = (lease or {}).get("id")
    if not lid:
        return False
    worker = (lease or {}).get("worker")
    return any(l.get("id") == lid and (not worker or l.get("worker") == worker)
               for l in _work._load_active(time.time()))


def _prompt(ticket: dict, constraints: dict | None) -> str:
    c = constraints or {}
    guard = []
    if c.get("no_publish"):
        guard.append("NIE publikuj")
    if c.get("no_payment"):
        guard.append("NIE płać")
    if c.get("no_secret_export"):
        guard.append("NIE eksportuj sekretów")
    tail = "; ".join(guard)
    return (f"Execute ticket {ticket.get('id')}: {ticket.get('name') or ticket.get('title', '')}. "
            f"Minimal, tested change. {'Ograniczenia: ' + tail + '. ' if tail else ''}"
            "When done, summarise in 3 lines.")


def run_agent_executor(ticket: dict, lease: dict | None, *, project: str = "",
                       constraints: dict | None = None, allowed_agents: list | None = None) -> dict:
    """Wykonaj ticket przez agent:// — TYLKO z aktywnym lease. {ok, executor, ...}."""
    if not _lease_active(lease):
        return {"ok": False, "denied": True, "category": "no_lease",
                "reason": "agent:// odmówiony — brak aktywnego lease (wykonawca nie omija bramek)"}
    avail = agents_available()
    if not avail:
        return {"ok": False, "category": "agent_missing", "reason": "brak zainstalowanego agenta headless"}
    allowed = [a for a in (allowed_agents or _DEFAULT_AGENTS) if a in avail]
    agent = allowed[0] if allowed else "auto"
    model = ""
    try:
        from urirun.host.env_loader import agent_model, load_project_env
        load_project_env(project)
        model = agent_model()
    except Exception:  # noqa: BLE001
        for key in ("URIRUN_AGENT_MODEL", "LLM_MODEL_DEVELOPER", "LLM_MODEL"):
            model = (os.environ.get(key) or "").strip()
            if model:
                break
    try:
        from urirun_connector_agents import core as ag
        r = ag.task_run(prompt=_prompt(ticket, constraints), agent=agent, model=model,
                        cwd=project or _work._project() if hasattr(_work, "_project") else project)
        return {"ok": bool(r.get("ok", True)), "executor": f"agent://{agent}", "result": r}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "category": "agent_unavailable", "reason": f"{type(exc).__name__}: {exc}"}


def run_twin_human_executor(ticket: dict, lease: dict | None = None, *, project: str = "", **_kw) -> dict:
    """Execute desktop/human KVM actions via urirun-twin-human (for kvm/lenovo/signal tickets)."""
    tid = ticket.get("id")
    try:
        # Use the twin for human-like execution (kvm clicks, unblock, act)
        # Try connector first for consistency with gates
        try:
            from urirun_connector_human_twin import core as htwin
            # simple scope for assess (reuse pattern from gates)
            scope = {"ticket_id": tid, "labels": ticket.get("labels"), "action": "kvm.execute" if _is_kvm_or_human_desktop_ticket(ticket) else "human.execute"}
            assess = htwin.assess({"id": tid, "action": "execute", "scope": scope, "risk": "medium"})
            if assess.get("decision") not in ("approve_delegated", "ask_clarification"):
                return {"ok": False, "category": "twin_delegation_denied", "reason": assess.get("reason")}
        except Exception:
            pass  # no assess connector, fall to exec twin

        # robust import (monorepo / different venvs)
        try:
            from urirun_twin_human.core import act_as_human, click_as
        except Exception:
            import sys
            from pathlib import Path
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "urirun-twin-human"))
            from urirun_twin_human.core import act_as_human, click_as
        goal = ticket.get("name") or f"execute {tid}"
        ctx = {"ticket": ticket, "project": project, "lease": lease}

        # If it's a deliver_signal / signal on lenovo, prefer the work goal (which does kvm)
        handler = str((ticket.get("executor") or {}).get("handler", "")).lower()
        text = (goal + " " + str(ticket.get("description", ""))).lower()
        if "deliver_signal" in handler or ("signal" in text and "lenovo" in text):
            from . import goal as wg
            res = wg.deliver_signal(approved=True)  # twin acts as the approver + executor
            # Respektuj walidację: decision musi być "done" I verified musi być true
            inner_v = bool((res.get("result") or res).get("verified"))
            ok = (res.get("decision") == "done") and (res.get("ok", False) or inner_v)
            return {"ok": ok, "executor": "twin-human", "result": res, "ticket": tid, "via": "deliver_signal", "verified": inner_v}

        # General human/kvm action
        res = act_as_human("tom", goal=goal, context=ctx)
        # additionally attempt direct kvm if labels suggest (the twin will have done reasoning)
        if _is_kvm_or_human_desktop_ticket(ticket):
            # the act_as_human already can trigger click_as internally; here we just record
            pass
        return {"ok": bool(res.get("ok", True)), "executor": "twin-human", "result": res, "ticket": tid}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "category": "twin_human_failed", "reason": f"{type(exc).__name__}: {exc}", "ticket": tid}


def _escalate_human(ticket: dict, reason: str) -> dict:
    tid = ticket.get("id")
    return {"executor": None, "ok": False, "category": "no_executor", "retry": False, "actor": "human",
            "escalation": f"human://operator/decision/{tid}", "reason": reason}


def _trace_exec(ticket: dict, chain: dict, outcome: str) -> None:
    _work._trace("executor_chain", ticket=ticket.get("id"), outcome=outcome, chain=dict(chain))


def _verify(result: dict, ticket: dict) -> dict:
    """Kontrakt weryfikacji na wyniku wykonawcy (IFURI-197/198) — efekt/prowieniencja."""
    try:
        from . import postcondition
        return postcondition.classify(result, uri=f"ticket://{ticket.get('id')}")
    except Exception:  # noqa: BLE001
        return {}


def execute_ticket(ticket: dict, lease: dict | None = None, *, native_runner: Callable | None = None,
                   mode: str = "", project: str = "", constraints: dict | None = None,
                   allowed_agents: list | None = None) -> dict:
    """Łańcuch wykonawców z rolloutem. Zwraca {executor, ok, executor_chain, ...}."""
    mode = (mode or os.environ.get("KORU_EXECUTOR_FALLBACK", "shadow")).lower()
    if mode not in _MODES:
        mode = "shadow"
    chain: dict[str, Any] = {"native": None, "agent": None, "twin_human": None, "human": None}

    native = None
    if native_runner:
        try:
            native = native_runner(ticket)
        except Exception as exc:  # noqa: BLE001
            native = {"ok": False, "error": str(exc)}
    if native and native.get("ok"):
        chain["native"] = "success"
        _trace_exec(ticket, chain, "native")
        return {"executor": "native", "ok": True, "result": native, "executor_chain": chain,
                "_verification": _verify(native, ticket)}

    # native padł — czy to problem WYKONAWCY? jeśli nie (test-fail, frontier, itp.) NIE fallbackuj
    if native is not None and not is_no_executor(native):
        chain["native"] = "failed (nie-executorowy)"
        return {"executor": "native", "ok": False, "result": native, "executor_chain": chain,
                "note": "błąd nie-executorowy → agent:// nie jest właściwy (upstream/frontier)"}
    chain["native"] = "failed no_executor"

    if mode == "off":
        chain["human"] = "escalated retry=false"
        _trace_exec(ticket, chain, "human")
        return {**_escalate_human(ticket, "no_executor + fallback=off"), "executor_chain": chain}

    if mode == "shadow":
        avail = agents_available()
        chain["agent"] = f"would_try ({'available' if avail else 'unavailable'})"
        _trace_exec(ticket, chain, "shadow")
        return {"executor": "native", "ok": False, "shadow": True, "agent_available": bool(avail),
                "executor_chain": chain, "note": "shadow: native no_executor; agent:// tylko zmierzony"}

    # soft / hard: realnie próbuj agent://
    ag_res = run_agent_executor(ticket, lease, project=project, constraints=constraints, allowed_agents=allowed_agents)
    if ag_res.get("ok"):
        chain["agent"] = "success"
        _trace_exec(ticket, chain, "agent")
        return {"executor": ag_res.get("executor"), "ok": True, "result": ag_res, "executor_chain": chain,
                "_verification": _verify(ag_res.get("result") or ag_res, ticket)}
    chain["agent"] = ag_res.get("category") or "failed"

    # NEW: for kvm / lenovo / human-desktop tickets, delegate execution to urirun-twin-human
    # (this makes koru/work autonomously drive Signal/KVM etc. instead of escalating to real human)
    if _is_kvm_or_human_desktop_ticket(ticket):
        th_res = run_twin_human_executor(ticket, lease, project=project, constraints=constraints)
        if th_res.get("ok"):
            chain["twin_human"] = "success"
            _trace_exec(ticket, chain, "twin_human")
            return {"executor": "twin-human", "ok": True, "result": th_res, "executor_chain": chain,
                    "_verification": _verify(th_res.get("result") or th_res, ticket)}
        chain["twin_human"] = th_res.get("category") or "failed"
        # fall through to human escalation only if twin also failed

    chain["human"] = "escalated retry=false"
    _trace_exec(ticket, chain, "human")
    return {**_escalate_human(ticket, f"agent executor {chain['agent']}"), "executor_chain": chain}
