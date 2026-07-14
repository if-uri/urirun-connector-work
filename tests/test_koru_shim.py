"""IFURI-199 — koru_shim passthrough must be byte-identical, shadow measurement is a side effect only."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from urirun_connector_work import core, koru_shim, ticket_source as ts


class _FakeResult(SimpleNamespace):
    pass


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(ts, "_LOG", tmp_path / "gate.jsonl")
    monkeypatch.delenv("KORU_WORK_GATE", raising=False)


def _fake_run(stdout="", stderr="", returncode=0):
    calls = []

    def run(cmd, capture_output, text):
        calls.append(cmd)
        return _FakeResult(stdout=stdout, stderr=stderr, returncode=returncode)

    return run, calls


def test_passthrough_is_byte_identical_and_forwards_argv(monkeypatch, capsys):
    run, calls = _fake_run(stdout="not json\n", stderr="warn\n", returncode=3)
    monkeypatch.setattr(koru_shim.subprocess, "run", run)
    monkeypatch.setattr(koru_shim, "_real_planfile_cmd", lambda: ["planfile"])

    rc = koru_shim.main(["ticket", "show", "IFURI-1"])

    assert rc == 3
    out = capsys.readouterr()
    assert out.out == "not json\n" and out.err == "warn\n"
    assert calls == [["planfile", "ticket", "show", "IFURI-1"]]


def test_non_matching_call_never_touches_ticket_source(monkeypatch, capsys):
    run, _ = _fake_run(stdout="[]\n")
    monkeypatch.setattr(koru_shim.subprocess, "run", run)
    monkeypatch.setattr(koru_shim, "_real_planfile_cmd", lambda: ["planfile"])

    def _boom(*a, **k):
        raise AssertionError("ticket_source must not run for non-listing calls")
    monkeypatch.setattr(ts, "next_ticket", _boom)

    koru_shim.main(["ticket", "update", "IFURI-1", "--status", "done"])
    capsys.readouterr()  # drain


def test_open_ticket_list_measures_shadow_without_changing_output(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("KORU_WORK_GATE", "shadow")
    stdout = json.dumps([{"id": "IFURI-033", "name": "legacy pick"}])
    run, _ = _fake_run(stdout=stdout, returncode=0)
    monkeypatch.setattr(koru_shim.subprocess, "run", run)
    monkeypatch.setattr(koru_shim, "_real_planfile_cmd", lambda: ["planfile"])
    monkeypatch.setattr(core, "claim_next",
                         lambda w, **k: {"ticket": {"id": "IFURI-177"}, "blocked": []})

    rc = koru_shim.main(["ticket", "list", "--status", "open", "--format", "json"])

    assert rc == 0
    assert capsys.readouterr().out == stdout
    ev = json.loads((tmp_path / "gate.jsonl").read_text().splitlines()[-1])
    assert ev["event"] == "koru.work_gate.shadow_compare"
    assert ev["legacy_ticket"] == "IFURI-033" and ev["work_ticket"] == "IFURI-177"


def test_shadow_measurement_failure_never_breaks_the_real_call(monkeypatch, capsys):
    stdout = json.dumps([{"id": "IFURI-033"}])
    run, _ = _fake_run(stdout=stdout, returncode=0)
    monkeypatch.setattr(koru_shim.subprocess, "run", run)
    monkeypatch.setattr(koru_shim, "_real_planfile_cmd", lambda: ["planfile"])

    def _boom(*a, **k):
        raise RuntimeError("work:// unreachable")
    monkeypatch.setattr(ts, "next_ticket", _boom)

    rc = koru_shim.main(["ticket", "list", "--status", "open", "--format", "json"])

    assert rc == 0
    assert capsys.readouterr().out == stdout


def test_real_planfile_cmd_prefers_env_override(monkeypatch):
    monkeypatch.setenv(koru_shim._REAL_CMD_ENV, "python -m planfile.cli")
    assert koru_shim._real_planfile_cmd() == ["python", "-m", "planfile.cli"]
