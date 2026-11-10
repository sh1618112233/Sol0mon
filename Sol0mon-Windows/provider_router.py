"""
Project Sol0mon — Provider Router (multi-provider inference with failover).

Routes generation requests through a tiered provider hierarchy:

    Tier 1: Cerebras  →  Tier 2: Groq  →  Tier 3: OpenRouter  →  Tier 4: Ollama

Each tier supports:
- Multiple API keys with rotation
- Health tracking (success/failure rates, cooldown periods)
- Automatic recovery after cooldown

Failover conditions:
- HTTP 429 (rate limit / queue_exceeded)
- Timeout
- Network failure
- Provider unavailable
- HTTP 5xx

NOT failed over:
- HTTP 400, 401, 403 (client errors)
- Authentication failures

Backward compatible: cerebras_client.generate() is refactored to delegate here.
"""

from __future__ import annotations

import os
import time
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Health tracking
# ---------------------------------------------------------------------------

HEALTH_THRESHOLD_FAILURES = 3  # consecutive failures to mark unhealthy
HEALTH_COOLDOWN_SECONDS = 60   # avoid unhealthy provider for this long
HEALTH_OBSERVATION_WINDOW = 60  # sliding window for success rate

FAILOVER_ERROR_PATTERNS = [
    "queue_exceeded",
    "429",
    "rate limit",
    "too many requests",
    "timeout",
    "timed out",
    "connection",
    "network",
    "unreachable",
    "unavailable",
    "service unavailable",
    "internal server error",
    "503",
    "502",
    "504",
]

NON_FAILOVER_PATTERNS = [
    "401",
    "403",
    "400",
    "authentication",
    "unauthorized",
    "forbidden",
    "invalid request",
    "malformed",
]


# ---------------------------------------------------------------------------
# Provider health dataclass
# ---------------------------------------------------------------------------


@dataclass
class ProviderHealth:
    """Tracks health state for a single provider."""

    name: str
    total_requests: int = 0
    total_successes: int = 0
    total_failures: int = 0
    consecutive_failures: int = 0
    last_failure: str = ""
    last_success: str = ""
    cooldown_until: float = 0.0  # epoch timestamp
    is_healthy: bool = True

    def record_success(self) -> None:
        self.total_requests += 1
        self.total_successes += 1
        self.consecutive_failures = 0
        self.last_success = datetime.now(timezone.utc).isoformat()
        self.is_healthy = True
        self.cooldown_until = 0.0

    def record_failure(self) -> None:
        self.total_requests += 1
        self.total_failures += 1
        self.consecutive_failures += 1
        self.last_failure = datetime.now(timezone.utc).isoformat()
        if self.consecutive_failures >= HEALTH_THRESHOLD_FAILURES:
            self.is_healthy = False
            self.cooldown_until = time.time() + HEALTH_COOLDOWN_SECONDS

    def check_recovery(self) -> bool:
        """If cooldown has expired, mark healthy again."""
        if not self.is_healthy and time.time() > self.cooldown_until:
            self.is_healthy = True
            self.consecutive_failures = 0
            return True
        return self.is_healthy

    @property
    def success_rate(self) -> float:
        if self.total_requests == 0:
            return 1.0
        return self.total_successes / self.total_requests

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "total_requests": self.total_requests,
            "total_successes": self.total_successes,
            "total_failures": self.total_failures,
            "consecutive_failures": self.consecutive_failures,
            "success_rate": round(self.success_rate, 3),
            "is_healthy": self.is_healthy,
            "last_failure": self.last_failure,
            "cooldown_remaining_s": max(0, self.cooldown_until - time.time()),
        }


# ---------------------------------------------------------------------------
# Provider health tracker (singleton)
# ---------------------------------------------------------------------------


class HealthTracker:
    """Thread-safe provider health state."""

    def __init__(self):
        self._lock = threading.Lock()
        self._health: dict[str, ProviderHealth] = {}

    def _get_unlocked(self, name: str) -> ProviderHealth:
        if name not in self._health:
            self._health[name] = ProviderHealth(name=name)
        return self._health[name]

    def get(self, name: str) -> ProviderHealth:
        with self._lock:
            return self._get_unlocked(name)

    def record_success(self, name: str) -> None:
        with self._lock:
            self._get_unlocked(name).record_success()

    def record_failure(self, name: str) -> None:
        with self._lock:
            self._get_unlocked(name).record_failure()

    def is_healthy(self, name: str) -> bool:
        with self._lock:
            h = self._get_unlocked(name)
            h.check_recovery()
            return h.is_healthy

    def all_health(self) -> dict[str, dict]:
        with self._lock:
            return {name: h.to_dict() for name, h in self._health.items()}


# Singleton
_health_tracker = HealthTracker()


def get_provider_health() -> dict[str, dict]:
    """Return health state for all providers."""
    return _health_tracker.all_health()


# ---------------------------------------------------------------------------
# Failover logic
# ---------------------------------------------------------------------------


def _should_failover(error: str) -> bool:
    """Determine if an error should trigger provider failover."""
    error_lower = error.lower()
    for pattern in NON_FAILOVER_PATTERNS:
        if pattern in error_lower:
            return False
    for pattern in FAILOVER_ERROR_PATTERNS:
        if pattern in error_lower:
            return True
    # Unknown errors → failover (conservative)
    return True


# ---------------------------------------------------------------------------
# Tiered provider generation
# ---------------------------------------------------------------------------


def _try_cerebras(prompt: str, module: str) -> str:
    """Attempt Cerebras (Tier 1)."""
    from cerebras_client import _cerebras_generate
    try:
        _health_tracker.get("cerebras").check_recovery()
        if not _health_tracker.is_healthy("cerebras"):
            raise RuntimeError("Cerebras is in cooldown")
        result = _cerebras_generate(prompt, module)
        _health_tracker.record_success("cerebras")
        return result
    except Exception as e:
        error_str = str(e)
        if _should_failover(error_str):
            _health_tracker.record_failure("cerebras")
            print(f"[PROVIDER ROUTER] Cerebras failed (will failover): {error_str[:120]}")
            raise
        # Non-failover errors propagate immediately
        _health_tracker.record_failure("cerebras")
        raise


def _try_groq(prompt: str, module: str) -> str:
    """Attempt Groq (Tier 2)."""
    from groq_client import generate as groq_generate, is_configured
    if not is_configured():
        raise RuntimeError("Groq not configured")
    try:
        _health_tracker.get("groq").check_recovery()
        if not _health_tracker.is_healthy("groq"):
            raise RuntimeError("Groq is in cooldown")
        result = groq_generate(prompt, module)
        _health_tracker.record_success("groq")
        return result
    except Exception as e:
        error_str = str(e)
        if _should_failover(error_str):
            _health_tracker.record_failure("groq")
            print(f"[PROVIDER ROUTER] Groq failed (will failover): {error_str[:120]}")
            raise
        _health_tracker.record_failure("groq")
        raise


def _try_openrouter(prompt: str, module: str) -> str:
    """Attempt OpenRouter (Tier 3)."""
    from openrouter_client import generate as openrouter_generate, is_configured
    if not is_configured():
        raise RuntimeError("OpenRouter not configured")
    try:
        _health_tracker.get("openrouter").check_recovery()
        if not _health_tracker.is_healthy("openrouter"):
            raise RuntimeError("OpenRouter is in cooldown")
        result = openrouter_generate(prompt, module)
        _health_tracker.record_success("openrouter")
        return result
    except Exception as e:
        error_str = str(e)
        if _should_failover(error_str):
            _health_tracker.record_failure("openrouter")
            print(f"[PROVIDER ROUTER] OpenRouter failed (will failover): {error_str[:120]}")
            raise
        _health_tracker.record_failure("openrouter")
        raise


def _try_ollama(prompt: str, module: str) -> str:
    """Attempt Ollama (Tier 4 — always last resort)."""
    from ollama_client import generate as ollama_generate
    try:
        _health_tracker.get("ollama").check_recovery()
        result = ollama_generate(prompt, module)
        _health_tracker.record_success("ollama")
        return result
    except Exception as e:
        _health_tracker.record_failure("ollama")
        raise


# ---------------------------------------------------------------------------
# Main router — the single entry point
# ---------------------------------------------------------------------------


def generate(prompt: str, module: str = "unknown") -> str:
    """Route a generation request through the provider hierarchy.

    Tries providers in order: Cerebras → Groq → OpenRouter → Ollama.
    Each provider is called with its own retry/key-rotation logic.
    On failover-eligible error, proceeds to the next tier.
    On terminal error (auth, bad request), raises immediately.
    On all-providers-failed, raises RuntimeError.

    This is THE entry point that cerebras_client.generate() now delegates to
    for backward compatibility.
    """
    providers = [
        ("Cerebras", _try_cerebras),
        ("Groq", _try_groq),
        ("OpenRouter", _try_openrouter),
        ("Ollama", _try_ollama),
    ]

    errors = []
    previous_name = None
    for name, fn in providers:
        if previous_name:
            print(f"[PROVIDER ROUTER] Failing over to {name}")
        print(f"[PROVIDER ROUTER] Attempting {name}")
        try:
            result = fn(prompt, module)
            print(f"[PROVIDER ROUTER] {name} succeeded")
            return result
        except Exception as e:
            error_str = str(e)
            errors.append(f"{name}: {error_str[:120]}")
            if not _should_failover(error_str):
                # Terminal error — don't bother trying other providers
                print(f"[PROVIDER ROUTER] Terminal error from {name}: {error_str[:120]}")
                raise
            previous_name = name

    # All providers exhausted
    raise RuntimeError(
        f"Provider router: all providers exhausted.\n"
        + "\n".join(f"  {e}" for e in errors)
    )


# ---------------------------------------------------------------------------
# Health reset (for testing)
# ---------------------------------------------------------------------------


def reset_health():
    """Reset all provider health state. Used in tests."""
    global _health_tracker
    _health_tracker = HealthTracker()


if __name__ == "__main__":
    # Quick diagnostic
    print("Provider Router — Health Report")
    print("=" * 40)
    health = get_provider_health()
    if health:
        for name, h in sorted(health.items()):
            print(f"  {name}: healthy={h['is_healthy']} "
                  f"success_rate={h['success_rate']} "
                  f"failures={h['total_failures']}")
    else:
        print("  No providers have been called yet.")

    # Test with a simple prompt if Cerebras is configured
    try:
        result = generate("Return exactly the word: OK", module="router_smoke_test")
        print(f"\nSmoke test: {result}")
    except Exception as e:
        print(f"\nSmoke test failed (expected without API keys): {e}")