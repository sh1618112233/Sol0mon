import sys
from pipeline_runner import execute

if __name__ == "__main__":
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
    print("\n====================\nFINAL RESULT\n====================")
    print(result)
