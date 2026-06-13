"""
llm_scorer.py -- LLM-based filing and news scorer.

Calls the configured LLM (OpenAI or LM Studio) to score a filing or news
article on materiality and confidence based on actual content.

Returns neutral 0.5 scores when:
- No text is available
- The LLM call fails for any reason

Never raises -- all failures are logged and the neutral fallback is returned.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List, Optional

from equity_intel.logging_config import get_logger
from equity_intel.lmstudio_runtime import (
    note_model_usage,
    register_atexit_unload,
    wait_for_local_model_capacity,
)

logger = get_logger(__name__)

_LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai" if os.getenv("OPENAI_API_KEY") else "lmstudio").lower()
_BASE_URL = (
    "https://api.openai.com/v1"
    if _LLM_PROVIDER == "openai"
    else os.getenv("LMSTUDIO_BASE_URL", "http://127.0.0.1:1234/v1")
)
_MODEL = (
    os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    if _LLM_PROVIDER == "openai"
    else os.getenv("LMSTUDIO_MODEL", "qwen/qwen3-14b")
)
_API_KEY = os.getenv("OPENAI_API_KEY", "") if _LLM_PROVIDER == "openai" else "lm-studio"
_IDLE_TIMEOUT = int(os.getenv("LLM_TOKEN_IDLE_TIMEOUT_SECONDS", "60"))

note_model_usage(_MODEL)
register_atexit_unload("stocks-llm-scorer")

_NEUTRAL: Dict[str, Any] = {
    "materiality": 0.5,
    "confidence": 0.5,
    "sentiment": "neutral",
    "event_type_hint": None,
    "summary": None,
    "llm_scored": False,
}

_SYSTEM_PROMPT = (
    "You are a US equity financial analyst scoring a document for materiality and confidence.\n\n"
    "Given a document from an SEC filing or news article, return a JSON object with exactly these fields:\n\n"
    "{\n"
    '  "materiality": <float 0.0-1.0>,\n'
    '  "confidence": <float 0.0-1.0>,\n'
    '  "sentiment": <"bullish" | "bearish" | "neutral">,\n'
    '  "event_type_hint": <"earnings" | "guidance_raised" | "guidance_lowered" | '
    '"merger_acquisition" | "offering_or_dilution" | "management_change" | '
    '"regulatory" | "restatement" | "insider_transaction" | "other" | null>,\n'
    '  "summary": <one sentence max 120 chars>\n'
    "}\n\n"
    "Scoring guide:\n"
    "- materiality: how much does this move the investment thesis? 0=noise, 1=company-changing\n"
    "- confidence: how certain are you of the classification given the text? 0=vague/ambiguous, 1=crystal clear\n"
    "- sentiment: bullish=positive for shareholders, bearish=negative, neutral=no directional signal\n\n"
    "Return ONLY valid JSON. No markdown, no explanation."
)


def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _call_llm(text: str) -> Optional[Dict[str, Any]]:
    try:
        import requests
    except ImportError:
        logger.warning("llm_scorer_requests_missing")
        return None

    snippet = text[:4000]
    wait_for_local_model_capacity("stocks-llm-scorer", _MODEL)
    messages: List[Dict[str, str]] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": snippet},
    ]
    payload: Dict[str, Any] = {
        "model": _MODEL,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": 300,
        "stream": True,
        "response_format": {"type": "json_object"},
    }
    if _LLM_PROVIDER != "openai":
        payload["context_length"] = int(os.getenv("LMSTUDIO_CONTEXT", "8192"))
        payload["ttl"] = int(os.getenv("LMSTUDIO_TTL_SECONDS", "60"))

    headers = {"Authorization": f"Bearer {_API_KEY}"}
    url = f"{_BASE_URL}/chat/completions"

    try:
        resp = requests.post(url, headers=headers, json=payload, stream=True,
                             timeout=(10, _IDLE_TIMEOUT))
        resp.raise_for_status()
        chunks: List[str] = []
        last_token = time.monotonic()
        for raw in resp.iter_lines():
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
            if not line.startswith("data: "):
                continue
            data = line[6:]
            if data.strip() == "[DONE]":
                break
            try:
                delta = json.loads(data)["choices"][0]["delta"].get("content", "")
                if delta:
                    chunks.append(delta)
                    last_token = time.monotonic()
            except (json.JSONDecodeError, KeyError, IndexError):
                pass
            if time.monotonic() - last_token > _IDLE_TIMEOUT:
                logger.warning("llm_scorer_stream_idle_timeout", idle_s=_IDLE_TIMEOUT)
                return None
        raw_text = _strip_think("".join(chunks))
        return json.loads(raw_text)
    except Exception as exc:
        logger.warning("llm_scorer_call_failed", error=str(exc)[:200])
        return None


def score_document(
    text: Optional[str],
    ticker: str = "",
    source_type: str = "filing",
) -> Dict[str, Any]:
    """
    Score a document using the LLM.

    Parameters
    ----------
    text        : full or partial document text; None or empty = neutral 0.5 fallback
    ticker      : for logging only
    source_type : "filing" or "news"

    Returns
    -------
    dict with keys: materiality, confidence, sentiment, event_type_hint, summary, llm_scored
    Signal still flows on fallback -- never blocks on missing text.
    """
    if not text or not text.strip():
        logger.debug("llm_scorer_no_text", ticker=ticker, source_type=source_type)
        return dict(_NEUTRAL)

    t0 = time.monotonic()
    result = _call_llm(text)

    if result is None:
        logger.debug("llm_scorer_fallback_neutral", ticker=ticker)
        return dict(_NEUTRAL)

    def _clamp(v: Any) -> float:
        try:
            return max(0.0, min(1.0, float(v)))
        except (TypeError, ValueError):
            return 0.5

    out = {
        "materiality": _clamp(result.get("materiality", 0.5)),
        "confidence": _clamp(result.get("confidence", 0.5)),
        "sentiment": result.get("sentiment", "neutral"),
        "event_type_hint": result.get("event_type_hint"),
        "summary": result.get("summary"),
        "llm_scored": True,
    }
    elapsed = round(time.monotonic() - t0, 2)
    logger.info(
        "llm_scorer_ok",
        ticker=ticker,
        source_type=source_type,
        materiality=out["materiality"],
        confidence=out["confidence"],
        sentiment=out["sentiment"],
        elapsed_s=elapsed,
    )
    return out
