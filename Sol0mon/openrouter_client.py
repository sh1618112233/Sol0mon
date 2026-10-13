"""
Project Sol0mon — OpenRouter API Client (Tier 3 Provider).

Supports:
- Multiple API keys with rotation on failure
- Multiple model fallback (ordered — first working model wins)
- Permanent failure after all keys AND models exhausted
- OpenRouter-specific headers (HTTP-Referer, X-Title)
- Configured via .env: OPENROUTER_API_KEYS, OPENROUTER_MODELS

Verified working FREE models (June 2026):
  - google/gemini-2.5-flash              (fast, free, high quality)
  - google/gemini-2.5-flash-lite         (ultra-fast free)
  - qwen/qwen-2.5-72b-instruct           (72B, fast)
  - deepseek/deepseek-chat-v3-0324       (strong reasoning)
  - meta-llama/llama-3.3-70b-instruct    (70B Llama)
  - meta-llama/llama-3.1-8b-instruct     (8B ultra-fast)
  - mistralai/mistral-small-3.1-24b-instruct (Mistral 24B)
  - qwen/qwen3-32b                       (Qwen 3 32B)
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
_KEYS_RAW = os.getenv("OPENROUTER_API_KEYS", "")
API_KEYS = [k.strip() for k in _KEYS_RAW.split(",") if k.strip()]

# Model fallback list — tried in order
_MODELS_RAW = os.getenv("OPENROUTER_MODELS", "")
DEFAULT_MODELS = [
    "google/gemini-2.5-flash",            # Primary — fast, free, high-quality
    "deepseek/deepseek-chat-v3-0324",     # Fallback 1 — strong reasoning (free)
    "meta-llama/llama-3.3-70b-instruct",  # Fallback 2 — Llama 70B (free)
    "qwen/qwen-2.5-72b-instruct",         # Fallback 3 — Qwen 72B
    "mistralai/mistral-small-3.1-24b-instruct",  # Fallback 4 — Mistral 24B
    "google/gemini-2.5-flash-lite",       # Fallback 5 — ultra-fast
    "meta-llama/llama-3.1-8b-instruct",   # Fallback 6 — 8B fastest
]
if _MODELS_RAW.strip():
    MODEL_LIST = [m.strip() for m in _MODELS_RAW.split(",") if m.strip()]
else:
    MODEL_LIST = DEFAULT_MODELS

# Primary model (backward compat)
MODEL = MODEL_LIST[0] if MODEL_LIST else "google/gemini-2.5-flash"

MAX_RETRIES = 3
REQUEST_TIMEOUT = 120
OPENROUTER_BASE = "https://openrouter.ai/api/v1"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


def _get_client(api_key: str) -> Any:
    """Lazy-import OpenAI client pointed at OpenRouter."""
    from openai import OpenAI
    return OpenAI(
        base_url=OPENROUTER_BASE,
        api_key=api_key,
        timeout=REQUEST_TIMEOUT,
    )


def _try_generate_with_key_and_model(client: Any, model: str, prompt: str,
                                      module: str) -> str:
    """Single attempt with a specific key+model combination."""
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        extra_headers={
            "HTTP-Referer": "https://github.com/sol0mon",
            "X-Title": "Project Sol0mon",
        },
    )
    raw = response.choices[0].message.content

    if not raw or not raw.strip():
        raise RuntimeError("Empty response from OpenRouter")

    output = raw.strip()
    if output.startswith("```json"):
        output = output[7:]
    if output.startswith("```"):
        output = output[3:]
    if output.endswith("```"):
        output = output[:-3]
    output = output.strip()

    if not output:
        raise RuntimeError("OpenRouter response collapsed to empty after stripping")

    # Detect thinking-mode outputs
    if output.lower().startswith("thinking"):
        raise RuntimeError(
            "OpenRouter thinking-mode model detected — use a non-reasoning model"
        )

    return output


# ---------------------------------------------------------------------------
# Main generate function
# ---------------------------------------------------------------------------


def generate(prompt: str, module: str = "unknown",
             model: str | None = None) -> str:
    """Generate a completion via OpenRouter with key rotation AND model fallback.

    Algorithm: try each key → for each key, try each model.
    Model-decommissioned errors skip immediately to the next model.
    Rate-limit/network errors retry with backoff before moving on.

    Args:
        prompt: The prompt to send.
        module: Identifier for logging.
        model: Optional model override (uses MODEL_LIST if None).
    """
    if not API_KEYS:
        raise RuntimeError(
            "OPENROUTER_API_KEYS environment variable is missing or empty. "
            "Set it in .env to use OpenRouter as a fallback provider."
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
                        f"[OPENROUTER SUCCESS] Model={m} Module={module} "
                        f"Duration={duration:.2f}s ResponseLength={len(output)}",
                        flush=True,
                    )
                    return output

                except Exception as e:
                    last_error = str(e)
                    err_lower = last_error.lower()

                    # Model-specific errors: skip immediately
                    if ("no endpoints found" in err_lower or
                        "model_not_found" in err_lower or
                        "does not exist" in err_lower):
                        print(
                            f"[OPENROUTER MODEL SKIP] Model={m} — {last_error[:120]}",
                            flush=True,
                        )
                        break

                    # Paid-only models: skip immediately
                    if "402" in last_error or "insufficient" in err_lower:
                        print(
                            f"[OPENROUTER MODEL SKIP] Model={m} is PAID-ONLY — "
                            f"{last_error[:120]}",
                            flush=True,
                        )
                        break

                    print(
                        f"[OPENROUTER FAILURE] Model={m} Module={module} "
                        f"Attempt={attempt}/{MAX_RETRIES} Key={key[:8]}... "
                        f"Error={last_error[:150]}",
                        flush=True,
                    )

                    if attempt < MAX_RETRIES:
                        time.sleep(2 * attempt)

    raise RuntimeError(
        f"OpenRouter client exhausted all keys ({len(API_KEYS)}) and models "
        f"({len(models_to_try)}). Last error: {last_error}"
    )


def is_configured() -> bool:
    """Returns True if OpenRouter has at least one API key configured."""
    return len(API_KEYS) > 0


def list_available_models() -> list[str]:
    """Return the list of configured model IDs."""
    return list(MODEL_LIST)


if __name__ == "__main__":
    print(f"OpenRouter Client — keys: {len(API_KEYS)}, models: {MODEL_LIST}")
    if is_configured():
        result = generate("Return exactly the word: OK", module="smoke_test")
        print(f"SMOKE TEST: {result}")
    else:
        print("No OPENROUTER_API_KEYS configured — skipping smoke test")