"""3-poziomowy łańcuch eskalacji w send_via_kvm: executor -> (validator FAIL) -> executor_twin ->
(validator FAIL) -> teacher proponuje konkretną poprawę. Testuje _validator_judge_attempt i
_teacher_propose_improvement w izolacji (bez realnego KVM/LLM).
"""
from __future__ import annotations

from urirun_connector_work import goal
from urirun_connector_work.signal_kvm import KVM_URI_HOST


def test_validator_judge_parses_pass(monkeypatch):
    monkeypatch.setattr(goal, "_llm_completion", lambda *a, **k: {
        "ok": True, "model": "validator-x", "content": '{"pass": true, "reason": "wyglada dobrze"}'})
    j = goal._validator_judge_attempt("executor-x", [{"uri": "kvm://x", "payload": {}, "result": "ok"}],
                                      verified=True, node="lenovo", ticket=None)
    assert j["pass"] is True and j["reason"] == "wyglada dobrze"


def test_validator_judge_falls_back_to_verified_on_parse_error(monkeypatch):
    monkeypatch.setattr(goal, "_llm_completion", lambda *a, **k: {
        "ok": True, "model": "validator-x", "content": "not json at all"})
    j = goal._validator_judge_attempt("executor-x", [], verified=False, node="lenovo", ticket=None)
    assert j["pass"] is False  # fallback = verified flag, nie ślepe "True"


def test_teacher_proposes_concrete_improvement(monkeypatch):
    monkeypatch.setattr(goal, "_llm_teacher_model", lambda: "teacher-x")
    monkeypatch.setattr(goal, "_capture_lowres", lambda *a, **k: (None, None))
    monkeypatch.setattr(goal, "_capture_quad", lambda *a, **k: (None, None))
    monkeypatch.setattr(goal, "_llm_completion", lambda *a, **k: {
        "ok": True, "model": "teacher-x",
        "content": '{"diagnosis": "zle lokalizowanie pola", "suggested_uri": "kvm://' + KVM_URI_HOST + '/ui/query/locate", '
                   '"suggested_payload": {"text": "Message"}, "prompt_improvement": "uzyj atspi zamiast OCR", '
                   '"needs_new_capability": null}'})
    timeline = [{"model": "executor-x", "uri": "kvm://x/input/command/type", "payload": {"text": "TST"},
                "result": "not-landed"}]
    improvement = goal._teacher_propose_improvement(timeline, "lenovo", None, "wyslij wiadomosc testowa")
    assert improvement["suggested_uri"] == f"kvm://{KVM_URI_HOST}/ui/query/locate"
    assert improvement["model"] == "teacher-x"
    assert improvement["needs_new_capability"] is None


def test_decide_next_accepts_model_override(monkeypatch):
    seen = {}

    def fake_completion(**kw):
        seen["model"] = kw["model"]
        class R:
            choices = [type("C", (), {"message": type("M", (), {"content": '{"uri": "done"}'})()})]
        return R()

    monkeypatch.setattr(goal._litellm, "completion", fake_completion)
    goal._llm_decide_next("state", "know", "lenovo", model="twin-model-x")
    assert seen["model"] == "twin-model-x"


def test_send_via_kvm_full_escalation_to_teacher(monkeypatch, tmp_path):
    """End-to-end (zamockowane sieć/KVM/LLM): executor i twin oboje nie przechodzą walidacji ->
    teacher zostaje wywołany i jego propozycja trafia do wyniku + do inquiry:// case.
    Zero realnych połączeń (node/LLM), zero dotknięcia prawdziwego Signal/Mateusza.
    """
    def fake_prepare(**k):
        prep = {
            "ok": True,
            "plan": [{"id": "focus", "uri": f"kvm://{KVM_URI_HOST}/window/command/focus", "payload": {}}],
            "executor_model": "exec-x",
            "executor_twin_model": "twin-x",
            "failover_triggered": True,
            "teacher_escalated": True,
            "teacher_improvement": {
                "diagnosis": "zle pole",
                "suggested_uri": f"kvm://{KVM_URI_HOST}/ui/query/locate",
                "suggested_payload": {},
                "prompt_improvement": "inny prompt",
                "needs_new_capability": None,
            },
        }
        goal._save_executor_outcome(prep["executor_model"], {
            "success": False,
            "verified": False,
            "validator_pass": False,
            "time": 0,
        })
        goal._save_executor_outcome(prep["executor_twin_model"], {
            "success": False,
            "verified": False,
            "validator_pass": False,
            "time": 0,
        })
        return prep
    monkeypatch.setattr(goal, "prepare_and_validate_for_signal_kvm", fake_prepare)
    monkeypatch.setattr(goal, "_llm_model", lambda: "exec-x")
    monkeypatch.setattr(goal, "_llm_executor_twin_model", lambda: "twin-x")
    monkeypatch.setattr(goal, "_llm_teacher_model", lambda: "teacher-x")
    monkeypatch.setattr(goal, "_speak_on_node", lambda *a, **k: None)
    monkeypatch.setattr(goal, "_capture_lowres", lambda *a, **k: ("/tmp/fake-lowres.png", None))
    monkeypatch.setattr(goal, "_capture_quad", lambda *a, **k: ("/tmp/fake-quad.png", None))
    monkeypatch.setattr(goal, "_get_executor_history_path", lambda: tmp_path / "executor_twin_history.jsonl")
    monkeypatch.setenv("SIGNAL_KVM_PREP", "1")
    monkeypatch.setattr(goal, "_ensure_gui_ready_for_signal", lambda *a, **k: {"ok": True, "observe": {}})
    node_calls: list[str] = []

    def fake_node_run(node, uri, payload=None, timeout=15.0):
        node_calls.append(uri)
        if "ui/query/verify" in uri:
            return {"present": False}  # nigdy nie zweryfikowane -> wymusza pełną eskalację
        if "ui/query/locate" in uri:
            return {"count": 0}
        if "router://" in uri:
            return {"ok": True}
        return {"ok": True}

    monkeypatch.setattr(goal, "_node_run", fake_node_run)

    micro_calls = {"n": 0}

    def fake_decide_next(state, know, node, max_tokens=200, model=None):
        micro_calls["n"] += 1
        if micro_calls["n"] > 2:  # po 2 mikrokrokach kończ próbę (szybciej niż pełny budget)
            return {"uri": "done"}
        return {"uri": f"kvm://{node}/input/command/type", "payload": {"text": "TST"}, "reason": "probe"}

    monkeypatch.setattr(goal, "_llm_decide_next", fake_decide_next)

    # Validator zawsze FAIL, teacher zwraca konkretną propozycję poprawy
    def fake_llm_completion(model, prompt, **kw):
        if model == "teacher-x":
            return {"ok": True, "model": model,
                    "content": '{"diagnosis": "zle pole", "suggested_uri": "kvm://' + KVM_URI_HOST + '/ui/query/locate", '
                               '"suggested_payload": {}, "prompt_improvement": "inny prompt", '
                               '"needs_new_capability": null}'}
        return {"ok": True, "model": model, "content": '{"pass": false, "reason": "nie widac tekstu w czacie"}'}

    monkeypatch.setattr(goal, "_llm_completion", fake_llm_completion)

    result = goal.send_via_kvm(recipient="TestOnlyLabel", text="tresc testowa", ticket=None, node="lenovo")

    assert result["failover_triggered"] is True
    assert result["teacher_escalated"] is True
    assert result["teacher_improvement"]["suggested_uri"] == f"kvm://{KVM_URI_HOST}/ui/query/locate"
    assert result["executor_model"] == "exec-x" and result["executor_twin_model"] == "twin-x"
    assert result["ok"] is False  # verify nigdy present=True -> zadanie poprawnie zgloszone jako niewykonane
    # oba modele zapisaly swoj outcome do historii porownan
    hist_lines = (tmp_path / "executor_twin_history.jsonl").read_text().strip().splitlines()
    assert len(hist_lines) == 2
    # inquiry:// case dostal propozycje teachera jako artefakt, nie tylko log porazki
    assert any("inquiry://" in c for c in node_calls)
