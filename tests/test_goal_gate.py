"""HARD goal-freeze + waiting_human_clean outcome (SIGNAL-E2E-001 phase)."""
from __future__ import annotations

import pytest

from urirun_connector_work import goal, goal_gate as gg


@pytest.fixture
def _frozen_goal(tmp_path, monkeypatch):
    monkeypatch.setattr(goal, "_GOAL_FILE", tmp_path / "current-goal.json")
    monkeypatch.setenv("CURRENT_GOAL", "signal.message.send")


# ── HARD freeze: no new code tickets while a delivery goal runs ────────────────

def test_refactor_ticket_denied_during_goal_freeze(_frozen_goal):
    d = gg.allows_new_code_ticket({"name": "refactor scheduler", "labels": ["refactor"]})
    assert d["allowed"] is False and "no new code tickets" in d["reason"]


def test_self_evolution_and_diagnosis_denied(_frozen_goal):
    assert gg.allows_new_code_ticket({"name": "[EWOLUCJA] provenance", "labels": ["evolution"]})["allowed"] is False
    assert gg.allows_new_code_ticket({"name": "DIAGNOZA: loop", "labels": ["diagnosis"]})["allowed"] is False


def test_direct_goal_blocker_is_allowed(_frozen_goal):
    d = gg.allows_new_code_ticket({"name": "signal account status", "labels": ["goal:signal.message.send"]})
    assert d["allowed"] is True and "goal blocker" in d["reason"]


def test_no_goal_means_no_freeze(tmp_path, monkeypatch):
    monkeypatch.setattr(goal, "_GOAL_FILE", tmp_path / "g.json")
    monkeypatch.delenv("CURRENT_GOAL", raising=False)
    assert gg.allows_new_code_ticket({"name": "refactor", "labels": ["refactor"]})["allowed"] is True
    assert gg.hard_freeze() is False


def test_uri_whitelist_only_signal_and_approval(_frozen_goal):
    assert gg.uri_allowed("signal://host/message/command/send") is True
    assert gg.uri_allowed("approval://adam/ticket/command/approve") is True
    assert gg.uri_allowed("connector://host/gen/command/create") is False  # no new connector


# ── waiting_human is NOT failure ──────────────────────────────────────────────

def test_clean_human_wait_is_a_valid_outcome():
    clean = {"decision": "waiting_human", "missing_only": "signal account QR link",
             "next_uri_after_human": "signal://host/account/query/status", "no_new_code_tickets_created": True}
    assert gg.classify_outcome(clean) == "waiting_human_clean"
    assert gg.is_valid_outcome(clean) is True          # counts as success, not blocked/churn


def test_dirty_human_wait_is_not_clean():
    # missing the resume URI / the no-code-churn guarantee → plain waiting_human (needs attention)
    assert gg.classify_outcome({"decision": "waiting_human", "missing_only": "x"}) == "waiting_human"


def test_done_and_failed_classify():
    assert gg.classify_outcome({"decision": "done", "message_sent": True}) == "done"
    assert gg.is_valid_outcome({"decision": "failed"}) is False
