"""
Circuit breaker for the order executor.

Pure state machine — no I/O, no side effects.
States:
  CLOSED    — normal operation, orders flow through
  OPEN      — fault detected; submissions blocked; cooldown timer active
  HALF_OPEN — cooldown expired; one probe order allowed

Usage:
  cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=300)
  if cb.can_attempt():
      try:
          submit_order(...)
          cb.record_success()
      except Exception:
          cb.record_failure()
  else:
      raise RuntimeError("Circuit breaker is OPEN")
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class CircuitState(Enum):
    CLOSED    = "closed"
    OPEN      = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    failure_threshold: int   = 3
    cooldown_seconds:  int   = 300

    # mutable state — reset via reset()
    state:         CircuitState = field(default=CircuitState.CLOSED)
    failure_count: int          = field(default=0)
    opened_at:     float | None = field(default=None)

    # ── public API ────────────────────────────────────────────────────────────

    def can_attempt(self) -> bool:
        """Return True if an order submission should be attempted right now."""
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            if self._cooldown_elapsed():
                self.state = CircuitState.HALF_OPEN
                return True
            return False
        # HALF_OPEN — one probe allowed; next call will block until success/failure recorded
        return True

    def record_success(self) -> None:
        """Call after a successful order fill."""
        self.failure_count = 0
        self.opened_at     = None
        self.state         = CircuitState.CLOSED

    def record_failure(self) -> None:
        """Call after a failed order fill attempt."""
        self.failure_count += 1
        if self.state == CircuitState.HALF_OPEN:
            # Probe failed — reopen
            self._open()
        elif self.failure_count >= self.failure_threshold:
            self._open()

    def is_open(self) -> bool:
        """True when the breaker is blocking submissions (OPEN state after cooldown check)."""
        return not self.can_attempt()

    def reset(self) -> None:
        """Manually reset to CLOSED (e.g., after operator intervention)."""
        self.state         = CircuitState.CLOSED
        self.failure_count = 0
        self.opened_at     = None

    # ── private helpers ───────────────────────────────────────────────────────

    def _open(self) -> None:
        self.state     = CircuitState.OPEN
        self.opened_at = time.monotonic()

    def _cooldown_elapsed(self) -> bool:
        if self.opened_at is None:
            return True
        return (time.monotonic() - self.opened_at) >= self.cooldown_seconds
