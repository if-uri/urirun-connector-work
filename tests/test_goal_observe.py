# Author: Tom Sapletta · Part of the ifURI solution.
"""Regression: observe+focus Signal BEFORE any KVM keyboard input (no curl in wrong window)."""
from __future__ import annotations

from urirun_connector_work import goal
from urirun_connector_work.signal_kvm import KVM_URI_HOST


def test_play_audio_remote_uses_detached_shell_not_kvm_type(monkeypatch):
    calls: list[tuple[str, dict | None]] = []

    def fake_node_run(node, uri, payload=None, timeout=15.0):
        calls.append((uri, payload))
        return {"ok": True}

    monkeypatch.setattr(goal, "_node_run", fake_node_run)
    monkeypatch.setattr(goal, "_run_detached_shell_on_node", lambda node, cmd, timeout=10: True)
    monkeypatch.setattr(goal.time, "sleep", lambda *_a, **_k: None)
    goal._play_audio_remote("/tmp/fake.wav", controller_ip="127.0.0.1", port=19999, node="lenovo")
    assert not any("input/command/type" in u for u, _ in calls)


def test_ensure_gui_ready_focuses_signal_when_not_visible(monkeypatch):
    state = {"alt_tab": False}
    keys_seen: list[str] = []

    def fake_node_run(node, uri, payload=None, timeout=15):
        if "desktop/query/list" in uri:
            return {"apps": [{"id": "org.signal.Signal", "name": "Signal"}]}
        if "ui/query/verify" in uri:
            return {"present": state["alt_tab"], "via": "tesseract"}
        if "input/command/key" in uri:
            keys = (payload or {}).get("keys", "")
            keys_seen.append(keys)
            state["alt_tab"] = "alt+Tab" in keys
        return {"ok": True}

    monkeypatch.setattr(goal, "_capture_lowres", lambda *a, **k: ("/tmp/o.png", None))
    monkeypatch.setattr(goal, "_node_run", fake_node_run)
    monkeypatch.setattr(goal, "_signal_visible_on_screen", lambda node: False)
    monkeypatch.setattr(goal.time, "sleep", lambda *_a, **_k: None)
    r = goal._ensure_gui_ready_for_signal("lenovo")
    assert r["ok"] is True
    assert r["observe"].get("signal_visible_after_alttab") is True
    assert any("alt+Tab" in k for k in keys_seen)


def test_guarded_node_run_blocks_type_when_signal_not_focused(monkeypatch):
    monkeypatch.setattr(goal, "_ensure_gui_ready_for_signal",
                        lambda node, ticket=None: {"ok": False, "observe": {"signal_visible_initial": False}})
    r = goal._guarded_node_run("lenovo", f"kvm://{KVM_URI_HOST}/input/command/type", {"text": "curl evil"}, ticket="T")
    assert r.get("error") == "signal_not_focused"
    assert r.get("reason")
    assert r.get("_meta", {}).get("source") == "send_via_kvm"
