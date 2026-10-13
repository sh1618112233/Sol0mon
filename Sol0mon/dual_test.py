from gpt_worker import GPTSession
from gemini_worker import GeminiSession

prompt = "What is the capital of France?"

gpt_session = GPTSession()
gpt_session.start()
gpt = gpt_session.ask(prompt)
gpt_session.close()

gemini_session = GeminiSession()
gemini_session.start()
gemini = gemini_session.ask(prompt)
gemini_session.close()

print("\n===== GPT =====\n")
print(gpt)

print("\n===== GEMINI =====\n")
print(gemini)