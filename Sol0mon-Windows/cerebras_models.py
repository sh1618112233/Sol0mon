from cerebras.cloud.sdk import Cerebras
from dotenv import load_dotenv
import os

load_dotenv()

client = Cerebras(
    api_key=os.getenv("CEREBRAS_API_KEYS")
)

models = client.models.list()

print(type(models))
print(models)

for item in models:
    print(item)
    print(type(item))
