# Author: Tom Sapletta · Part of the ifURI solution.
"""IFURI-199 — drop-in shim for koru's ``KORU_PLANFILE_CMD``, the last unwired seam.

koru's live queue runner shells out to the real ``planfile`` binary to pick its next ticket
(``planfile ticket list --status open --format json``). Wiring that call through the control
plane in one step is exactly what oscillated (IFURI-211): the shadow measurement in
``ticket_source``/``work_gate`` had only ever been exercised by calling those modules directly,
never from inside koru's actual live subprocess.

This shim closes that seam the safe way: it passes every call straight through to the real
``planfile`` executable and returns its stdout/stderr/exit code UNCHANGED — koru's behaviour is
byte-identical whether this shim is wired in or not. The only side effect is that an open-ticket
listing additionally runs the existing shadow comparison (legacy vs. a dry-run claim-next) and
logs the divergence, exactly like ``ticket_source.next_ticket(mode="shadow")`` already does when
called directly. The shim now reads KORU_WORK_GATE (default hard) and does appropriate telemetry.
Hard mode is the long-term target: claim-next (work gate + grants + leases) is the
only way to obtain work. Cutover is deliberate; shim stays transparent for planfile list.

Wire-up (manual, an ops decision — NOT performed by this shim or by importing it):
    export KORU_PLANFILE_CMD="urirun-work-koru-shim"
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys

_REAL_CMD_ENV = "URIRUN_REAL_PLANFILE_CMD"
_OPEN_TICKET_LIST = ("ticket", "list")


def _real_planfile_cmd() -> list[str]:
    override = os.environ.get(_REAL_CMD_ENV)
    if override:
        return shlex.split(override)
    found = shutil.which("planfile")
    return [found] if found else [sys.executable, "-m", "planfile.cli"]


def _is_open_ticket_list(argv: list[str]) -> bool:
    return (len(argv) >= 2 and tuple(argv[:2]) == _OPEN_TICKET_LIST
            and "--status" in argv and "open" in argv)


def _first_ticket_id(stdout: str) -> str | None:
    try:
        data = json.loads(stdout)
    except ValueError:
        return None
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0].get("id")
    return None


def _shadow_measure(argv: list[str], stdout: str) -> None:
    """Telemetry only — must never affect what koru sees, so failures are swallowed."""
    if not _is_open_ticket_list(argv):
        return
    try:
        from . import ticket_source
        legacy_id = _first_ticket_id(stdout)
        legacy_next = (lambda: {"id": legacy_id}) if legacy_id else None
        worker = os.environ.get("KORU_WORKER_ID") or f"koru-shim-{os.getpid()}"
        ticket_source.next_ticket(worker, project=os.getcwd(), legacy_next=legacy_next, mode="shadow")
    except Exception:  # noqa: BLE001 - shadow measurement must never break the real call
        pass


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    result = subprocess.run(_real_planfile_cmd() + argv, capture_output=True, text=True)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)

    # Respect KORU_WORK_GATE for telemetry / transition.
    # For hard mode we still pass through (koru config + ticket_source should drive the gate),
    # but we can measure with the real mode.
    try:
        from . import ticket_source
        mode = ticket_source.gate_mode()
        if mode in ("shadow", "soft"):
            _shadow_measure(argv, result.stdout)
        elif mode == "hard":
            # In hard, we expect koru to be wired via ticket_source / claim-next.
            # Still log a peek for observability (non-mutating).
            if _is_open_ticket_list(argv):
                from . import core as work
                worker = os.environ.get("KORU_WORKER_ID") or f"koru-shim-hard-{os.getpid()}"
                wc = work.claim_next(worker, project=os.getcwd(), dry_run=True)
                # log divergence if legacy list would differ (for audit)
                legacy_id = _first_ticket_id(result.stdout)
                work_id = (wc.get("ticket") or {}).get("id") if wc else None
                if legacy_id != work_id:
                    # write to log
                    import json, time
                    logp = __import__("pathlib").Path(os.environ.get("URIRUN_WORK_GATE_LOG") or "~/.urirun/host-dashboard/work-gate.jsonl").expanduser()
                    logp.parent.mkdir(parents=True, exist_ok=True)
                    with open(logp, "a", encoding="utf-8") as f:
                        f.write(json.dumps({"event": "koru.work_gate.hard_peek", "at": int(time.time()),
                                            "legacy": legacy_id, "work": work_id, "blocked": len(wc.get("blocked") or [])}) + "\n")
    except Exception:
        pass  # never break the shim
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
