# Author: Tom Sapletta · Part of the ifURI solution.
"""SIGNAL-E2E-001 — GOAL-DELIVERY MODE: mierz sukces REALNĄ akcją end-to-end, nie liczbą ticketów kodu.

Sedno (per Tom): system robi to co mu wychodzi (wykryj→ticket→kod→test→zamknij), a prawdziwy cel
to „wyślij wiadomość na Signal". Ten moduł przełącza scheduler z code-improvement na goal-delivery:

  * CURRENT_GOAL ustawiony → claim-next bierze TYLKO tickety związane z celem (goal_relevant) +
    bezpośrednie blockery; unrelated refaktory/self-evolution ZAMROŻONE (freeze).
  * Brak realnego wejścia (link/recipient/approval) → koniec jako `waiting_human` z JEDNYM inputem,
    BEZ tworzenia nowych ticketów kodowych. To też sukces autonomii (wskazał jedyny brak).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import socket
from pathlib import Path
from typing import Any

from .signal_kvm import (
    COMPOSER_LABELS,
    DEFAULT_NODE,
    DEFAULT_RECIPIENT,
    KVM_URI_HOST,
    SIGNAL_APP_ID,
    SIGNAL_TESSERACT_HINTS,
    SIGNAL_VISIBILITY_HINTS,
    recipient_search_prefix,
    registry_kvm_examples,
    registry_kvm_hint,
)

_GOAL_FILE = Path(os.environ.get("URIRUN_GOAL_FILE") or "~/.urirun/host-dashboard/current-goal.json").expanduser()


def current_goal() -> dict | None:
    """Aktywny cel (env CURRENT_GOAL wygrywa, potem plik). None = code-improvement mode."""
    env = os.environ.get("CURRENT_GOAL")
    if env:
        return {"goal": env, "freeze_self_evolution": True, "allow_only_goal_blockers": True}
    try:
        return json.loads(_GOAL_FILE.read_text()) if _GOAL_FILE.is_file() else None
    except Exception:  # noqa: BLE001
        return None


def set_goal(goal: str, *, domain: str = "", recipient: str = "", freeze: bool = True) -> dict:
    data = {"goal": goal, "domain": domain or goal.split(".")[0], "recipient": recipient,
            "freeze_self_evolution": freeze, "allow_only_goal_blockers": True}
    try:
        _GOAL_FILE.parent.mkdir(parents=True, exist_ok=True)
        _GOAL_FILE.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return data


def clear_goal() -> dict:
    try:
        _GOAL_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    return {"cleared": True}


def freeze_self_evolution() -> bool:
    """Czy zamrozić generację refaktor/self-evolution/backlog (bo trwa dostarczanie celu)."""
    if str(os.environ.get("FREEZE_SELF_EVOLUTION", "")).strip() in ("1", "true", "yes"):
        return True
    g = current_goal()
    return bool(g and g.get("freeze_self_evolution"))


# tickety, które są zawsze dozwolone w goal-mode (dostarczają wartość, nie mielą kodu)
_ALWAYS = ("signal", "email", "linkedin", "pypi", "publish", "deploy")


def goal_relevant(ticket: dict, goal: dict | None = None) -> bool:
    """Czy ticket należy do bieżącego celu (goal-mode filtruje resztę). Brak celu → wszystko."""
    goal = goal or current_goal()
    if not goal:
        return True
    domain = (goal.get("domain") or goal.get("goal", "").split(".")[0]).lower()
    if not domain:
        return True
    labels = [str(x).lower() for x in (ticket.get("labels") or [])]
    blob = f"{ticket.get('name', '')} {ticket.get('description', '')}".lower()
    # związane z domeną celu (np. signal) LUB grupą rollout (group:signal-rollout)
    return domain in blob or any(domain in l for l in labels)


def goal_filter(tickets: list[dict]) -> tuple[list[dict], list[dict]]:
    """Rozdziel na (goal-related, frozen-unrelated) wg bieżącego celu."""
    goal = current_goal()
    if not goal:
        return tickets, []
    rel = [t for t in tickets if goal_relevant(t, goal)]
    frozen = [t for t in tickets if not goal_relevant(t, goal)]
    return rel, frozen


_NODE_URLS = {
    "lenovo": os.environ.get("URIRUN_LENOVO_URL", "http://192.168.188.201:8765"),
    "nvidia": os.environ.get("URIRUN_NVIDIA_URL", "http://localhost:8765"),  # or host direct
    "host": os.environ.get("URIRUN_HOST_URL", "http://localhost:8765"),
}

def _get_node_url(node: str = "lenovo") -> str:
    node = (node or "lenovo").lower()
    return _NODE_URLS.get(node, _NODE_URLS["lenovo"])

def _get_node_mode(node: str = "lenovo") -> str:
    try:
        from urirun.host import ticket_meta
        return ticket_meta.get_digital_person_mode(f"{node}-node")
    except Exception:
        return "real"


def _generate_wav(text: str, lang: str = "pl", out_path: str = None) -> str:
    """Generate speech WAV on the controller (best quality). Prefers Piper."""
    if out_path is None:
        out_path = f"/tmp/tts_{int(time.time()*1000)}.wav"
    piper = os.environ.get("PIPER_BIN", "/home/tom/.local/bin/piper")
    vdir = os.environ.get("PIPER_VOICES", os.path.expanduser("~/.local/share/piper/voices"))
    model = None
    if os.path.isfile(piper) and os.path.isdir(vdir):
        pref = "pl" if lang.startswith("pl") else "en"
        for f in os.listdir(vdir):
            if f.endswith(".onnx") and pref in f.lower():
                model = os.path.join(vdir, f)
                break
    if model:
        try:
            subprocess.check_call([piper, "--model", model, "--output_file", out_path],
                                  input=text.encode(), timeout=30)
            return out_path
        except Exception:
            pass
    # Fallback espeak-ng WAV
    v = "pl" if lang.startswith("pl") else "en"
    subprocess.check_call(["espeak-ng", "-v", v, "-w", out_path, text], timeout=15)
    return out_path


def _play_audio_remote(wav_path: str, controller_ip: str = None, port: int = 18765, node: str = "lenovo") -> bool:
    """Serve the WAV briefly over HTTP and instruct node to fetch + play it.
    This is the 'send file' delivery option.
    """
    import http.server, socketserver, threading, os
    if not controller_ip:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            controller_ip = s.getsockname()[0]
            s.close()
        except Exception:
            controller_ip = os.environ.get("CONTROLLER_IP", "192.168.188.50")
    bn = os.path.basename(wav_path)
    dn = os.path.dirname(wav_path) or "/tmp"
    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k): super().__init__(*a, directory=dn, **k)
    httpd = socketserver.TCPServer(("", port), H)
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    time.sleep(0.4)
    url = f"http://{controller_ip}:{port}/{bn}"
    cmd = f'curl -s {url} | aplay -q 2>/dev/null || paplay {url} 2>/dev/null || echo no-audio-player'
    try:
        return _run_detached_shell_on_node(node, cmd, timeout=8)
    finally:
        httpd.shutdown()


def _play_stream_on_lenovo(text: str, lang: str = "pl", controller_ip: str = None, port: int = 18766, node: str = "lenovo") -> bool:
    """Realtime raw audio stream to Lenovo (no full file saved on node).
    Controller generates raw audio and listens with nc.
    Lenovo connects with nc | aplay.
    This is the 'stream audio' option.
    """
    import subprocess, threading, socket, time as _t
    if not controller_ip:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            controller_ip = s.getsockname()[0]
            s.close()
        except Exception:
            controller_ip = os.environ.get("CONTROLLER_IP", "192.168.188.50")

    # Choose generator for raw audio (22.05kHz S16_LE is common for these TTS)
    piper = os.environ.get("PIPER_BIN", "/home/tom/.local/bin/piper")
    vdir = os.environ.get("PIPER_VOICES", os.path.expanduser("~/.local/share/piper/voices"))
    model = None
    if os.path.isfile(piper) and os.path.isdir(vdir):
        pref = "pl" if lang.startswith("pl") else "en"
        for f in os.listdir(vdir):
            if f.endswith(".onnx") and pref in f.lower():
                model = os.path.join(vdir, f)
                break

    def audio_generator():
        if model:
            p = subprocess.Popen(
                [piper, "--model", model, "--output-raw"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE
            )
            p.stdin.write(text.encode())
            p.stdin.close()
            return p.stdout
        else:
            v = "pl" if lang.startswith("pl") else "en"
            p = subprocess.Popen(
                ["espeak-ng", "-v", v, "--stdout"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE
            )
            p.stdin.write(text.encode())
            p.stdin.close()
            return p.stdout

    # Start listener on controller
    listener = subprocess.Popen(
        ["nc", "-l", "-p", str(port)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE
    )

    def feeder():
        raw = audio_generator()
        try:
            while True:
                chunk = raw.read(4096)
                if not chunk:
                    break
                listener.stdin.write(chunk)
                listener.stdin.flush()
        finally:
            try:
                listener.stdin.close()
            except:
                pass

    feeder_thread = threading.Thread(target=feeder, daemon=True)
    feeder_thread.start()

    time.sleep(0.5)  # give listener time

    # Instruct node to connect and play (detached shell — never type into foreground GUI)
    cmd = f'nc {controller_ip} {port} | aplay -r 22050 -f S16_LE -t raw -q 2>/dev/null || echo stream-failed'
    try:
        _run_detached_shell_on_node(node, cmd, timeout=8)
        time.sleep(4.0)
    finally:
        try:
            listener.terminate()
        except:
            pass

    return True


def detect_lenovo_audio_capabilities(node: str = "lenovo") -> dict:
    """Probe node capabilities for choosing TTS delivery method.
    Supports deciding between local TTS vs sending/streaming audio file from controller.
    """
    caps = {"aplay": True, "local_tts_script": False, "nc": True, "controller_piper": False}
    try:
        _run_detached_shell_on_node(node, "ls -l /tmp/speak-pl 2>/dev/null && echo HAS_LOCAL_SCRIPT", timeout=5)
        caps["local_tts_script"] = True
    except Exception:
        pass
    piper = os.environ.get("PIPER_BIN", "/home/tom/.local/bin/piper")
    if os.path.exists(piper):
        caps["controller_piper"] = True
    return caps


def _speak_on_node(node: str, text: str, lang: str = "pl", method: str = "auto") -> None:
    """Announce on node using different delivery methods based on capabilities.
    Accepts flexible args for test mocks.
    """
    if not text:
        return
    # tolerate test mocks that may ignore node
    node = node or "lenovo"
    m = method.lower()
    if m == "local":
        lang_flag = "--lang en " if lang.lower().startswith("en") else ""
        cmd = f'/tmp/speak-pl {lang_flag}"{text}" &'
        try:
            _node_run(node, "app://host/desktop/command/launch", {
                "app": "gnome-terminal",
                "args": ["--", "bash", "-c", f"{cmd}; sleep 0.2; exit"],
                "settle": 0
            }, timeout=6)
            return
        except Exception:
            _run_detached_shell_on_node(node, cmd, timeout=6)
            return

    if m in ("remote_file", "file"):
        try:
            wav = _generate_wav(text, lang=lang)
            _play_audio_remote(wav, node=node)
            try: os.unlink(wav)
            except: pass
            return
        except Exception:
            if m != "auto":
                raise

    if m == "stream":
        try:
            _play_stream_on_lenovo(text, lang=lang, node=node)
            return
        except Exception:
            if m != "auto":
                raise

    if m in ("remote_file", "file", "stream", "auto"):
        # auto tries remote_file first (best quality), then stream, then local
        try:
            wav = _generate_wav(text, lang=lang)
            _play_audio_remote(wav, node=node)
            try: os.unlink(wav)
            except: pass
            return
        except Exception:
            try:
                _play_stream_on_lenovo(text, lang=lang, node=node)
                return
            except Exception:
                pass  # fall to local

    # default / fallback to local
    _speak_on_node(node or "lenovo", text, lang=lang, method="local")


def _run_detached_shell_on_node(node: str, shell_cmd: str, timeout: float = 10.0) -> bool:
    """Run shell on node WITHOUT kvm input/type into the foreground window (Wayland-safe).

    Never type curl/nc/aplay into whatever app happens to be focused — launch a detached shell instead."""
    node = node or "lenovo"
    try:
        r = _node_run(node, "app://host/desktop/command/launch", {
            "app": "gnome-terminal",
            "args": ["--", "bash", "-lc", f"{shell_cmd}; sleep 0.3; exit"],
            "settle": 0,
        }, timeout=timeout)
        if r.get("ok") is not False:
            return True
    except Exception:  # noqa: BLE001
        pass
    # Fallback: one-shot shell URI if the node exposes it (still not KVM keyboard typing).
    r = _node_run(node, f"kvm://{KVM_URI_HOST}/task/command/run", {"shell": shell_cmd, "detached": True}, timeout=timeout)
    return bool(r.get("ok"))


def _ensure_gui_ready_for_signal(node: str = "lenovo", ticket: str | None = None) -> dict:
    """OBSERVE first, then focus Signal — mandatory before any keyboard input on the node."""
    import time as _time
    node = (node or "lenovo").lower()
    observe: dict = {}
    cap, _ = _capture_lowres(node, 320)
    observe["capture"] = cap
    apps = _node_run(node, "app://host/desktop/query/list", {}, timeout=6)
    observe["apps"] = apps
    app_list = apps.get("apps") or (apps.get("value") or {}).get("apps") or []
    signal_running = any(
        isinstance(a, dict) and (
            "signal" in str(a.get("name", "")).lower()
            or "signal" in str(a.get("id", "")).lower()
        )
        for a in app_list
    )
    observe["signal_running"] = signal_running

    def _signal_visible() -> bool:
        if _signal_visible_on_screen(node):
            return True
        for hint in SIGNAL_VISIBILITY_HINTS:
            v = _node_run(node, f"kvm://{KVM_URI_HOST}/ui/query/verify", {"text": hint}, timeout=6)
            if _extract_present(v) and str(v.get("via") or "") == "tesseract":
                return True
        return False

    visible = _signal_visible()
    observe["signal_visible_initial"] = visible
    if not visible and signal_running:
        _node_run(node, f"kvm://{KVM_URI_HOST}/window/command/focus", {"title": "Signal"}, timeout=6)
        _time.sleep(0.8)
        visible = _signal_visible()
        observe["signal_focus_attempt"] = True
        observe["signal_visible_after_focus"] = visible
    if not visible and not signal_running:
        _node_run(node, "app://host/desktop/command/launch",
                  {"app": SIGNAL_APP_ID, "settle": 2}, timeout=12)
        _time.sleep(1.5)
        visible = _signal_visible()
        observe["signal_launched"] = True
        observe["signal_visible_after_launch"] = visible
    if not visible:
        _node_run(node, f"kvm://{KVM_URI_HOST}/ui/command/click-text", {"text": "Chats"}, timeout=8)
        _time.sleep(0.6)
        visible = _signal_visible()
    if not visible:
        for _ in range(6):
            _node_run(node, f"kvm://{KVM_URI_HOST}/input/command/key", {"keys": "alt+Tab"}, timeout=3)
            _time.sleep(0.5)
            visible = _signal_visible()
            if visible:
                observe["signal_visible_after_alttab"] = True
                break
    if ticket:
        _save_llm_trace(ticket, "observe-before-input", "system", "ensure_gui_ready_for_signal", "",
                        images=[cap] if cap else None, extra=observe)
    return {"ok": bool(visible), "focused": bool(visible), "observe": observe}


def _guarded_node_run(node: str, uri: str, payload: dict | None = None, timeout: float = 15.0,
                      *, ticket: str | None = None, require_signal: bool = True) -> dict:
    """Block KVM keyboard actions until screen was observed and Signal is foreground."""
    u = (uri or "").lower()
    p = payload or {}
    is_keyboard = (
        "input/command/type" in u
        or "input/command/key" in u
        or ("task/command/run" in u and p.get("steps"))
    )
    if require_signal and is_keyboard:
        ready = _ensure_gui_ready_for_signal(node, ticket=ticket)
        if not ready.get("ok"):
            return _kvm_fail(node, error="signal_not_focused",
                             reason="observe+focus Signal failed — refusing keyboard input into wrong window",
                             observe=ready.get("observe"))
    return _node_run(node, uri, payload, timeout)


def _node_run(node: str, uri: str, payload: dict | None = None, timeout: float = 15.0) -> dict:
    node = (node or "lenovo").lower()
    mode = _get_node_mode(node)
    if mode == "sim":
        # simulation
        u = uri.lower()
        if "app://host/desktop/query/list" in u:
            return {"apps": [{"id": "org.signal.Signal", "name": "Signal"}, {"id": "sim", "name": "Sim Desktop"}]}
        if "window/command/focus" in u or "ui/command/click-text" in u:
            return {"ok": True, "simulated": True, "via": f"digital-twin-{node}"}
        if "input/command/type" in u or "task/command/run" in u:
            return {"ok": True, "simulated": True, "via": f"digital-twin-{node}", "steps_executed": True}
        if "screen/query/capture" in u:
            return {"ok": True, "simulated": True, "path": f"/tmp/signal-sim-capture-{node}.png", "via": f"digital-twin-{node}"}
        if "ui/query/verify" in u:
            return {"ok": True, "present": True, "simulated": True, "via": f"digital-twin-{node}", "verified": True}
        return {"ok": True, "simulated": True, "via": f"digital-twin-{node}"}
    # real
    import json
    import urllib.request
    url = _get_node_url(node)
    body = json.dumps({"uri": uri, "payload": payload or {}}).encode()
    try:
        req = urllib.request.Request(f"{url}/run", data=body, headers={"Content-Type": "application/json"})
        res = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        v = res.get("result", res)
        return v.get("value", v) if isinstance(v, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


# --- LLM decisions for observe/act (uses LLM_MODEL from urirun/.env) ---------
try:
    from pathlib import Path as _P
    from urirun.host.env_loader import load_project_env as _load_project_env
except Exception:
    _load_project_env = None

try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None

if _load_project_env is not None:
    try:
        _load_project_env()
    except Exception:
        pass
elif load_dotenv is not None:
    try:
        env_path = os.environ.get("URIRUN_ENV")
        if env_path:
            load_dotenv(env_path)
        else:
            load_dotenv(_P(__file__).resolve().parents[3] / "urirun" / ".env")
    except Exception:
        pass

import litellm as _litellm


def _extract_present(v):
    """Robust parse of ui/query/verify responses (they vary: present / found / matches / tesseract details)."""
    if not isinstance(v, dict):
        return bool(v)
    if "present" in v:
        return bool(v.get("present"))
    if "found" in v:
        return bool(v.get("found"))
    if v.get("count"):
        return int(v.get("count", 0)) > 0
    if v.get("matches"):
        return len(v.get("matches") or []) > 0
    return bool(v.get("ok") and (v.get("text") or v.get("bbox") or v.get("center")))


def _locate_payload(query: str, *, min_conf: int = 40) -> dict:
    """kvm ui/query/locate expects ``query``, not ``text``."""
    return {"query": query, "min_conf": min_conf}


def _locate_match(node: str, query: str, *, timeout: float = 8, min_conf: int = 40) -> dict | None:
    """OCR locate with query filter — returns best match dict or None."""
    if not query:
        return None
    r = _node_run(node, f"kvm://{KVM_URI_HOST}/ui/query/locate", _locate_payload(query, min_conf=min_conf), timeout)
    needle = query.lower()
    for m in r.get("matches") or []:
        if needle in str(m.get("text", "")).lower():
            return m
    return None


def _present_on_screen_tesseract(node: str, text: str, *, timeout: float = 8) -> bool:
    """True only when OCR on captured pixels finds ``text`` (not atspi tree false positives)."""
    return _locate_match(node, text, timeout=timeout) is not None


def _verify_message_visible(node: str, message: str, *, timeout: float = 10) -> bool:
    """Strong success criterion for Signal E2E: the message text itself must be visible."""
    checks = [str(message or "").strip(), str(message or "").strip()[:40], str(message or "").strip()[:30]]
    for text in checks:
        if not text:
            continue
        if _present_on_screen_tesseract(node, text, timeout=timeout):
            return True
        v = _node_run(node, f"kvm://{KVM_URI_HOST}/ui/query/verify", {"text": text}, timeout=timeout)
        if _extract_present(v) and str(v.get("via") or "") == "tesseract":
            return True
    return False


def _signal_visible_on_screen(node: str) -> bool:
    """Signal UI visible in the active framebuffer (Wayland-safe)."""
    for hint in SIGNAL_TESSERACT_HINTS:
        if _present_on_screen_tesseract(node, hint, timeout=6):
            return True
    return False


def _llm_model() -> str:
    """Executor / Actor model (LLM_MODEL_EXECUTOR).
    Szybki model wykonujący decyzje podczas automatyzacji (observe-act loop).
    """
    return (
        os.environ.get("LLM_MODEL_EXECUTOR")
        or os.environ.get("LLM_MODEL")
        or "openrouter/google/gemini-3.5-flash"
    )


def _llm_validator_model() -> str:
    """Validator / Examiner model (drugi LLM).
    Używany do walidacji wiedzy i świadomości podstawowego modelu (Gemini) PRZED wykonaniem zadania.
    """
    return (
        os.environ.get("LLM_MODEL_VALIDATOR")
        or "openrouter/deepseek/deepseek-v4-pro"
    )


def _llm_teacher_model() -> str:
    """Teacher model specjalizujący się w analizie grafiki i obrazów.
    Lepszy od Gemini w zadaniach wizyjnych (screenshoty, GUI, low-res captures, quad zooms).
    Używany do głębokiej analizy wizualnej podczas prepare phase.
    """
    return (
        os.environ.get("LLM_MODEL_TEACHER")
        or "openrouter/qwen/qwen3.7-plus"
    )


def _llm_executor_twin_model() -> str:
    """Drugi blizniak executora (twin executor).
    Służy do A/B testowania w fazie przygotowania.
    Mierzymy: czas odpowiedzi + skuteczność (probe success, verify pass, overall efekt).
    Na końcu wybieramy lepszy z dwóch executorów (główny + twin) do rzeczywistego wykonania.
    """
    return (
        os.environ.get("LLM_MODEL_EXECUTOR_TWIN")
        or "openrouter/minimax/minimax-m3"
    )


def _mt(base_tokens: int) -> int:
    """Multiply a hardcoded max_tokens budget by LLM_MAX_TOKENS_MULTIPLIER (env, default 10x).
    Every LLM call in this module used small fixed max_tokens (down to 80) which silently
    truncated mid-thought (e.g. validator reasoning cut off in the ticket history view).
    Set LLM_MAX_TOKENS_MULTIPLIER=1 to restore original tight budgets, or any other factor.
    """
    try:
        mult = float(os.environ.get("LLM_MAX_TOKENS_MULTIPLIER", "10"))
    except ValueError:
        mult = 10.0
    return max(1, int(base_tokens * mult))


def _litellm_call_kwargs() -> dict[str, Any]:
    """Transport override for litellm: use our HTTPS proxy or direct OpenRouter."""
    api_base = (
        os.environ.get("URIRUN_LLM_API_BASE")
        or os.environ.get("OPENAI_API_BASE")
        or os.environ.get("OPENROUTER_BASE_URL")
        or ""
    ).strip().rstrip("/")
    return {"api_base": api_base} if api_base else {}


def _get_executor_history_path() -> Path:
    p = Path(os.environ.get("URIRUN_EXECUTOR_HISTORY") or "~/.urirun/executor_twin_history.jsonl").expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _save_executor_comparison(entry: dict):
    """Append one comparison record to history (JSONL)."""
    path = _get_executor_history_path()
    entry["type"] = entry.get("type", "comparison")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def _save_executor_outcome(executor_model: str, outcome: dict):
    """Record post-execution outcome for an executor (for effectiveness stats)."""
    path = _get_executor_history_path()
    entry = {
        "type": "outcome",
        "ts": time.time(),
        "model": executor_model,
        "outcome": outcome,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def _load_executor_history(limit: int = 200) -> list[dict]:
    path = _get_executor_history_path()
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").strip().splitlines()[-limit:]
    history = []
    for line in lines:
        try:
            history.append(json.loads(line))
        except Exception:
            pass
    return history


def _get_best_executor_from_history() -> dict:
    """Return stats and recommendation based on historical comparisons.
    Favors high win-rate, then low average time.
    """
    history = _load_executor_history()
    if not history:
        return {"best_model": None, "stats": {}, "message": "no history yet"}

    stats = {}
    for h in history:
        if h.get("type") == "outcome":
            model = h.get("model")
            if not model:
                continue
            if model not in stats:
                stats[model] = {"count": 0, "successes": 0, "wins": 0, "total_time": 0.0, "times": []}
            s = stats[model]
            s["count"] += 1
            out = h.get("outcome", {})
            if out.get("success"):
                s["successes"] += 1
                s["wins"] += 1
            t = float(out.get("time") or 0)
            if t > 0:
                s["total_time"] += t
                s["times"].append(t)
            continue
        # comparison
        for role in ("main", "twin"):
            data = h.get(role, {})
            model = data.get("model")
            if not model:
                continue
            if model not in stats:
                stats[model] = {"count": 0, "wins": 0, "total_time": 0.0, "times": [], "successes": 0}
            s = stats[model]
            s["count"] += 1
            t = float(data.get("time") or 0)
            s["total_time"] += t
            s["times"].append(t)
            if (h.get("winner") or "").upper() in (role.upper(), "MAIN" if role == "main" else "TWIN"):
                s["wins"] += 1

    if not stats:
        return {"best_model": None, "stats": {}, "message": "no valid entries"}

    best_model = None
    best_score = -999999
    for model, s in stats.items():
        if s["count"] == 0:
            continue
        win_rate = s["wins"] / s["count"]
        avg_time = s["total_time"] / s["count"]
        # score = win rate (primary) - normalized time penalty
        score = (win_rate * 100) - (avg_time * 0.5)
        if score > best_score:
            best_score = score
            best_model = model

    return {
        "best_model": best_model,
        "stats": stats,
        "message": f"best by score: {best_model}",
    }


def _get_uri_processes_knowledge(node: str = "lenovo", ticket: dict | None = None) -> str:
    """Środowisko URI + katalog procesów ze schematami — pierwszy prompt dla executora."""
    try:
        from urirun_runtime.ticket_llm_context import build_first_system_prompt, record_first_prompt
        prompt = build_first_system_prompt(ticket=ticket, node=node, node_run=_node_run)
        if ticket:
            record_first_prompt(ticket, node, prompt)
        return prompt
    except Exception:  # noqa: BLE001
        pass
    return f"(uri catalog unavailable; node={node})"



def _llm_decide_next(state: str, full_knowledge: str = None, node: str = "lenovo", max_tokens: int | None = None,
                     model: str | None = None) -> dict:
    """Small-step decision: given current OBSERVE state (captures, verifies, locates),
    LLM picks the SINGLE best next URI process from the FULL registry to advance the micro-goal.
    Returns {"uri": "...", "payload": {...}, "reason": "...", "expected_verify": "..."}
    This breaks the decision process into tiny steps so LLM can react to real outcomes.

    model: override the deciding model (used for executor-twin failover — see send_via_kvm).
    Defaults to LLM_MODEL_EXECUTOR when not given.
    max_tokens: base budget (default 200) scaled by LLM_MAX_TOKENS_MULTIPLIER via _mt().
    """
    model = model or _llm_model()
    max_tokens = _mt(max_tokens if max_tokens is not None else 200)
    know = full_knowledge or _get_uri_processes_knowledge(node)
    prompt = f"""You are making ONE micro decision in KVM GUI automation for Signal to {node}.

{know}

CURRENT OBSERVE STATE (from recent captures, verifies, locates - react to this exactly):
{state}

DECISION RULE (small step only):
- NEVER use input/command/type or input/command/key until CURRENT OBSERVE STATE confirms Signal is focused/visible.
- If Signal is not visible: screen/query/capture first, then window/command/focus Signal, then verify.
- Pick EXACTLY ONE best next URI from the FULL registry list above that makes progress on the current micro-goal (e.g. get to composer, verify probe landed, send, etc.).
- Payload must be precise (use locate results if present, or defensive coords).
- Include "expected_verify" text for the follow-up ui/query/verify.
- If you think the micro-goal is done or blocked, use special: uri="done" or "inquiry://host/case/command/create"
- Be creative with registry options - you have the whole catalog, not just kvm.

Output ONLY compact JSON:
{{"uri": "exact-uri-from-registry", "payload": {{...}}, "reason": "why this URI now", "expected_verify": "text that should appear"}}
"""
    try:
        resp = _litellm.completion(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=0.2,
            **_litellm_call_kwargs(),
        )
        content = (resp.choices[0].message.content or "").strip()
        if "{" in content:
            j = json.loads(content[content.find("{"): content.rfind("}") + 1])
            j.setdefault("model", model)
            return j
        return {"uri": f"kvm://{KVM_URI_HOST}/ui/query/verify", "payload": {"text": "Signal"}, "reason": "fallback", "raw": content}
    except Exception as e:
        return {"uri": f"kvm://{KVM_URI_HOST}/ui/query/verify", "payload": {}, "reason": f"error {str(e)[:50]}", "error": True}

def _llm_decide_observe(state: str, options: list[str] = None, node: str = "lenovo") -> dict:
    """Legacy/small helper. Prefer _llm_decide_next for full registry choice."""
    # delegate to new for consistency
    return _llm_decide_next(state, None, node)


def _save_llm_trace(ticket: str | None, phase: str, model: str, prompt: str, response: str, images: list | None = None, extra: dict | None = None):
    """Better per-ticket logging of full LLM conversations for traceability.
    Saves to journal/{ticket}-llm.jsonl (full) + host_db log.
    When images (paths) are provided, embeds base64 previews for UI display.
    """
    if not ticket:
        return
    previews = []
    if images:
        try:
            import base64
            for imgp in images:
                if isinstance(imgp, str) and os.path.exists(imgp):
                    with open(imgp, "rb") as fh:
                        b64 = base64.b64encode(fh.read()).decode("ascii")
                    previews.append({"path": imgp, "data_url": f"data:image/png;base64,{b64}"})
        except Exception:
            pass
    try:
        from pathlib import Path as _P
        jdir = _P("~/.urirun/host-dashboard/journal").expanduser()
        jdir.mkdir(parents=True, exist_ok=True)
        jf = jdir / f"{ticket}-llm.jsonl"
        entry = {
            "ts": time.time(),
            "phase": phase,
            "model": model,
            "prompt": prompt,
            "response": response,
            "has_images": bool(images),
            "image_previews": previews,
            "extra": extra or {},
        }
        with open(jf, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass
    try:
        from urirun.host import host_db as _hdb
        db = None
        _hdb.add_log(db, "llm-automation", "decision", {
            "model": model,
            "ticket": ticket,
            "phase": phase,
            "prompt": prompt[:2000] if len(prompt) > 2000 else prompt,
            "response": response[:2000] if len(response) > 2000 else response,
            "ts": time.time(),
        })
    except Exception:
        pass


def _llm_completion(model: str, prompt: str, system: str = None, max_tokens: int = 1200, temperature: float = 0.2, ticket: str | None = None, images: list[str] | None = None) -> dict:
    """Unified completion helper. Supports vision images (base64 data: urls or paths for litellm/OpenRouter).
    Full content is now logged (better tracing).
    """
    history_block = ""
    if ticket:
        try:
            from urirun_runtime.ticket_llm_context import format_turns_for_llm
            history_block = format_turns_for_llm(ticket)
        except Exception:  # noqa: BLE001
            pass
    if history_block:
        prompt = history_block + "\n\n" + prompt
    try:
        content_list = [{"type": "text", "text": prompt}]
        if images:
            for img in images:
                if isinstance(img, str):
                    if img.startswith("data:"):
                        content_list.append({"type": "image_url", "image_url": {"url": img}})
                    else:
                        # treat as path, read as base64
                        try:
                            import base64
                            with open(img, "rb") as fh:
                                b64 = base64.b64encode(fh.read()).decode()
                            content_list.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
                        except Exception:
                            pass
        user_content = content_list if images else prompt
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user_content})
        resp = _litellm.completion(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            **_litellm_call_kwargs(),
        )
        content = (resp.choices[0].message.content or "").strip()
        _save_llm_trace(ticket, "llm-call", model, prompt if not images else prompt + " [with images]", content, images=images)
        try:
            from urirun_runtime.ticket_llm_context import save_llm_turn
            save_llm_turn(ticket, role="assistant", phase="llm-call", content=content, model=model)
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "model": model, "content": content}
    except Exception as e:
        return {"ok": False, "model": model, "error": str(e)}


def _validator_judge_attempt(model_used: str, timeline: list, verified: bool, node: str,
                             ticket: str | None) -> dict:
    """LLM_MODEL_VALIDATOR reviews one executor's attempt (its own timeline, not just the raw
    text-match `verified` flag) and gives a semantic PASS/FAIL verdict — a second model checking
    the first model's work, same principle as the human-twin delegation assess() chain.
    Used by send_via_kvm to decide whether to trigger executor-twin failover / teacher escalation.
    """
    validator = _llm_validator_model()
    recent = timeline[-6:]
    steps_desc = "\n".join(
        f"- {t.get('uri')} {t.get('payload')} -> {str(t.get('result'))[:120]}" for t in recent
    ) or "(brak kroków)"
    prompt = (
        f"Jesteś walidatorem automatyzacji GUI. Model wykonawczy ({model_used}) próbował wykonać zadanie "
        f"krok po kroku. Techniczna weryfikacja tekstu w UI zwróciła: {verified}.\n\n"
        f"Ostatnie kroki (uri/payload/wynik):\n{steps_desc}\n\n"
        "Oceń: czy zadanie zostało FAKTYCZNIE wykonane poprawnie — nie tylko czy verify=true, ale czy "
        "kroki mają sens i nie ma sprzeczności/pułapek (np. tekst wylądował w złym polu). "
        'Odpowiedz WYŁĄCZNIE JSON {"pass": true/false, "reason": "krótkie uzasadnienie"}.'
    )
    resp = _llm_completion(validator, prompt, max_tokens=_mt(200), temperature=0.1, ticket=ticket)
    content = resp.get("content", "")
    try:
        j = json.loads(content[content.find("{"):content.rfind("}") + 1])
        return {"pass": bool(j.get("pass")), "reason": j.get("reason", ""), "model": validator}
    except Exception:
        return {"pass": verified, "reason": "validator-parse-fallback", "model": validator}


def _teacher_propose_improvement(timeline: list, node: str, ticket: str | None, task: str) -> dict:
    """Ostatnia linia obrony: gdy TAKŻE executor-twin nie przejdzie walidacji, LLM_MODEL_TEACHER
    (specjalista wizji/UI) analizuje pełny ślad + realne zrzuty ekranu i proponuje KONKRETNĄ poprawę —
    nie kolejną powtórkę tej samej strategii, tylko: inny proces URI, lepszy prompt, albo diagnozę że
    brakuje jakiejś zdolności (nowy connector). Zwraca artefakt do zalogowania (inquiry:// case /
    ticket note) — nie wykonuje niczego automatycznie.
    """
    teacher = _llm_teacher_model()
    images = []
    try:
        p, _ = _capture_lowres(node, 320)
        if p:
            images.append(p)
        q, _ = _capture_quad(node, 2, 480)
        if q:
            images.append(q)
    except Exception:
        pass
    recent = timeline[-8:]
    steps_desc = "\n".join(
        f"- [{t.get('model')}] {t.get('uri')} {t.get('payload')} -> {str(t.get('result'))[:120]}"
        for t in recent
    ) or "(brak kroków)"
    prompt = (
        f"Executor i jego twin OBAJ nie poradzili sobie z zadaniem: {task[:300]}\n\n"
        f"Historia prób (uri/payload/wynik):\n{steps_desc}\n\n"
        "Jako specjalista wizji/UI, na podstawie załączonych zrzutów zaproponuj KONKRETNĄ poprawę, "
        "nie powtórkę tej samej strategii:\n"
        "1. Czy brakuje jakiegoś procesu URI (np. inny sposób lokalizacji elementu — atspi vs OCR vs coord)?\n"
        "2. Czy prompt/dekompozycja zadania powinny być inne (np. mniejsze kroki, inna kolejność)?\n"
        "3. Podaj konkretny, wykonalny NASTĘPNY krok (uri+payload) różny od dotychczasowych prób.\n"
        'Odpowiedz WYŁĄCZNIE JSON {"diagnosis": "...", "suggested_uri": "...", "suggested_payload": {...}, '
        '"prompt_improvement": "...", "needs_new_capability": "opis brakującej zdolności albo null"}.'
    )
    resp = _llm_completion(teacher, prompt, max_tokens=_mt(500), temperature=0.3, ticket=ticket,
                           images=images if images else None)
    content = resp.get("content", "")
    try:
        j = json.loads(content[content.find("{"):content.rfind("}") + 1])
    except Exception:
        j = {"diagnosis": content[:500] or "(brak odpowiedzi teachera)"}
    j["model"] = teacher
    return j


def prepare_and_validate_for_signal_kvm(recipient: str = DEFAULT_RECIPIENT, ticket: str | None = None, node: str = DEFAULT_NODE) -> dict:
    """MANDATORY PREPARATION PHASE (before any real KVM actions).

    Triple-LLM safety mechanism:

    - LLM_MODEL_EXECUTOR (default: gemini-3.5-flash)
      Produces detailed task understanding + visual awareness report.

    - LLM_MODEL_VALIDATOR (e.g. qwen/qwen3.7-plus or deepseek-v4-pro)
      Acts as rigorous examiner. Must return PASS (with score) before any real actions.

    - LLM_MODEL_TEACHER (recommended: qwen/qwen3.7-plus)
      Specializes in graphics analysis. Reviews low-res captures, quad zooms and UI state.
      Provides visual understanding that is generally superior to plain Gemini Flash for
      screenshot/GUI tasks (GUI grounding, element localization, state verification).

    Real kvm:// actions are blocked unless Validator approves and Teacher contributes visual analysis.
    This enforces "przed wykonaniem zadania model musi wiedzieć z czym ma do czynienia".
    """
    executor = _llm_model()
    validator = _llm_validator_model()
    executor_twin = _llm_executor_twin_model()
    teacher = _llm_teacher_model()

    # Historical recommendation
    hist = _get_best_executor_from_history()
    if hist.get("best_model"):
        print(f"[HISTORY] Statystycznie najlepszy executor do tej pory: {hist['best_model']}")
        for m, s in hist.get("stats", {}).items():
            wr = s["wins"] / s["count"] if s.get("count") else 0
            avg = s["total_time"] / s["count"] if s.get("count") else 0
            print(f"  {m}: win_rate={wr:.0%}  avg_time={avg:.2f}s  (n={s['count']})")

    print("\n" + "="*70)
    print("[PREPARATION] Starting triple-LLM knowledge + visual validation phase")
    print(f"[PREPARATION] Executor       (LLM_MODEL_EXECUTOR):      {executor}")
    print(f"[PREPARATION] Executor Twin  (LLM_MODEL_EXECUTOR_TWIN): {executor_twin}  ← drugi blizniak do porównania")
    print(f"[PREPARATION] Validator      (LLM_MODEL_VALIDATOR):     {validator}")
    print(f"[PREPARATION] Teacher        (LLM_MODEL_TEACHER):       {teacher}   ← graphics/screenshot specialist")
    print(f"[PREPARATION] node: {node}")
    print("="*70 + "\n")

    _k = KVM_URI_HOST
    task = (
        f"Wysyłka prawdziwej wiadomości tekstowej do '{recipient}' przez Signal Desktop "
        f"zalogowanego na węźle {node} (1920x1080, Wayland) przy użyciu wyłącznie "
        f"prawdziwych procesów URI z rejestru ({registry_kvm_hint()}).\n\n"
        "LLM musi wygenerować KOMPLETNY plan jako listę {\"id\", \"uri\", \"payload\":{...} } obejmujący: setup TTS jeśli potrzeba, speak announcement, focus, locate/search, defensive probe+verify+clear, type full, send Return, multiple verifies (postcondition), final evidence capture, final speak, nextIntent.\n"
        "Kluczowe wymagania (nie wolno ich łamać):\n"
        "- Zawsze używaj 3-znakowego defensive probe zanim wpiszesz pełny tekst.\n"
        "- Natychmiast czyść (ctrl+a + BackSpace) jeśli probe nie wyląduje w poprawnym composerze lub header czatu się zgubi.\n"
        "- Używaj low-res capture jako feedback + quad zoom (1-4) gdy LLM decide_observe wymaga.\n"
        "- Weryfikacja sukcesu: tekst musi być realnie widoczny w czacie (effect + verified przez ui/query/verify + postcondition).\n"
        "- Nigdy nie ufaj 'ok:true' bez dowodu efektu + provenance.\n"
        "- Preferuj ui/query/locate przed sztywnymi coord; payloady zdecyduje LLM.\n"
        "- atspi często nie widzi pól tekstowych Electrona — licz się z tym, że verify będzie polegać głównie na tesseract/OCR."
    )

    # === STEP 1: Executor produces detailed awareness report ===
    executor_prompt = f"""Jesteś odpowiedzialnym agentem automatyzacji GUI przez KVM.

{_get_uri_processes_knowledge(node, ticket={"id": ticket, "name": task[:120], "labels": ["signal", "kvm", node]})}

DECISION LOOP (from docs): intent -> flow (steps as URIs + JSON payloads) -> execution/result -> observation (verify + postcondition) -> nextIntent -> next flow
Status: dry-run | done | blocked | retryable | failed
Use router://host/plan/query/diagnose BEFORE executing any sequence. On fail use real inquiry://... and reflection://...

Zadanie: {task}

**Twoim zadaniem jest NAPISAĆ JAK to wykonać** — nie używaj hardkodowanych koordynatów/sekwencji w Pythonie.
Wygeneruj KOMPLETNY plan jako blok ```urirun:processes``` (JSON array: id, name, actor, uri, payload, depends_on).

**WYMAGANA MINIMALNA SEKWENCJA (musi być w planie w tej lub podobnej kolejności, używając URIs z rejestru):**
1. router://.../diagnose (zawsze na początku)
2. focus Signal window (kvm://{_k}/window/command/focus)
3. capture initial (kvm://{_k}/screen/query/capture + quad)
4. locate search (kvm://{_k}/ui/query/locate)
5. probe 3 chars (kvm://{_k}/input/command/type "TST" or contact prefix)
6. verify probe + header (kvm://{_k}/ui/query/verify)
7. if fail: clear (kvm://{_k}/input/command/key "ctrl+a,BackSpace,escape") + re-locate + re-probe
8. full type message (kvm://{_k}/input/command/type)
9. send (kvm://{_k}/input/command/key "Return")
10. final capture + quad (kvm://{_k}/screen/query/capture)
11. final verify (kvm://{_k}/ui/query/verify)
12. speak (kvm://{_k}/task/command/run or speak)
13. decision_loop with nextIntent

Przygotuj plan w standardzie urirun-llm-runtime:

1. intent (krótki opis w polu name pierwszego kroku lub osobno w komentarzu)
2. ```urirun:processes``` = [ {{"id", "name", "actor", "uri", "payload", "depends_on", "human_approval"}}, ... ]
3. observation plan (verifies after each mutate)
4. risks, verification_criteria, failure_modes (krótko pod blokiem lub w human_approval krokach)

Bądź maksymalnie konkretny. Używaj TYLKO URIs z rejestru. Output: najpierw blok ```urirun:processes```, potem krótki komentarz ryzyk."""

    print("[PREP] Executor is preparing full task understanding report...")
    exec_report = _llm_completion(executor, executor_prompt, max_tokens=_mt(1400), temperature=0.3, ticket=ticket)
    print(f"[PREP EXECUTOR] model={executor}")
    print(exec_report.get("content", str(exec_report))[:2000])
    print("---\n")

    if not exec_report.get("ok"):
        return {"ok": False, "phase": "executor_report", "error": exec_report.get("error")}

    # Extract + RETRY plan logic (urirun:processes standard, fallback decision_loop)
    def _extract_plan_from_content(content: str) -> tuple[list, str]:
        try:
            from urirun_runtime.ticket_llm_context import parse_ticket_process_plan
            plan, fmt = parse_ticket_process_plan(content or "")
            if plan:
                return plan, fmt
        except Exception:
            pass
        return [], "none"

    def _is_good_plan(p: list) -> bool:
        if not p or len(p) < 2:
            return False
        uris = [str(s.get("uri", "")) for s in p if isinstance(s, dict)]
        return any("://" in u for u in uris)

    plan, plan_fmt = _extract_plan_from_content(exec_report.get("content", ""))
    retry_prompt_base = (
        executor_prompt
        + "\n\nIMPORTANT: Output ONLY a fenced ```urirun:processes``` block with a JSON array "
        "(id, name, actor, uri, payload, depends_on). No other text."
    )

    for attempt in range(1, 4):  # up to 3 attempts for good plan
        if _is_good_plan(plan):
            print(f"[PREP] Good plan extracted on attempt {attempt} (format={plan_fmt})")
            break
        if attempt > 1:
            print(f"[PREP] Plan extraction weak on attempt {attempt-1}, retrying EXECUTOR for urirun:processes...")
            stricter = _llm_completion(executor, retry_prompt_base, max_tokens=_mt(1400), temperature=0.2, ticket=ticket)
            _save_llm_trace(ticket, "executor-retry-plan", executor, retry_prompt_base, stricter.get("content", ""), extra={"attempt": attempt})
            plan, plan_fmt = _extract_plan_from_content(stricter.get("content", ""))

    if not _is_good_plan(plan):
        print("[PREP] Still no good plan after retries, using defensive minimal fallback")
        plan = [
            {"id": "focus-signal", "uri": f"kvm://{KVM_URI_HOST}/window/command/focus", "payload": {"title": "Signal"}},
            {"id": "locate-search", "uri": f"kvm://{KVM_URI_HOST}/ui/query/locate", "payload": {"query": recipient_search_prefix(recipient)}},
            {"id": "probe", "uri": f"kvm://{KVM_URI_HOST}/input/command/type", "payload": {"text": "TST"}},
            {"id": "verify-probe", "uri": f"kvm://{KVM_URI_HOST}/ui/query/verify", "payload": {"text": "TST"}},
            {"id": "clear-if-bad", "uri": f"kvm://{KVM_URI_HOST}/input/command/key", "payload": {"keys": "ctrl+a,BackSpace,escape"}},
            {"id": "full-message", "uri": f"kvm://{KVM_URI_HOST}/input/command/type", "payload": {"text": "[[MESSAGE]]"}},
            {"id": "send", "uri": f"kvm://{KVM_URI_HOST}/input/command/key", "payload": {"keys": "Return"}},
            {"id": "verify-sent", "uri": f"kvm://{KVM_URI_HOST}/ui/query/verify", "payload": {"text": "[[MESSAGE]]"}},
            {"id": "final-capture", "uri": f"kvm://{KVM_URI_HOST}/screen/query/capture", "payload": {"max_width": 480, "note": "final evidence"}},
            {"id": "final-speak", "uri": f"kvm://{KVM_URI_HOST}/task/command/run", "payload": {"steps": [{"op": "type", "text": f"spd-say 'Wiadomość do {recipient} wysłana' || echo final speak"}] }},
        ]

    # Ensure plan always ends with final evidence + speak + proper decision_loop shape (address validator feedback)
    last_uris = [str(s.get("uri","")) for s in plan[-3:]]
    if not any("capture" in u for u in last_uris):
        plan.append({"id": "final-evidence-capture", "uri": f"kvm://{KVM_URI_HOST}/screen/query/capture", "payload": {"max_width": 480, "quad": 2, "note": "final post-send evidence + quad zoom"}})
    if not any("speak" in u or "task" in u for u in last_uris):
        plan.append({"id": "final-speak", "uri": f"kvm://{KVM_URI_HOST}/task/command/run", "payload": {"steps": [{"op": "type", "text": f"echo 'Final speak: message to {recipient}'"}] }})
    if not any("nextIntent" in str(s) for s in plan):
        plan.append({"id": "decision-loop-nextIntent", "uri": "task://host/ticket/command/ready", "payload": {"status": "done" if True else "retryable", "ticket": ticket}})

    # Break into smaller steps: if plan is long, LLM can decide details at runtime via reactive loop; here ensure structure
    print(f"[PREP] Final plan has {len(plan)} steps (augmented for completeness)")

    # === IMPROVED: LLM_MODEL_TEACHER with real images (vision) ===
    # Capture low-res + quad for visual grounding before teacher analysis.
    teacher = _llm_teacher_model()
    image_paths = []
    try:
        p1, _ = _capture_lowres(node, max_width=320)
        if p1: image_paths.append(p1)
        p2, _ = _capture_quad(node, quad=2, max_width=480)  # focus on input area
        if p2: image_paths.append(p2)
        print(f"[PREP] Captured images for TEACHER vision: {image_paths}")
    except Exception as cap_e:
        print(f"[PREP] Capture for teacher failed (will use text only): {cap_e}")

    teacher_prompt = f"""Jesteś ekspertem od analizy grafiki i interfejsów użytkownika na podstawie zrzutów ekranu.

{_get_uri_processes_knowledge(node)}

DECISION LOOP observation: use screen/query/capture + ui/query/verify to produce observation.kind (e.g. "uri-step-failed" or "send-verified"), failedStep, error.

Zadanie: {task}

Na podstawie aktualnych zrzutów ekranu (low-res + quad) oraz typowych problemów z Signal Desktop na 1920x1080 Wayland:

1. Opisz co widzisz na załączonych obrazach (czy Signal jest otwarty, czy jesteśmy w czacie z {DEFAULT_RECIPIENT}?).
2. Jakie wizualne wskazówki (kolory, kształty, pozycje) pozwalają odróżnić composer od paska wyszukiwania.
3. Jakie błędy wizualne najczęściej prowadzą do wpisania tekstu w złe miejsce.
4. Podaj konkretne wskazówki dla modelu jak interpretować wyniki OCR na tych capture'ach. Output as observation + nextIntent + recommended next URI step.

Bądź bardzo precyzyjny wizualnie. Odwołuj się do konkretnych procesów URI gdy ma to sens. Analizuj załączone obrazy."""

    print(f"[PREP] Teacher ({teacher}) analyzing graphics/UI visuals with images...")
    teacher_report = _llm_completion(teacher, teacher_prompt, max_tokens=_mt(900), temperature=0.2, ticket=ticket, images=image_paths if image_paths else None)
    print(f"[PREP TEACHER] model={teacher}")
    print(teacher_report.get("content", "")[:1800])
    print("---\n")

    # === Executor Twin Comparison (drugi blizniak) ===
    # Testujemy obu executorów na tym samym zadaniu wizualnym.
    # Mierzymy: czas odpowiedzi + jakość (ocena przez Validator/Teacher).
    # Wybieramy lepszy na podstawie czasu + skuteczności (jak dobrze rozumie UI).
    print("[PREP] Testing both executors (main + twin) for speed + quality...")

    comparison_prompt = (
        _get_uri_processes_knowledge(node) + "\n\n"
        "DECISION LOOP: produce observation + nextIntent in response.\n\n"
        f"Current visual state from low-res capture + previous analysis:\n"
        f"{exec_report.get('content', '')[:800]}\n\n"
        f"Task: {task}\n\n"
        "What should be the next defensive action (click position or zoom request)? "
        "Be very specific and conservative. Output only the recommended next step in one short sentence. Include observation and nextIntent."
    )

    t_main = None
    t_twin = None
    winner = "MAIN"
    best_executor = executor

    try:
        # Main executor
        t1 = time.time()
        main_resp = _llm_completion(executor, comparison_prompt, max_tokens=_mt(120), temperature=0.2, ticket=ticket)
        t_main = time.time() - t1
        main_text = main_resp.get("content", "")[:200]

        # Twin executor
        t2 = time.time()
        twin_resp = _llm_completion(executor_twin, comparison_prompt, max_tokens=_mt(120), temperature=0.2, ticket=ticket)
        t_twin = time.time() - t2
        twin_text = twin_resp.get("content", "")[:200]

        print(f"[EXECUTOR MAIN] {executor} | time={t_main:.2f}s")
        print(f"  -> {main_text}")
        print(f"[EXECUTOR TWIN] {executor_twin} | time={t_twin:.2f}s")
        print(f"  -> {twin_text}")

        # Ask validator or teacher to judge which is better (quality + speed)
        judge_prompt = (
            f"Compare two responses for the same UI automation task.\n\n"
            f"Main Executor ({executor}, {t_main:.2f}s):\n{main_text}\n\n"
            f"Twin Executor ({executor_twin}, {t_twin:.2f}s):\n{twin_text}\n\n"
            "Which one is better considering: correctness of UI understanding, defensiveness, and speed?\n"
            "Reply with exactly one of: MAIN, TWIN, or TIE. Then one short reason."
        )
        judge_resp = _llm_completion(validator, judge_prompt, max_tokens=_mt(80), temperature=0.1, ticket=ticket)
        judge_text = judge_resp.get("content", "").strip().upper()

        if "TWIN" in judge_text and t_twin is not None and t_twin < (t_main or 999) * 1.5:
            best_executor = executor_twin
            best_time = t_twin
            winner = "TWIN"
        else:
            best_executor = executor
            best_time = t_main or 0
            winner = "MAIN"

        print(f"[EXECUTOR WINNER] {winner} -> {best_executor} (time={best_time:.2f}s)")
        print(f"[JUDGE] {judge_text[:150]}")

        # History bias: if one model is historically much stronger and this run is close, prefer history
        hist_best = hist.get("best_model")
        if hist_best and hist_best != best_executor:
            t_diff = abs((t_main or 0) - (t_twin or 0))
            if t_diff < 4.0:  # times are similar
                print(f"[HISTORY] Switching to historical best '{hist_best}' (live times close)")
                best_executor = hist_best
                winner = "TWIN" if hist_best == executor_twin else "MAIN"
    except Exception as e:
        print(f"[EXECUTOR COMPARISON] Failed, falling back to main executor: {e}")
        winner = "MAIN"
        best_executor = executor

    print("---\n")

    # Use the winner as the active executor for the rest of the flow
    executor = best_executor

    # Persist comparison for long-term statistical selection (extended with decision loop metrics)
    try:
        _save_executor_comparison({
            "ts": time.time(),
            "task": task[:120],
            "main": {"model": _llm_model(), "time": t_main, "response": main_text if 'main_text' in locals() else None},
            "twin": {"model": executor_twin, "time": t_twin, "response": twin_text if 'twin_text' in locals() else None},
            "winner": winner,
            "judge": judge_text[:200] if 'judge_text' in locals() else None,
            "used": best_executor,
            "intent": {"id": "signal-send-gui", "recipient": rec, "ticket": ticket},
            "flow": {"steps": ["focus", "search", "probe", "verify", "type", "send", "verify-sent"]},
            "observation": {"pre_state": "lowres+quad captured", "probe_candidate": "TST"},
            "nextIntent": {"uri": f"kvm://{KVM_URI_HOST}/ui/query/verify", "status": "pending-verify"},
        })
    except Exception as e:
        print(f"[HISTORY] Could not save comparison: {e}")

    # === STEP 2: Validator examines the report and runs a quiz ===
    validator_prompt = f"""Jesteś surowym, precyzyjnym egzaminatorem automatyzacji.

{_get_uri_processes_knowledge(node)}

DECISION LOOP: validate that the report contains a ```urirun:processes``` block with valid URI steps and postconditions.

Zadanie, które ma wykonać inny model (Executor = {executor}):
{task}

Oto raport przygotowany przez Executora:
{exec_report.get("content")}

Twoje zadanie:
1. Oceń jakość zrozumienia zadania przez Executora (0-100), check decision_loop shape.
2. Wskaż luki w wiedzy (np. czy rozumie dlaczego probe musi być widoczny w composerze, a nie tylko header?).
3. Zadaj 4-6 trudnych, konkretnych pytań sprawdzających świadomość UI i ograniczeń (np. o atspi vs tesseract, o koordynatach, o tym co się dzieje gdy klikniemy w złe miejsce). Używaj nazw konkretnych procesów URI w pytaniach.
4. Na podstawie odpowiedzi Executora (które symulujesz lub analizujesz) zdecyduj:
   - PASS (może iść dalej, z ewentualnymi poprawkami)
   - FAIL (brakuje krytycznej wiedzy — nie wolno uruchamiać automatyzacji)
5. Podaj jasne uzasadnienie i listę wymagań, które Executor musi spełnić przed startem.

Odpowiedz wyłącznie w formacie JSON:
{{
  "score": 0-100,
  "verdict": "PASS" | "FAIL",
  "critical_gaps": ["...", "..."],
  "quiz_questions": ["pytanie 1", "..."],
  "final_recommendation": "krótki tekst",
  "must_fix_before_execution": ["...", "..."]
}}"""

    print("[PREP] Validator is examining the report and creating quiz...")
    # Special handling for DeepSeek V4 Pro on OpenRouter (often needs reasoning + higher tokens)
    try:
        if "deepseek" in validator.lower():
            resp = _litellm.completion(
                model=validator,
                messages=[{"role": "user", "content": validator_prompt}],
                max_tokens=_mt(1400),
                temperature=0.1,
                extra_body={"reasoning": {"effort": "high"}},
                **_litellm_call_kwargs(),
            )
            val_content = resp.choices[0].message.content or ""
            val_result = {"ok": True, "model": validator, "content": val_content}
        else:
            val_result = _llm_completion(validator, validator_prompt, max_tokens=_mt(1200), temperature=0.1, ticket=ticket)
            val_content = val_result.get("content", "") or str(val_result)
    except Exception as e:
        val_result = {"ok": False, "model": validator, "content": f"ERROR: {e}"}
        val_content = val_result["content"]

    print(f"[PREP VALIDATOR] model={validator}")
    print("Validator response (full):")
    print(val_content[:3000] if val_content else "(empty response)")
    print("---\n")

    # Try to parse validator JSON (more robust)
    verdict = "UNKNOWN"
    val_json = None
    try:
        content = val_result.get("content", "")
        # Find the largest JSON object
        start = content.rfind("{")
        end = content.rfind("}") + 1
        if start != -1 and end > start:
            val_json = json.loads(content[start:end])
            verdict = val_json.get("verdict", val_json.get("decision", "UNKNOWN")).upper()
            if "PASS" in verdict:
                verdict = "PASS"
            elif "FAIL" in verdict:
                verdict = "FAIL"
            print(f"[PREP VALIDATOR VERDICT] {verdict} (score={val_json.get('score', '?')})")
        else:
            # Fallback: look for keywords in text
            cl = content.lower()
            if "pass" in cl and "fail" not in cl[:cl.find("pass")]:
                verdict = "PASS"
            elif "fail" in cl:
                verdict = "FAIL"
            print(f"[PREP VALIDATOR VERDICT] {verdict} (from text)")
    except Exception as e:
        print(f"[PREP] Could not parse validator JSON cleanly: {e}. Treating as caution.")

    preparation_result = {
        "ok": verdict == "PASS",
        "executor_model": executor,
        "executor_twin_model": executor_twin,
        "validator_model": validator,
        "teacher_model": teacher,
        "executor_report": exec_report.get("content"),
        "teacher_visual_analysis": teacher_report.get("content"),
        "validator_raw": val_result.get("content"),
        "executor_comparison": {
            "main": {"model": _llm_model(), "time": t_main},
            "twin": {"model": executor_twin, "time": t_twin},
            "winner": winner,
            "historical_best": hist.get("best_model"),
        },
        "plan": plan,
        "plan_format": plan_fmt,
        "verdict": verdict,
        "task": task,
        "failover_triggered": failover_triggered,
        "teacher_escalated": teacher_escalated,
        "teacher_improvement": teacher_improvement,
        "full_traces_saved_to": f"~/.urirun/host-dashboard/journal/{ticket}-llm.jsonl" if ticket else None,
        "images_used_for_teacher": image_paths if 'image_paths' in locals() else [],
    }

    if verdict != "PASS":
        print("[PREP] VALIDATION FAILED or INCONCLUSIVE — automation will not proceed without human intervention or fixes.")
        preparation_result["decision"] = "abort_or_escalate"

    print("="*70)
    print(f"[PREPARATION COMPLETE] Verdict from validator: {verdict}")
    print("="*70 + "\n")

    return preparation_result


def signal_channel(node: str = "lenovo") -> dict:
    """Realny KANAŁ wysyłki (nie tylko host signal-cli): 'signal-cli' (host) LUB 'signal-gui-kvm'
    (Signal Desktop zalogowany na danym node — źródło prawdy = app list + zrzut)."""
    import shutil
    if shutil.which("signal-cli"):
        return {"channel": "signal-cli", "node": "host", "ready": True}
    apps = _node_run(node, "app://host/desktop/query/list")
    lst = apps.get("apps") or apps.get("desktop") or []
    if any("signal" in str(a).lower() for a in (lst if isinstance(lst, list) else [])):
        return {"channel": "signal-gui-kvm", "node": node, "ready": True,
                "app": SIGNAL_APP_ID, "note": f"Signal Desktop na {node} → wysyłka przez GUI/KVM"}
    return {"channel": "none", "ready": False, "reason": f"brak signal-cli (host) i Signal Desktop ({node})"}


def _signal_account_status() -> str:
    ch = signal_channel()
    return "linked" if ch["ready"] else "cli_missing"


def _capture_lowres(node: str = "lenovo", max_width=320):
    p = f"/tmp/lowres_{int(time.time())}.png"
    _node_run(node, f"kvm://{KVM_URI_HOST}/screen/query/capture", {"output": p, "max_width": max_width}, timeout=8)
    return p, None

def _capture_quad(node: str = "lenovo", quad=2, max_width=480):
    # 1 top-right (12-3), 2 bottom-right (3-6), 3 bottom-left (6-9), 4 top-left (9-12)
    # for 1920x1080
    centers = {1: (1440, 270), 2: (1440, 810), 3: (480, 810), 4: (480, 270)}
    cx, cy = centers.get(quad, (960, 540))
    p = f"/tmp/quad{quad}_{int(time.time())}.png"
    _node_run(node, f"kvm://{KVM_URI_HOST}/screen/query/capture", {
        "output": p,
        "cx": cx, "cy": cy,
        "zoom": 2,
        "max_width": max_width
    }, timeout=8)
    return p, None


def _kvm_meta(node: str = "lenovo", **extra: Any) -> dict:
    return {"source": "send_via_kvm", "module": "urirun_connector_work.goal", "ranOn": node, **extra}


def _kvm_fail(node: str = "lenovo", **fields: Any) -> dict[str, Any]:
    """Uniform failure envelope — always carries _meta so postcondition never flags no-provenance."""
    return {"ok": False, "verified": False, "effect": False, "_meta": _kvm_meta(node), **fields}


def _click_label(node: str, label: str, *, ticket: str | None = None, guarded: bool = True) -> dict:
    """Locate text on screen and click it (locate→click, else click-text fallback)."""
    host = KVM_URI_HOST
    match = _locate_match(node, label, timeout=6)
    center = match.get("center") if match else None
    click_uri = f"kvm://{host}/input/command/click" if center else f"kvm://{host}/ui/command/click-text"
    payload = center if center else {"text": label}
    if guarded:
        return _guarded_node_run(node, click_uri, payload, ticket=ticket, require_signal=False)
    return _node_run(node, click_uri, payload, timeout=8)


def _kvm_input(node: str, uri: str, payload: dict | None = None, *, ticket: str | None = None,
               guarded: bool = True, timeout: float = 8.0) -> dict:
    if guarded:
        return _guarded_node_run(node, uri, payload, ticket=ticket, require_signal=False)
    return _node_run(node, uri, payload, timeout=timeout)


def _open_signal_chat(node: str, recipient: str, *, ticket: str | None = None,
                      guarded: bool = True) -> list[dict]:
    """Navigate Signal UI to the recipient chat (sidebar click, then Ctrl+F search fallback)."""
    import time as _time
    timeline: list[dict] = []
    rec = recipient or DEFAULT_RECIPIENT
    host = KVM_URI_HOST

    r = _click_label(node, rec, ticket=ticket, guarded=guarded)
    timeline.append({"step": "click-recipient", "result": r})
    _time.sleep(0.6)
    if _present_on_screen_tesseract(node, rec) or _extract_present(
        _node_run(node, f"kvm://{host}/ui/query/verify", {"text": rec}, timeout=6)
    ):
        return timeline

    for keys in ("ctrl+f", "ctrl+k"):
        _kvm_input(node, f"kvm://{host}/input/command/key", {"keys": keys}, ticket=ticket, guarded=guarded)
        _time.sleep(0.25)
        _kvm_input(node, f"kvm://{host}/input/command/type", {"text": rec}, ticket=ticket, guarded=guarded, timeout=10)
        _time.sleep(0.25)
        _kvm_input(node, f"kvm://{host}/input/command/key", {"keys": "Return"}, ticket=ticket, guarded=guarded)
        _time.sleep(0.6)
        if _present_on_screen_tesseract(node, rec) or _extract_present(
            _node_run(node, f"kvm://{host}/ui/query/verify", {"text": rec}, timeout=6)
        ):
            timeline.append({"step": "search-open", "keys": keys})
            return timeline
        _kvm_input(node, f"kvm://{host}/input/command/key", {"keys": "escape"}, ticket=ticket, guarded=guarded)
    return timeline


def _match_center(match: dict | None) -> tuple[int, int] | None:
    if not match:
        return None
    center = match.get("center")
    if isinstance(center, dict) and center.get("x") is not None and center.get("y") is not None:
        return int(center["x"]), int(center["y"])
    if isinstance(center, (list, tuple)) and len(center) >= 2:
        return int(center[0]), int(center[1])
    return None


def _locate_composer(node: str, *, min_conf: int = 25) -> tuple[dict | None, str]:
    """OCR-locate the Signal message composer — prefer the bottom-most match."""
    host = KVM_URI_HOST
    best: dict | None = None
    best_label = ""
    best_y = -1
    for label in COMPOSER_LABELS:
        for conf in (min_conf, max(15, min_conf - 10), 15):
            r = _node_run(
                node,
                f"kvm://{host}/ui/query/locate",
                _locate_payload(label, min_conf=conf),
                timeout=8,
            )
            for match in r.get("matches") or []:
                center = _match_center(match)
                if center is None:
                    continue
                text = str(match.get("text", "")).lower()
                if label.lower() not in text and not any(
                    token in text for token in ("message", "wiadomość", "send a message", "wyślij")
                ):
                    continue
                _, y = center
                if y > best_y:
                    best_y = y
                    best = match
                    best_label = label
            if best:
                return best, best_label
    return None, ""


def _screen_bottom_center(node: str) -> list[int]:
    """Heuristic composer click when OCR labels are missing (bottom input band)."""
    cap = _node_run(
        node,
        f"kvm://{KVM_URI_HOST}/screen/query/capture",
        {"max_width": 640},
        timeout=8,
    )
    screen = cap.get("screen") or cap.get("resolution") or [1920, 1080]
    if isinstance(screen, list) and len(screen) >= 2:
        w, h = int(screen[0]), int(screen[1])
    else:
        w, h = 1920, 1080
    return [max(1, w // 2), max(1, int(h * 0.92))]


def _focus_signal_composer(
    node: str,
    *,
    ticket: str | None = None,
    guarded: bool = True,
    verify_probe: str | None = "TST",
) -> dict[str, Any]:
    """Dedicated composer focus: window focus → OCR label → click → type-verified probe."""
    import time as _time

    host = KVM_URI_HOST
    timeline: list[dict] = []

    _node_run(node, f"kvm://{host}/window/command/focus", {"title": "Signal"}, timeout=4)
    _time.sleep(0.3)

    match, label = _locate_composer(node)
    center = _match_center(match) if match else None
    if center is None:
        cx, cy = _screen_bottom_center(node)
        label = "bottom-center-fallback"
        match = {"center": {"x": cx, "y": cy}, "text": label, "via": "heuristic"}
        timeline.append({"step": "composer-fallback", "center": [cx, cy]})
    else:
        cx, cy = center

    click_x, click_y = _composer_click_point([cx, cy])
    click_r = _kvm_input(
        node,
        f"kvm://{host}/input/command/click",
        {"x": click_x, "y": click_y},
        ticket=ticket,
        guarded=guarded,
    )
    timeline.append({
        "step": "click-composer",
        "label": label,
        "center": [cx, cy],
        "click": [click_x, click_y],
        "result": click_r,
    })
    _time.sleep(0.25)

    probe_ok = True
    if verify_probe:
        tv = _node_run(
            node,
            f"kvm://{host}/ui/command/type-verified",
            {
                "text": verify_probe,
                "x": click_x,
                "y": click_y,
                "submit": False,
                "draft_expect": verify_probe,
                "require_draft_verify": True,
            },
            timeout=45,
        )
        probe_ok = bool(tv.get("ok")) and bool(tv.get("verified"))
        timeline.append({
            "step": "type-verified-probe",
            "text": verify_probe,
            "ok": probe_ok,
            "result": {k: tv.get(k) for k in ("ok", "verified", "error", "submitted")},
        })
        if probe_ok:
            _kvm_input(
                node,
                f"kvm://{host}/input/command/key",
                {"keys": "ctrl+a,BackSpace"},
                ticket=ticket,
                guarded=guarded,
            )
            _time.sleep(0.15)

    return {
        "ok": True,
        "label": label,
        "center": [click_x, click_y],
        "match": match,
        "probe_ok": probe_ok,
        "timeline": timeline,
    }


def _preflight_signal_compose(
    node: str,
    recipient: str,
    *,
    ticket: str | None = None,
) -> dict[str, Any]:
    """Observe → verify window → open chat → grounded composer focus before any typing."""
    timeline: list[dict] = []
    rec = recipient or DEFAULT_RECIPIENT

    ready = _ensure_gui_ready_for_signal(node, ticket=ticket)
    timeline.append({"step": "observe-focus", "result": ready})
    if not ready.get("ok"):
        return {"ok": False, "error": "signal_not_focused", "timeline": timeline, "observe": ready.get("observe")}

    # Observe already proved Signal is on-screen (tesseract); avoid a second flaky verify pass.
    window_state = {"observe_confirmed": True, "observe": ready.get("observe")}
    timeline.append({"step": "verify-window", "ok": True, "result": window_state})

    timeline.extend(_open_signal_chat(node, rec, ticket=ticket, guarded=False))

    _node_run(node, f"kvm://{KVM_URI_HOST}/window/command/focus", {"title": "Signal"}, timeout=4)
    import time as _time
    _time.sleep(0.35)

    composer = _focus_signal_composer(node, ticket=ticket, guarded=False, verify_probe="TST")
    timeline.append({"step": "focus-composer", "result": composer})
    if not composer.get("ok"):
        return {
            "ok": False,
            "error": composer.get("error", "composer_focus_failed"),
            "timeline": timeline,
            "composer": composer,
        }

    return {"ok": True, "timeline": timeline, "composer": composer}


def _composer_click_point(center: list[int]) -> list[int]:
    """Nudge below OCR label — Signal's caret sits under the placeholder text."""
    cx, cy = int(center[0]), int(center[1])
    return [cx, max(1, cy + 28)]


def _send_signal_type_verified(
    node: str,
    message: str,
    center: list[int],
    *,
    ticket: str | None = None,
) -> dict[str, Any]:
    """Grounded send into the composer using kvm ui/command/type-verified."""
    import time as _time

    msg = (message or "").strip()
    if not msg or not center or len(center) < 2:
        return {"ok": False, "verified": False, "error": "missing_message_or_center"}

    attempts: list[dict[str, Any]] = []
    for point in (_composer_click_point(center), _composer_click_point([center[0], center[1] + 40])):
        cx, cy = int(point[0]), int(point[1])
        sent = _node_run(
            node,
            f"kvm://{KVM_URI_HOST}/ui/command/type-verified",
            {
                "text": msg,
                "x": cx,
                "y": cy,
                "submit": True,
                "submit_key": "Enter",
                "draft_expect": msg,
                "sent_expect": msg[:40],
                "require_draft_verify": True,
                "require_sent_verify": False,
            },
            timeout=60,
        )
        _time.sleep(0.8)
        verified = bool(sent.get("ok")) and bool(sent.get("verified"))
        if not verified:
            verified = _verify_message_visible(node, msg, timeout=8)
        attempts.append({"center": [cx, cy], "verified": verified, "sent": sent})
        if verified:
            return {"ok": True, "verified": True, "sent": sent, "center": [cx, cy], "attempts": attempts}

    click_x, click_y = _composer_click_point(center)
    _kvm_input(
        node,
        f"kvm://{KVM_URI_HOST}/input/command/click",
        {"x": click_x, "y": click_y},
        ticket=ticket,
        guarded=False,
    )
    _time.sleep(0.2)
    _kvm_input(node, f"kvm://{KVM_URI_HOST}/input/command/type", {"text": msg}, ticket=ticket, guarded=False, timeout=12)
    _time.sleep(0.2)
    _kvm_input(node, f"kvm://{KVM_URI_HOST}/input/command/key", {"keys": "Return"}, ticket=ticket, guarded=False)
    _time.sleep(1.0)
    verified = _verify_message_visible(node, msg, timeout=10)
    attempts.append({"center": [click_x, click_y], "verified": verified, "sent": {"via": "plain-type-fallback"}})
    if verified:
        return {
            "ok": True,
            "verified": True,
            "sent": {"via": "plain-type-fallback", "verified": True},
            "center": [click_x, click_y],
            "attempts": attempts,
        }
    last = attempts[-1] if attempts else {}
    return {
        "ok": False,
        "verified": False,
        "sent": last.get("sent"),
        "center": last.get("center"),
        "attempts": attempts,
    }


def _verify_signal_window(node: str, ticket: str | None = None) -> tuple[bool, dict]:
    """Confirm the Signal window is the active/visible target before typing into it."""
    host = KVM_URI_HOST
    focus = _node_run(node, f"kvm://{host}/window/command/focus", {"title": "Signal"}, timeout=4)
    tesseract_ok = _signal_visible_on_screen(node)
    verify = _node_run(node, f"kvm://{host}/ui/query/verify", {"text": "Signal"}, timeout=4)
    ok = bool(tesseract_ok or (_extract_present(verify) and str(verify.get("via") or "") == "tesseract"))
    return ok, {"focus": focus, "verify": verify, "tesseract_visible": tesseract_ok}


def _send_signal_gui_scripted(recipient: str, text: str, ticket: str | None = None,
                              node: str = "lenovo") -> dict[str, Any]:
    """Deterministic Signal Desktop send: observe → focus → confirm window → open chat → probe → type → verify.

    Replaces the slow triple-LLM micro-loop for production E2E (IFURI-229). LLM prep remains
    available via SIGNAL_KVM_PREP=1."""
    import time as _time
    node = (node or "lenovo").lower()
    rec = recipient or DEFAULT_RECIPIENT
    msg = (text or "").strip()
    if not msg:
        return _kvm_fail(node, error="empty_message")

    timeline: list[dict] = []
    preflight = _preflight_signal_compose(node, rec, ticket=ticket)
    timeline.extend(preflight.get("timeline") or [])
    if not preflight.get("ok"):
        return _kvm_fail(
            node,
            error=preflight.get("error", "preflight_failed"),
            timeline=timeline,
            preflight=preflight,
        )

    center = (preflight.get("composer") or {}).get("center")
    if not center or len(center) < 2:
        return _kvm_fail(node, error="composer_center_missing", timeline=timeline, preflight=preflight)

    sent = _send_signal_type_verified(node, msg, center, ticket=ticket)
    timeline.append({"step": "type-verified-send", "result": sent.get("sent")})
    verified = bool(sent.get("verified"))
    sent_verify: dict = sent.get("sent") if verified else {}
    if not verified:
        _capture_quad(node, 2, 480)
    capture = _node_run(node, f"kvm://{KVM_URI_HOST}/screen/query/capture",
                         {"output": f"/tmp/signal-send-{int(_time.time())}.png"}, timeout=8)

    return {
        "ok": verified,
        "effect": verified,
        "verified": verified,
        "recipient": rec,
        "executor": f"signal-gui-kvm-{node}",
        "sent_via": f"{node} Signal Desktop (scripted)",
        "sent_verify": sent_verify,
        "capture": capture.get("path") or capture.get("output", ""),
        "timeline": timeline,
        "plan_used": "scripted-signal-send",
        "_meta": _kvm_meta(node, mode="scripted"),
    }


def _send_via_llm_runtime_loop(
    recipient: str,
    text: str,
    *,
    ticket: str | None = None,
    node: str = "lenovo",
    initial_plan: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Ticket wykonywany wyłącznie przez pętlę LLM + URI runtime (gap, dogrywanie, retry)."""
    from urirun_runtime.llm_runtime_loop import LlmRuntimeLoop

    rec = recipient or DEFAULT_RECIPIENT
    msg = (text or "").strip()
    if not msg:
        return _kvm_fail(node, error="empty_message")

    preflight = _preflight_signal_compose(node, rec, ticket=ticket)
    if not preflight.get("ok"):
        return _kvm_fail(
            node,
            error=preflight.get("error", "preflight_failed"),
            timeline=preflight.get("timeline") or [],
            preflight=preflight,
        )

    ticket_dict: dict[str, Any] = {
        "id": ticket or "adhoc-signal",
        "name": f"Signal KVM → {rec}",
        "labels": ["signal", "kvm", node],
        "description": f"Wyślij wiadomość Signal do {rec}",
        "inputs": {"recipient": rec, "message": msg},
    }
    goal = (
        f"Wyślij wiadomość Signal Desktop do odbiorcy {rec!r}: {msg!r}. "
        f"Używaj wyłącznie URI z runtime. Dogrywaj brakujące kroki według gap/observation."
    )

    def _llm_fn(prompt: str, system: str | None) -> str:
        r = _llm_completion(_llm_model(), prompt, system=system, max_tokens=_mt(900), temperature=0.2, ticket=ticket)
        return r.get("content") or ""

    center = (preflight.get("composer") or {}).get("center")
    if center and len(center) >= 2:
        quick = _send_signal_type_verified(node, msg, center, ticket=ticket)
        timeline = list(preflight.get("timeline") or [])
        timeline.append({"step": "type-verified-send", "result": quick.get("sent")})
        if quick.get("verified"):
            return {
                "ok": True,
                "verified": True,
                "recipient": rec,
                "executor": _llm_model(),
                "sent_via": f"{node} Signal Desktop (preflight-type-verified)",
                "preflight": preflight,
                "timeline": timeline,
                "plan_used": "preflight-type-verified",
                "status": "done",
                "_meta": _kvm_meta(node, mode="preflight-type-verified"),
            }

    if center and len(center) >= 2 and not initial_plan:
        cx, cy = int(center[0]), int(center[1])
        initial_plan = [
            {
                "id": "send-type-verified",
                "uri": f"kvm://{KVM_URI_HOST}/ui/command/type-verified",
                "payload": {
                    "text": msg,
                    "x": cx,
                    "y": cy,
                    "submit": True,
                    "submit_key": "Enter",
                    "draft_expect": msg,
                    "sent_expect": msg[:40],
                    "require_draft_verify": True,
                    "require_sent_verify": False,
                },
            },
            {
                "id": "verify-sent",
                "uri": f"kvm://{KVM_URI_HOST}/ui/query/verify",
                "payload": {"text": msg[:40]},
            },
        ]

    loop = LlmRuntimeLoop(
        node=node,
        node_run=lambda n, u, p, t: _guarded_node_run(n, u, p, t, ticket=ticket),
        llm_fn=_llm_fn,
        ticket=ticket,
        ticket_dict=ticket_dict,
    )
    result = loop.run(goal, initial_plan=initial_plan)
    timeline = list(preflight.get("timeline") or []) + list(result.get("timeline") or [])
    verified = _verify_message_visible(node, msg, timeout=10)
    ok = bool(verified)
    status = result.get("status")
    if not verified and status == "done":
        status = "unverified"
    return {
        "ok": ok,
        "verified": verified,
        "recipient": rec,
        "executor": _llm_model(),
        "sent_via": f"{node} Signal Desktop (llm-runtime-loop)",
        "preflight": preflight,
        "timeline": timeline,
        "plan_used": result.get("plan_used", "llm-runtime-loop"),
        "status": status,
        "_meta": _kvm_meta(node, mode="llm-runtime-loop"),
        **{k: result[k] for k in ("turns", "failures_in_row", "pending_step") if k in result},
    }


def send_via_kvm(recipient: str, text: str, ticket: str | None = None, node: str = "lenovo") -> dict[str, Any]:
    """Wysyłka przez Signal Desktop na node (KVM) + TTS na node.
    Domyślnie: pętla LLM + URI runtime (URIRUN_LLM_RUNTIME_CONTROL=1).
    Scripted: URIRUN_LLM_RUNTIME_CONTROL=0. Triple-LLM prep seed: SIGNAL_KVM_PREP=1."""
    prep_mode = str(os.environ.get("SIGNAL_KVM_PREP", "0")).strip().lower() in ("1", "true", "yes", "on")
    try:
        from urirun_runtime.llm_runtime_loop import LlmRuntimeLoop, llm_runtime_control_enabled
        if llm_runtime_control_enabled() and not prep_mode:
            return _send_via_llm_runtime_loop(recipient, text, ticket=ticket, node=node)
    except Exception as exc:  # noqa: BLE001
        if str(os.environ.get("URIRUN_LLM_RUNTIME_CONTROL", "1")).strip().lower() not in ("0", "false", "no", "off"):
            return _kvm_fail(node or "lenovo", error="llm_runtime_loop_failed", detail=str(exc)[:200])
    if not prep_mode:
        return _send_signal_gui_scripted(recipient, text, ticket=ticket, node=node)

    errors: list[str] = []
    import time

    rec = recipient or DEFAULT_RECIPIENT

    # === OBSERVE + FOCUS FIRST (before TTS or any keyboard input) ===
    gui_ready = _ensure_gui_ready_for_signal(node, ticket=ticket)
    if not gui_ready.get("ok"):
        print("[SAFETY] Signal not visible/focused after observe+focus. Aborting before any input.")
        return _kvm_fail(node, error="signal_not_focused", observe=gui_ready.get("observe"))

    # Announce only after we know Signal is foreground (detached shell — never kvm-type curl)
    print(f"[{node}-tts] Mówię na {node}: start (po observe+focus Signal)")
    try:
        _speak_on_node(node, f"Rozpoczynam wysyłanie do {rec} na Signal", lang="pl", method="auto")
    except Exception:
        pass

    # === MANDATORY KNOWLEDGE PREPARATION + VALIDATION (triple LLM + executor twin) ===
    # Executor + Twin compared; Validator + Teacher approve. Plan from LLM.
    prep = prepare_and_validate_for_signal_kvm(recipient=rec, ticket=ticket, node=node)
    if not prep.get("ok"):
        print("[SAFETY] Preparation/validation did not pass. Aborting real KVM execution.")
        try:
            _speak_on_node(node, "Walidacja wiedzy nie przeszła. Nie wykonuję automatyzacji.", lang="pl", method="auto")
        except Exception:
            pass
        return _kvm_fail(node, error="triple_llm_preparation_failed", preparation=prep,
                         executor_model=prep.get("executor_model"),
                         executor_twin_model=prep.get("executor_twin_model"),
                         validator_model=prep.get("validator_model"))
    print("[SAFETY] Triple-LLM + Executor Twin comparison passed. Proceeding with real KVM actions.\n")

    # Get plan from prep (LLM-generated sequence of URI + payload json — this is the source of truth for "how")
    plan = prep.get("plan", []) or []
    if not plan:
        print("[WARNING] No plan from LLM prep, using ultra-minimal defensive fallback (LLM should have provided full)")
        plan = [
            {"id": "focus", "uri": f"kvm://{KVM_URI_HOST}/window/command/focus", "payload": {"title": "Signal"}},
            {"id": "probe-defensive", "uri": f"kvm://{KVM_URI_HOST}/input/command/type", "payload": {"text": "TST"}},
            {"id": "verify-probe", "uri": f"kvm://{KVM_URI_HOST}/ui/query/verify", "payload": {"text": "TST"}},
            {"id": "clear-bad", "uri": f"kvm://{KVM_URI_HOST}/input/command/key", "payload": {"keys": "escape,ctrl+a,BackSpace"}},
            {"id": "type-msg", "uri": f"kvm://{KVM_URI_HOST}/input/command/type", "payload": {"text": text}},
            {"id": "send", "uri": f"kvm://{KVM_URI_HOST}/input/command/key", "payload": {"keys": "Return"}},
            {"id": "verify-effect", "uri": f"kvm://{KVM_URI_HOST}/ui/query/verify", "payload": {"text": text[:30]}},
        ]

    print("[SAFETY] Triple-LLM passed. Execution → LLM runtime loop (initial plan + gap catch-up).\n")
    result = _send_via_llm_runtime_loop(rec, text, ticket=ticket, node=node, initial_plan=plan)
    for key in ("executor_model", "executor_twin_model", "failover_triggered", "teacher_escalated", "teacher_improvement"):
        if key in prep and key not in result:
            result[key] = prep[key]
    result.setdefault("executor_model", prep.get("executor_model", _llm_model()))
    result.setdefault("executor_twin_model", prep.get("executor_twin_model", _llm_executor_twin_model()))
    result.setdefault("failover_triggered", False)
    result.setdefault("teacher_escalated", False)
    result.setdefault("teacher_improvement", None)
    if not result.get("ok"):
        try:
            inq = _node_run(node, "inquiry://host/case/command/create", {
                "ticket": ticket or "unknown",
                "symptom": "signal-kvm-send-not-verified",
                "rootcause": "effect or verify failed after plan",
                "evidence": {
                    "sent_verify": result.get("sent_verify"),
                    "capture": result.get("capture"),
                    "plan": plan,
                },
                "observation": result.get("decision_loop", {}).get("observation") if isinstance(result.get("decision_loop"), dict) else None,
                "teacher_improvement": result.get("teacher_improvement"),
            }, timeout=8)
            print(f"[INQUIRY real] {inq}")
            refl = _node_run(node, "reflection://host/evaluate", {
                "ticket": ticket,
                "outcome": {"success": result.get("ok"), "verified": result.get("verified"), "effect": result.get("effect")},
                "observation": result.get("decision_loop", {}).get("observation") if isinstance(result.get("decision_loop"), dict) else None,
                "nextIntent": result.get("decision_loop", {}).get("nextIntent") if isinstance(result.get("decision_loop"), dict) else None,
            }, timeout=8)
            print(f"[REFLECTION real] {refl}")
            result["inquiry"] = inq
            result["reflection"] = refl
        except Exception as e:
            print(f"[CONTINUOUS] real inquiry/reflection call error: {e}")
    return result

    # --- legacy reactive micro-loop below (unreachable when prep uses llm-runtime-loop) ---
    diagnose = _node_run(node, "router://host/plan/query/diagnose", {
        "intent": {"id": "signal-send-gui-kvm", "recipient": rec, "ticket": ticket, "node": node},
        "flow": {"task": {"id": "send-signal-desktop"}, "steps": plan},
        "twin_state": {"screen": "1920x1080", "node": node},
        "ticket": ticket,
    }, timeout=10)
    print(f"[ROUTER DIAGNOSE] {diagnose}")
    if not (diagnose.get("ok") or diagnose.get("accepted") or diagnose.get("routable") or diagnose.get("status") == "ok"):
        print("[SAFETY] Router diagnose failed or blocked. Aborting.")
        return _kvm_fail(node, error="router_diagnose_failed", diagnose=diagnose, plan=plan)

    # REACTIVE EXECUTION: use initial plan as guide, but let LLM pick next URI at each small step from FULL registry.
    # FAILOVER: primary executor (LLM_MODEL_EXECUTOR) gets first attempt; if it doesn't reach a verified
    # effect, the twin executor (LLM_MODEL_EXECUTOR_TWIN) takes over for a second bounded attempt on the
    # SAME task, continuing the same timeline — it may ground the UI differently and succeed where the
    # primary didn't. Both attempts' outcomes are recorded so _get_best_executor_from_history() accumulates
    # real signal over time instead of staying empty.
    timeline = []
    know = _get_uri_processes_knowledge(node)

    def _verify_now() -> bool:
        v = _node_run(node, f"kvm://{KVM_URI_HOST}/ui/query/verify", {"text": text[:40]}, timeout=8)
        return _extract_present(v)

    def _run_micro_attempt(model: str, budget: int) -> bool:
        """Run up to `budget` micro decide/execute/verify steps using `model`. Returns final verified bool."""
        for micro in range(budget):
            obs = ""
            try:
                lowp = _capture_lowres(node, 320)[0]
                qp = _capture_quad(node, 2, 480)[0]
                loc = _node_run(node, f"kvm://{KVM_URI_HOST}/ui/query/locate", _locate_payload(rec[:4]), timeout=4)
                ver = _node_run(node, f"kvm://{KVM_URI_HOST}/ui/query/verify", {"text": rec}, timeout=4)
                obs = f"lowres={lowp} quad={qp} locate_matches={loc.get('count',0)} verify={ver} recent={ [t.get('step') for t in timeline[-2:]] }"
            except Exception as e:
                obs = f"obs-err:{e}"

            dec = _llm_decide_next(obs, know, node, model=model)
            u = dec.get("uri", "")
            p = dec.get("payload", {})
            if not u or u == "done":
                print(f"[MICRO {model}] LLM says done or no uri")
                break
            print(f"[MICRO-DECIDE {model} {micro}] {u} {p} | {dec.get('reason','')}")

            r = _guarded_node_run(node, u, p, timeout=10, ticket=ticket)
            if r.get("error") == "signal_not_focused":
                print(f"  [GUARD] blocked keyboard action — Signal not focused: {u}")
                timeline.append({"micro": micro, "model": model, "uri": u, "blocked": True, "observe": r.get("observe")})
                break
            timeline.append({"micro": micro, "model": model, "uri": u, "payload": p, "result": r, "reason": dec.get("reason")})
            print(f"  exec-res: {str(r)[:100]}")

            try:
                cap_path = f"/tmp/decide-{micro}-{int(time.time())}.png"
                _node_run(node, f"kvm://{KVM_URI_HOST}/screen/query/capture", {"output": cap_path, "max_width": 480}, timeout=5)
                _save_llm_trace(ticket, f"decide-{model}-{micro}-capture", "capture", "", "", images=[cap_path])
            except Exception:
                pass

            time.sleep(0.2)
            if _verify_now():
                print(f"  [VERIFY OK] ({model})")
                return True
            _node_run(node, f"kvm://{KVM_URI_HOST}/input/command/key", {"keys": "escape,ctrl+a,BackSpace"}, timeout=4)
        return _verify_now()

    executor_model = _llm_model()
    executor_twin_model = _llm_executor_twin_model()

    # === 3-poziomowy łańcuch eskalacji ===
    # 1. EXECUTOR próbuje; VALIDATOR sprawdza, czy naprawdę wykonał zadanie (nie tylko verify=true).
    # 2. Jeśli VALIDATOR mówi FAIL -> EXECUTOR_TWIN próbuje od tego samego punktu.
    # 3. Jeśli VALIDATOR znów mówi FAIL -> TEACHER (specjalista wizji) diagnozuje i proponuje
    #    konkretną poprawę (inny URI/prompt/dekompozycję) zamiast kolejnej ślepej powtórki.
    t0 = time.time()
    verified = _run_micro_attempt(executor_model, budget=8)
    judge = _validator_judge_attempt(executor_model, timeline, verified, node, ticket)
    executor_used = executor_model
    failover_triggered = False
    teacher_escalated = False
    teacher_improvement = None
    print(f"[VALIDATOR] executor={executor_model} pass={judge['pass']} reason={judge['reason']}")
    _save_executor_outcome(executor_model, {"success": verified and judge["pass"], "verified": verified,
                                            "validator_pass": judge["pass"], "time": round(time.time() - t0, 2)})

    if not (verified and judge["pass"]):
        print(f"[FAILOVER] Primary executor ({executor_model}) nie przeszedł walidacji "
              f"(verified={verified}, validator_pass={judge['pass']}) — przejmuje twin "
              f"({executor_twin_model}), kontynuując tę samą linię czasu.")
        failover_triggered = True
        t1 = time.time()
        verified = _run_micro_attempt(executor_twin_model, budget=6)
        judge = _validator_judge_attempt(executor_twin_model, timeline, verified, node, ticket)
        print(f"[VALIDATOR] executor_twin={executor_twin_model} pass={judge['pass']} reason={judge['reason']}")
        executor_used = executor_twin_model if (verified and judge["pass"]) else executor_used
        _save_executor_outcome(executor_twin_model, {"success": verified and judge["pass"], "verified": verified,
                                                      "validator_pass": judge["pass"], "time": round(time.time() - t1, 2)})

        if not (verified and judge["pass"]):
            print(f"[ESCALATE-TEACHER] Executor i twin oboje nie przeszli walidacji — "
                  f"{_llm_teacher_model()} analizuje ślad i proponuje konkretną poprawę.")
            teacher_escalated = True
            teacher_improvement = _teacher_propose_improvement(timeline, node, ticket, text)
            print(f"[TEACHER PROPOSAL] {teacher_improvement}")

    # Final (quad, capture, speak, decision_loop)
    final_cap = _capture_quad(node, 2, 480)[0]
    sent_verify = _node_run(node, f"kvm://{KVM_URI_HOST}/ui/query/verify", {"text": text[:40]}, timeout=8)
    verified = verified or _extract_present(sent_verify)
    capture = _node_run(node, f"kvm://{KVM_URI_HOST}/screen/query/capture", {"output": f"/tmp/final-{int(time.time())}.png"}, timeout=8)
    if not verified:
        _node_run(node, f"kvm://{KVM_URI_HOST}/window/command/focus", {"title": "Signal"}, timeout=3)
        verified = _extract_present(_node_run(node, f"kvm://{KVM_URI_HOST}/ui/query/verify", {"text": text[:40]}, timeout=5))

    effect = bool(verified)
    ok = bool(effect)
    final_msg = f"Wiadomość do {rec} wysłana." if verified else "Nie udało się zweryfikować wysyłki."
    try:
        _speak_on_node(node, final_msg, lang="pl", method="auto")
    except Exception:
        pass

    # Always complete decision_loop with nextIntent (per validator)
    final_decision_loop = {
        "intent": {"id": "signal-send-gui-kvm", "recipient": rec, "ticket": ticket},
        "flow": {"steps": plan},
        "execution": {"status": "done" if ok else "failed", "timeline": timeline, "executor_used": executor_used,
                     "failover_triggered": failover_triggered, "teacher_escalated": teacher_escalated},
        "observation": {"kind": "send-verified" if verified else "send-failed", "verified": verified, "final_quad": final_cap},
        "nextIntent": {"uri": "task://host/ticket/command/ready" if ok else "inquiry://host/case/command/create", "status": "done" if ok else "retryable"},
    }

    result = {
        "ok": ok,
        "effect": effect,
        "executor": f"signal-gui-kvm-{node}",
        "executor_used": executor_used,
        "executor_model": executor_model,
        "executor_twin_model": executor_twin_model,
        "failover_triggered": failover_triggered,
        "teacher_escalated": teacher_escalated,
        "teacher_improvement": teacher_improvement,
        "recipient": recipient,
        "sent_via": f"{node} Signal Desktop (LLM plan)",
        "verified": verified,
        "capture": capture.get("path") or capture.get("output", ""),
        "sent_verify": sent_verify,
        "plan_used": plan,
        "timeline": timeline,
        "errors": errors or None,
        "decision_loop": final_decision_loop,
        "_meta": _kvm_meta(node, mode="llm-prep"),
    }

    # On failure: real inquiry:// + reflection:// calls (from registry, not comments)
    if not ok:
        try:
            inq = _node_run(node, "inquiry://host/case/command/create", {
                "ticket": ticket or "unknown",
                "symptom": "signal-kvm-send-not-verified",
                "rootcause": "effect or verify failed after plan",
                "evidence": {"sent_verify": sent_verify, "capture": result.get("capture"), "plan": plan},
                "observation": result.get("decision_loop", {}).get("observation"),
                # Gdy executor + twin oboje zawiedli, TEACHER-owa diagnoza/propozycja poprawy
                # (inny URI/prompt/dekompozycja) trafia tu jako konkretny artefakt do działania,
                # nie tylko log porażki.
                "teacher_improvement": teacher_improvement,
            }, timeout=8)
            print(f"[INQUIRY real] {inq}")
            refl = _node_run(node, "reflection://host/evaluate", {
                "ticket": ticket,
                "outcome": {"success": ok, "verified": verified, "effect": effect},
                "observation": result.get("decision_loop", {}).get("observation"),
                "nextIntent": result.get("decision_loop", {}).get("nextIntent"),
            }, timeout=8)
            print(f"[REFLECTION real] {refl}")
            result["inquiry"] = inq
            result["reflection"] = refl
        except Exception as e:
            print(f"[CONTINUOUS] real inquiry/reflection call error: {e}")

    return result


def deliver_signal(recipient: str = "", message: str = "", approved: bool = False, ticket: str | None = None, node: str = "lenovo") -> dict[str, Any]:
    """Proces dostawy signal.message.send. Kanał wykryty realnie (host cli LUB node GUI/KVM).
    Kanał gotowy + brak zgody → waiting_human na WYSYŁKĘ (recipient+treść), nie na link. Bez ticketów kodu."""
    from . import postcondition
    ch = signal_channel(node)
    if not ch["ready"]:
        return {"goal": "signal.message.send", "decision": "waiting_human", "reason": "no_signal_channel",
                "channel": ch, "required_human_action": [f"Zainstaluj signal-cli LUB zaloguj Signal Desktop na {node}"],
                "missing_only": "kanał Signal", "no_new_code_tickets_created": True}
    # KANAŁ GOTOWY (np. Signal Desktop zalogowany na lenovo) → jedyna brama to ZGODA na wysyłkę
    draft = {"recipient": recipient, "text": message, "published": False}
    if not (recipient and message):
        return {"goal": "signal.message.send", "decision": "waiting_human", "reason": "need_recipient_and_text",
                "channel": ch["channel"], "missing_only": "recipient + treść wiadomości",
                "hint": "Signal Desktop na lenovo zalogowany; podaj do kogo i co → wyślę przez KVM",
                "no_new_code_tickets_created": True}
    try:
        from urirun_connector_human_twin import core as twin
        assess = twin.assess({"id": "SIGNAL-E2E-001", "action": "signal.message.send",
                              "scope": {"recipients": [recipient]}, "risk": "medium",
                              "reversible": False, "message_draft_exists": True, "evidence": ["draft gotowy"]})
    except Exception:  # noqa: BLE001
        assess = {"decision": "escalate_to_human"}
    if not (approved or assess.get("decision") == "approve_delegated"):
        return {"goal": "signal.message.send", "decision": "waiting_human", "reason": "send_needs_approval",
                "draft": draft, "channel": ch["channel"], "delegation": assess.get("decision"),
                "next_uri_after_human": "approval://adam/ticket/command/approve (lub deliver-signal approved=true)",
                "missing_only": "zgoda na wysyłkę do realnej osoby", "no_new_code_tickets_created": True}
    # ZATWIERDZONE → wyślij realnym kanałem
    if ch["channel"] == "signal-gui-kvm":
        res = send_via_kvm(recipient, message, ticket=ticket, node=node)
    else:
        try:
            from urirun_connector_signal import core as sig
            res = sig.message_command_send(to=recipient, message=message)
        except Exception as exc:  # noqa: BLE001
            res = {"ok": False, "error": str(exc)}

    # Używamy pełnego kontraktu weryfikacji (wieloetapowy proces)
    # Proper postcondition validation for task correctness (per project patterns)
    # Use enforce to attach _verification and optionally strict gate
    res = postcondition.enforce(res, uri="signal://message/send", strict=False)
    verified = postcondition.classify(res, uri="signal://message/send")

    # Success only if we have evidence of the effect (text visible in chat)
    success = bool(res.get("ok")) and bool(res.get("verified") or res.get("effect"))
    if verified.get("violations"):
        success = False

    decision = "done" if success else ("failed_verify" if not res.get("verified") else "failed")

    # Record post-execution outcome for the executor used (for stats and continuous selection)
    # Extended with decision loop: observation, nextIntent
    try:
        used = res.get("executor") or "unknown"
        outcome = {
            "success": success,
            "verified": bool(res.get("verified")),
            "effect": res.get("effect"),
            "violations": verified.get("violations", []),
            "decision": decision,
            "time": None,
            "observation": {"kind": "send-verified" if res.get("verified") else "send-failed", "sent_verify": res.get("sent_verify")},
            "nextIntent": {"uri": "task://host/ticket/command/ready" if success else "inquiry://host/case/command/create", "status": "done" if success else "retryable"},
        }
        _save_executor_outcome(used, outcome)
    except Exception:
        pass

    # Continuous improvement & autonomy: real inquiry:// / reflection:// call (from registry) on the result
    if not success:
        try:
            sent_v = res.get("sent_verify") or res.get("decision_loop", {}).get("observation")
            cap = res.get("capture")
            inquiry_payload = {
                "ticket": ticket or "unknown",
                "symptom": "signal-send-failed",
                "rootcause": (verified or {}).get("violations") if isinstance(verified, dict) else decision,
                "evidence": {"sent_verify": sent_v, "capture": cap, "plan": res.get("plan_used")},
            }
            inq = _node_run(node, "inquiry://host/case/command/create", inquiry_payload, timeout=10)
            print(f"[INQUIRY] {inq}")
            try:
                from urirun_runtime.ticket_llm_context import save_llm_turn
                save_llm_turn(ticket, role="tool", phase="debugger",
                              content=json.dumps({"inquiry": inq, "payload": inquiry_payload}, ensure_ascii=False),
                              model="inquiry")
            except Exception:  # noqa: BLE001
                pass
            refl = _node_run(node, "reflection://host/evaluate", {
                "ticket": ticket,
                "outcome": {"success": success, "decision": decision},
                "observation": res.get("decision_loop", {}).get("observation"),
                "nextIntent": res.get("decision_loop", {}).get("nextIntent"),
            }, timeout=10)
            print(f"[REFLECTION] {refl}")
        except Exception as e:
            print(f"[CONTINUOUS] inquiry/reflection failed: {e}")

    # For autonomy, the caller (loop/twin) will only complete ticket if success
    # This is how correctness of GUI task execution is validated: postcondition + visual effect + provenance

    return {
        "goal": "signal.message.send",
        "decision": decision,
        "message_sent": success,
        "verified": bool(res.get("verified")),
        "recipient": recipient,
        "channel": ch["channel"],
        "result": res,
        "_verification": verified,
        "executor_chain": ["work://claim-next", f"signal://{ch['channel']}"],
        "observation": {"kind": "send-verified" if verified else "send-failed", "verified": verified},
        "nextIntent": {"uri": "task://host/ticket/command/ready" if success else "inquiry://host/case/command/create", "status": "done" if success else "retryable"},
    }


def _deliver_signal_legacy(recipient: str = "", message: str = "") -> dict[str, Any]:
    from . import postcondition
    draft = {"recipient": recipient, "text": message, "published": False}
    try:
        from urirun_connector_grants import core as g
        pol = g.policy_check("signal.message.send")
    except Exception:  # noqa: BLE001
        pol = {"mode": "approval"}
    try:
        from urirun_connector_human_twin import core as twin
        assess = twin.assess({"id": "SIGNAL-E2E-001", "action": "signal.message.send",
                              "scope": {"recipients": [recipient]}, "risk": "medium",
                              "reversible": False, "evidence": ["draft gotowy"]})
    except Exception:  # noqa: BLE001
        assess = {"decision": "escalate_to_human"}
    if assess.get("decision") not in ("approve_delegated",):
        return {"goal": "signal.message.send", "decision": "waiting_human", "reason": "send_needs_approval",
                "draft": draft, "policy": pol.get("mode"), "delegation": assess.get("decision"),
                "next_uri_after_human": "approval://adam/ticket/command/approve", "no_new_code_tickets_created": True}
    try:
        from urirun_connector_signal import core as sig
        res = sig.message_command_send(to=recipient, message=message)
    except Exception as exc:  # noqa: BLE001
        res = {"ok": False, "error": str(exc)}
    verified = postcondition.classify(res, uri="signal://message/send")
    return {"goal": "signal.message.send", "decision": "done" if res.get("ok") else "failed",
            "message_sent": bool(res.get("ok")), "recipient": recipient, "result": res,
            "_verification": verified, "executor_chain": ["work://claim-next", "signal://message/send"]}
