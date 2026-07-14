# Author: Tom Sapletta · Part of the ifURI solution.
"""HARD goal-delivery freeze + honest-outcome classification (SIGNAL-E2E-001 phase).

Once a delivery goal is active, the system must stop escaping into code. This gate makes the
freeze HARD and gives ``waiting_human`` first-class status as a *valid* outcome (not a failure,
not churn). Two enforcement points koru's generators wire in:

  * ``allows_new_code_ticket(ticket)`` — the watchdog / self-evolution / diagnosis paths call
    this BEFORE creating a ticket. While a goal is frozen, only tickets that directly block the
    goal (``goal.goal_relevant``) pass; refactor/evolution/diagnosis/nora are DENIED. This is the
    guard that keeps the refactor loop from re-opening.
  * ``classify_outcome(result)`` — a clean human-wait (one named gap + resume URI + no code
    churn) classifies as ``waiting_human_clean``: a successful autonomous outcome, so the
    scheduler/watchdog treat it as *done-waiting*, never as blocked/looping.

Only signal-related work + an explicit URI whitelist are permitted while the signal goal runs.
"""
from __future__ import annotations

from typing import Any

# URIs a goal is allowed to touch while frozen — everything else (new connector, refactor) denied.
_ALLOW_ONLY: dict[str, tuple[str, ...]] = {
    "signal.message.send": (
        "signal://host/account/query/status", "signal://host/message/command/draft",
        "signal://host/message/command/send", "signal://host/message/query/verify",
        "approval://", "delegation://", "policy://host/action/query/check"),
}
# Ticket kinds that mean "more code" — frozen while a delivery goal is active.
_CODE_KINDS = ("refactor", "evolution", "ewolucja", "diagnosis", "diagnoza", "nora",
               "connector-gen", "self-evolution", "unlock")


def _active_goal() -> dict | None:
    from . import goal
    return goal.current_goal()


def hard_freeze() -> bool:
    g = _active_goal()
    return bool(g and g.get("freeze_self_evolution"))


def allow_only(goal_name: str = "") -> list[str]:
    """The URI whitelist for the active (or named) goal while frozen."""
    g = goal_name or (_active_goal() or {}).get("goal", "")
    return list(_ALLOW_ONLY.get(g, ()))


def uri_allowed(uri: str) -> bool:
    """Is a URI action permitted under the active goal freeze? (no freeze → everything allowed)."""
    if not hard_freeze():
        return True
    return any(uri.startswith(p) for p in allow_only())


def allows_new_code_ticket(ticket: dict) -> dict[str, Any]:
    """HARD FREEZE: may a NEW ticket be created right now? While a goal is frozen, a code/refactor/
    self-evolution/diagnosis ticket is DENIED unless it directly blocks the goal. ``{allowed, reason}``.
    Generators MUST honour ``allowed==False`` — that is what keeps the refactor loop shut."""
    if not hard_freeze():
        return {"allowed": True, "reason": "no active goal freeze"}
    from . import goal
    if goal.goal_relevant(ticket):
        return {"allowed": True, "reason": "direct goal blocker"}
    blob = f"{ticket.get('name', '')} {' '.join(str(l).lower() for l in (ticket.get('labels') or []))}".lower()
    active = (_active_goal() or {}).get("goal")
    if any(k in blob for k in _CODE_KINDS):
        return {"allowed": False, "reason": "goal-freeze: no new code tickets while goal active",
                "goal": active, "hint": "wznów przez next_uri_after_human, nie generuj kodu"}
    return {"allowed": False, "reason": "goal-freeze: ticket unrelated to active goal", "goal": active}


def classify_outcome(result: dict) -> str:
    """SYSTEMIC RULE: waiting_human is NOT failure. A clean human-wait (one named gap + resume URI +
    no code churn) is a VALID autonomous outcome → ``waiting_human_clean`` (not blocked/churn)."""
    d = (result or {}).get("decision")
    if d == "done" and result.get("message_sent"):
        return "done"
    if d == "waiting_human":
        clean = (result.get("missing_only") and result.get("next_uri_after_human")
                 and result.get("no_new_code_tickets_created"))
        return "waiting_human_clean" if clean else "waiting_human"
    return d or "unknown"


def is_valid_outcome(result: dict) -> bool:
    """Both a delivered action AND a clean human-wait count as success (not a failure to escalate)."""
    return classify_outcome(result) in ("done", "waiting_human_clean")
