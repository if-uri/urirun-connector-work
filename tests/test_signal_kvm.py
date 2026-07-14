# Author: Tom Sapletta · Part of the ifURI solution.
"""signal_kvm — shared constants and ticket parsing (no hardcoded ticket ids/recipients)."""
from __future__ import annotations

import os

from urirun_connector_work.signal_kvm import (
    DEFAULT_RECIPIENT,
    payload_from_ticket,
    recipient_from_ticket,
    message_from_ticket,
    KVM_URI_HOST,
)


def test_payload_from_ticket_uses_id_not_hardcoded_text():
    rec, msg = payload_from_ticket({
        "id": "IFURI-999",
        "name": "wyślij do mateusza na signal",
        "description": "BLOCKED: stale",
    })
    assert rec == DEFAULT_RECIPIENT
    assert "IFURI-999" in msg
    assert "IFURI-226" not in msg


def test_payload_prefers_ticket_inputs():
    rec, msg = payload_from_ticket({
        "id": "T-1",
        "inputs": {"recipient": "Anna", "message": "Cześć z testu"},
    })
    assert rec == "Anna"
    assert msg == "Cześć z testu"


def test_kvm_uri_host_from_env():
    assert KVM_URI_HOST == os.environ.get("URIRUN_KVM_URI_HOST", "host")


def test_recipient_from_description_pattern():
    assert recipient_from_ticket({"name": "wyślij do Kasia na signal"}) == "Kasia"
