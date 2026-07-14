# Author: Tom Sapletta · Part of the ifURI solution.
"""IFURI-199 — adopcja `claim-next` jako JEDYNEJ bramy pracy koru, bezpiecznym rolloutem.

Największy niedomknięty szew autonomii: koru bierze `planfile next`, a nie `work://claim-next`,
więc może wziąć ticket bez grantu/egress/readiness i potem generować diagnostykę. Ta brama
wymusza przejście przez control-plane, ale STOPNIOWO (nie twardo od razu):

  KORU_WORK_GATE=shadow  — koru wykonuje LEGACY ticket, claim-next liczony RÓWNOLEGLE (diff w trace).
  KORU_WORK_GATE=soft    — preferuj claim-next; gdy nic nie zwróci → fallback legacy (bez lease).
  KORU_WORK_GATE=hard    — bez lease NIE wolno zacząć pracy (control-plane = jedyna brama). Domyślny docelowy tryb dla autonomii.

Shadow niczego nie leasuje (dry_run) — czysty pomiar. Każda decyzja zostawia ślad (require_trace).
"""
from __future__ import annotations

import os
from typing import Any

from . import core as _work

_MODES = ("shadow", "soft", "hard")


def _legacy_next(project: str) -> dict | None:
    """Co koru wziąłby PO STAREMU: pierwszy open/ready wg priorytetu (bez bramki control-plane)."""
    tix = _work._open_tickets(("open", "ready"), True, project)
    tix = [t for t in tix if not _work._is_frontier(t)]  # frontier i tak nigdy nie jest pracą
    return {"id": tix[0]["id"], "title": tix[0].get("name")} if tix else None


def next_work(worker: str, *, mode: str = "", project: str = "", lane: str = "") -> dict[str, Any]:
    """Brama pracy koru wg trybu rolloutu. Zwraca {mode, source, ticket, lease, ...}."""
    # Long-term autonomy: default to hard (claim-next mandatory gate).
    mode = (mode or os.environ.get("KORU_WORK_GATE", "hard")).lower()
    if mode not in _MODES:
        mode = "hard"

    if mode == "hard":
        claim = _work.claim_next(worker, lane=lane, project=project)
        chosen = (claim.get("ticket") or {}).get("id")
        _work._trace("work_gate", mode="hard", chosen=chosen, lease=bool(claim.get("lease")))
        return {"mode": "hard", "source": "claim-next", "ticket": claim.get("ticket"),
                "lease": claim.get("lease"), "blocked": claim.get("blocked"),
                "note": "hard: bez lease nie wolno zacząć — control-plane to jedyna brama"}

    if mode == "soft":
        claim = _work.claim_next(worker, lane=lane, project=project)
        if claim.get("ticket"):
            return {"mode": "soft", "source": "claim-next", "ticket": claim.get("ticket"),
                    "lease": claim.get("lease"), "blocked": claim.get("blocked")}
        legacy = _legacy_next(project)
        _work._trace("work_gate", mode="soft", fallback_legacy=(legacy or {}).get("id"))
        return {"mode": "soft", "source": "legacy-fallback", "ticket": legacy, "lease": None,
                "note": "claim-next nic nie zwrócił → fallback legacy (bez lease); zawęź gdy stabilne"}

    # shadow (domyślnie): legacy autorytatywne, claim-next tylko porównywany (dry_run — nic nie leasuje)
    legacy = _legacy_next(project)
    peek = _work.claim_next(worker, lane=lane, project=project, dry_run=True)
    would = (peek.get("ticket") or {}).get("id")
    leg = (legacy or {}).get("id")
    diff = would != leg
    _work._trace("work_gate_shadow", legacy=leg, would_claim=would, diff=diff,
                 blocked=len(peek.get("blocked") or []))
    return {"mode": "shadow", "source": "legacy", "ticket": legacy, "lease": None,
            "shadow_would_claim": would, "diff": diff, "blocked": peek.get("blocked"),
            "note": "shadow: legacy wykonywany, claim-next tylko mierzy różnicę (diff w trace)"}
