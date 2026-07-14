# Author: Tom Sapletta · Part of the ifURI solution.
"""Regression: ui/query/locate must use ``query`` payload key (not ``text``)."""
from __future__ import annotations

from urirun_connector_work import goal
from urirun_connector_work.signal_kvm import KVM_URI_HOST


def test_click_label_passes_query_to_locate(monkeypatch):
    seen: list[dict] = []

    def fake_node(node, uri, payload=None, timeout=15.0):
        if "ui/query/locate" in uri:
            seen.append(payload or {})
            return {
                "matches": [{"text": "Tom Sapletta", "center": [100, 200]}],
            }
        return {"ok": True}

    monkeypatch.setattr(goal, "_node_run", fake_node)
    goal._click_label("lenovo", "Tom Sapletta", guarded=False)
    assert seen and seen[0].get("query") == "Tom Sapletta"
    assert "text" not in seen[0]


def test_locate_payload_helper():
    p = goal._locate_payload("hello", min_conf=50)
    assert p == {"query": "hello", "min_conf": 50}
