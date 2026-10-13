import os
import time
import json
import requests
from datetime import datetime

# Global debug flags to control terminal verbosity
DEBUG_OLLAMA = False
DEBUG_DIAGNOSTICS = False

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "qwen3:8b"
MAX_RETRIES = 3
LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "ama.log")


def ensure_log_dir():
    os.makedirs(LOG_DIR, exist_ok=True)


def write_log(
    module,
    prompt_size,
    response_size,
    duration,
    success,
    error=""
):
    ensure_log_dir()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(
            f"[{timestamp}] "
            f"module={module} "
            f"model={MODEL} "
            f"prompt_size={prompt_size} "
            f"response_size={response_size} "
            f"duration={duration:.2f}s "
            f"success={success} "
            f"error={error}\n"
        )


def generate(prompt, module="unknown"):
    last_error = None

    if module == "consensus_check":
        num_predict = 300
        timeout = 300
    elif module == "consensus_repair":
        num_predict = 300
        timeout = 300
    elif module == "conflict_extractor":
        num_predict = 1000
        timeout = 600
    elif module == "consensus_synthesis":
        num_predict = 2500
        timeout = 900
    elif module == "qwen_verifier":
        num_predict = 1000
        timeout = 600
    elif module == "startup_test":
        num_predict = 100
        timeout = 60
    else:
        num_predict = 1000
        timeout = 600

    for attempt in range(1, MAX_RETRIES + 1):
        start = time.time()

        try:
            response = requests.post(
                OLLAMA_URL,
                json={
                    "model": MODEL,
                    "prompt": prompt,
                    "stream": False,
                    "think": False,
                    "options": {
                        "temperature": 0,
                        "num_predict": num_predict,
                        "num_ctx": 16384  
                    }
                },
                timeout=timeout
            )

            response.raise_for_status()
            result = response.json()
            
            eval_count = result.get('eval_count') or 0
            prompt_eval_count = result.get('prompt_eval_count') or 0
            load_duration = result.get('load_duration') or 0
            prompt_eval_duration = result.get('prompt_eval_duration') or 0
            eval_duration = result.get('eval_duration') or 0
            
            context_usage = (prompt_eval_count / 16384) * 100

            # Gated structural diagnostics layer
            if DEBUG_DIAGNOSTICS:
                print("\n===== REQUEST CONFIG =====")
                print(f"module = {module}")
                print("think = False")
                print("==========================\n")

                print("===== OLLAMA DIAGNOSTICS =====")
                print(f"Module: {module}")
                print(f"done: {result.get('done')}")
                print(f"done_reason: {result.get('done_reason')}")
                print(f"eval_count: {eval_count} (Max: {num_predict})")
                print(f"prompt_eval_count: {prompt_eval_count} (Max: 16384)")
                print(f"context_usage: {context_usage:.2f}%")
                print(f"total_duration: {result.get('total_duration')}")
                print(f"load_duration: {load_duration}")
                print(f"prompt_eval_duration: {prompt_eval_duration}")
                print(f"eval_duration: {eval_duration}")
                
                slowest = max(load_duration, prompt_eval_duration, eval_duration)
                if slowest == load_duration and slowest > 0:
                    print("BOTTLENECK: MODEL LOAD")
                elif slowest == prompt_eval_duration and slowest > 0:
                    print("BOTTLENECK: PROMPT EVALUATION")
                elif slowest > 0:
                    print("BOTTLENECK: GENERATION")
                print("==============================\n")

            # Always-on Safety Warning Triggers
            if prompt_eval_count > 14000:
                print("\n!!! CRITICAL CONTEXT WARNING !!!\n")
            elif prompt_eval_count > 12000:
                print("\n!!! HIGH CONTEXT WARNING !!!\n")

            output = result.get("response", "")
            
            if output.startswith("Thinking"):
                print("\nWARNING: THINKING MODE DETECTED\n")
            if "<think>" in output.lower():
                print("\nWARNING: THINK TAG DETECTED\n")

            # Always-on Truncation Check
            if result.get("done_reason") == "length":
                print(
                    f"\nWARNING: OUTPUT TRUNCATED "
                    f"(eval_count={eval_count}, limit={num_predict})\n"
                )
            
            # Optimized clean telemetry logger output layer
            if len(output.strip()) == 0:
                print(
                    f"[{module}] ZERO RESPONSE",
                    flush=True
                )
                if DEBUG_OLLAMA:
                    print("--- PROMPT PREVIEW ---")
                    print(prompt[:1000])
                    print("--- RAW API RESPONSE ---")
                    print(json.dumps(result, indent=2))
            else:
                print(
                    f"[{module}] "
                    f"ResponseLength={len(output)} "
                    f"PromptTokens={prompt_eval_count} "
                    f"GeneratedTokens={eval_count}",
                    flush=True
                )
                if DEBUG_OLLAMA:
                    print(output[:300])

            output = output.strip()
            duration = time.time() - start

            write_log(
                module=module,
                prompt_size=len(prompt),
                response_size=len(output),
                duration=duration,
                success=True
            )

            return output

        except Exception as e:
            last_error = str(e)
            duration = time.time() - start

            write_log(
                module=module,
                prompt_size=len(prompt),
                response_size=0,
                duration=duration,
                success=False,
                error=last_error
            )

            if attempt < MAX_RETRIES:
                time.sleep(2)

    raise RuntimeError(
        f"Ollama failed after {MAX_RETRIES} retries: {last_error}"
    )


def validate_qwen():
    result = generate(
        "Return only OK",
        module="startup_test"
    )

    if (
        "thinking" in result.lower()
        or "<think>" in result.lower()
    ):
        raise RuntimeError(
            "Qwen thinking mode detected."
        )

    print("\nQwen validation passed.\n")


if __name__ == "__main__":
    validate_qwen()
