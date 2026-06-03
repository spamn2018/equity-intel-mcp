from __future__ import annotations

import atexit
import os
import subprocess
import time
from threading import Lock
from typing import Iterable


_PROVIDER = os.getenv(
    "LLM_PROVIDER",
    "openai" if os.getenv("OPENAI_API_KEY") else "lmstudio",
).lower()
_LMS_CLI = os.getenv("LMS_CLI", r"C:\Users\noleg\.lmstudio\bin\lms.exe")
_OLLAMA_CLI = os.getenv("OLLAMA_CLI", "ollama")
_LOCAL_MODEL_BLOCK_AT_OR_ABOVE = int(os.getenv("LOCAL_MODEL_BLOCK_AT_OR_ABOVE", "2"))
_LOCAL_MODEL_RETRY_SECONDS = float(os.getenv("LMSTUDIO_MODEL_RETRY_SECONDS", "600"))
_UNLOAD_REGISTERED = False
_TRACKED_MODELS: set[str] = set()
_LOCK = Lock()


def _is_lmstudio() -> bool:
    return _PROVIDER == "lmstudio"


def _cli_available() -> bool:
    return _is_lmstudio() and os.path.isfile(_LMS_CLI)


def note_model_usage(model: str | None) -> None:
    if not _is_lmstudio() or not model:
        return
    model_id = model.strip()
    if not model_id:
        return
    with _LOCK:
        _TRACKED_MODELS.add(model_id)


def _loaded_model_count(command: list[str], header_prefix: str) -> int:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except Exception:
        return 0
    if result.returncode != 0:
        return 0
    count = 0
    for line in (result.stdout or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(header_prefix):
            continue
        count += 1
    return count


def wait_for_local_model_capacity(project: str, model: str) -> None:
    if _PROVIDER not in {"lmstudio", "ollama"}:
        return
    while True:
        lmstudio_loaded = _loaded_model_count([_LMS_CLI, "ps"], "IDENTIFIER")
        ollama_loaded = _loaded_model_count([_OLLAMA_CLI, "ps"], "NAME")
        total_loaded = lmstudio_loaded + ollama_loaded
        if total_loaded < _LOCAL_MODEL_BLOCK_AT_OR_ABOVE:
            return
        print(
            f"[local-model-capacity] {project}:{model} waiting 10m — "
            f"{lmstudio_loaded} LM Studio + {ollama_loaded} Ollama loaded "
            f"(block at {_LOCAL_MODEL_BLOCK_AT_OR_ABOVE})."
        )
        time.sleep(_LOCAL_MODEL_RETRY_SECONDS)


def unload_tracked_models(reason: str = "") -> None:
    if not _cli_available():
        return
    with _LOCK:
        models = sorted(_TRACKED_MODELS)
    if not models:
        return
    unload_models(models, reason=reason)


def unload_models(models: Iterable[str], reason: str = "") -> None:
    if not _cli_available():
        return
    for model in list(dict.fromkeys(m for m in models if m)):
        try:
            subprocess.run(
                [_LMS_CLI, "unload", model],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except Exception:
            continue
    if reason:
        _ = reason


def register_atexit_unload(reason: str = "") -> None:
    global _UNLOAD_REGISTERED
    if not _is_lmstudio() or _UNLOAD_REGISTERED:
        return

    def _cleanup() -> None:
        unload_tracked_models(reason=reason)

    atexit.register(_cleanup)
    _UNLOAD_REGISTERED = True
