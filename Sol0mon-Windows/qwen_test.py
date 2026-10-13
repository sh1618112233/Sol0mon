import json
import requests


PROMPT = """
You are a verifier.

Return valid JSON only.

{
    "agreements": [],
    "conflicts": [],
    "confidence": 0
}

Question:
What is the capital of France?

GPT:
The capital of France is Paris.

Gemini:
The capital of France is Paris.
"""


response = requests.post(
    "http://localhost:11434/api/generate",
    json={
        "model": "qwen3:8b",
        "prompt": PROMPT,
        "stream": False
    },
    timeout=300
)

result = response.json()

print("\n===== RAW RESPONSE =====\n")

print(
    result["response"]
)
