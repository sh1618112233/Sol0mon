import sys
from pipeline_runner import execute

# Accept prompt from command line or fall back to interactive multi-line input.
# When entering interactively: type/paste your prompt, then type END
# on a line by itself to submit.  END is NOT part of the prompt.
if len(sys.argv) > 1:
    prompt = " ".join(sys.argv[1:])
else:
    print("Enter your prompt (type END on a new line to submit):")
    lines = []
    while True:
        line = input()
        if line.strip() == "END":
            break
        lines.append(line)
    prompt = "\n".join(lines)

result = execute(prompt)

print("\n====================")
print("FINAL RESULT")
print("====================")

print(result)
