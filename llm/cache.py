"""
PostgreSQL-backed LLM response cache with TTL.

Status: stub — to be implemented in Phase 4.
"""
from __future__ import annotations


class LLMCache:
    def get(self, prompt_hash: str) -> dict | None:
        raise NotImplementedError("LLMCache will be implemented in Phase 4")

    def set(self, prompt_hash: str, response: dict, ttl_seconds: int = 3600) -> None:
        raise NotImplementedError("LLMCache will be implemented in Phase 4")
