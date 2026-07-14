# Author: Tom Sapletta · Part of the ifURI solution.
"""IFURI-199 — koru adoption of ``work://claim-next`` as the sole work gate, in 4 SAFE modes.

koru should not have its live loop hard-swapped in one step. This thin ``ticket_source`` layer
lets koru migrate gradually — it replaces koru's direct ``planfile.next()`` with a single call
whose behaviour is chosen by ``KORU_WORK_GATE``:

    off      legacy planfile pick, no lease (today's behaviour)
    shadow   legacy pick + a DRY-RUN claim-next; log the divergence; still run the legacy ticket
    soft     claim-next; fall back to legacy ONLY if work:// itself errors (degraded event)
    hard     claim-next ONLY; if work:// is unavailable, STOP the worker with a clear error

Shadow mode is the proof step: it shows that claim-next filters frontier/gated tickets before
work ever starts, without steering production. The worker protocol stays tiny:

    item = ticket_source.next_ticket(worker_id, legacy_next=planfile_next)
    lease = (item or {}).get("lease")
    try:
        if item: run_ticket(item["ticket"])
    finally:
        ticket_source.release(item, worker_id)   # TTL is only a backstop
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable

_MODES = ("off", "shadow", "soft", "hard")
_LOG = Path(os.environ.get("URIRUN_WORK_GATE_LOG")
            or "~/.urirun/host-dashboard/work-gate.jsonl").expanduser()


class WorkGateUnavailable(RuntimeError):
    """Raised in hard mode when work:// cannot be reached — the worker must stop, not fall back."""


def gate_mode() -> str:
    # Long-term: hard mode is the target (claim-next is the only gate).
    # Set KORU_WORK_GATE=hard to enforce. Default now hard for new autonomy.
    m = (os.environ.get("KORU_WORK_GATE") or "hard").strip().lower()
    return m if m in _MODES else "hard"


def _log(event: str, **kw: Any) -> None:
    try:
        _LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"event": event, "at": int(time.time()), **kw}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _ticket_id(item: Any) -> str | None:
    if isinstance(item, dict):
        return item.get("id") or (item.get("ticket") or {}).get("id") if isinstance(item.get("ticket"), dict) else item.get("id")
    return getattr(item, "id", None)


def _legacy_result(legacy_next: Callable | None, mode: str, source: str = "legacy") -> dict | None:
    t = legacy_next() if legacy_next else None
    return {"ticket": t, "lease": None, "source": source, "mode": mode, "gate": None} if t else None


def _shadow(worker: str, project: str, lane: str, legacy_next: Callable | None, mode: str) -> dict | None:
    """Run legacy, compare against a dry-run claim-next, log the divergence, return legacy."""
    from . import core as work
    legacy_item = legacy_next() if legacy_next else None
    try:
        wc = work.claim_next(worker, project=project, lane=lane, dry_run=True)
    except Exception as exc:  # noqa: BLE001
        wc = {"ticket": None, "error": str(exc)[:120]}
    legacy_id = _ticket_id(legacy_item)
    work_id = (wc.get("ticket") or {}).get("id")
    blocked = wc.get("blocked", [])
    legacy_skip = next((b for b in blocked if b.get("ticket") == legacy_id), None)
    _log("koru.work_gate.shadow_compare", worker=worker, legacy_ticket=legacy_id, work_ticket=work_id,
         divergence=(legacy_id != work_id),
         legacy_is_frontier=bool(legacy_skip and legacy_skip.get("blocked_by") == "autonomy-frontier"),
         work_skipped_reason=(legacy_skip or {}).get("blocked_by"),
         work_blocked=[b.get("ticket") for b in blocked][:8],
         reason=wc.get("error") or "legacy may pick frontier/gated; work:// filters it")
    return {"ticket": legacy_item, "lease": None, "source": "legacy(shadow)", "mode": mode, "gate": None} if legacy_item else None


def _soft_hard(worker: str, project: str, lane: str, ttl_seconds: int, legacy_next: Callable | None, mode: str) -> dict | None:
    """claim-next; soft falls back to legacy only on work:// ERROR; hard never falls back."""
    from . import core as work
    try:
        claim = work.claim_next(worker, project=project, lane=lane, ttl_seconds=ttl_seconds)
    except Exception as exc:  # noqa: BLE001 - work:// unavailable
        if mode == "hard":
            _log("koru.work_gate.unavailable", mode="hard", worker=worker, error=str(exc)[:120])
            raise WorkGateUnavailable(f"work:// unavailable in hard mode — worker stopped: {exc}") from exc
        _log("koru.work_gate.degraded", mode="soft", worker=worker, error=str(exc)[:120])
        return _legacy_result(legacy_next, mode, source="legacy(degraded)")
    if claim.get("ticket") and claim.get("lease"):
        return {"ticket": claim["ticket"], "lease": claim["lease"], "source": "work://claim-next",
                "mode": mode, "gate": claim.get("gate")}
    # work:// reachable but nothing runnable (all gated/frontier/locked) → idle. Do NOT fall back
    # to legacy work here — that would defeat the gate. Fallback is only for work:// being down.
    return None


def next_ticket(worker: str, *, project: str = "", lane: str = "", ttl_seconds: int = 1800,
                legacy_next: Callable[[], Any] | None = None, mode: str = "") -> dict | None:
    """The single work gate. Returns ``{ticket, lease, source, mode, gate}`` or None."""
    mode = (mode or gate_mode()).lower()
    if mode == "off":
        return _legacy_result(legacy_next, mode)
    if mode == "shadow":
        return _shadow(worker, project, lane, legacy_next, mode)
    return _soft_hard(worker, project, lane, ttl_seconds, legacy_next, mode)


def renew(item: dict, worker: str, *, ttl_seconds: int = 1800, status: str = "running") -> dict:
    """Heartbeat while running — extend the lease TTL (no-op for legacy items with no lease)."""
    from . import core as work
    lease = (item or {}).get("lease")
    return work.renew(lease["id"], worker, ttl_seconds, status) if lease else {"ok": True, "no_lease": True}


def release(item: dict, worker: str) -> dict:
    """Release in ``finally`` — no-op for legacy items with no lease."""
    from . import core as work
    lease = (item or {}).get("lease")
    return work.release(worker=worker, lease_id=lease["id"]) if lease else {"ok": True, "released": 0}


def _recent_events(limit: int = 200) -> list[dict]:
    try:
        lines = _LOG.read_text(encoding="utf-8").splitlines()[-limit:] if _LOG.is_file() else []
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out


def rollout_status() -> dict:
    """The ``/work`` "Work gate rollout" section: current mode + the healthiness signals to
    watch while migrating (divergences, frontier-claims which MUST stay 0, gate errors, leases)."""
    from . import core as work
    ev = _recent_events()
    shadow = [e for e in ev if e.get("event") == "koru.work_gate.shadow_compare"]
    last_div = next((e for e in reversed(shadow) if e.get("divergence")), None)
    frontier_claims = sum(1 for e in ev if e.get("event") == "ticket_claimed"
                          and e.get("blocked_by") == "autonomy-frontier")  # must be 0
    gate_errors = sum(1 for e in ev if e.get("event") in ("koru.work_gate.unavailable", "koru.work_gate.degraded"))
    leases = work.locks_query_list()
    workers = work.workers_query_status()
    return {"mode": gate_mode(), "shadow_compares": len(shadow),
            "divergences": sum(1 for e in shadow if e.get("divergence")),
            "last_divergence": ({"legacy": last_div.get("legacy_ticket"), "work": last_div.get("work_ticket"),
                                 "legacy_is_frontier": last_div.get("legacy_is_frontier"),
                                 "reason": last_div.get("work_skipped_reason")} if last_div else None),
            "frontier_claims": frontier_claims, "gate_errors": gate_errors,
            "active_leases": leases.get("count", 0), "workers": workers.get("workers", {}),
            "healthy": frontier_claims == 0 and gate_errors == 0}
