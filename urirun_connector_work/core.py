# Author: Tom Sapletta · Part of the ifURI solution.
"""urirun-connector-work — `work://` control plane for LOCK-AWARE PARALLEL execution.

Several workers (koru fleet + a main agent) clear the backlog in parallel WITHOUT editing the
same repo/path at once. A worker no longer takes "the next ticket" — it asks
``work://…/claim-next`` and only gets a ticket together with a **lease** over the resource
**locks** that ticket needs, and only if nothing else holds a conflicting lock or a missing
grant. Leases carry a TTL and are refreshed by ``renew`` (heartbeat); a dead worker's lease
expires so the ticket returns to the pool. Concurrency stops being a convention and becomes a
runtime guarantee.

Locks are hierarchical resource keys (`repo:…`, `path:…/file.py`, `node:lenovo`,
`ticket:ID`); two conflict when one is an ancestor of the other, so disjoint files run in
parallel but a repo lock shadows everything inside it.

Routes:
  * ``work://host/lease/command/claim-next`` — lock+grant-aware scheduler: lease the next free ticket
  * ``work://host/lease/command/acquire``    — acquire a lease for an explicit ticket
  * ``work://host/lease/command/renew``      — heartbeat: extend a lease TTL + report status
  * ``work://host/lease/command/release``    — release a lease (ownership-guarded)
  * ``work://host/locks/query/list``         — active leases + held locks
  * ``work://host/workers/query/status``     — what each worker holds
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import urirun
from urirun_work.locks import locks_conflict

CONNECTOR_ID = "work"
conn = urirun.connector(CONNECTOR_ID, scheme="work")

_STORE = Path(os.environ.get("URIRUN_WORK_LEASES")
              or "~/.urirun/host-dashboard/work-leases.json").expanduser()
_DEFAULT_REPO = os.environ.get("URIRUN_WORK_REPO") or "if-uri/urirun"

# Conservative repo inference for older tickets that declare no files (better over-lock).
_HEURISTIC_REPO = {
    "fleet": "if-uri/urirun-fleet", "mind": "if-uri/urirun-mind",
    "kvm": "if-uri/urirun-connector-kvm", "watchdog": "if-uri/urirun-connector-watchdog",
    "signal": "if-uri/urirun-connector-signal", "cron": "if-uri/urirun-connector-cron",
}
# Signals that a ticket is NOT lock-runnable: needs a human or a grant, not just a free file.
_HUMAN_KW = ("signal-cli", "zalinkuj", "skan qr", "qr telefon", "pypi", "app-password",
             "zainstaluj", "na lenovo", "keyring set", "secret://")


def _ok(**kw: Any) -> dict[str, Any]:
    return urirun.ok(connector=CONNECTOR_ID, **kw)


def _trace(event: str, **fields: Any) -> None:
    """Audyt (require_trace): każdy claim zostawia ślad z pack_id. Best-effort."""
    try:
        from urirun_connector_grants import events
        events.append(event, **fields)
    except Exception:  # noqa: BLE001
        pass


def _fail(msg: str, action: str, **extra: Any) -> dict[str, Any]:
    return urirun.fail(msg, connector=CONNECTOR_ID, action=action, **extra)


# ── lock model ────────────────────────────────────────────────────────────────
# Conflict semantics come from urirun-work. This connector owns ticket-to-lock
# inference and durable leases, but deliberately has no second lock comparator.


def _heuristic_locks(ticket: dict, repo: str) -> list[str]:
    """Conservative locks inferred from labels/text (node, sibling-repo, hot paths)."""
    labels = [str(x).lower() for x in (ticket.get("labels") or [])]
    text = f"{ticket.get('name', '')} {ticket.get('description', '')}".lower()
    out: list[str] = []
    for lab in labels:
        if lab.startswith("node:"):
            out.append(f"node:{lab.split(':', 1)[1]}")
        if lab in _HEURISTIC_REPO:
            out.append(f"repo:{_HEURISTIC_REPO[lab]}")
        if lab.startswith("connector-gen"):
            out.append(f"repo:{repo}")  # generation touches many places → coarse
    for kw, lock in (("lenovo", "node:lenovo"), ("pyproject", f"path:{repo}/pyproject.toml"),
                     ("dashboard", f"path:{repo}/adapters/python/urirun/host/dashboard")):
        if kw in text:
            out.append(lock)
    return out


def locks_for_ticket(ticket: dict, repo: str = "") -> list[str]:
    """Resource locks a ticket needs: its id + per-FILE path locks (so two workers on
    different files in the same folder run in parallel), plus heuristic node/repo locks, with a
    coarse whole-repo fallback only when nothing more specific is known."""
    repo = repo or _DEFAULT_REPO
    locks = [f"ticket:{ticket.get('id')}"]
    files = ticket.get("files") or []
    if files:
        locks.extend(f"path:{repo}/{f.strip('/')}" for f in files)
    heur = _heuristic_locks(ticket, repo)
    locks.extend(heur)
    if not files and not any(l.startswith("repo:") for l in heur):
        locks.append(f"repo:{repo}")
    return list(dict.fromkeys(locks))


def blocked_reason(ticket: dict) -> dict | None:
    """Why a ticket is NOT lock-runnable even if its files are free: human/grant dependency.
    Returns ``{reason, unlock_uri}`` or None. Keeps workers from wasting a lease on something
    they may not execute anyway (the point where grants:// meets work://)."""
    labels = [str(x).lower() for x in (ticket.get("labels") or [])]
    tid = ticket.get("id")
    if "actor:human" in labels or any(l.startswith("needs-human:") for l in labels):
        return {"reason": "actor:human / needs-human", "unlock_uri": f"human://operator/decision/{tid}"}
    for lab in labels:
        if lab.startswith("needs-grant:"):
            return {"reason": f"missing grant: {lab.split(':', 1)[1]}",
                    "unlock_uri": "unlock://host/request/command/create"}
    text = f"{ticket.get('name', '')} {ticket.get('description', '')}".lower()
    if any(k in text for k in _HUMAN_KW):
        return {"reason": "external human dependency", "unlock_uri": f"human://operator/decision/{tid}"}
    return None


# ── lease store ───────────────────────────────────────────────────────────────

def _load_active(now: float) -> list[dict]:
    """Active (non-expired) leases; expired ones are dropped so a dead worker never blocks."""
    try:
        data = json.loads(_STORE.read_text()) if _STORE.is_file() else []
    except Exception:  # noqa: BLE001
        return []
    return [l for l in data if isinstance(l, dict) and l.get("expires", 0) > now]


def _save(leases: list[dict]) -> None:
    try:
        _STORE.parent.mkdir(parents=True, exist_ok=True)
        _STORE.write_text(json.dumps(leases, indent=1, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _conflict(locks: list[str], leases: list[dict], worker: str, ticket: str) -> dict | None:
    for lease in leases:
        if lease.get("worker") == worker and lease.get("ticket") == ticket:
            continue  # a worker may re-acquire/extend its own lease
        for want in locks:
            for held in lease.get("locks", []):
                if locks_conflict(want, held):
                    return {"lock": want, "held_by": lease.get("worker"),
                            "held_lock": held, "ticket": lease.get("ticket")}
    return None


def acquire(ticket: str, worker: str, locks: list[str], ttl_minutes: int = 30) -> dict:
    """Grant a lease iff no other worker holds a conflicting lock. Idempotent per
    (worker, ticket) — re-acquiring refreshes the TTL. Returns granted or the blocking conflict."""
    if not ticket or not worker or not locks:
        return {"ok": False, "granted": False, "error": "ticket, worker and locks are required"}
    now = time.time()
    leases = _load_active(now)
    clash = _conflict(list(locks), leases, worker, ticket)
    if clash:
        clash["retry_after"] = min((round(l["expires"] - now) for l in leases
                                    if l.get("worker") == clash["held_by"]), default=None)
        return {"ok": True, "granted": False, "conflict": clash}
    leases = [l for l in leases if not (l.get("worker") == worker and l.get("ticket") == ticket)]
    lease = {"id": f"lease:{worker}:{ticket}", "ticket": ticket, "worker": worker,
             "locks": list(locks), "acquired": now, "expires": now + ttl_minutes * 60,
             "ttl_minutes": ttl_minutes, "status": "reserved"}
    leases.append(lease)
    _save(leases)
    return {"ok": True, "granted": True, "lease": lease}


def renew(lease_id: str, worker: str, ttl_seconds: int = 1800, status: str = "") -> dict:
    """Heartbeat: extend a lease's TTL and record its status. Rejects a foreign worker."""
    now = time.time()
    leases = _load_active(now)
    for l in leases:
        if l.get("id") == lease_id:
            if l.get("worker") != worker:
                return {"ok": False, "error": "lease owned by another worker", "rejected": True}
            l["expires"] = now + max(1, int(ttl_seconds))
            if status:
                l["status"] = status
            _save(leases)
            return {"ok": True, "lease": l}
    return {"ok": False, "error": "lease not found or expired"}


def _release_by_id(leases: list[dict], lease_id: str, worker: str) -> list[dict] | None:
    """Drop the lease with this id; None when a foreign worker tries to release it."""
    owner = next((l for l in leases if l.get("id") == lease_id), None)
    if owner and worker and owner.get("worker") != worker:
        return None
    return [l for l in leases if l.get("id") != lease_id]


def _release_by_match(leases: list[dict], worker: str, ticket: str) -> list[dict]:
    return [l for l in leases
            if not ((not worker or l.get("worker") == worker) and (not ticket or l.get("ticket") == ticket))]


def release(worker: str = "", ticket: str = "", lease_id: str = "") -> dict:
    """Release a lease. With ``lease_id`` the caller's ``worker`` must own it (foreign release
    rejected). Otherwise release everything matching worker and/or ticket."""
    if not worker and not ticket and not lease_id:
        return {"ok": False, "error": "give worker, ticket or lease_id"}
    leases = _load_active(time.time())
    if lease_id:
        kept = _release_by_id(leases, lease_id, worker)
        if kept is None:
            return {"ok": False, "rejected": True, "error": "cannot release another worker's lease"}
    else:
        kept = _release_by_match(leases, worker, ticket)
    _save(kept)
    return {"ok": True, "released": len(leases) - len(kept), "active": len(kept)}


def _open_tickets(statuses: tuple[str, ...], exclude_blocked: bool, project: str) -> list[dict]:
    """Runnable tickets (priority order) from planfile. exclude_blocked drops any ticket still
    held by an unfinished blocked_by. [] if planfile is absent."""
    try:
        from planfile import Planfile
        pf = Planfile(project) if project else Planfile.auto_discover()
    except Exception:  # noqa: BLE001
        return []
    order = {"critical": 0, "high": 1, "normal": 2, "low": 3}
    allt = list(pf.list_tickets(sprint="current"))
    stat = {t.id: (t.status.value if hasattr(t.status, "value") else str(t.status)) for t in allt}
    out = []
    for t in allt:
        if stat[t.id] not in statuses:
            continue
        if exclude_blocked and any(stat.get(b) not in ("done", "canceled") for b in (t.blocked_by or [])):
            continue
        out.append({"id": t.id, "priority": str(t.priority), "files": list(t.files or []),
                    "labels": list(t.labels or []), "name": t.name})
    out.sort(key=lambda d: order.get(d["priority"], 9))
    return out


def _lane_ok(ticket: dict, lane: str) -> bool:
    """A ticket matches a lane if it declares that lane, or declares none (any-lane)."""
    if not lane:
        return True
    tlanes = [l.split(":", 1)[1].lower() for l in ticket["labels"] if str(l).lower().startswith("lane:")]
    return not tlanes or lane.lower() in tlanes


def _goal_relevant(ticket: dict) -> bool:
    try:
        from . import goal
        return goal.goal_relevant(ticket)
    except Exception:  # noqa: BLE001
        return True


def _is_frontier(ticket: dict) -> bool:
    """Zamrożona granica autonomii (autonomy-frontier) — NIGDY nie claimowana jako praca."""
    return any(str(l).lower() == "autonomy-frontier" for l in (ticket.get("labels") or []))



def claim_next(worker: str, *, lane: str = "", project: str = "", statuses: tuple[str, ...] = ("open", "ready"),
               ttl_seconds: int = 1800, exclude_blocked: bool = True, dry_run: bool = False) -> dict:
    """Lock+grant-aware scheduler core. Walk runnable tickets in priority order; skip those
    frozen (autonomy-frontier), OFF-GOAL (goal-mode), off-lane, gate-blocked, or lock-conflicting;
    lease the first free one. ``dry_run`` reports what it WOULD claim without acquiring a lease.
    ``goal`` (or env ``CURRENT_GOAL``) restricts selection to that goal's tickets (goal-delivery mode)."""
    from . import gates as _gates
    ttl_min = max(1, int(ttl_seconds) // 60)
    now = time.time()
    active = _load_active(now)
    skipped, blocked = [], []
    for t in _open_tickets(statuses, exclude_blocked, project):
        if not _lane_ok(t, lane):
            continue
        if not _goal_relevant(t):  # GOAL-MODE: pomiń unrelated (zamrożone na czas celu)
            blocked.append({"ticket": t["id"], "blocked_by": "goal-freeze",
                            "reason": "unrelated do bieżącego celu (goal-delivery mode)"})
            continue
        if _is_frontier(t):  # zamrożone granice — nie wybieraj jako pracy (kryt. 4)
            blocked.append({"ticket": t["id"], "blocked_by": "autonomy-frontier",
                            "reason": "frozen frontier — not claimable"})
            continue
        # STAGE 4: pełna bramka — policy ∧ grant ∧ proxy ∧ delegation ∧ readiness ∧ containment
        gate = _gates.runnable_gate(t, project)
        if not gate.get("runnable"):
            blocked.append({"ticket": t["id"], "blocked_by": gate.get("blocked_by"),
                            "reason": gate.get("reason"), "unlock_uri": gate.get("unlock_uri"),
                            "delegation": gate.get("delegation")})
            continue
        lk = locks_for_ticket(t)
        clash = _conflict(lk, active, worker, t["id"])
        if clash:
            skipped.append({"ticket": t["id"], "held_by": clash["held_by"]})
            continue
        if dry_run:  # shadow: pokaż wybór bez rezerwacji
            return {"ticket": {"id": t["id"], "title": t["name"]}, "lease": None, "would_claim": True,
                    "dry_run": True, "locks": lk, "gate": gate.get("gate"), "skipped": skipped, "blocked": blocked}
        res = acquire(t["id"], worker, lk, ttl_min)
        if res.get("granted"):
            _trace("ticket_claimed", ticket=t["id"], worker=worker, action=gate.get("action"),
                   gate=gate.get("gate"), delegation=gate.get("delegation"))
            return {"ticket": {"id": t["id"], "title": t["name"]}, "lease": res["lease"],
                    "locks": lk, "gate": gate.get("gate"), "delegation": gate.get("delegation"),
                    "skipped": skipped, "blocked": blocked}
    return {"ticket": None, "skipped": skipped, "blocked": blocked,
            "reason": "no non-conflicting runnable ticket"}



@contextmanager
def leased(worker: str, *, lane: str = "", project: str = "", ttl_seconds: int = 1800):
    """Worker protocol as a context manager — guarantees the lease is released in ``finally``
    even on crash. ``with work.leased('w1') as claim: if claim['ticket']: run(...)``."""
    claim = claim_next(worker, lane=lane, project=project, ttl_seconds=ttl_seconds)
    lease = claim.get("lease")
    try:
        yield claim
    finally:
        if lease:
            release(worker=worker, lease_id=lease["id"])


# ── handlers ──────────────────────────────────────────────────────────────────

@conn.handler("lease/command/acquire", isolated=True,
              meta={"label": "Zarezerwuj ticket (lease + locki); grant albo zgłoś konflikt"})
def lease_command_acquire(ticket: str = "", worker: str = "", locks: Any = None,
                          ttl_minutes: int = 30) -> dict[str, Any]:
    lk = list(locks) if isinstance(locks, (list, tuple)) else ([locks] if locks else [])
    res = acquire(ticket, worker, lk, int(ttl_minutes))
    return _ok(action="lease-acquire", **res) if res.get("ok") else _fail(res.get("error", "acquire failed"), "lease-acquire")


@conn.handler("lease/command/claim-next", isolated=True,
              meta={"label": "Scheduler lock+grant-aware: zajmij następny wykonalny ticket"})
def lease_command_claim_next(worker: str = "", lane: str = "", project: str = "",
                             statuses: str = "open,ready", ttl_seconds: int = 1800,
                             exclude_blocked: bool = True) -> dict[str, Any]:
    if not worker:
        return _fail("worker is required", "claim-next")
    want = tuple(s.strip() for s in statuses.split(",") if s.strip()) or ("open",)
    res = claim_next(worker, lane=lane, project=project, statuses=want,
                     ttl_seconds=int(ttl_seconds), exclude_blocked=bool(exclude_blocked))
    return _ok(action="claim-next", worker=worker, **res)


@conn.handler("lease/command/renew", isolated=True,
              meta={"label": "Heartbeat: przedłuż lease + zgłoś status (odrzuca obcego workera)"})
def lease_command_renew(lease_id: str = "", worker: str = "", ttl_seconds: int = 1800,
                        status: str = "") -> dict[str, Any]:
    res = renew(lease_id, worker, int(ttl_seconds), status)
    return _ok(action="lease-renew", **res) if res.get("ok") else _fail(res.get("error", "renew failed"), "lease-renew", **res)


@conn.handler("lease/command/release", isolated=True,
              meta={"label": "Zwolnij lease (ownership-guarded)"})
def lease_command_release(worker: str = "", ticket: str = "", lease_id: str = "") -> dict[str, Any]:
    res = release(worker, ticket, lease_id)
    return _ok(action="lease-release", **res) if res.get("ok") else _fail(res.get("error", "release failed"), "lease-release", **res)


@conn.handler("locks/query/list", isolated=False,
              meta={"label": "Aktywne leasy + trzymane locki"})
def locks_query_list() -> dict[str, Any]:
    now = time.time()
    leases = _load_active(now)
    for l in leases:
        l["expires_in"] = round(l["expires"] - now)
    return _ok(action="locks-list", active=leases, count=len(leases),
               locks=sorted({lk for lease in leases for lk in lease.get("locks", [])}))


@conn.handler("workers/query/status", isolated=False,
              meta={"label": "Co trzyma każdy worker (leasy + status + TTL)"})
def workers_query_status() -> dict[str, Any]:
    now = time.time()
    by_worker: dict[str, list] = {}
    for l in _load_active(now):
        by_worker.setdefault(l.get("worker", "?"), []).append(
            {"ticket": l.get("ticket"), "status": l.get("status"), "locks": l.get("locks", []),
             "expires_in": round(l["expires"] - now)})
    return _ok(action="workers-status", workers=by_worker, count=len(by_worker))


def run_action(ticket: dict, *, project: str = "", root: str = "", publish: bool = False,
               timeout: float = 600.0) -> dict[str, Any]:
    """IFURI-170: the executor a claimed action-ticket was missing. Deterministic script for
    known actions (e.g. pypi.publish → publish_release_chain.py, plan-only unless
    ``publish=True``); agent:// (any installed headless coding tool) for everything else.
    This replaces koru's generic CLI-drive, which has no code to change on an action ticket
    and fails with no_executor / "client command failed"."""
    from . import gates as _gates
    action = _gates.action_of_ticket(ticket)
    if not action:
        return _fail("brak wykrywalnej akcji na tickecie", "run-action", ticket=ticket.get("id"))
    argv = _gates.executor_for(action, root=root)
    if argv:
        if action == "pypi.publish" and publish:
            argv = [*argv, "--publish"]
        try:
            cp = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)  # noqa: S603
        except Exception as exc:  # noqa: BLE001
            return _fail(str(exc), "run-action", action=action, executor="script")
        return _ok(action="run-action", ticket=ticket.get("id"), executed_action=action, executor="script",
                   argv=argv, returncode=cp.returncode, ok_run=cp.returncode == 0,
                   stdout=(cp.stdout or "")[-4000:], stderr=(cp.stderr or "")[-1000:])
    try:
        from urirun_connector_agents import core as agents
    except Exception as exc:  # noqa: BLE001
        return _fail(f"brak deterministycznego executora dla {action!r}, agent:// niedostępny: {exc}",
                     "run-action", action=action, executor="none")
    prompt = f"Wykonaj ticket {ticket.get('id')}: {ticket.get('name')}\n{ticket.get('description', '')}"
    res = agents.task_run(prompt=prompt, cwd=project, timeout=timeout)
    return {**res, "action": "run-action", "ticket": ticket.get("id"), "executed_action": action, "executor": "agent"}


@conn.handler("lease/command/run-action", isolated=True,
              meta={"label": "IFURI-170: wykonaj akcję claimed ticketu — deterministyczny script > agent:// (fix no_executor)"})
def lease_command_run_action(ticket: str = "", worker: str = "", project: str = "",
                             publish: bool = False, timeout: float = 600.0) -> dict[str, Any]:
    if not ticket or not worker:
        return _fail("ticket i worker wymagane", "run-action")
    active = _load_active(time.time())
    if not any(l.get("ticket") == ticket and l.get("worker") == worker for l in active):
        return _fail(f"brak aktywnego lease dla {ticket} @ {worker} — claim-next najpierw", "run-action")
    t = next((x for x in _open_tickets(("open", "ready", "in_progress"), False, project)
              if x["id"] == ticket), None) or {"id": ticket}
    return run_action(t, project=project, publish=publish, timeout=timeout)


@conn.handler("gate/command/next-work", isolated=True,
              meta={"label": "IFURI-199: brama pracy koru (shadow/soft/hard) — claim-next jako jedyna brama"})
def gate_command_next_work(worker: str = "", mode: str = "", lane: str = "", project: str = "") -> dict[str, Any]:
    from . import work_gate
    if not worker:
        return _fail("worker wymagany", "next-work")
    return _ok(action="next-work", worker=worker, **work_gate.next_work(worker, mode=mode, lane=lane, project=project))


@conn.handler("verify/command/postcondition", isolated=False,
              meta={"label": "IFURI-197/198: kontrakt weryfikacji command — effect+verified+provenance (ok≠efekt)"})
def verify_command_postcondition(result: dict | None = None, uri: str = "", expect: Any = None, strict: bool = False) -> dict[str, Any]:
    from . import postcondition
    return _ok(action="postcondition", **postcondition.enforce(result or {}, uri, expect, strict=bool(strict)))


@conn.handler("goal/command/set", isolated=True,
              meta={"label": "GOAL-DELIVERY: ustaw nadrzędny cel (freeze self-evolution, filtruj unrelated)"})
def goal_command_set(goal: str = "", domain: str = "", recipient: str = "") -> dict[str, Any]:
    from . import goal as _goal
    return _ok(action="goal-set", **_goal.set_goal(goal, domain=domain, recipient=recipient)) if goal else _fail("goal wymagany", "goal-set")


@conn.handler("goal/query/current", isolated=False, meta={"label": "Bieżący cel + czy freeze aktywny"})
def goal_query_current() -> dict[str, Any]:
    from . import goal as _goal
    return _ok(action="goal-current", goal=_goal.current_goal(), freeze=_goal.freeze_self_evolution())


@conn.handler("goal/command/clear", isolated=True, meta={"label": "Wyczyść cel → powrót do code-improvement mode"})
def goal_command_clear() -> dict[str, Any]:
    from . import goal as _goal
    return _ok(action="goal-clear", **_goal.clear_goal())


@conn.handler("goal/command/deliver-signal", isolated=True,
              meta={"label": "SIGNAL-E2E-001: wykonaj dostawę signal.message.send (waiting_human jeśli brak linku)"})
def goal_command_deliver_signal(recipient: str = "", message: str = "") -> dict[str, Any]:
    from . import goal as _goal
    return _ok(action="deliver-signal", **_goal.deliver_signal(recipient, message))


@conn.handler("executor/query/chain", isolated=False,
              meta={"label": "IFURI-200: klasyfikacja porażki (no_executor?) + dostępni agenci fallback + tryb"})
def executor_query_chain(error: str = "") -> dict[str, Any]:
    from . import executor
    return _ok(action="executor-chain", is_no_executor=executor.is_no_executor({"error": error}),
               agents_available=executor.agents_available(),
               fallback_mode=os.environ.get("KORU_EXECUTOR_FALLBACK", "shadow"))


@conn.handler("gate/query/report", isolated=False,
              meta={"label": "Stan bramek dla WSZYSTKICH ticketów: runnable / blocked_by (policy/grant/proxy/delegation/readiness)"})
def gate_query_report(project: str = "") -> dict[str, Any]:
    from . import gates as _gates
    rows, by = [], {}
    for t in _open_tickets(("open", "ready", "waiting_input"), False, project):
        g = _gates.runnable_gate(t, project)
        rows.append({"ticket": t["id"], "runnable": g.get("runnable"), "action": g.get("action"),
                     "blocked_by": g.get("blocked_by"), "delegation": g.get("delegation"),
                     "reason": (g.get("reason") or "")[:60]})
        k = "runnable" if g.get("runnable") else (g.get("blocked_by") or "other")
        by[k] = by.get(k, 0) + 1
    return _ok(action="gate-report", tickets=rows, summary=by, total=len(rows))


@conn.handler("rollout/query/status", isolated=False,
              meta={"label": "Panel /work: rollout IFURI-199 — mode/source/divergence/frontier-claims/gate-errors"})
def rollout_query_status() -> dict[str, Any]:
    from . import ticket_source as ts
    return _ok(action="rollout-status", **ts.rollout_status())


def urirun_bindings() -> dict[str, Any]:
    return conn.bindings()


def main(argv: list[str] | None = None) -> int:
    return conn.cli(argv, manifest_prose=None)


if __name__ == "__main__":
    raise SystemExit(main())
