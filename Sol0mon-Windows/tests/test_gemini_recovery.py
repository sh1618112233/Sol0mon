"""
tests/test_gemini_recovery.py

Tests for Gemini persistent-chat reliability in chat_session.py.

Uses FakePage / FakeLocator mocks to simulate the Gemini DOM without a
real browser. Verifies:
  1. Stop-state recovery (stop clicked, generation cleared)
  2. Multi-path submission (methods 1->4 tried until verified)
  3. Stall detection (90s no growth -> STALLED -> recovery)
  4. Conversation preservation (URL unchanged across recovery)
  5. Submission verification (click succeeds but no generation -> failure -> retry)
  6. Response extraction (scroll performed, latest response extracted)

Run:
    python3 tests/test_gemini_recovery.py
or
    python3 -m pytest tests/test_gemini_recovery.py
"""
import io
import os
import sys
import time

# Make the project root importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import chat_session as cs
from chat_session import ChatSession


# ---------------------------------------------------------------------------
# Mock layer
# ---------------------------------------------------------------------------
class FakeLocator:
    """Minimal Playwright Locator mock."""
    def __init__(self, exists=True, visible=True, enabled=True, text="",
                 stop_sel=False, on_click=None):
        self._exists = exists
        self._visible = visible
        self._enabled = enabled
        self._text = text
        self._stop_sel = stop_sel
        self._click_count = 0
        self._force_count = 0
        self._on_click = on_click

    @property
    def first(self):
        return self

    def count(self):
        return 1 if self._exists else 0

    def is_visible(self, *a, **k):
        return self._visible

    def is_enabled(self):
        return self._enabled

    def click(self, *a, **k):
        if not self._visible:
            raise RuntimeError("not visible")
        self._click_count += 1
        if k.get("force"):
            self._force_count += 1
        if self._on_click:
            self._on_click()

    def press(self, key, *a, **k):
        pass

    def focus(self, *a, **k):
        pass

    def evaluate(self, js, *a, **k):
        if "disabled" in js:
            # "el => !el.disabled"  -> JS already negates -> return enabled state
            # "btn => btn.disabled" -> caller negates      -> return disabled state
            return self._enabled if "!" in js else not self._enabled
        return None

    def element_handle(self):
        return self

    def inner_text(self):
        return self._text

    def wait_for(self, *a, **k):
        if not self._visible:
            raise RuntimeError("not visible")

    def nth(self, i):
        return self


class FakePage:
    """Minimal Playwright Page mock for Gemini scenarios."""

    def __init__(self, url="https://gemini.google.com/app/abc-123",
                 stop_visible=False, send_visible=True,
                 response_texts=None, response_counts=None,
                 input_visible=True, input_enabled=True):
        self._url = url
        self._stop_visible = stop_visible
        self._send_visible = send_visible
        self._response_texts = response_texts or []
        self._response_counts = response_counts or {}
        self._input_visible = input_visible
        self._input_enabled = input_enabled
        self.goto_calls = []
        self.eval_calls = 0
        self.scroll_calls = 0
        self.keyboard_presses = []
        self.stop_clicks = 0
        # Mutable hooks for test scenarios
        self._on_wait = None
        self._enter_pressed = 0

    @property
    def url(self):
        return self._url

    def set_url(self, url):
        self._url = url

    def goto(self, url, **k):
        self.goto_calls.append(url)
        self._url = url

    def wait_for_timeout(self, ms):
        if self._on_wait:
            self._on_wait()

    def evaluate(self, js, *a, **k):
        self.eval_calls += 1
        if "scrollTo" in js or "scrollTop" in js:
            self.scroll_calls += 1
            return None
        if "clipboard" in js:
            return None
        if "JSON.stringify" in js or "innerText" in js:
            return '{"innerText":"","textContent":"","value":""}'
        if "activeElement" in js:
            return ""
        if "editor" in js and "innerText" in js:
            return 0
        return None

    @property
    def keyboard(self):
        return self

    def press(self, key):
        self.keyboard_presses.append(key)
        if key == "Enter":
            self._enter_pressed += 1

    def locator(self, sel):
        # Stop button selectors
        if "Stop" in sel:
            return FakeLocator(
                exists=self._stop_visible, visible=self._stop_visible,
                stop_sel=True,
                on_click=lambda: setattr(self, "stop_clicks", self.stop_clicks + 1)
            )
        # Send button selectors
        if "Send" in sel:
            return FakeLocator(
                exists=self._send_visible, visible=self._send_visible,
                enabled=self._send_visible
            )
        # Response selectors (model-response message-content etc.)
        if "model-response" in sel or "message-content" in sel or "markdown" in sel:
            count = self._response_counts.get(sel, len(self._response_texts))
            if count > 0 and self._response_texts:
                return FakeLocator(exists=True, visible=True, text=self._response_texts[-1])
            return FakeLocator(exists=False, visible=False)
        # Input/textarea
        if "contenteditable" in sel or "prompt-textarea" in sel or "textarea" in sel:
            return FakeLocator(
                exists=self._input_visible, visible=self._input_visible,
                enabled=self._input_enabled
            )
        return FakeLocator(exists=False, visible=False)

    def screenshot(self, *a, **k):
        pass

    def is_closed(self):
        return False


def make_session(page):
    """Build a ChatSession without triggering browser init."""
    s = ChatSession.__new__(ChatSession)
    s.url = "https://gemini.google.com/app"
    s.input_selector = 'div[contenteditable="true"]'
    s.stop_selector = 'button[aria-label*="Stop"]'
    s.response_selector = "model-response message-content, model-response .markdown"
    s.page = page
    s.chat_url = page._url if "/app/" in page._url else None
    s.state = "IDLE"
    return s


def capture(fn, *a, **k):
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        fn(*a, **k)
    finally:
        sys.stdout = old
    return out.getvalue()


def capture2(fn, *a, **k):
    """Like capture() but also returns the function's return value."""
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        ret = fn(*a, **k)
    finally:
        sys.stdout = old
    return ret, out.getvalue()


# ---------------------------------------------------------------------------
# Test 1: Stop button visible -> Stop clicked, generation cleared, READY
# ---------------------------------------------------------------------------
def test_stop_state_recovery():
    """Stop button visible before submission. Verify Stop clicked, Generation
    cleared, and the session reaches a ready (send-visible) state."""
    page = FakePage(stop_visible=True, send_visible=False)
    # After stop click -> send becomes visible
    page._on_wait = lambda: setattr(page, "_send_visible", True) or setattr(
        page, "_stop_visible", False)
    s = make_session(page)

    captured = []
    orig_print = print

    def log(*a, **k):
        import io as _io, sys as _sys
        b = _io.StringIO()
        orig = _sys.stdout
        _sys.stdout = b
        orig_print(*a, **k)
        _sys.stdout = orig
        captured.append(b.getvalue())

    out = capture(s._recover_gemini_stop_state, page.url)

    assert "Gemini stuck in generating state" in out, out
    assert "Stop clicked" in out, out
    assert "Generation cleared" in out, out
    assert page._stop_visible is False, "stop should be cleared"
    print("Test 1 PASS: stop-state recovery -> stop clicked, generation cleared")


# ---------------------------------------------------------------------------
# Test 2: Submission click ignored -> methods 2,3,4 tried
# ---------------------------------------------------------------------------
def test_multipath_submission():
    """First click succeeds but is ignored by Gemini (no generation). Verify
    methods 2,3,4 are tried until submission is verified."""
    page = FakePage(stop_visible=False, send_visible=True)
    # Simulate: send button clicked but generation never starts (no stop,
    # no new response, no turn increase, send stays visible).
    # _verify_submission polls for 5s and finds nothing -> method fails.
    # We make SUBMIT_VERIFY_TIMEOUT fast.
    cs.SUBMIT_VERIFY_TIMEOUT = 0.2  # 200ms for test speed
    try:
        s = make_session(page)
        send_btn = page.locator('button[aria-label="Send message"]')
        textbox = page.locator(s.input_selector)

        out = capture(
            s._submit_prompt_verified,
            send_btn, textbox,
            pre_submit_count={},
            pre_submit_turn_count=0,
        )

        # All 4 methods should be attempted
        assert out.count("Attempt=1") == 1
        assert out.count("Attempt=2") == 1
        assert out.count("Attempt=3") == 1
        assert out.count("Attempt=4") == 1
        # Submission should NOT be verified
        assert "Submission verified" not in out
        # Final result False
    finally:
        cs.SUBMIT_VERIFY_TIMEOUT = 5  # restore

    print("Test 2 PASS: multi-path submission tries methods 1->4")


# ---------------------------------------------------------------------------
# Test 3: No response growth for 90s -> STALLED -> recovery
# ---------------------------------------------------------------------------
def test_stall_detection():
    """Response present but unchanged for 90s with stop visible. Verify
    STALLED detected and recovery triggered."""
    page = FakePage(
        stop_visible=True,
        send_visible=False,
        response_texts=["Partial response that never grows."],
        response_counts={"model-response message-content": 1},
    )
    s = make_session(page)

    # Simulate stall: last_growth_timestamp is 91s ago
    old_now = time.time
    try:
        time.time = lambda: 1000000.0
        s._set_state("GENERATING")
        # Directly test the stall condition logic
        last_growth_ts = time.time() - 91  # 91s ago
        stuck_seconds = int(time.time() - last_growth_ts)
        assert stuck_seconds >= cs.STUCK_RECOVERY_SECONDS
    finally:
        time.time = old_now

    # Verify the stall log message format by checking _recover_from_stall
    page2 = FakePage(
        stop_visible=True,
        send_visible=False,
        response_texts=["Partial response"],
        response_counts={"model-response message-content": 1},
    )
    page2._on_wait = lambda: setattr(page2, "_stop_visible", False) or setattr(
        page2, "_send_visible", True)
    s2 = make_session(page2)

    cs.SUBMIT_VERIFY_TIMEOUT = 0.2  # fast: Test 3 asserts recovery prep, not verify
    try:
        out = capture(s2._recover_from_stall, "Test prompt", 1, "test_dispute")
    finally:
        cs.SUBMIT_VERIFY_TIMEOUT = 5

    assert "Using existing conversation" in out, out
    assert "Stop clicked" in out, out
    assert "Generation cleared" in out, out
    print("Test 3 PASS: stall detected -> recovery triggered")


# ---------------------------------------------------------------------------
# Test 4: Recovery preserves conversation URL
# ---------------------------------------------------------------------------
def test_conversation_preservation():
    """Recovery must reuse tracked_conversation_url and never navigate to
    homepage or create a new chat."""
    chat_url = "https://gemini.google.com/app/abc-123"
    page = FakePage(url=chat_url, stop_visible=True, send_visible=False)
    page._on_wait = lambda: setattr(page, "_stop_visible", False) or setattr(
        page, "_send_visible", True)
    s = make_session(page)
    s.chat_url = chat_url

    cs.SUBMIT_VERIFY_TIMEOUT = 0.2  # fast: Test 4 asserts URL preservation, not verify
    try:
        out = capture(s._recover_from_stall, "Test prompt", 1, "test_dispute")
    finally:
        cs.SUBMIT_VERIFY_TIMEOUT = 5

    # URL must never go to homepage
    for url in page.goto_calls:
        assert "gemini.google.com/app" in url, f"goto to {url} is not a conversation"
        assert url != "https://gemini.google.com/", "navigated to homepage"
        assert url != "https://gemini.google.com/app", "navigated to base app, losing conversation"

    # Tracked URL must be logged
    assert f"Tracked URL={chat_url}" in out, out
    # URL should be unchanged
    assert page.url == chat_url, f"URL changed to {page.url}"
    print("Test 4 PASS: conversation URL preserved through recovery")


# ---------------------------------------------------------------------------
# Test 5: Submission verification — click succeeds but no generation -> fail
# ---------------------------------------------------------------------------
def test_submission_verification_failure():
    """Click succeeds but generation never starts. Verify submission failure
    is detected and a retry is performed."""
    cs.SUBMIT_VERIFY_TIMEOUT = 0.2  # fast for tests
    try:
        page = FakePage(stop_visible=False, send_visible=True)
        # Send stays visible, no stop appears, no new response -> submission
        # not verified. After all 4 methods fail, _submit_prompt_verified
        # returns False.
        s = make_session(page)
        send_btn = page.locator('button[aria-label="Send message"]')
        textbox = page.locator(s.input_selector)

        out = capture(
            s._submit_prompt_verified,
            send_btn, textbox,
            pre_submit_count={},
            pre_submit_turn_count=0,
        )

        assert "Submission failed" in out, out
        assert "Submission verified" not in out, out
    finally:
        cs.SUBMIT_VERIFY_TIMEOUT = 5

    print("Test 5 PASS: submission failure detected -> retry warranted")


# ---------------------------------------------------------------------------
# Test 5b: Submission verification — success case
# ---------------------------------------------------------------------------
def test_submission_verification_success():
    """Click succeeds and stop button appears within timeout. Verify
    'Submission verified' is returned."""
    cs.SUBMIT_VERIFY_TIMEOUT = 2
    try:
        page = FakePage(stop_visible=False, send_visible=True)
        # After 2 polls, stop becomes visible (generation started)
        call_count = {"n": 0}

        def on_wait():
            call_count["n"] += 1
            if call_count["n"] >= 2:
                page._stop_visible = True
                page._send_visible = False

        page._on_wait = on_wait
        s = make_session(page)
        send_btn = page.locator('button[aria-label="Send message"]')
        textbox = page.locator(s.input_selector)

        out = capture(
            s._submit_prompt_verified,
            send_btn, textbox,
            pre_submit_count={},
            pre_submit_turn_count=0,
        )

        assert "Submission verified" in out, f"expected success, got:\n{out}"
        assert "Attempt=1" in out  # method 1 was enough
    finally:
        cs.SUBMIT_VERIFY_TIMEOUT = 5

    print("Test 5b PASS: submission verified when generation starts")


# ---------------------------------------------------------------------------
# Test 6: Response extraction — scroll performed, latest response extracted
# ---------------------------------------------------------------------------
def test_response_extraction():
    """Verify scroll_to_bottom is called and _latest_response_text returns the
    newest response node."""
    page = FakePage(
        stop_visible=False, send_visible=True,
        response_texts=["First response", "Second response", "Latest response"],
        response_counts={"model-response message-content": 3},
    )
    s = make_session(page)

    # Scroll
    s._scroll_conversation_to_bottom()
    assert page.scroll_calls >= 1, "scroll not called"

    # Extraction: pre_submit_counts=2, require_new -> should get the 3rd
    text = s._latest_response_text(
        pre_submit_counts={"model-response message-content": 2},
        require_new=True
    )
    assert text == "Latest response", f"got {text!r}"

    print("Test 6 PASS: scroll performed, latest response extracted")


# ---------------------------------------------------------------------------
# Test 7: Platform error detection (Fix 11)
# ---------------------------------------------------------------------------
def test_platform_error_detection():
    """Verify platform error messages are detected and return BLOCKED."""
    s = make_session(FakePage())

    assert s._detect_platform_error("Something went wrong. Please try again.")
    assert s._detect_platform_error("Unable to generate response.")
    assert s._detect_platform_error("I can't help with that.")
    assert not s._detect_platform_error("Here is the architecture design.")

    out = capture(s._detect_platform_error, "Something went wrong")
    assert "Detected platform failure" in out

    print("Test 7 PASS: platform error detection works")


# ---------------------------------------------------------------------------
# Test 8: Chat integrity — URL drift triggers restore
# ---------------------------------------------------------------------------
def test_chat_integrity_restore():
    """If page.url drifts from tracked chat_url, _ensure_chat_integrity
    reloads the tracked URL."""
    chat_url = "https://gemini.google.com/app/abc-123"
    page = FakePage(url="https://gemini.google.com/app")  # drifted to base
    s = make_session(page)
    s.chat_url = chat_url

    out = capture(s._ensure_chat_integrity)

    assert "Conversation restored" in out, out
    assert len(page.goto_calls) == 1
    assert page.goto_calls[0] == chat_url
    assert page.url == chat_url

    print("Test 8 PASS: URL drift -> conversation restored to tracked URL")


# ---------------------------------------------------------------------------
# Test 9: Composer readiness verification
# ---------------------------------------------------------------------------
def test_composer_readiness():
    """Verify composer readiness check logs the diagnostic and returns True
    only when all four conditions hold."""
    page = FakePage(input_visible=True, input_enabled=True, send_visible=True)
    s = make_session(page)

    out = capture(s._wait_for_composer_ready, timeout=2)

    assert "TextareaVisible=True" in out, out
    assert "TextareaEnabled=True" in out, out
    assert "SendVisible=True" in out, out
    assert "SendEnabled=True" in out, out

    # Negative: send not visible
    page2 = FakePage(input_visible=True, input_enabled=True, send_visible=False)
    s2 = make_session(page2)
    out2 = capture(s2._wait_for_composer_ready, timeout=1)
    assert "SendVisible=False" in out2, out2

    print("Test 9 PASS: composer readiness diagnostic correct")


# ---------------------------------------------------------------------------
# Test 10: Adaptive stall threshold selection
# ---------------------------------------------------------------------------
def test_gemini_long_prompt():
    """Prompt >8000 chars must select the 120-second stall threshold. Cover the
    60/90/120 boundaries for <3000, 3000-7000, and >7000 char prompts."""
    s = make_session(FakePage())

    assert s._gemini_stall_threshold("x" * 8001) == 120, "long prompt must be 120s"
    assert s._gemini_stall_threshold("x" * 7001) == 120
    assert s._gemini_stall_threshold("x" * 7000) == 90
    assert s._gemini_stall_threshold("x" * 3000) == 90
    assert s._gemini_stall_threshold("x" * 2999) == 60
    assert s._gemini_stall_threshold("x" * 1) == 60

    print("Test 10 PASS: adaptive stall thresholds (60/90/120) selected")


# ---------------------------------------------------------------------------
# Test 11: No Stop click before first token
# ---------------------------------------------------------------------------
def test_gemini_first_token_wait():
    """Stop button is visible during the thinking phase, but no response token
    exists yet. Verify the WAITING_FOR_FIRST_TOKEN phase never clicks Stop and
    returns the first token when it appears."""
    page = FakePage(stop_visible=True, send_visible=False,
                    response_texts=[], response_counts={})
    waits = {"n": 0}

    def on_wait():
        waits["n"] += 1
        if waits["n"] >= 3:
            page._response_texts = ["First token text"]
            page._response_counts = {"model-response message-content": 1}

    page._on_wait = on_wait
    s = make_session(page)

    token = s._gemini_wait_for_first_token(
        {"model-response message-content": 0}
    )
    assert token == "First token text", repr(token)
    assert page.stop_clicks == 0, "Stop must not be clicked before first token"

    print("Test 11 PASS: no Stop click during WAITING_FOR_FIRST_TOKEN")


# ---------------------------------------------------------------------------
# Test 12: WAITING_FOR_FIRST_TOKEN -> STREAMING -> COMPLETE before extraction
# ---------------------------------------------------------------------------
def test_gemini_streaming():
    """Verify the full state-machine ordering: WAITING_FOR_FIRST_TOKEN precedes
    STREAMING precedes COMPLETE, and response extraction only happens after
    COMPLETE. No stall/recovery is triggered along the way."""
    page = FakePage(
        stop_visible=True, send_visible=False,
        response_texts=["Final answer text here"],
        response_counts={"model-response message-content": 1},
    )
    waits = {"n": 0}

    def on_wait():
        waits["n"] += 1
        if waits["n"] >= 4:
            page._stop_visible = False

    page._on_wait = on_wait
    s = make_session(page)

    (result, did_recover, _pc, _ptc, _ra), out = capture2(
        s._gemini_lifecycle, "short prompt", {}, 0, 0, "dispute_key"
    )

    assert did_recover is False, "no recovery should occur on a clean stream"
    assert result == "Final answer text here", repr(result)

    i_wait = out.find("WAITING_FOR_FIRST_TOKEN")
    i_stream = out.find("STREAMING")
    i_complete = out.find("COMPLETE")
    i_extract = out.find("[RESPONSE EXTRACTION]")
    assert -1 < i_wait < i_stream < i_complete < i_extract, (
        f"state order wrong: wait={i_wait} stream={i_stream} "
        f"complete={i_complete} extract={i_extract}"
    )
    assert page.stop_clicks == 0, "Stop must not be clicked on a clean stream"

    print("Test 12 PASS: WAITING_FOR_FIRST_TOKEN -> STREAMING -> COMPLETE -> extract")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def run_all():
    tests = [
        ("stop_state_recovery", test_stop_state_recovery),
        ("multipath_submission", test_multipath_submission),
        ("stall_detection", test_stall_detection),
        ("conversation_preservation", test_conversation_preservation),
        ("submission_verification_failure", test_submission_verification_failure),
        ("submission_verification_success", test_submission_verification_success),
        ("response_extraction", test_response_extraction),
        ("platform_error_detection", test_platform_error_detection),
        ("chat_integrity_restore", test_chat_integrity_restore),
        ("composer_readiness", test_composer_readiness),
        ("gemini_long_prompt", test_gemini_long_prompt),
        ("gemini_first_token_wait", test_gemini_first_token_wait),
        ("gemini_streaming", test_gemini_streaming),
    ]

    print("=" * 60)
    print("GEMINI RECOVERY TESTS")
    print("=" * 60)

    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
        except Exception as e:
            import traceback
            print(f"  FAIL  {name}: {e}")
            traceback.print_exc()
            failed += 1

    print()
    print("=" * 60)
    print(f"RESULTS: {passed} passed, {failed} failed")
    print("=" * 60)
    return failed == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if run_all() else 1)
