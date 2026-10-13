from chat_session import ChatSession


class GeminiSession:

    def __init__(self):

        self.session = ChatSession(
            url="https://gemini.google.com/app",

            input_selector=
            'div[contenteditable="true"]',

            # C10: Updated stop-button selector to match Gemini's actual UI.
            # The literal "Stop response" label is not present in current Gemini.
            # Use a broader match on aria-label containing "Stop".
            stop_selector=
            'button[aria-label*="Stop"]',

            # Current Gemini DOM: <model-response> -> <message-content> -> .markdown.
            # Keep old selectors last as recovery for older sessions.
            response_selector=
            "model-response message-content, model-response .markdown, model-response .model-response-text, model-response .response-content, model-response .message-content, model-response [data-message-content]"
        )

    def start(self):

        self.session.start()

    def ask(
        self,
        prompt,
        dispute_key=None
    ):

        return self.session.ask(
            prompt,
            dispute_key=dispute_key
        )

    def close(self):

        self.session.close()


def ask_gemini(prompt):
    """Backward-compatible standalone function. Creates, uses, and closes a session."""
    session = GeminiSession()
    session.start()
    try:
        return session.ask(prompt)
    finally:
        session.close()


if __name__ == "__main__":

    gemini = GeminiSession()

    gemini.start()

    result = gemini.ask(
        "What is the capital of France?"
    )

    print(result)

    gemini.close()
