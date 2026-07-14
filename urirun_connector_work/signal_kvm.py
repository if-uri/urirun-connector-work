# Author: Tom Sapletta · Part of the ifURI solution.
"""Signal/KVM — single source of truth for defaults, URI host segment, ticket→payload parsing.

Operator overrides via ``urirun/.env`` (see SIGNAL_* / URIRUN_KVM_URI_HOST / URIRUN_TWIN_HUMAN_ACTOR).
"""
from __future__ import annotations

import os
import re
from typing import Any

DEFAULT_NODE = (os.environ.get("URIRUN_SIGNAL_KVM_NODE") or "lenovo").strip().lower()
DEFAULT_RECIPIENT = (os.environ.get("SIGNAL_DEFAULT_RECIPIENT") or "Mateusz").strip()
SIGNAL_APP_ID = (os.environ.get("SIGNAL_APP_ID") or "org.signal.Signal").strip()
TWIN_HUMAN_ACTOR = (os.environ.get("URIRUN_TWIN_HUMAN_ACTOR") or "twin-human:tom").strip()
KVM_URI_HOST = (os.environ.get("URIRUN_KVM_URI_HOST") or "host").strip()

KVM_DELEGATION_LABELS = frozenset({"kvm", "lenovo", "signal-gui", "signal", "e2e"})
KNOWN_NODES = ("lenovo", "nvidia")
SIGNAL_VISIBILITY_HINTS = ("Signal", "Message", "Wiadomość", "Search")
# Wayland: atspi widzi drzewo a11y poza aktywnym oknem — weryfikuj OCR na pikselach.
SIGNAL_TESSERACT_HINTS = ("Chats", "Search", "Signal", "Stories")
COMPOSER_LABELS = ("Message", "Wiadomość", "Send a message", "Wyślij wiadomość")
KVM_HUMAN_KEYWORDS = ("kvm", "lenovo", "signal-gui", "desktop gui", "kvm://", "node:lenovo")

# URI procesy KVM na węźle urirun: prefiks hosta (segment po kvm://) z env, nie funkcja-opakowanie.
# Transport: URIRUN_KVM_URI_HOST=host  →  pełny proces np. kvm://host/ui/query/locate
#
# Wysyłka Signal: domyślnie scripted (szybka, bez triple-LLM). Pełny łańcuch
# executor→validator→twin→teacher: ustaw SIGNAL_KVM_PREP=1 przed send_via_kvm.


def registry_kvm_hint() -> str:
    return (
        f"kvm://{KVM_URI_HOST}/*, app://host/desktop/*, ui/query/*, "
        "router://host/plan/query/diagnose, inquiry://, reflection://"
    )


def registry_kvm_examples() -> str:
    p = KVM_URI_HOST
    return "\n".join(
        f"- kvm://{p}/{seg}"
        for seg in (
            "window/command/focus : activate window by title",
            "ui/query/locate : OCR locate → bbox/center",
            "ui/query/verify : OCR/AT-SPI postcondition",
            "screen/query/capture : screenshot (cx/cy/zoom/crop)",
            "input/command/type : type text (probe 3 chars first)",
            "input/command/click : click at x,y",
            "input/command/key : keys (Return, ctrl+a, escape)",
            "task/command/run : bounded input sequence",
        )
    )


def default_message(ticket_id: str) -> str:
    tid = ticket_id or "ticket"
    tpl = os.environ.get("SIGNAL_E2E_MESSAGE_TEMPLATE") or "E2E {ticket_id} ok — if-uri autonomy"
    return tpl.format(ticket_id=tid)[:500]


def recipient_from_ticket(t: dict[str, Any]) -> str:
    inputs = t.get("inputs") or {}
    if isinstance(inputs, dict):
        for key in ("recipient", "to", "contact"):
            val = inputs.get(key)
            if val:
                return str(val).strip()
    name = t.get("name") or ""
    desc = (t.get("description") or "").split("BLOCKED:")[0]
    for blob in (name, desc):
        if not blob:
            continue
        low = blob.lower()
        if DEFAULT_RECIPIENT.lower() in low:
            return DEFAULT_RECIPIENT
        m = re.search(r"(?:do|to|→)\s+([A-Za-zÀ-ž][\w.-]*)", blob, re.I)
        if m:
            return m.group(1).strip()
    return DEFAULT_RECIPIENT


def message_from_ticket(t: dict[str, Any]) -> str:
    inputs = t.get("inputs") or {}
    if isinstance(inputs, dict):
        for key in ("message", "text", "body"):
            val = inputs.get(key)
            if val and len(str(val).strip()) >= 4:
                return str(val).strip()[:500]
    desc = (t.get("description") or "")
    clean = desc.split("BLOCKED:")[0].strip()
    if len(clean) >= 8:
        return clean[:500]
    return default_message(str(t.get("id") or "ticket"))


def payload_from_ticket(t: dict[str, Any]) -> tuple[str, str]:
    return recipient_from_ticket(t), message_from_ticket(t)


def resolve_node(t: dict[str, Any], *, default: str = DEFAULT_NODE) -> str:
    name = (t.get("name") or "").lower()
    labels = [str(x).lower() for x in (t.get("labels") or [])]
    blob = f"{name} {' '.join(labels)}"
    return next((n for n in KNOWN_NODES if n in blob or n in labels), default)


def recipient_search_prefix(recipient: str) -> str:
    r = (recipient or DEFAULT_RECIPIENT).strip()
    return (r[:4] if len(r) >= 4 else r[:3]) or "TST"


def is_signal_ticket(t: dict[str, Any]) -> bool:
    name = (t.get("name") or "").lower()
    labels = [str(x).lower() for x in (t.get("labels") or [])]
    rec = DEFAULT_RECIPIENT.lower()
    return "signal" in name or rec in name or "signal-gui" in labels or "mateusz" in name
