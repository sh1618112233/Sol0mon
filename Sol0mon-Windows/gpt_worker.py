from chat_session import ChatSession


class GPTSession:

    def __init__(self):

        self.session = ChatSession(
            url="https://chatgpt.com",

            input_selector=
            "#prompt-textarea",

            stop_selector=
            '[data-testid="stop-button"]',

            response_selector=
            '[data-message-author-role="assistant"]'
        )

    def start(self):

        self.session.start()

    def ask(
        self,
        prompt
    ):

        return self.session.ask(
            prompt
        )

    def close(self):

        self.session.close()


def ask_chatgpt(prompt):
    """Backward-compatible standalone function. Creates, uses, and closes a session."""
    session = GPTSession()
    session.start()
    try:
        return session.ask(prompt)
    finally:
        session.close()


if __name__ == "__main__":

    gpt = GPTSession()

    gpt.start()

    result = gpt.ask(
        "What is the capital of France?"
    )

    print(result)

    gpt.close()
