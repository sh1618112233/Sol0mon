import json

from conflict_extractor import extract_conflicts
from qwen_verifier import verify
from consensus_engine import build_consensus


gpt_final = "The capital of France is Paris."
gemini_final = "The capital of France is Paris."

conflicts = extract_conflicts(
    gpt_final_position=gpt_final,
    gemini_final_position=gemini_final
)

print("\n===== CONFLICT PACKAGE =====\n")
print(json.dumps(conflicts, indent=4))

verifier_result = verify(
    conflicts
)

print("\n===== VERIFIER RESULT =====\n")
print(json.dumps(verifier_result, indent=4))

consensus = build_consensus(
    conflicts,
    verifier_result,
    gpt_final,
    gemini_final
)

print("\n===== CONSENSUS =====\n")
print(json.dumps(consensus, indent=4))