from cerebras_client import generate

if __name__ == "__main__":
    result = generate("Reply with only OK", module="test")
    print(result)
