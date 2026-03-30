"""
Parse LLM output into a structured LLMEstimate.

Expects the fixed 5-line format produced by the prompt templates:
    PROBABILITY: 0.73
    CONFIDENCE: HIGH
    EDGE: +0.08
    REASONING: ...
    SOURCES: reuters, coindesk

Raises ValueError if the format is invalid — never returns None silently.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Literal

from config.schemas import LLMEstimate

# Map text labels to float confidence levels
_CONFIDENCE_MAP: dict[str, float] = {
    "low":    0.33,
    "medium": 0.66,
    "high":   1.00,
}

_PROB_RE   = re.compile(r"PROBABILITY\s*:\s*([\d.]+)", re.IGNORECASE)
_CONF_RE   = re.compile(r"CONFIDENCE\s*:\s*(\w+)", re.IGNORECASE)
_EDGE_RE   = re.compile(r"EDGE\s*:\s*([+-]?[\d.]+)", re.IGNORECASE)
_REASON_RE = re.compile(r"REASONING\s*:\s*(.+?)(?=\nSOURCES|\Z)", re.IGNORECASE | re.DOTALL)
_SOURCES_RE = re.compile(r"SOURCES\s*:\s*(.+)", re.IGNORECASE)


def prompt_hash(prompt_text: str) -> str:
    """Return a 16-char hex hash of the prompt for cache keying."""
    return hashlib.sha256(prompt_text.encode()).hexdigest()[:16]


def parse_response(
    raw_text: str,
    condition_id: str,
    model: str,
    prompt_text: str = "",
) -> LLMEstimate:
    """Parse a raw LLM response string into an LLMEstimate.

    Args:
        raw_text: The raw text returned by the LLM.
        condition_id: Market condition ID (stored in the estimate).
        model: Model identifier used for the call.
        prompt_text: Original prompt (used to compute prompt_hash).

    Returns:
        LLMEstimate with validated fields.

    Raises:
        ValueError: If PROBABILITY or CONFIDENCE fields cannot be parsed,
                    or if probability is outside [0, 1].
    """
    # --- PROBABILITY ---
    prob_match = _PROB_RE.search(raw_text)
    if not prob_match:
        raise ValueError(f"Could not parse PROBABILITY from LLM response:\n{raw_text[:300]}")
    probability = float(prob_match.group(1))
    if not 0.0 <= probability <= 1.0:
        raise ValueError(f"Parsed probability {probability} is outside [0, 1]")

    # --- CONFIDENCE ---
    conf_match = _CONF_RE.search(raw_text)
    if not conf_match:
        raise ValueError(f"Could not parse CONFIDENCE from LLM response:\n{raw_text[:300]}")
    conf_label = conf_match.group(1).strip().lower()
    confidence = _CONFIDENCE_MAP.get(conf_label)
    if confidence is None:
        raise ValueError(f"Unknown CONFIDENCE value '{conf_label}' — expected LOW/MEDIUM/HIGH")

    # --- REASONING (optional but expected) ---
    reason_match = _REASON_RE.search(raw_text)
    reasoning = reason_match.group(1).strip() if reason_match else None

    # --- SOURCES (optional) ---
    sources: list[str] | None = None
    src_match = _SOURCES_RE.search(raw_text)
    if src_match:
        raw_sources = src_match.group(1).strip()
        if raw_sources.upper() != "NONE":
            sources = [s.strip() for s in raw_sources.split(",") if s.strip()]

    return LLMEstimate(
        condition_id=condition_id,
        model=model,
        prompt_hash=prompt_hash(prompt_text) if prompt_text else "unknown",
        probability=round(probability, 4),
        confidence=confidence,
        reasoning=reasoning,
        sources=sources,
    )


class ResponseParser:
    """Stateless wrapper for DI compatibility."""

    def parse(self, raw_response: str, condition_id: str = "", model: str = "", prompt_text: str = "") -> dict:
        """Parse and return estimate as dict (legacy interface)."""
        estimate = parse_response(raw_response, condition_id, model, prompt_text)
        return estimate.model_dump()
