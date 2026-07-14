# urirun-connector-work — `work://` control plane

Lock-aware **leases** so a fleet of workers (koru + a main agent) clears the backlog in
parallel **without editing the same repo/path at once**. A worker declares the resource locks
a ticket needs, acquires a time-boxed lease, and proceeds only if nothing holds a conflicting
lock — otherwise it takes the next non-conflicting ticket. Leases auto-expire (TTL) so a dead
worker never blocks the fleet.

## Locks

Hierarchical resource keys; two locks conflict when one is an **ancestor** of the other:

| Lock | Conflicts with |
|------|----------------|
| `repo:if-uri/urirun` | any `path:if-uri/urirun/…` inside it |
| `path:if-uri/urirun/adapters/x/core.py` | the same file, or a repo lock above it |
| `path:…/a.py` vs `path:…/b.py` | **no** — siblings run in parallel |
| `ticket:IFURI-077` | only the same ticket (no double-work) |

`locks_for_ticket()` derives them: the ticket id + one lock per declared file, or a coarse
whole-repo lock when no files are declared.

## Routes

| URI | Purpose |
|-----|---------|
| `work://host/lease/command/claim-next` | **Lock+grant-aware scheduler**: lease the next runnable ticket that is on-lane, not human/grant-blocked, and lock-free. |
| `work://host/lease/command/acquire` | Acquire a lease for an explicit ticket (grant, or report the conflict + retry_after). |
| `work://host/lease/command/renew` | Heartbeat: extend a lease's TTL + report status (rejects a foreign worker). |
| `work://host/lease/command/release` | Release a lease (ownership-guarded via `lease_id`). |
| `work://host/locks/query/list` | Active leases + held locks. |
| `work://host/workers/query/status` | What each worker holds (+ status + TTL). |

## Worker protocol (IFURI-176)

**Claim-next is now the mandatory gate (hard mode default).** 
KORU_WORK_GATE=hard (or soft/shadow for transition). Workers (koru etc.) must go through
work://claim-next + lease. Legacy direct planfile next is deprecated for autonomy.

A worker no longer takes "the next ticket" — it asks `claim-next` and only proceeds with the
lease it gets back. **Always release in `finally`** (TTL is only a backup):

```python
from urirun_connector_work import core as work

# option A — explicit, with heartbeat
claim = work.claim_next("koru-worker-1", lane="fleet", project="/home/tom/github/if-uri")
if claim["ticket"]:
    lease = claim["lease"]
    try:
        run_ticket(claim["ticket"], locks=claim["locks"])
        work.renew(lease["id"], "koru-worker-1", ttl_seconds=1800, status="running_tests")  # heartbeat
    finally:
        work.release(worker="koru-worker-1", lease_id=lease["id"])

# option B — context manager guarantees the finally-release even on crash
with work.leased("koru-worker-1", lane="fleet", project="…") as claim:
    if claim["ticket"]:
        run_ticket(claim["ticket"])
```

`claim-next` response carries `ticket`, `lease` (id/locks/expires), `skipped` (lock conflicts),
and `blocked` (human/grant — with an `unlock_uri`), so the panel shows the truth. If a ticket's
files are free but it needs a grant/human, it is reported `blocked`, not claimed — this is where
**grants://** meets **work://**. Store: `~/.urirun/host-dashboard/work-leases.json`
(`URIRUN_WORK_LEASES`).

Tickets that declare `files` get file-level locks → **parallel**; un-scoped tickets get a coarse
repo lock → safely serialized (over-lock rather than conflict). Decompose big tickets into
file-scoped subtasks (`planfile ticket split … "name @ file"`) to unlock real parallelism.

## Tests

```bash
python -m pytest tests/ -q
```
