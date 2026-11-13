from __future__ import annotations

import build_workspace
import human_decision_manager
import resume
import stage_status
import staged_executor


def test_staged_executor_blocks_pending_and_unavailable_store(monkeypatch):
    monkeypatch.setattr(human_decision_manager, "load_pending",
                        lambda _: [{"decision_id": "d1", "decision": "choose"}])
    result = staged_executor.execute_stages("workspace")
    assert result["blocked_reason"] == "pending_human_decisions"
    assert result["pending_count"] == 1
    assert result["pending_decisions"][0]["decision_id"] == "d1"

    def unavailable(_):
        raise OSError("database unavailable")

    monkeypatch.setattr(human_decision_manager, "load_pending", unavailable)
    result = staged_executor.execute_stages("workspace")
    assert result["blocked_reason"] == "human_decision_store_unavailable"
    assert "database unavailable" in result["error"]


def test_resume_force_cannot_bypass_humans_or_store_failure(monkeypatch):
    pending_count = resume._pending_human_count
    monkeypatch.setattr(resume, "_pending_human_count", lambda _: 2)
    pending = resume.resume_build("workspace", force=True)
    assert pending["reason"] == "blocked_pending_humans"
    assert pending["pending_count"] == 2

    def fail_store(_):
        raise OSError("database unavailable")

    monkeypatch.setattr(human_decision_manager, "load_pending", fail_store)
    monkeypatch.setattr(resume, "_pending_human_count", pending_count)
    unavailable = resume.resume_build("workspace", force=True)
    assert unavailable["reason"] == "human_decision_store_unavailable"


def test_force_rearms_failed_stage_after_humans_are_resolved(tmp_path, monkeypatch):
    monkeypatch.setattr(build_workspace, "WORKSPACE_ROOT", tmp_path)
    wid = build_workspace.create_workspace(source_question="resume")
    stage_status.write_build_plan(wid, {
        "stages": [{"stage_id": "stage_1", "dependencies": []}],
    })
    stage_status.write_stage_status(wid, "stage_1", stage_status.StageStatus.FAILED)
    monkeypatch.setattr(resume, "_pending_human_count", lambda _: 0)

    result = resume.resume_build(wid, force=True)
    assert result["reason"] == "staged_offline_no_execution"
    assert stage_status.read_stage_status(wid, "stage_1")["status"] == "PENDING"


def test_chatgpt_user_message_count_uses_current_bubble_selector():
    import chat_session

    class Locator:
        def count(self):
            return 3

    class Page:
        url = "https://chatgpt.com/c/example"

        def __init__(self):
            self.selector = None

        def locator(self, selector):
            self.selector = selector
            return Locator()

    session = chat_session.ChatSession.__new__(chat_session.ChatSession)
    session.page = Page()
    assert session._user_message_count() == 3
    assert session.page.selector == '[data-user-message-bubble="true"]'


def test_chatgpt_submission_requires_new_user_bubble():
    import chat_session

    class Locator:
        def __init__(self, page):
            self.page = page

        @property
        def first(self):
            return self

        def evaluate(self, expression):
            if "outerHTML" in expression:
                return "<button></button>"
            return ""

        def count(self):
            return 1 if self.page.stop_visible else 0

        def is_visible(self):
            return self.page.stop_visible

    class Page:
        url = "https://chatgpt.com/c/example"
        stop_visible = True

        def locator(self, selector):
            return Locator(self)

        def wait_for_timeout(self, milliseconds):
            pass

    session = chat_session.ChatSession.__new__(chat_session.ChatSession)
    session.page = Page()
    session.input_selector = '[contenteditable="true"]'
    session.stop_selector = '[data-testid="stop-button"]'
    session._user_message_count = lambda: 4

    accepted = session._verify_submission(
        4, session.page.locator("button"), timeout=0.01
    )
    assert accepted is False


def test_gemini_submission_keeps_existing_fallback_signals():
    import chat_session

    class Locator:
        @property
        def first(self):
            return self

        def evaluate(self, expression):
            if "outerHTML" in expression:
                return "<button></button>"
            return ""

        def count(self):
            return 0

        def is_visible(self):
            return False

    class Page:
        url = "https://gemini.google.com/app/example"

        def locator(self, selector):
            return Locator()

        def wait_for_timeout(self, milliseconds):
            pass

    session = chat_session.ChatSession.__new__(chat_session.ChatSession)
    session.page = Page()
    session.input_selector = "rich-textarea"
    session.stop_selector = 'button[aria-label*="Stop"]'
    session._user_message_count = lambda: 2

    accepted = session._verify_submission(
        2, session.page.locator("button"), timeout=0.01
    )
    assert accepted is True
