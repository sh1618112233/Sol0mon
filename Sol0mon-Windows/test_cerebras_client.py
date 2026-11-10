from cerebras_client import generate

result = generate(
    "Reply with only OK",
    module="test"
)

print(result)
