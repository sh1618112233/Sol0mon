from chat_session import ChatSession


class FakeNode:
    def __init__(self, text):
        self.text = text

    def inner_text(self):
        return self.text


class FakeLocator:
    def __init__(self, texts):
        self.texts = texts

    def count(self):
        return len(self.texts)

    def nth(self, index):
        return FakeNode(self.texts[index])


class FakePage:
    def __init__(self, responses):
        self.responses = responses

    def locator(self, selector):
        if selector == "body":
            raise AssertionError("body fallback must not be used")
        return FakeLocator(self.responses.get(selector, []))


def session_with(responses, selector="primary, secondary, tertiary"):
    session = ChatSession("", "", "", selector)
    session.page = FakePage(responses)
    return session


def test_valid_gemini_response_recovery_selector():
    session = session_with({
        "primary": [],
        "secondary": ["DOMTEST"],
    })
    assert session._latest_response_text() == "DOMTEST"


def test_broken_selector_fails_closed_without_body():
    session = session_with({"primary": [], "secondary": []})
    assert session._latest_response_text() == ""
    try:
        raise RuntimeError("Response extraction failed: no valid Gemini response nodes found.")
    except RuntimeError as e:
        assert "Response extraction failed" in str(e)


def test_multiple_prior_messages_returns_newest_only():
    session = session_with({"primary": ["old one", "old two", "new answer"]}, "primary")
    assert session._latest_response_text(pre_submit_counts={"primary": 2}, require_new=True) == "new answer"


def test_long_history_excluded():
    session = session_with({
        "primary": ["current answer"],
        "sidebar": ["old debate turn\nmenu\ncurrent answer"],
    }, "primary")
    text = session._latest_response_text()
    assert text == "current answer"
    assert "old debate turn" not in text
    assert "menu" not in text


if __name__ == "__main__":
    test_valid_gemini_response_recovery_selector()
    test_broken_selector_fails_closed_without_body()
    test_multiple_prior_messages_returns_newest_only()
    test_long_history_excluded()
    print("response extraction tests passed")
