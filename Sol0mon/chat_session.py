import json
import hashlib
import threading
import time
from playwright.sync_api import sync_playwright

# Global debug flag to control detailed text/JSON payload dumping in terminal logs
DEBUG_VERBOSE = False
STUCK_RECOVERY_SECONDS = 90
MAX_GEMINI_RECOVERY_ATTEMPTS = 3
MEANINGFUL_GROWTH_CHARS = 25
MAX_GENERATION_TIME = 300
SUBMIT_VERIFY_TIMEOUT = 30
GEMINI_FIRST_TOKEN_TIMEOUT = 120
WEB_SEARCH_STALL_THRESHOLD = 300
WEB_SEARCH_INDICATORS = ("searching", "browsing", "researching", "sources")

# ChatGPT response-extraction state machine: consecutive stable (zero-growth)
# observations required to declare a response COMPLETE. 3 with a 1s poll = 3s
# of no growth. ponytail: a real streaming pause > 3s could false-complete; if
# that appears in practice, raise this or add a stop-button-gone gate.
REQUIRED_STABLE_OBSERVATIONS = 3

GEMINI_ERROR_PATTERNS = [
    "something went wrong",
    "unable to generate",
    "try again",
    "i can't help with that",
    "error 1155",
]


class ResponseExtractionError(Exception):
    """Structured failure from the chat-response lifecycle.

    Raised INSTEAD of returning a response when Sol0mon cannot prove the
    extracted content is the NEW response to the current submission. A stale
    or partial response is worse than a timeout: this error makes the failure
    explicit and carries diagnostics so callers can tell
    SUCCESS / RESPONSE_TIMEOUT / EXTRACTION_FAILURE apart.

    ponytail: one class + an ``error_code`` discriminator instead of a class
    hierarchy nobody catches. Existing callers do ``_clean(gpt.ask(...))`` and
    let exceptions propagate (ask() already raised RuntimeError on terminal
    failure), so raising here preserves the success-str contract while making
    timeout/extraction-failure distinguishable upstream. The ``diagnostics``
    dict carries the spec contract (error_code, conversation_url, elapsed_time,
    baseline/observed identity, stability_count, poll_count, browser_state,
    last_error).
    """

    def __init__(self, error_code, diagnostics=None, message=""):
        self.error_code = error_code
        self.diagnostics = diagnostics or {}
        super().__init__(message or f"{error_code}: {self.diagnostics}")


PLAYWRIGHT = None
BROWSER = None
CONTEXT = None

# C5: Thread safety for shared browser globals
_BROWSER_LOCK = threading.Lock()


def initialize_browser():
    global PLAYWRIGHT
    global BROWSER
    global CONTEXT

    with _BROWSER_LOCK:
        if PLAYWRIGHT is not None:
            return

        PLAYWRIGHT = sync_playwright().start()
        BROWSER = PLAYWRIGHT.chromium.connect_over_cdp("http://127.0.0.1:9222")

        # Issue 1 Fix: Covered edge cases where CDP connections spin up without an active window frame
        if BROWSER.contexts:
            CONTEXT = BROWSER.contexts[0]
        else:
            print("[BROWSER INITIALIZATION] Context list empty. Constructing explicit context frame layer...")
            CONTEXT = BROWSER.new_context()


def shutdown_browser():
    global PLAYWRIGHT
    global BROWSER
    global CONTEXT
    
    # Issue 2 Fix: Enforced absolute cleanup isolation to systematically drop active system handle hooks
    try:
        if CONTEXT:
            CONTEXT.close()
    except Exception:
        pass
        
    try:
        if BROWSER:
            BROWSER.close()
    except Exception:
        pass

    try:
        if PLAYWRIGHT:
            PLAYWRIGHT.stop()
    except Exception:
        pass
        
    CONTEXT = None
    BROWSER = None
    PLAYWRIGHT = None


class ChatSession:

    def __init__(self, url, input_selector, stop_selector, response_selector):
        self.url = url
        self.input_selector = input_selector
        self.stop_selector = stop_selector
        self.response_selector = response_selector
        
        self.page = None
        self.chat_url = None
        self.state = "IDLE"

    def start(self):
        initialize_browser()
        global CONTEXT

        with _BROWSER_LOCK:
            self.page = CONTEXT.new_page()

        print(f"Created dedicated browser tab. Active Context Pages: {len(CONTEXT.pages)}")

        self.page.goto(self.url, wait_until="domcontentloaded", timeout=120000)
        self.page.wait_for_timeout(5000)

        print(f"Session Started URL: {self.page.url}")
        current_url = self.page.url
        if self._is_specific_chat_url(current_url):
            self.chat_url = current_url
        else:
            self.chat_url = None

    def is_alive(self):
        return self.page is not None and not self.page.is_closed()

    # ------------------------------------------------------------------
    # Fix 1: Explicit state machine
    # ------------------------------------------------------------------
    def _set_state(self, new_state):
        old = self.state
        self.state = new_state
        print("[STATE]")
        print(f"{old} -> {new_state}")

    # ------------------------------------------------------------------
    # C12: URL tracking helpers
    # ------------------------------------------------------------------
    def _is_specific_chat_url(self, url):
        if not url:
            return False
        if "chatgpt.com/c/" in url:
            return True
        if "gemini.google.com/app/" in url:
            path = url.split("app/", 1)[1] if "app/" in url else ""
            if path and path.strip():
                return True
        return False

    def _response_selectors(self):
        return [selector.strip() for selector in self.response_selector.split(",") if selector.strip()]

    def _response_selector_counts(self):
        counts = {}
        for selector in self._response_selectors():
            try:
                counts[selector] = self.page.locator(selector).count()
            except Exception:
                counts[selector] = 0
        return counts

    def _is_invalid_response(self, prompt, response):
        text = (response or "").strip()
        if not text:
            return True
        if len(prompt.strip()) > 80 and len(text) < 20:
            return True
        if len(prompt.strip()) > 200 and len(text) < 60:
            return True
        return False

    def _response_hash(self, text):
        return hashlib.sha256((text or "").encode()).hexdigest()

    def _latest_response_text(self, pre_submit_counts=None, require_new=False):
        pre_submit_counts = pre_submit_counts or {}
        for selector in self._response_selectors():
            responses = self.page.locator(selector)
            count = responses.count()
            if count <= 0:
                continue
            if require_new and count <= pre_submit_counts.get(selector, 0):
                continue
            text = responses.nth(count - 1).inner_text().strip()
            if text:
                return text
        return ""

    def _assistant_turn_count(self):
        """Count assistant response nodes in the DOM."""
        total = 0
        for selector in self._response_selectors():
            try:
                total += self.page.locator(selector).count()
            except Exception:
                pass
        return total

    def _get_recent_message_text(self):
        """M7: Returns the text of the most recent message for dedup detection."""
        try:
            text = self._latest_response_text()
            if text:
                return text[:500]
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Fix 4: Stop-state detection + recovery helpers
    # ------------------------------------------------------------------
    def _gemini_send_visible(self):
        for sel in (
            'button[aria-label="Send message"]',
            'button[aria-label*="Send"]:not([aria-label*="More"])',
        ):
            try:
                loc = self.page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    return True
            except Exception:
                continue
        return False

    def _gemini_stop_button(self):
        for sel in (
            'button[aria-label*="Stop generating"]',
            'button[aria-label*="Stop"]',
        ):
            try:
                loc = self.page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    return loc
            except Exception:
                continue
        return None

    def _gemini_stuck_in_stop_state(self):
        if self._gemini_stop_button() is not None:
            return True
        if not self._gemini_send_visible():
            try:
                loc = self.page.locator('button:has(svg)').first
                if loc.count() > 0 and loc.is_visible():
                    return True
            except Exception:
                pass
        return False

    def _gemini_wait_for_send_button(self, timeout=10000):
        deadline = time.time() + (timeout / 1000.0)
        while time.time() < deadline:
            if self._gemini_send_visible():
                return True
            self.page.wait_for_timeout(500)
        return False

    def _recover_gemini_stop_state(self, current_url):
        """Fix 4: Recover from pre-submission stop state."""
        is_gemini = "gemini.google.com" in (current_url or "")
        if is_gemini and self._gemini_stuck_in_stop_state():
            print("[RECOVERY]")
            print("Gemini stuck in generating state")

            stop_btn = self._gemini_stop_button()
            if stop_btn is not None:
                try:
                    stop_btn.click(timeout=5000)
                    print("[RECOVERY]")
                    print("Stop clicked")
                except Exception as e:
                    if DEBUG_VERBOSE:
                        print(f"[RECOVERY] stop click failed: {e}")

            self._gemini_wait_for_send_button(timeout=10000)
            print("[RECOVERY]")
            print("Generation cleared")

            # Fix 7: If URL drifted, restore tracked conversation
            self._ensure_chat_integrity()

    # ------------------------------------------------------------------
    # Fix 2: Submission verification (Gemini false-submit fix)
    # ------------------------------------------------------------------
    def _user_message_count(self):
        """Count user-side message nodes in the conversation DOM.

        Gemini renders accepted prompts as <user-query> elements; each
        real submission appends exactly one. ChatGPT tags user turns with
        data-message-author-role="user". A stable count lets the
        before/after delta reliably prove acceptance.
        """
        url = self.page.url or ""
        if "gemini.google.com" in url:
            selector = "user-query"
        else:
            selector = '[data-message-author-role="user"]'
        try:
            return self.page.locator(selector).count()
        except Exception:
            return 0

    def _verify_submission(self, user_message_count_before, send_button,
                           timeout=None):
        timeout = SUBMIT_VERIFY_TIMEOUT if timeout is None else timeout
        """Submission is successful ONLY if:
        - user_message_count_after > user_message_count_before (a new user
          message node appeared -> Gemini actually accepted the prompt), OR
        - composer content cleared (Gemini empties the textbox on accept), OR
        - stop button appeared (generation started).
        The old "send button disappeared" check is GONE: it treated a
        successful click as a successful submission (root cause of the false
        submits). Logs [SUBMIT CHECK] and [SEND BUTTON HTML] diagnostics.
        """
        user_message_count_after = user_message_count_before
        composer_cleared = False
        stop_appeared = False
        deadline = time.time() + timeout
        while time.time() < deadline:
            # Condition A: a new user message node appeared in the conversation
            try:
                user_message_count_after = self._user_message_count()
                if user_message_count_after > user_message_count_before:
                    break
            except Exception:
                pass

            # Condition B: composer cleared (Gemini empties the box on accept)
            try:
                composer_text = self.page.locator(
                    self.input_selector
                ).first.evaluate(
                    "el => (el.innerText || el.value || '').trim()"
                )
                if composer_text == "":
                    composer_cleared = True
                    break
            except Exception:
                pass

            # Condition C: stop button appeared (generation started)
            try:
                stop = self.page.locator(self.stop_selector).first
                if stop.count() > 0 and stop.is_visible():
                    stop_appeared = True
                    break
            except Exception:
                pass

            self.page.wait_for_timeout(500)

        # Re-read final counts for the diagnostic
        try:
            user_message_count_after = self._user_message_count()
        except Exception:
            pass

        accepted = (
            user_message_count_after > user_message_count_before
            or composer_cleared
            or stop_appeared
        )

        # ponytail: stop / composer-cleared alone are weaker than the
        # user-count bump; if they fire without a count increase, the click
        # may have landed but Gemini may not have accepted the prompt. The
        # log makes that visible so the decision is never silent.
        print("[SUBMIT CHECK]")
        print(f"Before={user_message_count_before}")
        print(f"After={user_message_count_after}")
        print(f"ComposerCleared={'1' if composer_cleared else '0'}")
        print(f"StopAppeared={'1' if stop_appeared else '0'}")
        print(f"Accepted={'1' if accepted else '0'}")

        send_html = "<not found>"
        try:
            send_html = send_button.evaluate("el => el.outerHTML")
        except Exception as e:
            if DEBUG_VERBOSE:
                print(f"[SEND BUTTON HTML] read failed: {e}")
        print("[SEND BUTTON HTML]")
        print(send_html)

        return accepted

    # ------------------------------------------------------------------
    # Fix 3: Multi-path submission with verification
    # ------------------------------------------------------------------
    def _submit_prompt_verified(self, send_button, send_selector, textbox=None,
                                recovery=False, **legacy_kwargs):
        # Preserve the pre-selector compatibility call: (send_button, textbox,
        # pre_submit_count=..., pre_submit_turn_count=...).
        if textbox is None:
            textbox = send_selector
            send_selector = "legacy"
        """Fix 3: Try 4 submission methods in order; wait up to 30s per attempt
        for generation to begin. Returns True on verified submission.
        recovery=True emits the [RECOVERY] log sequence.

        Verification (Fix 2) gates success on a real accept signal --
        user-message-count increase, composer cleared, or stop-button
        appearance -- never on "send button disappeared".
        """
        tag = "RECOVERY" if recovery else "SUBMIT"
        user_message_count_before = self._user_message_count()

        for method in range(1, 5):
            print(f"[{tag}]")
            print(f"Submit attempt {method}" if recovery else f"Attempt={method}")
            try:
                if method == 1:
                    print(f"Selector={send_selector}")
                    send_button.click(timeout=10000)
                elif method == 2:
                    print(f"Selector={send_selector} (force)")
                    send_button.click(force=True, timeout=10000)
                elif method == 3:
                    print("Selector=Enter(textbox)")
                    textbox.press("Enter", timeout=10000)
                elif method == 4:
                    print("Selector=Enter(keyboard)")
                    self.page.keyboard.press("Enter")
            except Exception as e:
                if DEBUG_VERBOSE:
                    print(f"[{tag}] method {method} error: {e}")
                if not recovery:
                    print("[SUBMIT]")
                    print("Submission failed")
                continue

            if recovery:
                print("[RECOVERY]")
                print("Waiting 30s for generation start")

            self.page.wait_for_timeout(500)

            if self._verify_submission(user_message_count_before, send_button):
                print(f"[{tag}]")
                print("Generation started" if recovery else "Submission verified")
                return True

            print(f"[{tag}]")
            print("Submit attempt failed after 30s" if recovery else "Submission failed")

        return False

    # ------------------------------------------------------------------
    # Fix 6 + Fix 7: Chat integrity verification
    # ------------------------------------------------------------------
    def _ensure_chat_integrity(self):
        """Fix 7: Verify page.url == tracked conversation URL. If not, reload
        the tracked URL. Never create a new conversation.
        """
        if not self.chat_url or not self._is_specific_chat_url(self.chat_url):
            return
        if self.page.url != self.chat_url:
            print("[RECOVERY]")
            print("Conversation restored")
            try:
                self.page.goto(self.chat_url, wait_until="domcontentloaded",
                               timeout=120000)
                self.page.wait_for_timeout(3000)
            except Exception as e:
                if DEBUG_VERBOSE:
                    print(f"[RECOVERY] restore failed: {e}")

    # ------------------------------------------------------------------
    # Fix 8: Composer readiness verification
    # ------------------------------------------------------------------
    def _wait_for_composer_ready(self, timeout=10):
        """Fix 8: Wait up to `timeout` seconds for textarea visible+enabled and
        send button visible+enabled. Always logs the diagnostic.
        """
        deadline = time.time() + timeout
        tb_vis = tb_en = send_vis = send_en = False

        while time.time() < deadline:
            try:
                tb = self.page.locator(self.input_selector).first
                tb_vis = tb.count() > 0 and tb.is_visible()
                if tb_vis:
                    tb_en = tb.evaluate("el => !el.disabled")
            except Exception:
                tb_en = False

            if "gemini.google.com" in self.page.url:
                send_vis = self._gemini_send_visible()
                send_en = send_vis
            else:
                try:
                    sb_sel = 'button[data-testid="send-button"]'
                    sb = self.page.locator(sb_sel).first
                    send_vis = sb.count() > 0 and sb.is_visible()
                    send_en = (not sb.evaluate("btn => btn.disabled")
                               if send_vis else False)
                except Exception:
                    send_en = False

            if tb_vis and tb_en and send_vis and send_en:
                break
            self.page.wait_for_timeout(500)

        print("[SUBMIT DIAGNOSTIC]")
        print(f"TextareaVisible={tb_vis}")
        print(f"TextareaEnabled={tb_en}")
        print(f"SendVisible={send_vis}")
        print(f"SendEnabled={send_en}")
        return tb_vis and tb_en and send_vis and send_en

    # ------------------------------------------------------------------
    # Fix 11: Safety / error detection
    # ------------------------------------------------------------------
    def _detect_platform_error(self, text):
        """Fix 11: Detect Gemini platform-level error messages in response text."""
        if not text:
            return False
        lower = text.lower()
        for pattern in GEMINI_ERROR_PATTERNS:
            if pattern in lower:
                print("[GEMINI ERROR]")
                print("Detected platform failure")
                return True
        return False

    # ------------------------------------------------------------------
    # Fix 9: Scroll to bottom
    # ------------------------------------------------------------------
    def _scroll_conversation_to_bottom(self):
        try:
            self.page.evaluate("""
            () => {
                try { window.scrollTo(0, document.body.scrollHeight); } catch (e) {}
                document.querySelectorAll('div, main, [role="main"]').forEach(el => {
                    try {
                        const s = getComputedStyle(el);
                        if ((s.overflowY === 'auto' || s.overflowY === 'scroll')
                            && el.scrollHeight > el.clientHeight) {
                            el.scrollTop = el.scrollHeight;
                        }
                    } catch (e) {}
                });
            }
            """)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Fix 5 + Fix 6 + Fix 10: Stall recovery (factored)
    # ------------------------------------------------------------------
    def _recover_from_stall(self, prompt, recovery_attempts, dispute_key):
        """Fix 5+6: Stop generation, stay in same chat, wait for idle,
        focus composer, reinsert prompt, resubmit with verification.
        Returns (recovered: bool, new_pre_submit_count: dict, new_pre_submit_turn_count: int).
        """
        print("[RECOVERY]")
        print("Using existing conversation")
        print(f"[RECOVERY] Tracked URL={self.chat_url}")

        # Stop generation
        try:
            stop_btn = self._gemini_stop_button() or self.page.locator(
                self.stop_selector).first
            if stop_btn.count() > 0:
                stop_btn.click(timeout=5000)
                print("[RECOVERY]")
                print("Stop clicked")
                try:
                    stop_btn.wait_for(state="hidden", timeout=30000)
                except Exception:
                    self.page.wait_for_timeout(3000)
        except Exception as stop_err:
            if DEBUG_VERBOSE:
                print(f"[RECOVERY] stop error: {stop_err}")

        # Fix 7: Ensure we're still on the tracked conversation
        self._ensure_chat_integrity()

        # Wait for idle state
        print("[RECOVERY]")
        print("Generation cleared")
        self._gemini_wait_for_send_button(timeout=10000)

        # Fix 8: Composer readiness
        self._wait_for_composer_ready(timeout=10)

        # Reinsert prompt
        self._set_state("INPUT_READY")
        submit_state = self._record_and_submit_prompt(prompt)
        pre_submit_count = submit_state["pre_submit_count"]
        pre_submit_turn_count = self._assistant_turn_count()

        print("[RECOVERY]")
        print("Prompt reinserted")

        # Locate send button
        if "chatgpt.com" in self.page.url:
            send_button_sel = 'button[data-testid="send-button"]'
            send_button = self.page.locator(send_button_sel).first
        else:
            send_button_sel = 'button[aria-label="Send message"]'
            send_button = self.page.locator(send_button_sel).first
            if send_button.count() == 0:
                send_button_sel = \
                    'button[aria-label*="Send"]:not([aria-label*="More"])'
                send_button = self.page.locator(send_button_sel).first

        textbox = self.page.locator(self.input_selector).first

        # Fix 3: Verified multi-path resubmission (30s generation-start window)
        self._set_state("SUBMITTING")
        verified = self._submit_prompt_verified(
            send_button, send_button_sel, textbox, recovery=True
        )
        if not verified:
            return False, pre_submit_count, pre_submit_turn_count

        self._set_state("SUBMITTED")
        self._set_state("GENERATING")
        return True, pre_submit_count, pre_submit_turn_count

    # ------------------------------------------------------------------
    # Gemini lifecycle: WAITING_FOR_FIRST_TOKEN -> STREAMING -> COMPLETE
    # ------------------------------------------------------------------
    def _gemini_stall_threshold(self, prompt):
        n = len(prompt)
        if n > 7000:
            return 120
        if n >= 3000:
            return 90
        return 60

    # ------------------------------------------------------------------
    # Web-search + activity signals (stall may only be declared when
    # response text, Gemini DOM, response nodes, AND page activity are
    # ALL dormant for the threshold). Stop-button visibility is the stall
    # precondition, NOT an activity indicator.
    # ------------------------------------------------------------------
    def _gemini_web_search_active(self):
        try:
            return self.page.evaluate(
                r"""() => {
                    const t = (document.body.innerText || "").toLowerCase();
                    return ["searching","browsing","researching","sources"]
                           .some(k => t.indexOf(k) !== -1);
                }"""
            )
        except Exception:
            return False
    # ponytail: body-text match latches 300s threshold once seen; a false
    # positive (e.g. the word "sources" in prose) only makes us more
    # conservative (longer wait), never interrupts a live generation.
    # Tighten to a Gemini web-search chip selector if false negatives appear.

    def _gemini_dom_signature(self):
        try:
            return self.page.evaluate(
                r"""() => {
                    const root = document.querySelector('main, [role="main"]') || document.body;
                    return JSON.stringify([root.querySelectorAll('*').length, root.scrollHeight]);
                }"""
            )
        except Exception:
            return ""

    def _gemini_response_node_signature(self):
        try:
            sigs = []
            for sel in self._response_selectors():
                cnt = self.page.locator(sel).count()
                html_len = 0
                if cnt > 0:
                    try:
                        html_len = self.page.locator(sel).nth(cnt - 1).evaluate(
                            "el => el.innerHTML.length"
                        )
                    except Exception:
                        pass
                sigs.append((cnt, html_len))
            return tuple(sigs)
        except Exception:
            return ()

    def _gemini_page_activity(self):
        try:
            return self.page.evaluate(
                r"""() => {
                    const body = document.body.innerText || "";
                    if (/thinking|analyzing|processing|generating\b/i.test(body)) return true;
                    const spinners = document.querySelectorAll(
                        '[role="progressbar"], .animate-spin, .loading-indicator'
                    );
                    for (const s of spinners) {
                        const r = s.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) return true;
                    }
                    return false;
                }"""
            )
        except Exception:
            return False

    def _gemini_wait_for_first_token(self, pre_submit_count,
                                     timeout=GEMINI_FIRST_TOKEN_TIMEOUT):
        """Wait up to `timeout` seconds for the first response token. Does NOT
        click stop, trigger recovery, or extract. Returns the first non-empty
        response text, or "" on timeout.
        """
        print("[GEMINI LOOP] FIRST_TOKEN_WAIT ENTERED")
        deadline = time.time() + timeout
        _ft_tick = 0
        while time.time() < deadline:
            _ft_tick += 1
            print(f"[GEMINI LOOP] FIRST_TOKEN_WAIT TICK={_ft_tick}")
            self._scroll_conversation_to_bottom()
            try:
                current_text = self._latest_response_text(
                    pre_submit_counts=pre_submit_count, require_new=True
                )
            except Exception as _ft_err:
                print(f"[GEMINI LOOP] FIRST_TOKEN_WAIT swallowed: {type(_ft_err).__name__}: {_ft_err}")
                current_text = ""
            print(f"[GEMINI LOOP] FIRST_TOKEN_WAIT Length={len(current_text or '')}")
            if current_text:
                return current_text
            self.page.wait_for_timeout(1000)
        print(f"[GEMINI LOOP] FIRST_TOKEN_WAIT TIMEOUT after {timeout}s")
        return ""

    def _gemini_lifecycle(self, prompt, pre_submit_count, pre_submit_turn_count,
                          recovery_attempts, dispute_key):
        """Gemini-specific generation lifecycle.

        States: WAITING_FOR_FIRST_TOKEN -> STREAMING -> COMPLETE.
        Never clicks Stop, recovers, or extracts before the first token.

        A stall is declared ONLY when ALL of these are dormant for the
        effective stall threshold:
          1. response text growth
          2. Gemini DOM mutations
          3. response node mutations
          4. page activity indicators (web search / spinners / thinking)
        Web-search mode (Searching/Browsing/Researching/Sources) latches the
        threshold to WEB_SEARCH_STALL_THRESHOLD and suppresses auto-stop
        while the indicator is visible. Stop-button visibility is the stall
        precondition, not an activity signal, so a web-search generation is
        never interrupted merely because its text output temporarily pauses.

        Returns (result, did_recover, pre_submit_count, pre_submit_turn_count,
                 recovery_attempts).
        """
        # ---- WAITING_FOR_FIRST_TOKEN ----
        self._set_state("WAITING_FOR_FIRST_TOKEN")
        print("[GEMINI STATE]")
        print("WAITING_FOR_FIRST_TOKEN")

        first_token = self._gemini_wait_for_first_token(pre_submit_count)
        if not first_token:
            self._set_state("FAILED")
            raise RuntimeError(
                "Gemini first token timeout: no response after "
                f"{GEMINI_FIRST_TOKEN_TIMEOUT}s."
            )

        # ---- STREAMING ----
        self._set_state("STREAMING")
        print("[GEMINI STATE]")
        print("STREAMING")

        effective_threshold = self._gemini_stall_threshold(prompt)
        last_response_length = len(first_token)
        last_response_text = first_token
        last_growth_text = first_token
        last_growth_hash = self._response_hash(first_token)
        last_activity_timestamp = time.time()
        generation_start = time.time()
        stable_text_checks = 0
        required_stable = 3
        result = ""

        last_dom_signature = self._gemini_dom_signature()
        last_node_signature = self._gemini_response_node_signature()

        web_search_seen = False
        effective_max = MAX_GENERATION_TIME
        elapsed = 0
        _loop_tick = 0

        print("[GEMINI LOOP] ENTERED")
        while elapsed < effective_max:
            _loop_tick += 1
            print(f"[GEMINI LOOP] TICK={_loop_tick}")
            self._scroll_conversation_to_bottom()
            try:
                current_text = self._latest_response_text(
                    pre_submit_counts=pre_submit_count, require_new=True
                )
            except Exception as _loop_err:
                print(f"[GEMINI LOOP] swallowed: {type(_loop_err).__name__}: {_loop_err}")
                current_text = ""
            current_text = current_text or ""
            current_length = len(current_text)
            print(f"[GEMINI LOOP] Length={current_length}")
            current_hash = self._response_hash(current_text)

            text_grew = current_length > last_response_length + MEANINGFUL_GROWTH_CHARS
            if text_grew:
                last_response_length = current_length
            if current_length > len(last_growth_text) + MEANINGFUL_GROWTH_CHARS:
                last_growth_text = current_text
                last_growth_hash = current_hash

            dom_signature = self._gemini_dom_signature()
            dom_changed = dom_signature != last_dom_signature
            if dom_changed:
                last_dom_signature = dom_signature

            node_signature = self._gemini_response_node_signature()
            node_changed = node_signature != last_node_signature
            if node_changed:
                last_node_signature = node_signature

            web_search_active = self._gemini_web_search_active()
            if web_search_active and not web_search_seen:
                web_search_seen = True
                effective_threshold = WEB_SEARCH_STALL_THRESHOLD
                effective_max = MAX_GENERATION_TIME + WEB_SEARCH_STALL_THRESHOLD
            page_activity = web_search_active or self._gemini_page_activity()

            if text_grew or dom_changed or node_changed or page_activity:
                last_activity_timestamp = time.time()

            stuck_seconds = int(time.time() - last_activity_timestamp)
            elapsed = int(time.time() - generation_start)
            hit_hard_timeout = elapsed >= effective_max

            stop_visible = False
            try:
                stop_visible = self.page.locator(self.stop_selector).first.is_visible()
            except Exception:
                stop_visible = False

            web_search_suppressed = web_search_active

            stall_triggered = (
                not web_search_suppressed
                and stop_visible and current_length > 0
                and (stuck_seconds >= effective_threshold or hit_hard_timeout)
            )

            if stall_triggered:
                decision = "STALL"
            elif web_search_suppressed:
                decision = "WEB_SEARCH"
            elif stuck_seconds > 0:
                decision = f"WAIT({stuck_seconds}/{effective_threshold})"
            else:
                decision = "CONTINUE"
            print("[GEMINI HEALTH]")
            print(f"ResponseLength={current_length}")
            print(f"LastGrowth={stuck_seconds}")
            print(f"DOMChanges={'1' if dom_changed else '0'}")
            print(f"PageActivity={'1' if page_activity else '0'}")
            print(f"Decision={decision}")

            if current_text and self._detect_platform_error(current_text):
                self._set_state("FAILED")
                raise RuntimeError("Invalid response token footprint detected.")

            if not current_text:
                self.page.wait_for_timeout(1000)
                elapsed = int(time.time() - generation_start)
                continue

            if stall_triggered:
                self._set_state("STALLED")
                print("[GEMINI STALL]")
                print(f"Threshold={effective_threshold}")
                if hit_hard_timeout:
                    print("[GEMINI STALL DETECTED]")
                    print(f"Generation exceeded {effective_max}s hard timeout.")
                else:
                    print("[GEMINI STALL DETECTED]")
                    print(f"No activity for {stuck_seconds} seconds "
                          f"(text/dom/node/page all dormant).")

                if recovery_attempts >= MAX_GEMINI_RECOVERY_ATTEMPTS:
                    self._set_state("FAILED")
                    raise RuntimeError(
                        "Gemini generation stalled after 3 retry attempts."
                    )

                print("[GEMINI RETRY]")
                print(f"Attempt={recovery_attempts + 1}/{MAX_GEMINI_RECOVERY_ATTEMPTS}")
                self._set_state("RECOVERING")
                recovery_attempts += 1

                recovered, pre_submit_count, pre_submit_turn_count = (
                    self._recover_from_stall(
                        prompt, recovery_attempts, dispute_key
                    )
                )
                if not recovered:
                    self._set_state("FAILED")
                    raise RuntimeError(
                        "Recovery submission failed: could not verify resubmission."
                    )
                return "", True, pre_submit_count, pre_submit_turn_count, recovery_attempts

            if current_text == last_response_text:
                stable_text_checks += 1
                if stable_text_checks >= required_stable and not stop_visible:
                    result = current_text
                    self._set_state("COMPLETE")
                    print("[GEMINI STATE]")
                    print("COMPLETE")
                    print(f"[RESPONSE EXTRACTION] New response stabilized after {required_stable} checks. Length={len(result)}")
                    break
            else:
                stable_text_checks = 0
                last_response_text = current_text

            self.page.wait_for_timeout(1000)
            elapsed = int(time.time() - generation_start)

        return result, False, pre_submit_count, pre_submit_turn_count, recovery_attempts

    # ==================================================================
    # ChatGPT response-extraction lifecycle (correctness-first).
    # States: WAITING_FOR_NEW_RESPONSE -> GENERATING -> COMPLETE.
    # A response is returned ONLY when a new assistant node (count > baseline)
    # has stabilized for REQUIRED_STABLE_OBSERVATIONS consecutive polls.
    # Timeout -> RESPONSE_TIMEOUT (never a silent GENERATING -> IDLE, never a
    # stale "last available response"). Stale guard -> EXTRACTION_FAILURE.
    # ==================================================================

    def _build_extraction_diagnostics(self, error_code, baseline_identity,
                                       observed_identity, observed_length,
                                       stability_count, poll_count, elapsed,
                                       last_error=""):
        return {
            "error_code": error_code,
            "conversation_url": getattr(self, "chat_url", None),
            "elapsed_time": elapsed,
            "baseline_response_identity": baseline_identity,
            "observed_response_identity": observed_identity,
            "observed_response_length": observed_length,
            "stability_count": stability_count,
            "poll_count": poll_count,
            "browser_state": self.state,
            "last_error": last_error,
        }

    def _capture_baseline_identity(self):
        """Snapshot the assistant-response state BEFORE submission.

        Establishes the baseline a post-submission response must be provably
        newer than. ``counts`` is per-selector assistant-node counts;
        ``last_hash`` / ``last_length`` describe the most recent existing
        assistant turn. A post-submission response is accepted ONLY when a
        selector's count exceeds its baseline count (a brand-new assistant
        node appeared) -- never by matching the baseline text.
        """
        try:
            counts = self._response_selector_counts()
        except Exception:
            counts = {}
        last_text = ""
        try:
            last_text = self._latest_response_text() or ""
        except Exception:
            last_text = ""
        return {
            "counts": counts,
            "last_hash": self._response_hash(last_text),
            "last_length": len(last_text),
            "url": self.page.url if self.page is not None else None,
        }

    def _new_node_proven(self, baseline_identity):
        """True iff a new assistant node demonstrably exists vs the baseline.

        At least one response selector must have a count strictly greater than
        its baseline count. This is the stale-response guard: if no selector
        increased, the "response" in view is the previous turn and must NOT be
        returned as the current submission's result.
        """
        base = (baseline_identity or {}).get("counts") or {}
        try:
            now = self._response_selector_counts()
        except Exception:
            return False
        for selector, cnt in now.items():
            if cnt > base.get(selector, 0):
                return True
        return False

    def _stop_button_visible(self):
        """True iff the stop-generating button is currently visible.

        Used as the ChatGPT completion finishing signal: a stable response with
        the stop button still up is a mid-stream pause, not completion -- the
        lifecycle keeps waiting. When the stop selector does not match the
        current UI (e.g. reasoning models swap it for a different indicator),
        this returns False and completion falls back to stability-only.
        """
        try:
            loc = self.page.locator(self.stop_selector).first
            return loc.count() > 0 and loc.is_visible()
        except Exception:
            return False

    def _chatgpt_lifecycle(self, prompt, pre_submit_count, baseline_identity):
        """ChatGPT response lifecycle (correctness-first).

        Returns the verified NEW response text, or raises ResponseExtractionError.

        Guarantees (spec):
          1. A response is returned ONLY when a new assistant node (count >
             baseline) exists AND it has stabilized for
             REQUIRED_STABLE_OBSERVATIONS consecutive observations. A timeout
             is NEVER treated as success.
          2. On max-generation timeout -> RESPONSE_TIMEOUT. The partial
             response is discarded; the state never silently transitions
             GENERATING -> IDLE.
          3. The stale "last available response" fallback is gone. A count
             that never exceeds baseline means no new response -> failure
             (never the previous turn's text).

        Stop-button visibility is a generation hint, not a completion
        precondition: a fast response that never shows Stop still completes via
        stability; a stop that lingers during a brief pause does not flip the
        stability window.
        """
        required_stable = REQUIRED_STABLE_OBSERVATIONS
        poll_count = 0
        stability_count = 0
        last_response_text = ""
        last_response_length = 0
        observed_identity = None
        generation_start = time.time()

        # ---- WAITING_FOR_NEW_RESPONSE ----
        self._set_state("WAITING_FOR_NEW_RESPONSE")
        new_node_seen = False
        while time.time() - generation_start < MAX_GENERATION_TIME:
            poll_count += 1
            self._scroll_conversation_to_bottom()
            try:
                current_text = self._latest_response_text(
                    pre_submit_counts=pre_submit_count, require_new=True
                )
            except Exception:
                current_text = ""
            current_text = current_text or ""

            if current_text:
                new_node_seen = True
                observed_identity = self._response_hash(current_text)
                last_response_text = current_text
                last_response_length = len(current_text)
                self._set_state("GENERATING")
                break

            # Stop appearing without a new node is a hint; keep polling for
            # the node. Never extract here.
            self.page.wait_for_timeout(1000)

        if not new_node_seen:
            elapsed = int(time.time() - generation_start)
            self._set_state("FAILED")
            raise ResponseExtractionError(
                "RESPONSE_TIMEOUT",
                diagnostics=self._build_extraction_diagnostics(
                    "RESPONSE_TIMEOUT", baseline_identity, observed_identity,
                    last_response_length, stability_count, poll_count, elapsed,
                    last_error=(f"No new assistant response node appeared "
                                f"within {MAX_GENERATION_TIME}s generation "
                                "timeout."),
                ),
            )

        # ---- GENERATING ----
        while time.time() - generation_start < MAX_GENERATION_TIME:
            poll_count += 1
            self._scroll_conversation_to_bottom()
            try:
                current_text = self._latest_response_text(
                    pre_submit_counts=pre_submit_count, require_new=True
                )
            except Exception:
                current_text = ""
            current_text = current_text or ""
            current_length = len(current_text)

            if current_text and self._detect_platform_error(current_text):
                elapsed = int(time.time() - generation_start)
                self._set_state("FAILED")
                raise ResponseExtractionError(
                    "EXTRACTION_FAILURE",
                    diagnostics=self._build_extraction_diagnostics(
                        "EXTRACTION_FAILURE", baseline_identity,
                        self._response_hash(current_text), current_length,
                        stability_count, poll_count, elapsed,
                        last_error="Platform error detected in response text.",
                    ),
                )

            if not current_text:
                # Transient read miss; do not disturb stability / last good.
                self.page.wait_for_timeout(1000)
                continue

            if current_length > last_response_length + MEANINGFUL_GROWTH_CHARS:
                # Meaningful growth -> still generating. Reset stability.
                stability_count = 0
                last_response_length = current_length
                last_response_text = current_text
            elif current_text == last_response_text:
                stability_count += 1
                if stability_count >= required_stable:
                    # Ignore a stale stop control after 10 stable polls; otherwise
                    # a completed ChatGPT turn can wait forever for a UI toggle.
                    if self._stop_button_visible() and stability_count < 10:
                        self.page.wait_for_timeout(1000)
                        continue
                    # Stale guard: prove a new node exists vs baseline.
                    if not self._new_node_proven(baseline_identity):
                        elapsed = int(time.time() - generation_start)
                        self._set_state("FAILED")
                        raise ResponseExtractionError(
                            "EXTRACTION_FAILURE",
                            diagnostics=self._build_extraction_diagnostics(
                                "EXTRACTION_FAILURE", baseline_identity,
                                self._response_hash(current_text), current_length,
                                stability_count, poll_count, elapsed,
                                last_error=("Stale-response guard: no new "
                                            "assistant node proven vs "
                                            "baseline."),
                            ),
                        )
                    if self._is_invalid_response(prompt, current_text):
                        elapsed = int(time.time() - generation_start)
                        self._set_state("FAILED")
                        raise ResponseExtractionError(
                            "EXTRACTION_FAILURE",
                            diagnostics=self._build_extraction_diagnostics(
                                "EXTRACTION_FAILURE", baseline_identity,
                                self._response_hash(current_text), current_length,
                                stability_count, poll_count, elapsed,
                                last_error="Extracted response failed "
                                           "validity check.",
                            ),
                        )
                    self._set_state("COMPLETE")
                    print(f"[RESPONSE EXTRACTION] New response stabilized "
                          f"after {required_stable} checks. "
                          f"Length={len(current_text)}")
                    return current_text
            else:
                # Small textual change (< meaningful growth): treat as
                # activity, reset stability, update the comparison text.
                stability_count = 0
                last_response_text = current_text
                last_response_length = current_length

            self.page.wait_for_timeout(1000)

        # Timeout during generation: partial response is NOT returned.
        elapsed = int(time.time() - generation_start)
        self._set_state("FAILED")
        raise ResponseExtractionError(
            "RESPONSE_TIMEOUT",
            diagnostics=self._build_extraction_diagnostics(
                "RESPONSE_TIMEOUT", baseline_identity,
                self._response_hash(last_response_text) if last_response_text else None,
                last_response_length, stability_count, poll_count, elapsed,
                last_error=(f"Response did not stabilize within "
                            f"{MAX_GENERATION_TIME}s generation timeout; "
                            "partial response discarded."),
            ),
        )

    # ------------------------------------------------------------------
    # Prompt insertion (input prep)
    # ------------------------------------------------------------------
    def _record_and_submit_prompt(self, prompt):
        """Submit prompt and return pre-submit response count for delta detection."""
        # Fix 4: Stop-state recovery before submission
        if "gemini.google.com" in self.page.url:
            self._recover_gemini_stop_state(self.page.url)

        # Baseline identity is captured BEFORE the textbox is touched: this is
        # the assistant-response state a post-submission response must be
        # provably newer than (count strictly greater than baseline).
        try:
            baseline_identity = self._capture_baseline_identity()
        except Exception:
            baseline_identity = {"counts": {}, "last_hash": "", "last_length": 0,
                                  "url": None}
        pre_submit_count = baseline_identity.get("counts", {}) or {}

        pre_submit_last_message = self._get_recent_message_text()

        textbox = self.page.locator(self.input_selector).first
        textbox.wait_for(state="visible", timeout=30000)

        textbox.click()
        textbox.focus()
        self.page.wait_for_timeout(1000)

        # Clear
        try:
            textbox.evaluate("el => { el.innerHTML = ''; el.value = ''; }")
        except Exception:
            for _ in range(5):
                self.page.keyboard.press("Control+A")
                self.page.keyboard.press("Backspace")
                self.page.wait_for_timeout(300)

        current_url = self.page.url

        # Platform-Isolated Ingestion Flow Routing
        if "chatgpt.com" in current_url:
            print("Target Host Identified: ChatGPT. Utilizing localized execCommand insertion.")
            textbox.evaluate("""
            (el, text) => {
                el.focus();
                document.execCommand('insertText', false, text);
            }
            """, prompt)

            self.page.wait_for_timeout(2000)
            editor_length = self.page.evaluate("""
            () => {
                const editor = document.querySelector('#prompt-textarea');
                return editor ? editor.innerText.length : -1;
            }
            """)
            if DEBUG_VERBOSE:
                print(f"[CHATGPT INSERT TEST] Editor Length={editor_length}")

        else:
            print("Target Host Identified: Gemini. Falling back to platform clipboard paste route.")
            self.page.evaluate(
                """async (text) => {
                    await navigator.clipboard.writeText(text);
                }""",
                prompt
            )
            self.page.wait_for_timeout(1000)
            textbox.click()
            textbox.focus()
            self.page.keyboard.press("Control+V")

        self.page.wait_for_timeout(500)

        return {
            "pre_submit_count": pre_submit_count,
            "pre_submit_last_message": pre_submit_last_message,
            "current_url": current_url,
            "baseline_identity": baseline_identity,
        }

    # ------------------------------------------------------------------
    # Main ask() — state-machine driven
    # ------------------------------------------------------------------
    def ask(self, prompt, max_retries=3, dispute_key=None):
        print("\n===== PROMPT RECEIVED =====")
        print(f"Prompt Length: {len(prompt)}")
        if DEBUG_VERBOSE:
            print(prompt[:1000])
        print("===========================\n")
        print("STATUS:")
        print("Completed: prompt received")
        print("Current task: submit prompt and wait for response")
        print("Next action: monitor response growth")

        self._set_state("INPUT_READY")

        for attempt in range(1, max_retries + 1):
            try:
                # Fix 7: Chat integrity before each attempt
                self._ensure_chat_integrity()

                # Fix 4: Stop-state recovery (Gemini)
                if "gemini.google.com" in self.page.url:
                    self._recover_gemini_stop_state(self.page.url)

                # Input prep + paste
                submit_state = self._record_and_submit_prompt(prompt)
                pre_submit_count = submit_state["pre_submit_count"]
                pre_submit_last_message = submit_state["pre_submit_last_message"]
                current_url = submit_state["current_url"]
                baseline_identity = submit_state["baseline_identity"]
                is_gemini = "gemini.google.com" in current_url

                # Prompt insertion verification
                threshold = 0.95
                current_text = ""
                max_wait_seconds = 30 if len(prompt) > 10000 else 15
                last_length = -1
                stable_checks = 0
                max_stable_allowed = 10

                print(f"Beginning dynamic growth pass (Window Limit: {max_wait_seconds}s | Threshold: {threshold*100}%)")

                textbox = self.page.locator(self.input_selector).first
                for poll_attempt in range(max_wait_seconds):
                    if poll_attempt == 5 and last_length <= 0 and "chatgpt.com" in current_url:
                        print("[FALLBACK ENGAGED] Empty editor node detected at 5s checkpoint. Forcing manual ProseMirror DOM layout build.")
                        textbox.evaluate("""
                        (el, text) => {
                            el.innerHTML = '';
                            const paragraph = document.createElement('p');
                            paragraph.textContent = text;
                            el.appendChild(paragraph);
                            el.dispatchEvent(new InputEvent('input', { bubbles: true }));
                        }
                        """, prompt)

                    try:
                        element = textbox.element_handle()
                        if element:
                            raw_diagnostics = self.page.evaluate("""
                            (el) => {
                                return JSON.stringify({
                                    innerText: el.innerText || "",
                                    textContent: el.textContent || "",
                                    value: el.value || ""
                                });
                            }
                            """, element)
                            data = json.loads(raw_diagnostics)
                            current_text = data.get("innerText") or data.get("textContent") or data.get("value") or ""
                    except Exception as eval_err:
                        if DEBUG_VERBOSE:
                            print(f"DOM read attempt warning: {eval_err}")

                    current_length = len(current_text)
                    if DEBUG_VERBOSE:
                        print(f"[VERIFY] {current_length}/{len(prompt)} Stable={stable_checks}")

                    if current_length > last_length:
                        stable_checks = 0
                        last_length = current_length
                    else:
                        stable_checks += 1

                    if current_length >= len(prompt) * threshold:
                        print("Prompt verification threshold matched fully.")
                        break

                    if stable_checks >= max_stable_allowed:
                        print("Prompt buffer growth stalled completely. Breaking out.")
                        break

                    self.page.wait_for_timeout(1000)

                if len(current_text) < len(prompt) * threshold:
                    print("\nPROMPT PASTE FAILED\n")
                    print(f"Expected Length: {len(prompt)} (Threshold Limit: {threshold * 100}%)")
                    print(f"Actual Length: {len(current_text)}")
                    raise RuntimeError("Prompt paste verification failed due to data truncation.")

                print(f"\nPROMPT VERIFIED | Expected={len(prompt)} | Actual={len(current_text)}\n")

                # M7: Dedup check
                recent_msg = self._get_recent_message_text()
                already_submitted = False
                if pre_submit_last_message and recent_msg:
                    prompt_head = prompt.strip()[:200]
                    if prompt_head in recent_msg:
                        print("[M7 DEDUP] Prompt already visible in chat. Waiting for response instead of resubmitting.")
                        already_submitted = True

                pre_submit_turn_count = 0
                if not already_submitted:
                    # Fix 8: Composer readiness
                    self._wait_for_composer_ready(timeout=10)

                    pre_submit_turn_count = self._assistant_turn_count()
                    self._set_state("SUBMITTING")

                    # Locate send button
                    if "chatgpt.com" in current_url:
                        send_button_sel = 'button[data-testid="send-button"]'
                        send_button = self.page.locator(send_button_sel).first
                    else:
                        send_button_sel = 'button[aria-label="Send message"]'
                        send_button = self.page.locator(send_button_sel).first
                        if send_button.count() == 0:
                            send_button_sel = \
                                'button[aria-label*="Send"]:not([aria-label*="More"])'
                            send_button = self.page.locator(send_button_sel).first

                    if len(prompt) > 8000:
                        self.page.wait_for_timeout(2000)

                    # Fix 2+3: Multi-path verified submission
                    submission_verified = self._submit_prompt_verified(
                        send_button, send_button_sel, textbox
                    )

                    if not submission_verified:
                        raise RuntimeError(
                            "Submission failed: no generation started after all methods."
                        )

                    self._set_state("SUBMITTED")

                # Fix 6: Track conversation URL (same-chat preservation)
                if self._is_specific_chat_url(self.page.url):
                    self.chat_url = self.page.url
                    print("[RECOVERY]")
                    print("Using existing conversation")
                    print(f"[RECOVERY] Tracked URL={self.chat_url}")

                # Gemini sets GENERATING here; ChatGPT owns its own
                # WAITING_FOR_NEW_RESPONSE -> GENERATING -> COMPLETE machine
                # inside _chatgpt_lifecycle.
                if is_gemini:
                    self._set_state("GENERATING")

                # Fix 9: Scroll to bottom before extraction
                if is_gemini:
                    self._scroll_conversation_to_bottom()
                    print("[EXTRACTION]")
                    print("Scrolled to latest response")

                recovery_attempts = 0
                result = ""

                while True:
                    if is_gemini:
                        # Gemini-specific lifecycle:
                        # WAITING_FOR_FIRST_TOKEN -> STREAMING -> COMPLETE.
                        result, did_recover, pre_submit_count, pre_submit_turn_count, recovery_attempts = (
                            self._gemini_lifecycle(
                                prompt, pre_submit_count, pre_submit_turn_count,
                                recovery_attempts, dispute_key
                            )
                        )
                        if did_recover:
                            continue
                        break

                    # ChatGPT correctness-first lifecycle. Returns a verified
                    # NEW response (state -> COMPLETE) or raises
                    # ResponseExtractionError (RESPONSE_TIMEOUT /
                    # EXTRACTION_FAILURE). Never falls back to a stale last
                    # available response.
                    result = self._chatgpt_lifecycle(
                        prompt, pre_submit_count, baseline_identity
                    )
                    break

                # Fallback extraction -- Gemini only. ChatGPT must NEVER fall
                # back to a last-available node: that is the stale-response bug
                # (the previous turn's text returned as the current result). The
                # ChatGPT lifecycle raises ResponseExtractionError on any
                # failure, so reaching here with is_gemini=False and no result
                # is itself a bug -- hard-fail below rather than silent success.
                if is_gemini and not result:
                    try:
                        result = self._latest_response_text()
                        if result:
                            print(f"[RESPONSE EXTRACTION FALLBACK] Using last available response. Length={len(result)}")
                    except Exception:
                        pass

                if not result:
                    raise RuntimeError("Response extraction failed: no valid response nodes found.")

                if self._is_invalid_response(prompt, result):
                    raise RuntimeError("Invalid or truncated response extracted; retrying prompt.")

                # Fix 11: Check the extracted result for platform errors
                if self._detect_platform_error(result):
                    self._set_state("FAILED")
                    raise RuntimeError("Gemini platform error detected in extracted response.")

                if DEBUG_VERBOSE:
                    print("\n===== RESPONSE PREVIEW =====")
                    print(result[:1000])
                    print("============================")
                print(f"\n===== RESPONSE LENGTH: {len(result)} =====")

                current_url = self.page.url
                if self._is_specific_chat_url(current_url):
                    self.chat_url = current_url
                    print(f"Tracked Conversation URL: {self.chat_url}")

                self._set_state("IDLE")
                return result

            except Exception as e:
                print(f"\n[Attempt {attempt}/{max_retries}] ChatSession ask failed: {str(e)}")
                self._set_state("FAILED")
                if attempt < max_retries:
                    if (isinstance(e, ResponseExtractionError)
                        or "Gemini generation stalled" in str(e)
                        or "Invalid or truncated response" in str(e)
                        or "Submission failed" in str(e)
                        or "Recovery submission failed" in str(e)):
                        print("[RETRY] Retrying prompt inline in the same conversation.")
                        # Fix 7: Restore conversation if needed
                        self._ensure_chat_integrity()
                        self.page.wait_for_timeout(2000)
                    elif self.chat_url and self._is_specific_chat_url(self.chat_url):
                        print(f"[RETRY] Reloading specific chat URL: {self.chat_url}")
                        self.page.goto(self.chat_url, wait_until="domcontentloaded", timeout=120000)
                        self.page.wait_for_timeout(3000)
                    elif self.chat_url:
                        print("[RETRY] chat_url is a base URL -- NOT reloading to avoid wiping chat history. "
                              "Attempting to clear input and retry inline.")
                        try:
                            textbox = self.page.locator(self.input_selector).first
                            if textbox.is_visible():
                                textbox.click()
                                textbox.evaluate("el => { el.innerHTML = ''; el.value = ''; }")
                        except Exception:
                            self.page.keyboard.press("Control+A")
                            self.page.keyboard.press("Backspace")
                        self.page.wait_for_timeout(2000)
                    else:
                        print("[RETRY] No specific chat URL tracked. Attempting inline recovery without page reload.")
                        try:
                            textbox = self.page.locator(self.input_selector).first
                            if textbox.is_visible():
                                textbox.click()
                                textbox.evaluate("el => { el.innerHTML = ''; el.value = ''; }")
                        except Exception:
                            self.page.keyboard.press("Control+A")
                            self.page.keyboard.press("Backspace")
                        self.page.wait_for_timeout(2000)
                    self._set_state("INPUT_READY")
                else:
                    # Terminal attempt: preserve a structured ResponseExtractionError
                    # so the caller can distinguish SUCCESS / RESPONSE_TIMEOUT /
                    # EXTRACTION_FAILURE. A generic wrap would erase the
                    # diagnostics contract.
                    if isinstance(e, ResponseExtractionError):
                        raise
                    try:
                        screenshot_path = f"failure_attempt_{attempt}.png"
                        self.page.screenshot(path=screenshot_path, full_page=True)
                        print(f"Diagnostic screenshot saved to: {screenshot_path}")
                    except Exception:
                        pass

                    raise RuntimeError(f"ChatSession permanently failed after {max_retries} attempts. Last error: {str(e)}")

    def close(self):
        try:
            if self.page:
                self.page.close()
                self.page = None
        except Exception:
            pass


# ------------------------------------------------------------------
# Ponytail self-check: Gemini lifecycle (run via `python chat_session.py`)
# No frameworks, no browser. Verification of the spec's three guarantees.
# ------------------------------------------------------------------
if __name__ == "__main__":

    def _pass(name):
        print(f"{name}: PASS")

    def test_gemini_long_prompt():
        # Spec: prompt > 8000 chars selects the 120s stall threshold.
        s = ChatSession.__new__(ChatSession)
        assert s._gemini_stall_threshold("x" * 9000) == 120
        assert s._gemini_stall_threshold("x" * 8001) == 120
        assert s._gemini_stall_threshold("x" * 7001) == 120
        assert s._gemini_stall_threshold("x" * 7000) == 90
        assert s._gemini_stall_threshold("x" * 3000) == 90
        assert s._gemini_stall_threshold("x" * 2999) == 60
        _pass("test_gemini_long_prompt")

    class _FakeLocator:
        def __init__(self, page):
            self._page = page

        @property
        def first(self):
            return self

        def count(self):
            return 1 if self._page._stop_visible else 0

        def is_visible(self):
            return self._page._stop_visible

        def click(self, *a, **k):
            self._page._stop_clicks += 1

    class _FakePage:
        def __init__(self, stop_visible=False):
            self._stop_visible = stop_visible
            self._stop_clicks = 0

        def locator(self, selector):
            return _FakeLocator(self)

        def wait_for_timeout(self, ms):
            return None

    def _scripted_latest(script):
        idx = {"i": 0}

        def _fn(*args, **kwargs):
            v = script[idx["i"]] if idx["i"] < len(script) else script[-1]
            idx["i"] += 1
            return v

        return _fn

    def test_gemini_first_token_wait():
        # Spec: no Stop click occurs before the first token, even when Stop is visible.
        s = ChatSession.__new__(ChatSession)
        s.state = "IDLE"
        s.stop_selector = 'button[aria-label*="Stop"]'
        page = _FakePage(stop_visible=True)
        s.page = page
        s._latest_response_text = _scripted_latest(["", "", "", "", "first token"])
        s._scroll_conversation_to_bottom = lambda *a, **k: None
        token = s._gemini_wait_for_first_token({}, timeout=120)
        assert token == "first token", token
        assert page._stop_clicks == 0, "Stop clicked before first token"
        _pass("test_gemini_first_token_wait")

    def test_gemini_streaming():
        # Spec: WAITING_FOR_FIRST_TOKEN -> STREAMING -> COMPLETE before extraction.
        import time as _time
        real_time = _time.time
        clock = {"t": 0.0}

        def _fake_time():
            clock["t"] += 0.1
            return clock["t"]

        _time.time = _fake_time
        try:
            s = ChatSession.__new__(ChatSession)
            s.state = "IDLE"
            s.stop_selector = 'button[aria-label*="Stop"]'
            page = _FakePage(stop_visible=False)
            s.page = page
            s._latest_response_text = _scripted_latest(
                ["", "", "", "Hi", "Hi there", "Hi there world",
                 "FINAL", "FINAL", "FINAL"]
            )
            s._scroll_conversation_to_bottom = lambda *a, **k: None
            states = []
            orig_set = s._set_state

            def trace_set(st):
                states.append(st)
                orig_set(st)

            s._set_state = trace_set
            result, did_recover, *_rest = s._gemini_lifecycle(
                "Tell me about Gemini.", {}, 0, 0, None
            )
        finally:
            _time.time = real_time
        assert did_recover is False
        assert result == "FINAL", result
        assert states == ["WAITING_FOR_FIRST_TOKEN", "STREAMING", "COMPLETE"], states
        assert page._stop_clicks == 0
        _pass("test_gemini_streaming")

    # ------------------------------------------------------------------
    # Web-search stall-detection tests (no browser; fake clock + stubs).
    # ------------------------------------------------------------------
    class _StopTest(Exception):
        pass

    class _HealthLocator:
        def __init__(self, page):
            self._page = page

        @property
        def first(self):
            return self

        def count(self):
            return 1 if self._page._stop_visible else 0

        def is_visible(self):
            return self._page._stop_visible

        def nth(self, i):
            return self

        def click(self, *a, **k):
            self._page._stop_clicks += 1

        def wait_for(self, *a, **k):
            return None

        def evaluate(self, *a, **k):
            return 0

    class _HealthPage:
        def __init__(self, stop_visible=True, raise_after=10 ** 9):
            self._stop_visible = stop_visible
            self._stop_clicks = 0
            self._raise_after = raise_after

        def locator(self, selector):
            return _HealthLocator(self)

        def wait_for_timeout(self, ms):
            if time.time() > self._raise_after:
                raise _StopTest()

    def _seq(values):
        idx = {"i": 0}

        def _fn(*a, **k):
            v = values[idx["i"]] if idx["i"] < len(values) else values[-1]
            idx["i"] += 1
            return v

        return _fn

    def _inc():
        n = {"v": 0}

        def _fn(*a, **k):
            n["v"] += 1
            return n["v"]

        return _fn

    def _patch_clock(per_call=1.0):
        real = time.time
        state = {"t": 0.0}

        def _fake():
            state["t"] += per_call
            return state["t"]

        time.time = _fake
        return real

    def _build_health_session(page, latest, dom, node, web_search, activity):
        s = ChatSession.__new__(ChatSession)
        s.state = "IDLE"
        s.stop_selector = 'button[aria-label*="Stop"]'
        s.page = page
        s._latest_response_text = latest
        s._gemini_dom_signature = dom
        s._gemini_response_node_signature = node
        s._gemini_web_search_active = web_search
        s._gemini_page_activity = activity
        s._scroll_conversation_to_bottom = lambda *a, **k: None
        recover = {"called": False}

        def fake_recover(*a, **k):
            recover["called"] = True
            btn = s._gemini_stop_button()
            if btn is not None:
                btn.click()
            return (True, {}, 0)

        s._recover_from_stall = fake_recover
        states = []
        orig = s._set_state

        def trace(st):
            states.append(st)
            orig(st)

        s._set_state = trace
        return s, states, recover

    def test_web_search_pause():
        # Spec: no text growth for 120s WITH DOM changes -> no stop click.
        real = _patch_clock(1.0)
        try:
            page = _HealthPage(stop_visible=True, raise_after=125)
            s, states, recover = _build_health_session(
                page,
                latest=_seq(["RESPONSE"]),
                dom=_inc(),
                node=_seq([()]),
                web_search=_seq([False]),
                activity=_seq([False]),
            )
            try:
                s._gemini_lifecycle("x" * 100, {}, 0, 0, None)
            except _StopTest:
                pass
        finally:
            time.time = real
        assert page._stop_clicks == 0, \
            f"stop clicked {page._stop_clicks}x during a DOM-active pause"
        assert recover["called"] is False
        assert "STALLED" not in states
        _pass("test_web_search_pause")

    def test_long_reasoning_pause():
        # Spec: 180s pause with active page mutations -> generation continues.
        # Latches the 300s web-search threshold on an initial web-search
        # indicator, then DOM mutations keep the stall clock reset.
        real = _patch_clock(1.0)
        try:
            page = _HealthPage(stop_visible=True, raise_after=185)
            s, states, recover = _build_health_session(
                page,
                latest=_seq(["RESPONSE"]),
                dom=_inc(),
                node=_seq([()]),
                web_search=_seq([True, True, True, False]),
                activity=_seq([False]),
            )
            try:
                s._gemini_lifecycle("x" * 100, {}, 0, 0, None)
            except _StopTest:
                pass
        finally:
            time.time = real
        assert page._stop_clicks == 0, \
            f"stop clicked {page._stop_clicks}x during active reasoning"
        assert recover["called"] is False
        assert "STALLED" not in states
        _pass("test_long_reasoning_pause")

    def test_true_stall():
        # Spec: no text growth, no DOM changes, no activity -> stop allowed.
        real = _patch_clock(1.0)
        try:
            page = _HealthPage(stop_visible=True, raise_after=10 ** 9)
            s, states, recover = _build_health_session(
                page,
                latest=_seq(["RESPONSE"]),
                dom=_seq(["d"]),
                node=_seq([()]),
                web_search=_seq([False]),
                activity=_seq([False]),
            )
            s._gemini_lifecycle("x" * 100, {}, 0, 0, None)
        finally:
            time.time = real
        assert page._stop_clicks >= 1, "stop click not allowed on a true stall"
        assert recover["called"] is True
        assert "STALLED" in states
        _pass("test_true_stall")

    def _growing(step=10, start=20):
        n = {"i": 0}
        def _fn(*a, **k):
            n["i"] += 1
            return "x" * (start + step * n["i"])
        return _fn

    def test_slow_streaming():
        # Concern: text growing < MEANINGFUL_GROWTH_CHARS per tick could
        # falsely stall. Slow steady growth must not be interrupted.
        # No DOM changes, no web search, no activity — only text grows.
        real = _patch_clock(1.0)
        try:
            page = _HealthPage(stop_visible=True, raise_after=150)
            s, states, recover = _build_health_session(
                page,
                latest=_growing(step=10, start=20),
                dom=_seq(["d"]),
                node=_seq([()]),
                web_search=_seq([False]),
                activity=_seq([False]),
            )
            try:
                s._gemini_lifecycle("x" * 100, {}, 0, 0, None)
            except _StopTest:
                pass
        finally:
            time.time = real
        assert page._stop_clicks == 0, \
            f"stop clicked {page._stop_clicks}x during slow streaming"
        assert recover["called"] is False
        assert "STALLED" not in states
        _pass("test_slow_streaming")

    def test_bursty_streaming():
        # Concern: text growing in bursts with brief pauses between bursts
        # must not be interrupted. Each burst resets the stall clock.
        # Pauses here are ~10s, well under the 60s threshold.
        script = []
        base = 100
        for _ in range(10):
            for _ in range(5):
                script.append("x" * base)
            base += 200
            for _ in range(2):
                script.append("x" * base)
        real = _patch_clock(1.0)
        try:
            page = _HealthPage(stop_visible=True, raise_after=120)
            s, states, recover = _build_health_session(
                page,
                latest=_seq(script),
                dom=_seq(["d"]),
                node=_seq([()]),
                web_search=_seq([False]),
                activity=_seq([False]),
            )
            try:
                s._gemini_lifecycle("x" * 100, {}, 0, 0, None)
            except _StopTest:
                pass
        finally:
            time.time = real
        assert page._stop_clicks == 0, \
            f"stop clicked {page._stop_clicks}x during bursty streaming"
        assert recover["called"] is False
        assert "STALLED" not in states
        _pass("test_bursty_streaming")

    # ------------------------------------------------------------------
    # False-submit verification tests (no browser; stubbed DOM).
    # Spec: success ONLY on user-count bump / composer cleared / stop
    # appeared; otherwise failed. The old "send button disappeared"
    # signal is gone (it caused the false submits).
    # ------------------------------------------------------------------
    class _VPage:
        def __init__(self, composer="prompt text", stop_visible=False):
            self._composer = composer
            self._stop_visible = stop_visible

        def locator(self, selector):
            return _VLoc(self, selector)

        def wait_for_timeout(self, ms):
            pass

    class _VLoc:
        def __init__(self, page, selector):
            self._page = page
            self._selector = selector

        @property
        def first(self):
            return self

        def count(self):
            return 1 if self._page._stop_visible else 0

        def is_visible(self):
            return self._page._stop_visible

        def evaluate(self, expr, *a, **k):
            if "outerHTML" in expr:
                return '<button aria-label="Send message"></button>'
            return self._page._composer

    def test_verify_submit_accept_via_count():
        s = ChatSession.__new__(ChatSession)
        s.state = "IDLE"
        s.input_selector = "rich-textarea"
        s.stop_selector = 'button[aria-label*="Stop"]'
        s.page = _VPage(composer="leftover prompt", stop_visible=False)
        s._user_message_count = lambda: 2
        ok = s._verify_submission(1, s.page.locator("button"), timeout=0)
        assert ok is True, "count bump must verify submission"
        _pass("test_verify_submit_accept_via_count")

    def test_verify_submit_reject_no_signal():
        s = ChatSession.__new__(ChatSession)
        s.state = "IDLE"
        s.input_selector = "rich-textarea"
        s.stop_selector = 'button[aria-label*="Stop"]'
        s.page = _VPage(composer="prompt still here", stop_visible=False)
        s._user_message_count = lambda: 2
        ok = s._verify_submission(2, s.page.locator("button"), timeout=0)
        assert ok is False, "no count bump / cleared / stop must reject"
        _pass("test_verify_submit_reject_no_signal")

    # ==================================================================
    # ChatGPT response-extraction lifecycle tests (no browser; fake clock
    # + scripted DOM). Verifies the spec: a response is returned ONLY when a
    # new assistant node has stabilized, timeouts never become silent
    # success, the previous turn is never returned, and a partial response
    # that never stabilizes is discarded as RESPONSE_TIMEOUT.
    # ==================================================================
    _AST = '[data-message-author-role="assistant"]'

    class _ChatGPTFakeLoc:
        # Snapshot stop-state once per locator: a real browser's count() and
        # is_visible() reflect the same DOM instant, so the two reads inside
        # _stop_button_visible must NOT consume two scripted values.
        def __init__(self, page):
            self._page = page
            self._sv = page._stop_visible()

        @property
        def first(self):
            return self

        def count(self):
            return 1 if self._sv else 0

        def is_visible(self):
            return self._sv

    class _ChatGPTFakePage:
        def __init__(self, url, stop_visible_fn, advance):
            self.url = url
            self._stop_visible = stop_visible_fn
            self._advance = advance

        def locator(self, selector):
            return _ChatGPTFakeLoc(self)

        def wait_for_timeout(self, ms):
            if self._advance is not None:
                self._advance(ms / 1000.0)

    def _chatgpt_clock():
        real = time.time
        state = {"t": 0.0}
        time.time = lambda: state["t"]

        def advance(seconds):
            state["t"] += seconds

        return real, advance

    def _build_chatgpt_session(latest_fn, counts_fn, baseline_counts,
                               baseline_last_text="",
                               url="https://chatgpt.com/c/abc",
                               stop_visible_fn=None, advance=None):
        s = ChatSession.__new__(ChatSession)
        s.state = "IDLE"
        s.stop_selector = '[data-testid="stop-button"]'
        s.response_selector = _AST
        s.chat_url = url
        page = _ChatGPTFakePage(url, stop_visible_fn or (lambda: False), advance)
        s.page = page
        s._latest_response_text = latest_fn
        s._response_selector_counts = counts_fn
        s._scroll_conversation_to_bottom = lambda *a, **k: None
        states = []
        orig = s._set_state

        def trace(st):
            states.append(st)
            orig(st)

        s._set_state = trace
        baseline_identity = {
            "counts": baseline_counts,
            "last_hash": s._response_hash(baseline_last_text),
            "last_length": len(baseline_last_text or ""),
            "url": url,
        }
        return s, states, baseline_identity, page

    _DIAG_FIELDS = (
        "error_code", "conversation_url", "elapsed_time",
        "baseline_response_identity", "observed_response_identity",
        "observed_response_length", "stability_count", "poll_count",
        "browser_state", "last_error",
    )

    def _expect_extraction_error(fn, code):
        try:
            fn()
        except ResponseExtractionError as e:
            assert e.error_code == code, (e.error_code, code)
            for field in _DIAG_FIELDS:
                assert field in e.diagnostics, f"missing diag field {field}"
            return e
        raise AssertionError(f"expected {code}, no error raised")

    SHORT_PROMPT = "What is 2+2?"

    def test_chatgpt_new_response_completes():
        # 1. New response detected and completes normally.
        real, advance = _chatgpt_clock()
        try:
            latest = _seq(["", "ANSWER", "ANSWER", "ANSWER", "ANSWER"])
            s, states, baseline, _page = _build_chatgpt_session(
                latest, lambda: {_AST: 2}, {_AST: 1},
                baseline_last_text="OLD", advance=advance)
            result = s._chatgpt_lifecycle(SHORT_PROMPT, {_AST: 1}, baseline)
            assert result == "ANSWER", result
            assert states[-1] == "COMPLETE", states
            assert "FAILED" not in states
        finally:
            time.time = real
        _pass("test_chatgpt_new_response_completes")

    def test_chatgpt_stop_absent_completes():
        # 2. Stop button is absent but response stabilizes.
        real, advance = _chatgpt_clock()
        try:
            text30 = "x" * 30
            text60 = "x" * 60
            latest = _seq(["", text30, text60, text60, text60, text60])
            s, states, baseline, _page = _build_chatgpt_session(
                latest, lambda: {_AST: 2}, {_AST: 1},
                baseline_last_text="OLD", advance=advance,
                stop_visible_fn=lambda: False)
            result = s._chatgpt_lifecycle(SHORT_PROMPT, {_AST: 1}, baseline)
            assert result == text60
            assert states[-1] == "COMPLETE", states
        finally:
            time.time = real
        _pass("test_chatgpt_stop_absent_completes")

    def test_chatgpt_stop_appears_completes():
        # 3. Stop button appears and response stabilizes.
        real, advance = _chatgpt_clock()
        try:
            latest = _seq(["", "ANSWER", "ANSWER", "ANSWER",
                           "ANSWER", "ANSWER"])
            s, states, baseline, _page = _build_chatgpt_session(
                latest, lambda: {_AST: 2}, {_AST: 1},
                baseline_last_text="OLD", advance=advance,
                stop_visible_fn=_seq([True] * 12))
            result = s._chatgpt_lifecycle(SHORT_PROMPT, {_AST: 1}, baseline)
            assert result == "ANSWER", result
            assert states[-1] == "COMPLETE", states
        finally:
            time.time = real
        _pass("test_chatgpt_stop_appears_completes")

    def test_chatgpt_baseline_captures_previous():
        # 4. Previous response exists before submission -> baseline holds it.
        real = time.time
        time.time = lambda: 0.0
        try:
            prev = "PREVIOUS ANSWER A"
            s = ChatSession.__new__(ChatSession)
            s.page = _ChatGPTFakePage("https://chatgpt.com/c/abc",
                                       lambda: False, None)
            s.response_selector = _AST
            s._response_selector_counts = lambda: {_AST: 1}
            s._latest_response_text = lambda *a, **k: prev
            baseline = s._capture_baseline_identity()
            assert baseline["counts"] == {_AST: 1}
            assert baseline["last_length"] == len(prev)
            assert baseline["last_hash"] == s._response_hash(prev)
        finally:
            time.time = real
        _pass("test_chatgpt_baseline_captures_previous")

    def test_chatgpt_new_response_follows_previous():
        # 5. New response follows previous response.
        real, advance = _chatgpt_clock()
        try:
            latest = _seq(["", "ANSWER_B", "ANSWER_B", "ANSWER_B", "ANSWER_B"])
            s, states, baseline, _page = _build_chatgpt_session(
                latest, lambda: {_AST: 2}, {_AST: 1},
                baseline_last_text="ANSWER_A", advance=advance)
            result = s._chatgpt_lifecycle(SHORT_PROMPT, {_AST: 1}, baseline)
            assert result == "ANSWER_B", result
            assert result != "ANSWER_A"
            assert states[-1] == "COMPLETE", states
        finally:
            time.time = real
        _pass("test_chatgpt_new_response_follows_previous")

    def test_chatgpt_previous_response_not_returned():
        # 6. Previous response is NOT returned (no new node -> timeout).
        real, advance = _chatgpt_clock()
        try:
            latest = lambda *a, **k: ""  # require_new finds nothing
            s, states, baseline, _page = _build_chatgpt_session(
                latest, lambda: {_AST: 1}, {_AST: 1},
                baseline_last_text="PREVIOUS ANSWER A", advance=advance)
            e = _expect_extraction_error(
                lambda: s._chatgpt_lifecycle(SHORT_PROMPT, {_AST: 1}, baseline),
                "RESPONSE_TIMEOUT")
            assert "PREVIOUS" not in (e.diagnostics.get("last_error") or "")
            assert e.diagnostics["observed_response_length"] == 0
            assert e.diagnostics["browser_state"] == "FAILED"
        finally:
            time.time = real
        _pass("test_chatgpt_previous_response_not_returned")

    def test_chatgpt_partial_does_not_complete_prematurely():
        # 7. Partial response does not complete during a temporary pause.
        real, advance = _chatgpt_clock()
        try:
            latest = _seq(["A", "AB", "AB", "AB", "AB",
                           "ABC", "ABC", "ABC", "ABC"])
            s, states, baseline, _page = _build_chatgpt_session(
                latest, lambda: {_AST: 2}, {_AST: 1},
                baseline_last_text="OLD", advance=advance,
                stop_visible_fn=_seq([True, False]))
            result = s._chatgpt_lifecycle(SHORT_PROMPT, {_AST: 1}, baseline)
            assert result == "ABC", result
            assert result != "AB", "completed prematurely during pause"
            assert states[-1] == "COMPLETE", states
        finally:
            time.time = real
        _pass("test_chatgpt_partial_does_not_complete_prematurely")

    def test_chatgpt_response_resumes_growing_after_pause():
        # 8. Response resumes growing after a temporary pause (meaningful
        # growth path resets stability after the pause).
        real, advance = _chatgpt_clock()
        try:
            short = "x" * 5
            long_text = "x" * 60
            latest = _seq([short, short, short, short,
                           long_text, long_text, long_text, long_text])
            s, states, baseline, _page = _build_chatgpt_session(
                latest, lambda: {_AST: 2}, {_AST: 1},
                baseline_last_text="OLD", advance=advance,
                stop_visible_fn=_seq([True, False]))
            result = s._chatgpt_lifecycle(SHORT_PROMPT, {_AST: 1}, baseline)
            assert result == long_text, result
            assert len(result) > len(short) + MEANINGFUL_GROWTH_CHARS
            assert states[-1] == "COMPLETE", states
        finally:
            time.time = real
        _pass("test_chatgpt_response_resumes_growing_after_pause")

    def test_chatgpt_empty_response_times_out():
        # 9. Empty response times out (empty is NOT success).
        real, advance = _chatgpt_clock()
        try:
            latest = lambda *a, **k: ""
            s, states, baseline, _page = _build_chatgpt_session(
                latest, lambda: {_AST: 0}, {_AST: 0},
                baseline_last_text="", advance=advance)
            e = _expect_extraction_error(
                lambda: s._chatgpt_lifecycle(SHORT_PROMPT, {_AST: 0}, baseline),
                "RESPONSE_TIMEOUT")
            assert e.diagnostics["observed_response_length"] == 0
            assert e.diagnostics["poll_count"] > 0
        finally:
            time.time = real
        _pass("test_chatgpt_empty_response_times_out")

    def test_chatgpt_generation_timeout():
        # 10. Generation timeout returns RESPONSE_TIMEOUT (partial discarded).
        real, advance = _chatgpt_clock()
        try:
            latest = _growing(step=10, start=20)  # never stabilizes
            s, states, baseline, _page = _build_chatgpt_session(
                latest, lambda: {_AST: 2}, {_AST: 1},
                baseline_last_text="OLD", advance=advance)
            e = _expect_extraction_error(
                lambda: s._chatgpt_lifecycle(SHORT_PROMPT, {_AST: 1}, baseline),
                "RESPONSE_TIMEOUT")
            assert e.diagnostics["observed_response_length"] > 0
            assert "partial response discarded" in e.diagnostics["last_error"]
            assert e.diagnostics["browser_state"] == "FAILED"
        finally:
            time.time = real
        _pass("test_chatgpt_generation_timeout")

    def test_chatgpt_extraction_failure_structured():
        # 11. Extraction failure returns a structured failure (stale guard:
        # a non-empty read whose count never exceeds baseline -> the previous
        # turn must NOT be returned).
        real, advance = _chatgpt_clock()
        try:
            latest = _seq(["", "STALE OLD", "STALE OLD", "STALE OLD", "STALE OLD"])
            s, states, baseline, _page = _build_chatgpt_session(
                latest, lambda: {_AST: 1}, {_AST: 1},  # counts == baseline
                baseline_last_text="STALE OLD", advance=advance)
            e = _expect_extraction_error(
                lambda: s._chatgpt_lifecycle(SHORT_PROMPT, {_AST: 1}, baseline),
                "EXTRACTION_FAILURE")
            assert "Stale-response guard" in e.diagnostics["last_error"]
            assert e.diagnostics["observed_response_length"] == len("STALE OLD")
            assert e.diagnostics["stability_count"] >= REQUIRED_STABLE_OBSERVATIONS
            assert e.diagnostics["browser_state"] == "FAILED"
        finally:
            time.time = real
        _pass("test_chatgpt_extraction_failure_structured")

    def test_chatgpt_repeated_prompts_persistent_conversation():
        # 12. Repeated prompts in the same persistent conversation: Prompt A
        # leaves ANSWER_A in the DOM (baseline count 1). Prompt B must return
        # ANSWER_B -- never ANSWER_A.
        real, advance = _chatgpt_clock()
        try:
            latest = _seq(["", "ANSWER_B", "ANSWER_B", "ANSWER_B", "ANSWER_B"])
            s, states, baseline, _page = _build_chatgpt_session(
                latest, lambda: {_AST: 2}, {_AST: 1},  # A is node 1, B is node 2
                baseline_last_text="ANSWER_A", advance=advance)
            result = s._chatgpt_lifecycle(SHORT_PROMPT, {_AST: 1}, baseline)
            assert result == "ANSWER_B", result
            assert result != "ANSWER_A"
            assert states[-1] == "COMPLETE", states
        finally:
            time.time = real
        _pass("test_chatgpt_repeated_prompts_persistent_conversation")

    test_gemini_long_prompt()
    test_gemini_first_token_wait()
    test_gemini_streaming()
    test_web_search_pause()
    test_long_reasoning_pause()
    test_true_stall()
    test_slow_streaming()
    test_bursty_streaming()
    test_verify_submit_accept_via_count()
    test_verify_submit_reject_no_signal()
    print("\nAll Gemini lifecycle tests passed.")

    test_chatgpt_new_response_completes()
    test_chatgpt_stop_absent_completes()
    test_chatgpt_stop_appears_completes()
    test_chatgpt_baseline_captures_previous()
    test_chatgpt_new_response_follows_previous()
    test_chatgpt_previous_response_not_returned()
    test_chatgpt_partial_does_not_complete_prematurely()
    test_chatgpt_response_resumes_growing_after_pause()
    test_chatgpt_empty_response_times_out()
    test_chatgpt_generation_timeout()
    test_chatgpt_extraction_failure_structured()
    test_chatgpt_repeated_prompts_persistent_conversation()
    print("\nAll ChatGPT response-extraction lifecycle tests passed.")
