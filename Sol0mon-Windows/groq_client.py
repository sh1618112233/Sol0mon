"""
Project Sol0mon — Groq API Client (Tier 2 Provider).

Supports:
- Multiple API keys with rotation on failure
- Multiple model fallback (ordered — first working model wins)
- Permanent failure after all keys AND models exhausted
- Configured via .env: GROQ_API_KEYS, GROQ_MODELS (comma-separated)

Verified working models (June 2026):
  - llama-3.3-70b-versatile   (70B, production, fast)
  - llama-3.1-8b-instant       (8B, ultra-fast cheap backup)

DECOMMISSIONED (DO NOT USE):
  - llama-3.3-70b-specdec
  - llama-3.2-1b-preview
  - llama-3.2-3b-preview
"""

from __future__ import annotations

import os
import time
from typing import Any

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------
_KEYS_RAW = os.getenv("GROQ_API_KEYS", "")
API_KEYS = [k.strip() for k in _KEYS_RAW.split(",") if k.strip()]

# Model fallback list — try in order, first one that works is used
_MODELS_RAW = os.getenv("GROQ_MODELS", "")
DEFAULT_MODELS = [
    "llama-3.3-70b-versatile",   # Primary — 70B production model
    "llama-3.1-8b-instant",      # Fallback — 8B ultra-fast, cheap
]
# Allow override via GROQ_MODELS env var (comma-separated)
if _MODELS_RAW.strip():
    MODEL_LIST = [m.strip() for m in _MODELS_RAW.split(",") if m.strip()]
else:
    MODEL_LIST = DEFAULT_MODELS

# Primary model (for backward compat)
MODEL = MODEL_LIST[0] if MODEL_LIST else "llama-3.3-70b-versatile"

MAX_RETRIES = 3
REQUEST_TIMEOUT = 120


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


def _get_client(api_key: str) -> Any:
    """Lazy-import Groq client for a specific key."""
    from groq import Groq
    return Groq(api_key=api_key, timeout=REQUEST_TIMEOUT)


def _try_generate_with_key_and_model(client: Any, model: str, prompt: str,
                                      module: str) -> str:
    """Single attempt with a specific key+model combination.

    Returns the cleaned output string on success.
    Raises on any failure (retried by caller).
    """
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
    )
    raw = response.choices[0].message.content

    if not raw or not raw.strip():
        raise RuntimeError("Empty response from Groq")

    output = raw.strip()
    # Strip markdown fences
    if output.startswith("```json"):
        output = output[7:]
    if output.startswith("```"):
        output = output[3:]
    if output.endswith("```"):
        output = output[:-3]
    output = output.strip()

    if not output:
        raise RuntimeError("Groq response collapsed to empty after stripping")

    # Detect thinking-mode models (e.g. qwen with thinking tags)
    if output.lower().startswith("thinking"):
        raise RuntimeError(
            "Groq thinking-mode model detected — model output starts with 'thinking'. "
            "Use a non-reasoning model for structured output."
        )

    return output


# ---------------------------------------------------------------------------
# Main generate function — key rotation + model fallback
# ---------------------------------------------------------------------------


def generate(prompt: str, module: str = "unknown",
             model: str | None = None) -> str:
    """Generate a completion via Groq with key rotation AND model fallback.

    Algorithm:
      1. Try each key in order
      2. For each key, try each model in MODEL_LIST (or override)
      3. If a model fails with a model-specific error (decommissioned, not found),
         continue to next model
      4. If a model fails with a rate-limit or network error, retry with backoff
      5. If all key×model combinations fail, raise RuntimeError

    Args:
        prompt: The prompt to send.
        module: Identifier for logging.
        model: Optional model override (uses MODEL_LIST if None).

    Raises:
        RuntimeError: All keys and models exhausted.
    """
    if not API_KEYS:
        raise RuntimeError(
            "GROQ_API_KEYS environment variable is missing or empty. "
            "Set it in .env to use Groq as a fallback provider."
        )

    models_to_try = [model] if model else MODEL_LIST
    last_error = None

    for key in API_KEYS:
        client = _get_client(key)

        for m in models_to_try:
            for attempt in range(1, MAX_RETRIES + 1):
                start = time.time()
                try:
                    output = _try_generate_with_key_and_model(client, m, prompt, module)
                    duration = time.time() - start
                    print(
                        f"[GROQ SUCCESS] Model={m} Module={module} "
                        f"Duration={duration:.2f}s ResponseLength={len(output)}",
                        flush=True,
                    )
                    return output

                except Exception as e:
                    last_error = str(e)
                    err_lower = last_error.lower()

                    # Decommissioned/not-found models: skip immediately
                    if "decommissioned" in err_lower or "model_not_found" in err_lower or \
                       "does not exist" in err_lower:
                        print(
                            f"[GROQ MODEL SKIP] Model={m} — {last_error[:120]}",
                            flush=True,
                        )
                        break  # Skip this model, try next

                    print(
                        f"[GROQ FAILURE] Model={m} Module={module} "
                        f"Attempt={attempt}/{MAX_RETRIES} Key={key[:8]}... "
                        f"Error={last_error[:150]}",
                        flush=True,
                    )

                    if attempt < MAX_RETRIES:
                        time.sleep(2 * attempt)

    raise RuntimeError(
        f"Groq client exhausted all keys ({len(API_KEYS)}) and models "
        f"({len(models_to_try)}). Last error: {last_error}"
    )


def is_configured() -> bool:
    """Returns True if Groq has at least one API key configured."""
    return len(API_KEYS) > 0


def list_available_models() -> list[str]:
    """Return the list of configured model IDs."""
    return list(MODEL_LIST)


if __name__ == "__main__":
    print(f"Groq Client — keys: {len(API_KEYS)}, models: {MODEL_LIST}")
    if is_configured():
        result = generate("Return exactly the word: OK", module="smoke_test")
        print(f"SMOKE TEST: {result}")
    else:
        print("No GROQ_API_KEYS configured — skipping smoke test")