# Author: Tom Sapletta · Part of the ifURI solution.
"""Scripted Signal KVM path — observe before type; ticket payload from signal_kvm."""
from __future__ import annotations

from urirun_connector_work import goal
from urirun_connector_work.signal_kvm import DEFAULT_RECIPIENT, payload_from_ticket


def test_send_via_kvm_uses_llm_loop_by_default(monkeypatch):
    seen = {}

    def fake_llm_loop(recipient, text, ticket=None, node="lenovo", initial_plan=None):
        seen["args"] = (recipient, text, ticket, node, initial_plan)
        return {"ok": True, "verified": True, "plan_used": "llm-runtime-loop", "_meta": goal._kvm_meta(node)}

    monkeypatch.delenv("SIGNAL_KVM_PREP", raising=False)
    monkeypatch.setenv("URIRUN_LLM_RUNTIME_CONTROL", "1")
    monkeypatch.setattr(goal, "_send_via_llm_runtime_loop", fake_llm_loop)
    r = goal.send_via_kvm(DEFAULT_RECIPIENT, "hello test", ticket="IFURI-229", node="lenovo")
    assert seen["args"][:4] == (DEFAULT_RECIPIENT, "hello test", "IFURI-229", "lenovo")
    assert r["ok"] is True


def test_send_via_kvm_uses_scripted_when_llm_disabled(monkeypatch):
    seen = {}

    def fake_scripted(recipient, text, ticket=None, node="lenovo"):
        seen["args"] = (recipient, text, ticket, node)
        return {"ok": True, "verified": True, "effect": True, "_meta": goal._kvm_meta(node)}

    monkeypatch.delenv("SIGNAL_KVM_PREP", raising=False)
    monkeypatch.setenv("URIRUN_LLM_RUNTIME_CONTROL", "0")
    monkeypatch.setattr(goal, "_send_signal_gui_scripted", fake_scripted)
    r = goal.send_via_kvm(DEFAULT_RECIPIENT, "hello test", ticket="IFURI-229", node="lenovo")
    assert seen["args"] == (DEFAULT_RECIPIENT, "hello test", "IFURI-229", "lenovo")
    assert r["ok"] is True


def test_scripted_sequence_observe_before_type(monkeypatch):
    order: list[str] = []

    def fake_node(node, uri, payload=None, timeout=15.0):
        order.append(uri)
        if "type-verified" in uri:
            return {"ok": True, "verified": True, "submitted": True}
        if "ui/query/verify" in uri:
            text = (payload or {}).get("text", "")
            return {"present": text in ("Signal", DEFAULT_RECIPIENT, "HEL", "hello", "IFURI-229")}
        if "ui/query/locate" in uri:
            q = (payload or {}).get("query", "")
            if q in ("IFURI-229", "hello"):
                return {"found": True, "count": 1, "matches": [{"center": {"x": 100, "y": 200}}]}
            return {"count": 1, "center": {"x": 100, "y": 200}}
        return {"ok": True}

    monkeypatch.setattr(goal, "_node_run", fake_node)
    monkeypatch.setattr(goal, "_preflight_signal_compose", lambda node, recipient, ticket=None: {
        "ok": True,
        "timeline": [{"step": "observe-focus"}],
        "composer": {"center": [100, 200], "ok": True, "label": "Message"},
    })
    monkeypatch.setattr(goal, "_present_on_screen_tesseract", lambda node, text, **kw: text in (
        "Signal", DEFAULT_RECIPIENT, "HEL", "hello", "IFURI-229", "TOM", "Tom",
    ))
    monkeypatch.setattr(goal, "_capture_quad", lambda *a, **k: ("/tmp/q.png", None))
    monkeypatch.setattr(goal.time, "sleep", lambda *_a, **_k: None)

    r = goal._send_signal_gui_scripted(DEFAULT_RECIPIENT, "hello", ticket="IFURI-229", node="lenovo")
    assert r["ok"] is True
    assert any("type-verified" in u for u in order)


def test_scripted_sequence_verifies_target_window_before_typing(monkeypatch):
    order: list[str] = []

    def fake_node(node, uri, payload=None, timeout=15.0):
        order.append(uri)
        if "type-verified" in uri:
            return {"ok": True, "verified": True, "submitted": True}
        return {"ok": True}

    monkeypatch.setattr(goal, "_node_run", fake_node)
    monkeypatch.setattr(goal, "_preflight_signal_compose", lambda node, recipient, ticket=None: {
        "ok": True,
        "timeline": [{"step": "verify-window"}],
        "composer": {"center": [100, 200], "ok": True},
    })
    monkeypatch.setattr(goal, "_present_on_screen_tesseract", lambda node, text, **kw: text in (
        "Signal", DEFAULT_RECIPIENT, "HEL", "hello", "IFURI-229", "TOM", "Tom",
    ))
    monkeypatch.setattr(goal, "_capture_quad", lambda *a, **k: ("/tmp/q.png", None))
    monkeypatch.setattr(goal.time, "sleep", lambda *_a, **_k: None)

    goal._send_signal_gui_scripted(DEFAULT_RECIPIENT, "HEL", ticket="IFURI-229", node="lenovo")
    assert any("type-verified" in u for u in order)


def test_signal_payload_from_ticket():
    rec, msg = payload_from_ticket({
        "id": "IFURI-229",
        "name": "wyślij wiadomość do mateusza na signal",
        "description": "BLOCKED: watchdog stale",
    })
    assert rec == DEFAULT_RECIPIENT
    assert "IFURI-229" in msg
    assert msg.startswith("E2E")


def test_llm_loop_result_requires_message_not_recipient(monkeypatch):
    monkeypatch.setattr(goal, "_llm_model", lambda: "test-model")
    monkeypatch.setattr(goal, "_kvm_meta", lambda node, mode="": {"source": "test", "mode": mode, "node": node})
    monkeypatch.setattr(goal, "_preflight_signal_compose", lambda node, recipient, ticket=None: {
        "ok": True, "timeline": [], "composer": {"center": [1, 2], "ok": True},
    })
    monkeypatch.setattr(goal, "_present_on_screen_tesseract", lambda node, text, **kw: text == "hello test")
    monkeypatch.setattr(goal, "_node_run", lambda node, uri, payload=None, timeout=15.0: {"ok": True, "present": False, "via": "tesseract"})

    class FakeLoop:
        def __init__(self, **kwargs):
            pass

        def run(self, goal_text, initial_plan=None):
            return {"ok": True, "status": "done", "plan_used": "llm-runtime-loop", "timeline": []}

    monkeypatch.setattr("urirun_runtime.llm_runtime_loop.LlmRuntimeLoop", FakeLoop)
    r = goal._send_via_llm_runtime_loop(DEFAULT_RECIPIENT, "hello test", ticket="IFURI-236", node="lenovo")
    assert r["ok"] is True
    assert r["verified"] is True

    monkeypatch.setattr(goal, "_present_on_screen_tesseract", lambda node, text, **kw: text == DEFAULT_RECIPIENT)
    r = goal._send_via_llm_runtime_loop(DEFAULT_RECIPIENT, "hello test", ticket="IFURI-236", node="lenovo")
    assert r["ok"] is False
    assert r["verified"] is False
    assert r["status"] == "unverified"


def test_preflight_signal_compose_orders_recipient_before_composer(monkeypatch):
    order: list[str] = []

    monkeypatch.setattr(goal, "_ensure_gui_ready_for_signal",
                        lambda node, ticket=None: {"ok": True, "observe": {"signal_visible_initial": True}})
    monkeypatch.setattr(goal, "_open_signal_chat", lambda node, recipient, **kw: (
        order.append("click-recipient") or [{"step": "click-recipient"}]
    ))
    monkeypatch.setattr(goal, "_focus_signal_composer", lambda node, **kw: (
        order.append("focus-composer") or {"ok": True, "center": [10, 20], "timeline": []}
    ))
    monkeypatch.setattr(goal, "_node_run", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(goal.time, "sleep", lambda *_a, **_k: None)

    r = goal._preflight_signal_compose("lenovo", DEFAULT_RECIPIENT, ticket="IFURI-237")
    assert r["ok"] is True
    assert order == ["click-recipient", "focus-composer"]


def test_focus_signal_composer_uses_type_verified(monkeypatch):
    calls: list[tuple[str, dict | None]] = []

    def fake_node(node, uri, payload=None, timeout=15.0):
        calls.append((uri, payload))
        if "ui/query/locate" in uri:
            return {"matches": [{"text": "Message", "center": {"x": 50, "y": 400}}]}
        if "type-verified" in uri:
            return {"ok": True, "verified": True}
        return {"ok": True}

    monkeypatch.setattr(goal, "_node_run", fake_node)
    monkeypatch.setattr(goal, "_kvm_input", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(goal.time, "sleep", lambda *_a, **_k: None)

    r = goal._focus_signal_composer("lenovo", verify_probe="TST")
    assert r["ok"] is True
    assert r["center"] == [50, 428]
    assert any("type-verified" in u for u, _ in calls)


def test_llm_loop_preflight_type_verified_short_circuit(monkeypatch):
    monkeypatch.setattr(goal, "_preflight_signal_compose", lambda node, recipient, ticket=None: {
        "ok": True,
        "timeline": [{"step": "focus-composer"}],
        "composer": {"center": [120, 340], "ok": True, "label": "Message"},
    })
    monkeypatch.setattr(goal, "_send_signal_type_verified", lambda node, message, center, **kw: {
        "ok": True, "verified": True, "sent": {"ok": True, "verified": True},
    })
    monkeypatch.setattr(goal, "_llm_model", lambda: "test-model")
    monkeypatch.setattr(goal, "_kvm_meta", lambda node, mode="": {"source": "test", "mode": mode, "node": node})

    class FakeLoop:
        def __init__(self, **kwargs):
            raise AssertionError("LLM loop should not run when preflight type-verified succeeds")

    monkeypatch.setattr("urirun_runtime.llm_runtime_loop.LlmRuntimeLoop", FakeLoop)
    r = goal._send_via_llm_runtime_loop(DEFAULT_RECIPIENT, "hello test", ticket="IFURI-237", node="lenovo")
    assert r["ok"] is True
    assert r["plan_used"] == "preflight-type-verified"


def test_llm_loop_seeds_type_verified_plan_when_quick_send_fails(monkeypatch):
    captured: dict = {}

    monkeypatch.setattr(goal, "_preflight_signal_compose", lambda node, recipient, ticket=None: {
        "ok": True,
        "timeline": [],
        "composer": {"center": [120, 340], "ok": True, "label": "Message"},
    })
    monkeypatch.setattr(goal, "_send_signal_type_verified", lambda node, message, center, **kw: {
        "ok": False, "verified": False, "sent": {"ok": False},
    })
    monkeypatch.setattr(goal, "_llm_model", lambda: "test-model")
    monkeypatch.setattr(goal, "_kvm_meta", lambda node, mode="": {"source": "test", "mode": mode, "node": node})
    monkeypatch.setattr(goal, "_present_on_screen_tesseract", lambda node, text, **kw: True)

    class FakeLoop:
        def __init__(self, **kwargs):
            captured["init"] = kwargs

        def run(self, goal_text, initial_plan=None):
            captured["initial_plan"] = initial_plan
            return {"ok": True, "status": "done", "plan_used": "llm-runtime-loop", "timeline": []}

    monkeypatch.setattr("urirun_runtime.llm_runtime_loop.LlmRuntimeLoop", FakeLoop)
    goal._send_via_llm_runtime_loop(DEFAULT_RECIPIENT, "hello test", ticket="IFURI-237", node="lenovo")
    plan = captured.get("initial_plan") or []
    assert plan and plan[0]["uri"].endswith("/ui/command/type-verified")
    assert plan[0]["payload"]["x"] == 120
    assert plan[0]["payload"]["text"] == "hello test"


def test_llm_loop_runs_preflight_before_loop(monkeypatch):
    seen: list[str] = []

    monkeypatch.setattr(goal, "_preflight_signal_compose", lambda node, recipient, ticket=None: (
        seen.append("preflight") or {"ok": True, "timeline": [], "composer": {"center": [1, 2], "ok": True}}
    ))
    monkeypatch.setattr(goal, "_send_signal_type_verified", lambda *a, **k: {"ok": False, "verified": False})
    monkeypatch.setattr(goal, "_llm_model", lambda: "test-model")
    monkeypatch.setattr(goal, "_kvm_meta", lambda node, mode="": {"source": "test", "mode": mode, "node": node})
    monkeypatch.setattr(goal, "_present_on_screen_tesseract", lambda node, text, **kw: True)

    class FakeLoop:
        def __init__(self, **kwargs):
            pass

        def run(self, goal_text, initial_plan=None):
            seen.append("loop")
            return {"ok": True, "status": "done", "plan_used": "llm-runtime-loop", "timeline": []}

    monkeypatch.setattr("urirun_runtime.llm_runtime_loop.LlmRuntimeLoop", FakeLoop)
    r = goal._send_via_llm_runtime_loop(DEFAULT_RECIPIENT, "hello test", ticket="IFURI-237", node="lenovo")
    assert seen == ["preflight", "loop"]
    assert r["ok"] is True
    assert "preflight" in r
