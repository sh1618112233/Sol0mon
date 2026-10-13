"""
Project Sol0mon — Cerebras API Client (Tier 1 Provider).

Backward-compatible wrapper around provider_router.
All existing `from cerebras_client import generate` imports continue to work.

Cerebras available models (all FREE on public endpoints, rate-limited):
  - zai-glm-4.7        (355B MoE) — primary, in .env as CEREBRAS_MODEL
  - gpt-oss-120b       (120B) — production, fallback
  - gemma-4-31b        (31B) — preview/coming soon

The provider router now delegates through: Cerebras → Groq → OpenRouter → Ollama
"""

import os
import time
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Cerebras configuration
# ---------------------------------------------------------------------------
CEREBRAS_MODEL = os.getenv("CEREBRAS_MODEL", "zai-glm-4.7")

# Model fallback list for within-Cerebras (before Groq takes over)
_CEREBRAS_MODELS_RAW = os.getenv("CEREBRAS_MODELS", "")
CEREBRAS_MODEL_LIST = (
    [m.strip() for m in _CEREBRAS_MODELS_RAW.split(",") if m.strip()]
    if _CEREBRAS_MODELS_RAW.strip()
    else ["zai-glm-4.7", "gpt-oss-120b"]  # Default: try GLM first, then GPT-OSS
)

MAX_RETRIES = 3
REQUEST_TIMEOUT = 120
_client = None


def _get_client():
    """Lazy client construction for direct Cerebras calls."""
    global _client
    if _client is None:
        from cerebras.cloud.sdk import Cerebras
        api_key = os.getenv("CEREBRAS_API_KEYS")
        if not api_key:
            raise RuntimeError(
                "CEREBRAS_API_KEYS environment variable is missing or empty."
            )
        _client = Cerebras(api_key=api_key, timeout=REQUEST_TIMEOUT)
    return _client


def _cerebras_generate(prompt, module="unknown", model=None):
    """Raw Cerebras generation with model fallback — used by provider_router Tier 1.

    Tries each model in CEREBRAS_MODEL_LIST. If a model fails with a
    model-specific error, continues to the next model.
    """
    models_to_try = [model] if model else CEREBRAS_MODEL_LIST
    last_error = None

    client = _get_client()

    for m in models_to_try:
        for attempt in range(1, MAX_RETRIES + 1):
            start = time.time()
            try:
                response = client.chat.completions.create(
                    model=m,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0,
                )

                raw_content = response.choices[0].message.content
                if not raw_content or not raw_content.strip():
                    raise RuntimeError("Empty token payload from Cerebras")

                output = raw_content.strip()

                # Markdown code fence stripping
                if output.startswith("```json"):
                    output = output[7:]
                if output.startswith("```"):
                    output = output[3:]
                if output.endswith("```"):
                    output = output[:-3]
                output = output.strip()

                if not output:
                    raise RuntimeError("Cerebras payload collapsed to empty after stripping")

                duration = time.time() - start
                print(
                    f"[CEREBRAS SUCCESS] Model={m} Module={module} "
                    f"Duration={duration:.2f}s ResponseLength={len(output)}",
                    flush=True,
                )
                return output

            except Exception as e:
                last_error = str(e)
                err_lower = last_error.lower()

                # Model-specific errors → skip to next model
                if "model" in err_lower and ("not found" in err_lower or
                    "invalid" in err_lower or "decommissioned" in err_lower):
                    print(
                        f"[CEREBRAS MODEL SKIP] Model={m} — {last_error[:120]}",
                        flush=True,
                    )
                    break  # Skip this model, try next

                print(
                    f"[CEREBRAS ATTEMPT] Model={m} Attempt={attempt}/{MAX_RETRIES} "
                    f"Error: {last_error[:150]}",
                    flush=True,
                )
                if attempt < MAX_RETRIES:
                    time.sleep(2 * attempt)

    raise RuntimeError(
        f"Cerebras client exhausted all models ({len(models_to_try)}). "
        f"Last error: {last_error}"
    )


def generate(prompt, module="unknown"):
    """Route a generation request through the provider hierarchy.

    Backward-compatible: all existing `from cerebras_client import generate`
    imports delegate to provider_router, which tries:
      Cerebras → Groq → OpenRouter → Ollama
    """
    from provider_router import generate as router_generate
    return router_generate(prompt, module=module)