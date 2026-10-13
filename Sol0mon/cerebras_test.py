import os

from dotenv import load_dotenv
from cerebras.cloud.sdk import Cerebras

load_dotenv()

key = os.getenv("CEREBRAS_API_KEYS")

client = Cerebras(
    api_key=key
)

response = client.chat.completions.create(
    model="llama-4-scout-17b-16e-instruct",
    messages=[
        {
            "role": "user",
            "content": "Reply with only OK"
        }
    ]
)

print(
    response.choices[0]
    .message
    .content
)
