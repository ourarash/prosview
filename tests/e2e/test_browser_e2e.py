"""End-to-end tests that drive Proseview in a real browser.

These cover the surface that only exists once the page's JavaScript runs: the
ProseMirror editor, the selection menu, highlight passes, deep links, the
terminal, and the AI proposal bridge arriving over SSE.

Opt-in -- excluded from the default ``pytest`` run by the ``e2e_browser`` marker.

    pip install -e ".[e2e]"
    python -m playwright install chromium
    pytest -m e2e_browser

ProseMirror is vendored under ``proseview/templates/vendor/pm/`` and served
from the app's own origin, so this tier needs no network access at all. The
36-module graph is cached in-process to keep page loads cheap; see
``_install_esm_cache``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from urllib.parse import urlparse
from pathlib import Path
from typing import Callable, Iterator

import pytest

pytest.importorskip("playwright.sync_api", reason="pip install -e '.[e2e]'")

from playwright.sync_api import Browser, Page, Route, sync_playwright  # noqa: E402

from .conftest import (
    AGENT_MARKER,
    ANNOTATED_SCENE_REL,
    BARE_SCENE_REL,
    HTML_LEAD_SCENE_REL,
    LARGE_SCENE_REL,
    NESTED_MANUSCRIPT_NOTE,
    SCENE_REL,
    STORY_SCENES,
    ProseviewServer,
)

pytestmark = pytest.mark.e2e_browser

#: The dashboard dock can only offer the Terminal, which needs a PTY.
POSIX_ONLY_BROWSER = pytest.mark.skipif(
    os.name != "posix", reason="the dashboard dock is terminal-only, and PTYs are POSIX-only"
)

# ── browser plumbing ────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def browser() -> Iterator[Browser]:
    with sync_playwright() as pw:
        instance = pw.chromium.launch()
        try:
            yield instance
        finally:
            instance.close()


#: Vendored ProseMirror modules, cached in-process across the whole session.
_VENDOR_MODULE_CACHE: dict[str, bytes] = {}


def _install_esm_cache(page: Page) -> None:
    """Serve ``/vendor/pm/*`` from memory instead of the test server.

    ProseMirror is vendored as a 36-module ES graph, so every page load would
    otherwise make 36 round trips to a thread-per-request server -- hundreds of
    page loads into a full run, that is enough extra load to lose timing races
    in unrelated tests.

    Keyed by path rather than URL: each test gets its own server on its own
    port, and the bytes are identical across them.

    (Named for the esm.sh cache it replaced, which became dead the moment
    ProseMirror stopped being fetched from a CDN.)
    """
    def handler(route: Route) -> None:
        key = urlparse(route.request.url).path
        body = _VENDOR_MODULE_CACHE.get(key)
        if body is None:
            fetched = route.fetch()
            if fetched.status != 200:
                route.fulfill(status=fetched.status, body=fetched.body())
                return
            body = fetched.body()
            _VENDOR_MODULE_CACHE[key] = body
        route.fulfill(
            status=200,
            body=body,
            headers={"content-type": "application/javascript; charset=utf-8"},
        )

    page.route("**/vendor/pm/*", handler)


#: Console noise that is not a JavaScript fault. A 409 from the save conflict
#: guard and an aborted request from a deliberate ``location.reload()`` both
#: log here, and both are the app working as designed.
_CONSOLE_NOISE = (
    "Failed to load resource",
    "net::ERR_ABORTED",
    "net::ERR_EMPTY_RESPONSE",
)


@pytest.fixture
def page(browser: Browser, request: pytest.FixtureRequest) -> Iterator[Page]:
    """A page wired to the esm cache that fails the test on any JS or server error.

    A test may declare known-buggy output with
    ``@pytest.mark.allow_js_errors("substring")`` -- used to keep a regression
    documented rather than silently tolerated everywhere. A test that drives an
    endpoint's failure path on purpose declares it with
    ``@pytest.mark.allow_http_errors("/endpoint")``.

    The HTTP half matters because the mutating endpoints answer with status 500
    and ``{"ok": false}``, which the app reports through an ``alert()``. Without
    this guard a server-side regression looks exactly like a passing test.
    """
    marker = request.node.get_closest_marker("allow_js_errors")
    allowed = tuple(marker.args) if marker else ()
    http_marker = request.node.get_closest_marker("allow_http_errors")
    allowed_http = tuple(http_marker.args) if http_marker else ()

    context = browser.new_context(viewport={"width": 1500, "height": 1200})
    pg = context.new_page()
    _install_esm_cache(pg)

    errors: list[str] = []
    server_errors: list[str] = []

    def record(text: str) -> None:
        if any(noise in text for noise in _CONSOLE_NOISE):
            return
        if any(ok in text for ok in allowed):
            return
        errors.append(text)

    def record_response(response) -> None:
        if response.status < 500:
            return
        if any(ok in response.url for ok in allowed_http):
            return
        server_errors.append(f"{response.status} {response.request.method} {response.url}")

    pg.on("pageerror", lambda exc: record(str(exc)))
    pg.on("console", lambda msg: record(msg.text) if msg.type == "error" else None)
    pg.on("response", record_response)

    try:
        yield pg
    finally:
        context.close()

    assert not errors, "uncaught JavaScript errors:\n" + "\n".join(errors)
    assert not server_errors, "server returned 5xx:\n" + "\n".join(server_errors)


# ── page helpers ────────────────────────────────────────────────────────────


def _wait_until(predicate: Callable[[], bool], timeout: float = 10.0, message: str = "") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError(message or f"condition not met within {timeout}s")


def open_dashboard(page: Page, server: ProseviewServer) -> None:
    # `networkidle` never fires: the page holds the /events SSE stream open.
    page.goto(server.base_url, wait_until="load")
    page.wait_for_selector("#sceneTable tbody tr")
    page.wait_for_function("() => !!window._PM")


def _track_event_sources(page: Page) -> None:
    """Expose EventSource lifecycle state before the application scripts run."""
    page.add_init_script(
        """(() => {
            const NativeEventSource = window.EventSource;
            window.__trackedEventSources = [];
            window.EventSource = class extends NativeEventSource {
                constructor(url, options) {
                    super(url, options);
                    this.__openCount = 0;
                    this.addEventListener('open', () => { this.__openCount += 1; });
                    window.__trackedEventSources.push(this);
                }
            };
        })()"""
    )


def open_scene(page: Page, server: ProseviewServer, rel: str = SCENE_REL) -> None:
    page.goto(f"{server.base_url}#/scene/{rel}", wait_until="load")
    page.wait_for_function("() => !!window._PM")
    page.wait_for_selector("#sceneModal", state="visible")
    page.wait_for_selector("#sceneProseHost .ProseMirror")


def open_scene_appearance(page: Page) -> None:
    if not page.locator("#sceneAppearanceMenu").is_visible():
        page.locator("#sceneAppearanceBtn").focus()
        page.keyboard.press("Enter")
    page.wait_for_selector("#sceneAppearanceMenu", state="visible")


def enter_edit_mode(page: Page) -> None:
    page.click("#sceneEditBtn")
    page.wait_for_function("() => window._pmEditMode === true")


def append_to_paragraph(page: Page, needle: str, text: str) -> None:
    """Put the caret at the end of the paragraph containing *needle* and type."""
    page.evaluate(
        """(needle) => {
            const host = document.querySelector('#sceneProseHost .ProseMirror');
            const para = Array.from(host.querySelectorAll('p'))
                .find(p => p.textContent.includes(needle));
            if (!para) throw new Error('paragraph not found: ' + needle);
            para.scrollIntoView();
            const range = document.createRange();
            range.selectNodeContents(para);
            range.collapse(false);
            const sel = window.getSelection();
            sel.removeAllRanges();
            sel.addRange(range);
        }""",
        needle,
    )
    page.keyboard.type(text)


def save_scene(page: Page) -> None:
    page.keyboard.press("ControlOrMeta+s")


def wait_for_discuss_answer(page: Page, text: str = "Fake answer") -> None:
    page.wait_for_function(
        "needle => document.querySelector('#discussLog').innerText.includes(needle)",
        arg=text,
        timeout=10_000,
    )


_SELECT_PROSE_JS = """({needle, block}) => {
            const host = document.getElementById('sceneProseHost');
            const walker = document.createTreeWalker(host, NodeFilter.SHOW_TEXT);
            let node = null, idx = -1;
            while ((node = walker.nextNode())) {
                idx = node.data.indexOf(needle);
                if (idx >= 0) break;
            }
            if (!node) throw new Error('text not found in prose: ' + needle);
            if (node.parentElement) {
                // A writer can only start a drag selection after the target is
                // on screen. Make that precondition synchronous: inheriting
                // the app's smooth-scroll CSS let the helper create a Range
                // while the prose was still moving, intermittently anchoring
                // the action menu outside the viewport under full-suite load.
                node.parentElement.scrollIntoView({ behavior: 'instant', block });
            }
            const range = document.createRange();
            range.setStart(node, idx);
            range.setEnd(node, idx + needle.length);
            const sel = window.getSelection();
            sel.removeAllRanges();
            sel.addRange(range);
            const rect = range.getBoundingClientRect();
            document.getElementById('modalBody').dispatchEvent(new MouseEvent('mouseup', {
                bubbles: true, clientX: rect.left, clientY: rect.bottom,
            }));
            return sel.toString();
        }"""


def select_prose(page: Page, needle: str, *, block: str = "start") -> str:
    """Select *needle* in the rendered prose and raise the selection pill.

    The pill is bound to ``mouseup`` on ``#modalBody``, so a synthetic Range has
    to be followed by that event for the UI to react. ``block`` lets placement
    tests exercise selections near both vertical viewport edges.
    """
    selected = page.evaluate(_SELECT_PROSE_JS, {"needle": needle, "block": block})

    # A re-render can drop the range before the app records it -- applying an
    # AI proposal rebuilds the prose, and on a slow machine that lands after
    # the selection is made. Redo it rather than wait out a timeout: the whole
    # point of the helper is to leave a selection the app has seen.
    for attempt in range(4):
        try:
            page.wait_for_selector("#selectionPill", state="visible", timeout=5_000)
            page.wait_for_function(
                "needle => currentSelectionText.includes(needle)", arg=needle, timeout=5_000
            )
            return selected
        except Exception:
            if attempt == 3:
                raise
            selected = page.evaluate(_SELECT_PROSE_JS, {"needle": needle, "block": block})
    return selected


def assert_fully_inside_viewport(page: Page, selector: str) -> None:
    """Assert that a rendered control is completely reachable in the viewport."""
    box = page.locator(selector).bounding_box()
    viewport = page.viewport_size
    assert box and viewport, f"{selector} does not have a rendered box"
    assert box["x"] >= 0, f"{selector} extends past the left viewport edge: {box}"
    assert box["y"] >= 0, f"{selector} extends past the top viewport edge: {box}"
    assert box["x"] + box["width"] <= viewport["width"], (
        f"{selector} extends past the right viewport edge: {box}"
    )
    assert box["y"] + box["height"] <= viewport["height"], (
        f"{selector} extends past the bottom viewport edge: {box}"
    )


def test_discuss_scene_streams_safe_document_aware_conversation(page: Page, server: ProseviewServer):
    open_scene(page, server)
    selected = select_prose(page, "ledger")
    assert selected == "ledger"
    page.evaluate("openDiscuss(document.querySelector('#utilityTabCodex'))")
    page.wait_for_selector("#discussPanel", state="visible")
    page.wait_for_function("() => document.querySelector('#discussConnection').innerText.startsWith('Live')")
    assert page.locator("#discussContext .discuss-chip-current").count() == 0
    assert "Selection · 1 words" in page.locator("#discussSelectionChip").inner_text()

    page.fill("#discussInput", "Explain this scene")
    page.evaluate("sendDiscussQuestion(); sendDiscussQuestion()")
    wait_for_discuss_answer(page, "<script>hostile()</script>")
    panel_text = page.locator("#discussPanel").inner_text()
    # The turn reports itself in the strip, in writer language, instead of
    # leaving a pile of protocol nouns below the answer it produced.
    assert "Answered in" in panel_text
    assert "Read context" in panel_text
    assert "commandExecution" not in panel_text
    page.click("#discussTurnTrailToggle")
    assert "Running printf" in page.locator("#discussTurnTrail").inner_text()
    assert "PRIVATE RAW REASONING" not in panel_text
    assert "<script>hostile()</script>" in panel_text
    assert page.locator("#discussLog script").count() == 0
    link = page.locator("#discussLog a", has_text="link")
    assert link.get_attribute("href") == "https://example.test"
    assert page.locator("#discussLog a", has_text="unsafe").count() == 0
    assert page.locator(".discuss-message.user").count() == 1

    page.evaluate("_discussEventSource.close(); setDiscussConnection('Reconnecting', ''); connectDiscussEvents()")
    page.wait_for_function("() => document.querySelector('#discussConnection').innerText.startsWith('Live')", timeout=15_000)

    conversation_id = page.evaluate("() => window._discussConversationId")
    page.locator("#sceneModal .nav-btn").nth(1).click()
    page.wait_for_function("previous => !location.hash.includes(previous)", arg=SCENE_REL)
    assert page.locator("#discussContext .discuss-chip-current").count() == 0
    assert page.evaluate("() => window._discussConversationId") == conversation_id
    page.wait_for_function("() => document.querySelector('#discussConnection').innerText.startsWith('Live')")

    # Escape belongs to whatever the writer is inside, not to the dock. It used
    # to close the panel and was taken away from the editor underneath, so the
    # panel now stays put.
    page.press("body", "Escape")
    page.wait_for_timeout(300)
    assert page.locator("#discussPanel").is_visible()


@pytest.mark.parametrize("agent", ["codex", "claude"])
def test_discuss_current_document_is_opt_in_and_does_not_follow_navigation(
    page: Page, server: ProseviewServer, agent: str
):
    question_requests: list[dict] = []
    page.on(
        "request",
        lambda request: question_requests.append(request.post_data_json)
        if "/api/discuss/conversations/" in request.url and request.url.endswith("/questions")
        else None,
    )

    open_scene(page, server)
    page.evaluate("agent => showDiscussAgentTab(agent)", agent)
    page.wait_for_function(
        "agent => window._discussAgent === agent"
        " && document.querySelector('#discussConnection').innerText.startsWith('Live')",
        arg=agent,
    )

    assert page.locator("#discussContext .discuss-chip-current").count() == 0
    attach_current = page.get_by_role(
        "button", name=f"Attach current document {SCENE_REL}"
    )
    assert attach_current.is_visible()

    page.fill("#discussInput", "What makes an opening effective?")
    page.click("#discussSend")
    wait_for_discuss_idle(page)
    assert question_requests[-1]["include_current_document"] is False

    attach_current.click()
    current_chip = page.locator("#discussContext .discuss-chip-current")
    assert SCENE_REL in current_chip.inner_text()
    page.fill("#discussInput", "Compare this attached opening with the next scene")
    page.locator("#sceneModal .nav-btn").nth(1).click()
    page.wait_for_function("previous => !location.hash.includes(previous)", arg=SCENE_REL)
    next_path = page.evaluate("() => paths[curIdx]")

    assert SCENE_REL in current_chip.inner_text()
    assert next_path not in current_chip.inner_text()
    page.click("#discussSend")
    wait_for_discuss_idle(page)
    assert question_requests[-1]["document"] == {"kind": "scene", "path": SCENE_REL}
    assert question_requests[-1]["include_current_document"] is True

    page.click("#discussClose")
    page.evaluate("agent => showDiscussAgentTab(agent)", agent)
    page.wait_for_function(
        "() => document.querySelector('#discussConnection').innerText.startsWith('Live')"
    )
    current_chip = page.locator("#discussContext .discuss-chip-current")
    assert SCENE_REL in current_chip.inner_text()

    current_chip.get_by_role(
        "button", name=f"Remove current document {SCENE_REL}"
    ).click()
    assert page.locator("#discussContext .discuss-chip-current").count() == 0
    assert page.get_by_role("button", name=f"Attach current document {next_path}").is_visible()

    page.fill("#discussInput", "Continue without reading either scene")
    page.click("#discussSend")
    wait_for_discuss_idle(page)
    assert question_requests[-1]["document"] == {"kind": "scene", "path": next_path}
    assert question_requests[-1]["include_current_document"] is False


@pytest.mark.parametrize("agent", ["codex", "claude"])
def test_discuss_project_conversation_and_draft_survive_scene_navigation(
    page: Page, server: ProseviewServer, agent: str
):
    question_requests: list[dict] = []

    def record_question(request) -> None:
        if "/api/discuss/conversations/" in request.url and request.url.endswith("/questions"):
            question_requests.append(request.post_data_json)

    page.on("request", record_question)
    open_scene(page, server)
    page.evaluate("agent => showDiscussAgentTab(agent)", agent)
    page.wait_for_function("() => document.querySelector('#discussConnection').innerText.startsWith('Live')")
    page.fill("#discussInput", "Remember this opening")
    page.click("#discussSend")
    wait_for_discuss_answer(page)
    wait_for_discuss_idle(page)
    page.wait_for_function("() => document.querySelectorAll('#discussLog .discuss-message').length >= 2")
    original_id = page.evaluate("() => window._discussConversationId")
    page.evaluate("() => { window._discussEventSource.__navigationTest = true; }")
    original_messages = page.locator("#discussLog .discuss-message").count()

    # A draft freezes its origin metadata while navigation leaves the project
    # conversation and its live event stream untouched. The scene's contents
    # are still omitted unless the writer explicitly attaches them.
    page.fill("#discussInput", "Compare the image I was reading")
    page.locator("#sceneModal .nav-btn").nth(1).click()
    page.wait_for_function("previous => !location.hash.includes(previous)", arg=SCENE_REL)
    next_path = page.evaluate("() => paths[curIdx]")
    assert page.evaluate("() => window._discussConversationId") == original_id
    assert page.evaluate("() => window._discussEventSource.__navigationTest") is True
    assert page.locator("#discussLog .discuss-message").count() == original_messages
    assert page.input_value("#discussInput") == "Compare the image I was reading"
    assert page.locator("#discussContext .discuss-chip-current").count() == 0
    assert page.get_by_role("button", name=f"Attach current document {next_path}").is_visible()

    page.click("#discussSend")
    page.wait_for_function(
        "count => document.querySelectorAll('#discussLog .discuss-message').length > count",
        arg=original_messages,
    )
    wait_for_discuss_idle(page)
    assert question_requests[-1]["document"] == {"kind": "scene", "path": SCENE_REL}
    assert question_requests[-1]["include_current_document"] is False

    # Once the draft is sent, the next turn follows the scene now on screen.
    assert page.get_by_role("button", name=f"Attach current document {next_path}").is_visible()
    page.fill("#discussInput", "What changes in this scene?")
    page.click("#discussSend")
    wait_for_discuss_idle(page)
    assert question_requests[-1]["document"] == {"kind": "scene", "path": next_path}
    assert question_requests[-1]["include_current_document"] is False
    assert page.evaluate("() => window._discussConversationId") == original_id


@pytest.mark.parametrize("agent", ["codex", "claude"])
def test_discuss_migrates_legacy_draft_and_clearing_it_follows_the_new_scene(
    page: Page, server: ProseviewServer, agent: str
):
    legacy_key = f"proseview-draft:{agent}:scene:{SCENE_REL}"
    page.goto(server.base_url, wait_until="load")
    page.evaluate(
        "([key, value]) => sessionStorage.setItem(key, value)",
        [legacy_key, "A draft saved before project conversations"],
    )
    question_requests: list[dict] = []
    page.on(
        "request",
        lambda request: question_requests.append(request.post_data_json)
        if "/api/discuss/conversations/" in request.url and request.url.endswith("/questions")
        else None,
    )

    open_scene(page, server)
    page.evaluate("agent => showDiscussAgentTab(agent)", agent)
    page.wait_for_function("() => document.querySelector('#discussConnection').innerText.startsWith('Live')")
    assert page.input_value("#discussInput") == "A draft saved before project conversations"
    assert page.evaluate("agent => sessionStorage.getItem('proseview-draft:' + agent)", agent)
    assert page.evaluate("key => sessionStorage.getItem(key)", legacy_key) is None

    page.locator("#sceneModal .nav-btn").nth(1).click()
    page.wait_for_function("previous => !location.hash.includes(previous)", arg=SCENE_REL)
    next_path = page.evaluate("() => paths[curIdx]")
    assert page.locator("#discussContext .discuss-chip-current").count() == 0

    page.fill("#discussInput", "")
    assert page.get_by_role("button", name=f"Attach current document {next_path}").is_visible()
    page.fill("#discussInput", "Use the scene now on screen")
    page.click("#discussSend")
    wait_for_discuss_idle(page)
    assert question_requests[-1]["document"] == {"kind": "scene", "path": next_path}
    assert question_requests[-1]["include_current_document"] is False


@pytest.mark.parametrize("agent", ["codex", "claude"])
def test_discuss_recovers_multiple_legacy_document_drafts_without_reload(
    page: Page, server: ProseviewServer, agent: str
):
    page.goto(server.base_url, wait_until="load")
    next_path = page.evaluate(
        "first => paths[paths.indexOf(first) + 1]", SCENE_REL
    )
    first_key = f"proseview-draft:{agent}:scene:{SCENE_REL}"
    second_key = f"proseview-draft:{agent}:scene:{next_path}"
    page.evaluate(
        "([firstKey, secondKey]) => {"
        " sessionStorage.setItem(firstKey, 'Legacy opening draft');"
        " sessionStorage.setItem(secondKey, 'Legacy next-scene draft');"
        "}",
        [first_key, second_key],
    )
    question_requests: list[dict] = []
    page.on(
        "request",
        lambda request: question_requests.append(request.post_data_json)
        if "/api/discuss/conversations/" in request.url and request.url.endswith("/questions")
        else None,
    )

    open_scene(page, server)
    page.evaluate("agent => showDiscussAgentTab(agent)", agent)
    page.wait_for_function("() => document.querySelector('#discussConnection').innerText.startsWith('Live')")
    assert page.input_value("#discussInput") == "Legacy opening draft"
    assert page.evaluate("key => sessionStorage.getItem(key)", first_key) is None
    assert page.evaluate("key => sessionStorage.getItem(key)", second_key) == "Legacy next-scene draft"

    page.locator("#sceneModal .nav-btn").nth(1).click()
    page.wait_for_function("path => location.hash.includes(encodeURIComponent(path))", arg=next_path)
    assert page.input_value("#discussInput") == "Legacy opening draft"
    assert "saved draft for this file is waiting" in page.locator("#discussAnnouncement").inner_text().lower()

    page.fill("#discussInput", "")
    page.wait_for_function(
        "() => document.querySelector('#discussInput').value === 'Legacy next-scene draft'"
    )
    assert page.locator("#discussContext .discuss-chip-current").count() == 0
    assert page.evaluate("key => sessionStorage.getItem(key)", second_key) is None

    page.click("#discussSend")
    wait_for_discuss_idle(page)
    assert question_requests[-1]["document"] == {"kind": "scene", "path": next_path}
    assert question_requests[-1]["include_current_document"] is False


@pytest.mark.parametrize("agent", ["codex", "claude"])
def test_discuss_reopens_the_newest_saved_provider_draft(
    page: Page, server: ProseviewServer, agent: str
):
    other_agent = "claude" if agent == "codex" else "codex"
    open_scene(page, server)
    page.evaluate("agent => showDiscussAgentTab(agent)", agent)
    page.wait_for_function(
        "agent => window._discussAgent === agent"
        " && document.querySelector('#discussConnection').innerText.startsWith('Live')",
        arg=agent,
    )
    page.fill("#discussInput", "Old unsent draft")

    page.evaluate("agent => showDiscussAgentTab(agent)", other_agent)
    page.wait_for_function(
        "agent => window._discussAgent === agent"
        " && document.querySelector('#discussConnection').innerText.startsWith('Live')",
        arg=other_agent,
    )
    page.evaluate("agent => showDiscussAgentTab(agent)", agent)
    page.wait_for_function(
        "agent => window._discussAgent === agent"
        " && document.querySelector('#discussConnection').innerText.startsWith('Live')",
        arg=agent,
    )
    assert page.input_value("#discussInput") == "Old unsent draft"

    page.fill("#discussInput", "New unsent draft")
    page.click("#discussClose")
    assert page.locator("#discussPanel").is_hidden()
    page.evaluate("agent => showDiscussAgentTab(agent)", agent)
    page.wait_for_function(
        "() => document.querySelector('#discussConnection').innerText.startsWith('Live')"
    )

    assert page.input_value("#discussInput") == "New unsent draft"
    assert page.evaluate(
        "agent => sessionStorage.getItem('proseview-draft:' + agent)", agent
    ) == "New unsent draft"
    assert page.locator("#discussContext .discuss-chip-current").count() == 0
    assert page.get_by_role("button", name=f"Attach current document {SCENE_REL}").is_visible()


@pytest.mark.parametrize("agent", ["codex", "claude"])
def test_discuss_restores_an_inactive_providers_legacy_draft_on_its_source_file(
    page: Page, server: ProseviewServer, agent: str
):
    other_agent = "claude" if agent == "codex" else "codex"
    page.goto(server.base_url, wait_until="load")
    next_path = page.evaluate(
        "first => paths[paths.indexOf(first) + 1]", SCENE_REL
    )
    legacy_key = f"proseview-draft:{agent}:scene:{next_path}"
    page.evaluate(
        "key => sessionStorage.setItem(key, 'Released draft for the next scene')",
        legacy_key,
    )

    open_scene(page, server)
    page.evaluate("agent => showDiscussAgentTab(agent)", agent)
    page.wait_for_function(
        "agent => window._discussAgent === agent"
        " && document.querySelector('#discussConnection').innerText.startsWith('Live')",
        arg=agent,
    )
    assert page.input_value("#discussInput") == ""
    page.evaluate("agent => showDiscussAgentTab(agent)", other_agent)
    page.wait_for_function(
        "agent => window._discussAgent === agent"
        " && document.querySelector('#discussConnection').innerText.startsWith('Live')",
        arg=other_agent,
    )

    page.locator("#sceneModal .nav-btn").nth(1).click()
    page.wait_for_function("path => location.hash.includes(encodeURIComponent(path))", arg=next_path)
    page.evaluate("agent => showDiscussAgentTab(agent)", agent)
    page.wait_for_function(
        "agent => window._discussAgent === agent"
        " && document.querySelector('#discussConnection').innerText.startsWith('Live')",
        arg=agent,
    )

    assert page.input_value("#discussInput") == "Released draft for the next scene"
    assert page.locator("#discussContext .discuss-chip-current").count() == 0
    assert page.get_by_role("button", name=f"Attach current document {next_path}").is_visible()
    assert page.evaluate("key => sessionStorage.getItem(key)", legacy_key) is None


def test_discuss_canon_refactor_audits_then_hands_off_and_verifies_without_silent_writes(
    page: Page, server: ProseviewServer
):
    before = server.scene_path().read_bytes()
    open_scene(page, server)
    open_discuss(page)
    page.wait_for_function("() => document.querySelector('#discussConnection').innerText.startsWith('Live')")

    trace = page.get_by_role("button", name=re.compile("Trace a canon change"))
    assert trace.is_visible()
    assert page.locator("#discussHistoryClear").is_hidden()
    page.fill("#discussInput", "Mira grew up in Chicago, not Boston.")
    trace.click()
    assert page.locator("#discussSend").inner_text() == "Scan"
    assert "Read-only scan" in page.locator("#discussTaskMode").inner_text()
    assert page.locator(".discuss-story-action").count() == 0
    assert page.locator("#discussInput").input_value() == "Mira grew up in Chicago, not Boston."

    page.get_by_role("button", name="Change action").click()
    assert page.get_by_role("button", name=re.compile("Trace a canon change")).is_visible()
    assert page.locator("#discussSend").inner_text() == "Send"
    assert page.locator("#discussInput").input_value() == "Mira grew up in Chicago, not Boston."
    page.get_by_role("button", name=re.compile("Trace a canon change")).click()
    assert page.locator(".discuss-story-action").count() == 0
    page.fill("#discussInput", "Rena changed the safe code this spring.")
    page.locator("#discussSend").click()

    page.wait_for_selector(".discuss-refactor-finding", state="visible")
    assert page.locator("#discussHistoryClear").inner_text() == "Clear results"
    assert page.locator("#discussHistoryClear").is_visible()
    report = page.locator(".discuss-task", has_text="Trace a canon change")
    assert "Read-only scan complete" in report.inner_text()
    assert "manuscript/ch01/01-opening.md#L18" in report.inner_text()
    assert server.scene_path().read_bytes() == before

    report.get_by_role("button", name="Mark intentional").click()
    page.get_by_role("button", name="Mark unresolved").wait_for(state="visible")
    assert server.scene_path().read_bytes() == before

    report.get_by_role("button", name="Review proposed edit").click()
    page.wait_for_selector("#aiProposalPanel", state="visible")
    assert "This sentence preserves the old safe-code history." in page.locator("#aiProposalPanel").inner_text()
    assert server.scene_path().read_bytes() == before

    page.locator("#aiProposalPanel").get_by_role("button", name=re.compile("Dismiss|Close")).click()
    page.wait_for_function(
        "() => document.querySelector('.discuss-refactor-finding')?.dataset.decision === 'dismissed'"
    )
    report.get_by_role("button", name="Review proposed edit").click()
    page.get_by_role("button", name="Use this version").wait_for(state="visible")
    page.get_by_role("button", name="Use this version").click()
    page.wait_for_function(
        "() => document.querySelector('.discuss-refactor-finding')?.dataset.decision === 'applied'"
    )
    assert server.scene_path().read_bytes() == before
    page.get_by_role("button", name="Undo").click()
    page.wait_for_function(
        "() => document.querySelector('.discuss-refactor-finding')?.dataset.decision === 'proposal'"
    )
    assert server.scene_path().read_bytes() == before
    page.get_by_role("button", name="Reject").click()
    page.wait_for_function(
        "() => document.querySelector('.discuss-refactor-finding')?.dataset.decision === 'rejected'"
    )
    report.get_by_role("button", name="Verify after edits").click()
    page.wait_for_function(
        "() => [...document.querySelectorAll('.discuss-task-heading strong')]"
        ".some(node => node.innerText === 'Verify a canon change')"
    )
    page.wait_for_function(
        "() => [...document.querySelectorAll('.discuss-task')]"
        ".some(node => node.innerText.includes('Verify a canon change') && node.innerText.includes('Read-only scan complete'))"
    )
    assert server.scene_path().read_bytes() == before


def test_discuss_canon_refactor_marks_a_proposal_resolved_only_after_scene_save(
    page: Page, server: ProseviewServer
):
    before = server.scene_path().read_bytes()
    open_scene(page, server)
    open_discuss(page)
    page.wait_for_function("() => document.querySelector('#discussConnection').innerText.startsWith('Live')")
    page.get_by_role("button", name=re.compile("Trace a canon change")).click()
    page.fill("#discussInput", "Rena changed the safe code this spring.")
    page.locator("#discussSend").click()
    page.wait_for_selector(".discuss-refactor-finding", state="visible")
    page.get_by_role("button", name="Review proposed edit").click()
    page.get_by_role("button", name="Use this version").wait_for(state="visible")
    page.get_by_role("button", name="Use this version").click()
    page.wait_for_function(
        "() => document.querySelector('.discuss-refactor-finding')?.dataset.decision === 'applied'"
    )
    assert server.scene_path().read_bytes() == before

    page.get_by_role("button", name="Save scene").click()
    _wait_until(lambda: server.scene_path().read_bytes() != before)
    page.wait_for_function(
        "() => document.querySelector('.discuss-refactor-finding')?.dataset.decision === 'resolved'"
    )


def test_discuss_scene_continuity_starts_without_an_optional_focus(
    page: Page, server: ProseviewServer
):
    before = server.scene_path().read_bytes()
    open_scene(page, server)
    open_discuss(page)
    page.wait_for_function("() => document.querySelector('#discussConnection').innerText.startsWith('Live')")

    page.get_by_role("button", name=re.compile("Check this scene's continuity")).click()

    assert page.get_by_text("Ready to scan this scene", exact=True).is_visible()
    assert "optional focus" in page.locator("#discussLog").inner_text().lower()
    assert page.locator("#discussInput").input_value() == ""
    page.locator("#discussSend").click()

    page.wait_for_function(
        "() => [...document.querySelectorAll('.discuss-task-status')]"
        ".some(node => ['Ready', 'Failed'].includes(node.innerText))"
    )
    report = page.locator(".discuss-task", has_text="Check this scene's continuity")
    assert "Read-only scan complete" in report.inner_text()
    assert report.locator(".discuss-refactor-finding").is_visible()
    assert server.scene_path().read_bytes() == before


def test_discuss_scene_continuity_bounds_large_repository_context(
    page: Page, server: ProseviewServer
):
    plans = server.root / "plans"
    plans.mkdir(exist_ok=True)
    for index in range(4):
        (plans / f"large-continuity-context-{index}.md").write_text(
            f"# Large continuity context {index}\n\n" + ("A configured story fact.\n" * 15_000),
            encoding="utf-8",
        )
    before = server.scene_path().read_bytes()
    open_scene(page, server)
    open_discuss(page)
    page.wait_for_function("() => document.querySelector('#discussConnection').innerText.startsWith('Live')")
    page.get_by_role("button", name=re.compile("Check this scene's continuity")).click()
    page.locator("#discussSend").click()

    page.wait_for_function(
        "() => [...document.querySelectorAll('.discuss-task-status')]"
        ".some(node => ['Ready', 'Failed'].includes(node.innerText))"
    )
    report = page.locator(".discuss-task", has_text="Check this scene's continuity")
    assert "Read-only scan complete" in report.inner_text()
    assert report.locator(".discuss-refactor-finding").is_visible()
    assert "Codex input limit" in report.inner_text()
    assert "files were omitted" in report.inner_text()
    assert "Input exceeds the maximum length" not in page.locator("#discussPanel").inner_text()
    assert server.scene_path().read_bytes() == before


def test_discuss_scene_continuity_reports_that_a_scan_is_starting_and_recovers_on_failure(
    page: Page, server: ProseviewServer
):
    open_scene(page, server)
    open_discuss(page)
    page.wait_for_function("() => document.querySelector('#discussConnection').innerText.startsWith('Live')")
    page.get_by_role("button", name=re.compile("Check this scene's continuity")).click()
    page.evaluate(
        """
        () => {
            window.__discussOriginalFetch = window.fetch;
            window.fetch = function(input, options) {
                if (String(input).includes('/questions')) {
                    return new Promise(function(resolve) {
                        window.__resolveDiscussQuestion = function() {
                            resolve(new Response(JSON.stringify({error: 'Continuity scan could not start.'}), {
                                status: 503,
                                headers: {'Content-Type': 'application/json'}
                            }));
                        };
                    });
                }
                return window.__discussOriginalFetch(input, options);
            };
        }
        """
    )

    page.locator("#discussSend").click()

    assert page.get_by_text("Starting continuity scan…", exact=True).is_visible()
    assert page.locator("#discussSend").is_disabled()
    assert page.locator("#discussSend").inner_text() == "Starting…"
    page.evaluate(
        """
        () => {
            window.fetch = window.__discussOriginalFetch;
            window.__resolveDiscussQuestion();
        }
        """
    )
    page.wait_for_function(
        "() => !document.getElementById('discussSend').disabled "
        "&& document.getElementById('discussSend').innerText === 'Scan'"
    )
    assert page.get_by_text("Ready to scan this scene", exact=True).is_visible()
    assert page.get_by_text("Continuity scan could not start.", exact=True).is_visible()


def test_discuss_send_times_out_and_recovers_from_a_stalled_request(
    page: Page, server: ProseviewServer
):
    open_scene(page, server)
    open_discuss(page)
    page.wait_for_function("() => document.querySelector('#discussConnection').innerText.startsWith('Live')")
    page.fill("#discussInput", "Why is the opening quiet?")
    page.evaluate(
        """
        () => {
            window._discussRequestTimeoutMs = 100;
            window.__discussOriginalFetch = window.fetch;
            window.fetch = function(input, options) {
                if (!String(input).includes('/questions')) {
                    return window.__discussOriginalFetch(input, options);
                }
                return new Promise(function(_resolve, reject) {
                    options.signal.addEventListener('abort', function() {
                        reject(new DOMException('The operation was aborted.', 'AbortError'));
                    }, {once: true});
                });
            };
        }
        """
    )

    page.locator("#discussSend").click()

    assert page.locator("#discussSend").is_disabled()
    assert page.locator("#discussSend").inner_text() == "Sending…"
    assert page.locator("#discussSend").evaluate("node => getComputedStyle(node).cursor") != "wait"
    page.wait_for_function(
        "() => !document.getElementById('discussSend').disabled "
        "&& document.getElementById('discussSend').innerText === 'Send'",
        timeout=2_000,
    )
    assert page.get_by_text(
        "Request timed out. Check the connection and try again.", exact=True
    ).is_visible()
    assert page.locator("#discussInput").input_value() == "Why is the opening quiet?"


def test_discuss_open_times_out_with_an_operable_retry(
    page: Page, server: ProseviewServer
):
    open_scene(page, server)
    page.evaluate(
        """
        () => {
            window._discussRequestTimeoutMs = 100;
            window.__discussOriginalFetch = window.fetch;
            window.__stallDiscussOpen = true;
            window.fetch = function(input, options) {
                if (!window.__stallDiscussOpen || !String(input).includes('/conversations/open')) {
                    return window.__discussOriginalFetch(input, options);
                }
                window.__stallDiscussOpen = false;
                return new Promise(function(_resolve, reject) {
                    options.signal.addEventListener('abort', function() {
                        reject(new DOMException('The operation was aborted.', 'AbortError'));
                    }, {once: true});
                });
            };
        }
        """
    )

    open_discuss(page)

    page.wait_for_function(
        "() => !document.getElementById('discussSend').disabled "
        "&& document.getElementById('discussSend').innerText === 'Try again'",
        timeout=2_000,
    )
    assert page.locator("#discussConnection").inner_text().startswith("Unavailable")
    assert page.get_by_text(
        "Request timed out. Check the connection and try again.", exact=True
    ).is_visible()

    # Restore a realistic budget before retrying. The 100ms above exists to
    # force the *first* request to time out; leaving it in place also caps the
    # retry at 100ms, so whether this test passed depended on how warm the
    # machine was -- it passed inside a full run and failed run alone.
    page.evaluate("() => { window._discussRequestTimeoutMs = 30000; }")

    page.locator("#discussSend").click()
    page.wait_for_function("() => document.querySelector('#discussConnection').innerText.startsWith('Live')")
    assert page.locator("#discussSend").inner_text() == "Send"
    assert not page.locator("#discussSend").is_disabled()


def test_discuss_detects_a_server_restart_and_recovers_by_reload(
    page: Page, server: ProseviewServer
):
    open_scene(page, server)
    open_discuss(page)
    page.wait_for_function("() => document.querySelector('#discussConnection').innerText.startsWith('Live')")
    draft = "What do you think about this scene?"
    page.fill("#discussInput", draft)

    server.restart()

    page.wait_for_function(
        "() => document.getElementById('discussConnection').innerText.startsWith('Reload required')",
        timeout=15_000,
    )
    assert page.get_by_text(
        "Proseview restarted. Reload this page to reconnect.", exact=True
    ).is_visible()
    assert page.locator("#discussSend").is_disabled()
    assert page.locator("#discussInput").input_value() == draft

    page.get_by_role("button", name="Reload page").click()
    page.wait_for_function("() => !!window._PM")
    page.wait_for_selector("#sceneModal", state="visible")
    open_discuss(page)
    page.wait_for_function("() => document.querySelector('#discussConnection').innerText.startsWith('Live')")
    assert page.locator("#discussInput").input_value() == draft


def test_stale_tabs_release_event_streams_after_restart_and_do_not_starve_new_requests(
    page: Page, server: ProseviewServer
):
    stale_pages = [page]
    current = None
    try:
        _track_event_sources(page)
        open_dashboard(page, server)
        for _ in range(3):
            stale = page.context.new_page()
            _install_esm_cache(stale)
            _track_event_sources(stale)
            open_dashboard(stale, server)
            stale_pages.append(stale)

        for stale in stale_pages:
            stale.wait_for_function(
                """() => window.__trackedEventSources.some(source =>
                    new URL(source.url).pathname === '/events'
                    && source.readyState === EventSource.OPEN
                    && source.__openCount === 1
                )"""
            )

        previous_token = page.evaluate("pageSessionToken")
        stale_urls = []
        for stale in stale_pages:
            stale_urls.append(stale.evaluate(
                """() => {
                    const source = window.__trackedEventSources.find(candidate =>
                        new URL(candidate.url).pathname === '/events'
                    );
                    source.close();
                    return source.url;
                }"""
            ))
        server.restart()
        assert server.session_token != previous_token

        for stale, stale_url in zip(stale_pages, stale_urls, strict=True):
            stale.evaluate(
                """url => {
                    window.__forcedStaleEventSource = new EventSource(url);
                }""",
                stale_url,
            )
            stale.wait_for_function(
                "() => window.__forcedStaleEventSource.readyState !== EventSource.CONNECTING",
                timeout=15_000,
            )
            assert stale.evaluate(
                "window.__forcedStaleEventSource.readyState === EventSource.CLOSED"
            )

        current = page.context.new_page()
        _install_esm_cache(current)
        open_scene(current, server)
        open_discuss(current)
        current.wait_for_function(
            "() => document.querySelector('#discussConnection').innerText.startsWith('Live')"
        )
        current.evaluate("window._discussRequestTimeoutMs = 750")

        current.click("#discussNewConversation")
        current.click("#discussNewConversationConfirm")
        current.wait_for_selector("#discussNewConversationDialog", state="hidden", timeout=3_000)

        current.fill("#discussInput", "Can a fresh tab still reach Codex?")
        current.press("#discussInput", "Enter")
        wait_for_discuss_answer(current)
        assert current.locator(".discuss-local-error").count() == 0
    finally:
        if current is not None:
            current.close()
        for stale in stale_pages[1:]:
            stale.close()
        if not page.is_closed():
            page.goto("about:blank")


def test_discuss_repository_action_selected_state_reflows_at_dark_200_percent_zoom(
    page: Page, server: ProseviewServer
):
    page.set_viewport_size({"width": 1024, "height": 768})
    open_scene(page, server)
    open_scene_appearance(page)
    page.select_option("#modalThemeSelect", "dark")
    open_discuss(page)
    page.wait_for_function("() => document.querySelector('#discussConnection').innerText.startsWith('Live')")
    page.evaluate("document.body.style.zoom = '2'")

    trace = page.get_by_role("button", name=re.compile("Trace a canon change"))
    trace.click()

    assert page.locator(".discuss-story-action").count() == 0
    change_action = page.get_by_role("button", name="Change action")
    assert change_action.is_visible()
    assert page.locator("#discussSend").inner_text() == "Scan"
    assert page.evaluate("document.activeElement === document.getElementById('discussInput')")
    assert_fully_inside_viewport(page, "#discussTaskMode")
    assert_fully_inside_viewport(page, "#discussInput")
    assert_fully_inside_viewport(page, "#discussSend")
    change_action.focus()
    assert change_action.evaluate("button => document.activeElement === button")
    change_action.press("Enter")
    assert page.get_by_role("button", name=re.compile("Trace a canon change")).is_visible()
    assert page.evaluate("document.activeElement === document.getElementById('discussInput')")


def test_discuss_decodes_restored_assistant_prose_after_server_restart(
    page: Page, server: ProseviewServer
):
    open_scene(page, server)
    page.evaluate("openDiscuss(document.querySelector('#utilityTabCodex'))")
    page.wait_for_function("() => document.querySelector('#discussConnection').innerText.startsWith('Live')")
    page.fill("#discussInput", "Describe Patel's setting")
    page.press("#discussInput", "Enter")
    wait_for_discuss_answer(page, "Patel's note")

    server.restart()
    page.context.new_cdp_session(page).send("Network.setCacheDisabled", {"cacheDisabled": True})
    page.goto("about:blank")
    open_scene(page, server)
    page.evaluate("openDiscuss(document.querySelector('#utilityTabCodex'))")
    page.wait_for_function("() => document.querySelector('#discussConnection').innerText.startsWith('Live')")
    wait_for_discuss_answer(page, "Patel's note")

    assistant = page.locator(".discuss-message.assistant")
    assert "Patel's note" in assistant.inner_text()
    assert "&#39;" not in assistant.inner_text()
    assert assistant.locator("code").inner_text() == "&amp;"
    assert assistant.locator("script").count() == 0
    assert assistant.locator("a", has_text="link").get_attribute("href") == "https://example.test"
    assert assistant.locator("a", has_text="unsafe").count() == 0


def test_discuss_repository_links_open_inside_prosview_and_target_source_line(
    page: Page, server: ProseviewServer
):
    open_scene(page, server)
    page.evaluate("openDiscuss(document.querySelector('#utilityTabCodex'))")
    page.wait_for_function("() => document.querySelector('#discussConnection').innerText.startsWith('Live')")
    page.fill("#discussInput", "SHOW_FILE_LINKS")
    page.press("#discussInput", "Enter")
    wait_for_discuss_answer(page, "current scene")

    assistant = page.locator(".discuss-message.assistant").last
    current = assistant.get_by_role("link", name="current scene")
    assert current.get_attribute("href").endswith("#/scene/ch01%2F01-opening.md")
    assert current.get_attribute("target") is None
    assert current.get_attribute("title") == "Open in Prosview at line 18"
    assert assistant.get_by_role("link", name="another scene").get_attribute("href").endswith(
        "#/scene/ch01%2F02-walk.md"
    )
    assert assistant.get_by_role("link", name="repository file").get_attribute("href").endswith(
        "#/file/scripts%2Fcheck_continuity.py"
    )
    assert assistant.locator("a", has_text="outside repository").count() == 0
    assert assistant.locator("a", has_text="unsafe").count() == 0
    external = assistant.get_by_role("link", name="external")
    assert external.get_attribute("href") == "https://example.test/reference"
    assert external.get_attribute("target") == "_blank"

    current.click()
    assert page.evaluate("decodeURIComponent(location.hash)") == f"#/scene/{SCENE_REL}"
    target = page.locator("#sceneProseHost .para-flash")
    target.wait_for(state="visible")
    assert target.get_attribute("data-line") == "18"

    assistant.get_by_role("link", name="repository file").click()
    page.wait_for_function(
        "() => document.getElementById('filePreviewTitle').innerText === 'scripts/check_continuity.py'"
    )
    assert page.evaluate("decodeURIComponent(location.hash)") == "#/file/scripts/check_continuity.py"


def test_discuss_repository_link_preserves_unsaved_scene_edits(
    page: Page, server: ProseviewServer
):
    open_scene(page, server)
    page.evaluate("openDiscuss(document.querySelector('#utilityTabCodex'))")
    page.wait_for_function("() => document.querySelector('#discussConnection').innerText.startsWith('Live')")
    page.fill("#discussInput", "SHOW_FILE_LINKS")
    page.press("#discussInput", "Enter")
    wait_for_discuss_answer(page, "another scene")

    page.click("#sceneEditBtn")
    page.evaluate(
        """() => {
            _pmView.dispatch(_pmView.state.tr.insertText('Unsaved local note. ', 1));
            setPmDirty(true);
        }"""
    )
    page.locator(".discuss-message.assistant").last.get_by_role("link", name="another scene").click()

    assert page.evaluate("decodeURIComponent(location.hash)") == f"#/scene/{SCENE_REL}"
    assert "Unsaved local note." in _editor_text(page)
    assert "Save or cancel your scene edits before opening another file." in page.locator("#discussLog").inner_text()


def test_discuss_context_picker_attaches_only_the_files_the_writer_selects(
    page: Page,
    server: ProseviewServer,
    fake_home: Path,
):
    open_scene(page, server)
    page.evaluate("openDiscuss(document.querySelector('#utilityTabCodex'))")
    page.wait_for_function("() => document.querySelector('#discussConnection').innerText.startsWith('Live')")

    context_button = page.locator("#discussContextButton")
    assert context_button.get_attribute("aria-label") == "Add files and more"
    assert "+ Context" not in page.locator("#discussComposerArea").inner_text()
    assert page.locator("#discussContext .discuss-chip-current").count() == 0
    assert page.get_by_role("button", name=f"Attach current document {SCENE_REL}").is_visible()
    page.locator("#discussInput").press("@")
    page.wait_for_selector("#discussContextPicker", state="visible")
    assert page.locator("#discussContextOptions").get_attribute("role") == "listbox"
    assert not page.locator("#discussContextPicker").evaluate("node => node.matches(':modal')")
    assert context_button.get_attribute("aria-expanded") == "true"
    page.locator("#discussInput").press("Escape")
    page.wait_for_selector("#discussContextPicker", state="hidden")
    assert context_button.get_attribute("aria-expanded") == "false"
    assert page.locator("#discussPanel").is_visible()

    page.fill("#discussInput", "Compare ")
    page.locator("#discussInput").press_sequentially("@02-walk")
    page.wait_for_selector("#discussContextPicker", state="visible")
    assert "manuscript/ch01/02-walk.md" in page.locator("#discussContextOptions").inner_text()
    page.locator("#discussInput").press("Enter")
    page.wait_for_selector("#discussContextPicker", state="hidden")
    assert page.locator("#discussInput").input_value() == "Compare "
    assert "manuscript/ch01/02-walk.md" in page.locator("#discussContext").inner_text()
    page.locator("#discussInput").press_sequentially("@check_continuity")
    page.wait_for_selector("#discussContextPicker", state="visible")
    assert "scripts/check_continuity.py" in page.locator("#discussContextOptions").inner_text()
    page.locator("#discussInput").press("Enter")
    assert "scripts/check_continuity.py" in page.locator("#discussContext").inner_text()
    assert page.locator("#discussContext .discuss-chip-current").count() == 0

    question = "Compare BROWSER OMIT CURRENT DOCUMENT SENTINEL"
    page.fill("#discussInput", question)
    page.locator("#discussSend").click()
    wait_for_discuss_answer(page, "<script>hostile()</script>")

    records = [json.loads(line) for line in (fake_home / "fake-codex-received.jsonl").read_text(encoding="utf-8").splitlines()]
    prompt = next(
        record["params"]["input"][0]["text"]
        for record in reversed(records)
        if question in json.dumps(record)
    )
    assert "Opening Ledger" not in prompt
    assert "manuscript/ch01/02-walk.md" in prompt
    assert "def check_continuity" in prompt
    assert question in prompt


def test_discuss_approval_file_navigation_and_shared_terminal_dock(page: Page, server: ProseviewServer):
    page.goto(f"{server.base_url}#/file/plans/book-plan.md", wait_until="load")
    page.wait_for_function("() => !!window._PM")
    page.wait_for_selector("#file-preview-panel", state="visible")
    open_discuss(page)
    page.wait_for_function("() => document.querySelector('#discussConnection').innerText.startsWith('Live')")
    assert page.locator("#discussContext .discuss-chip-current").count() == 0
    assert page.get_by_role(
        "button", name="Attach current document plans/book-plan.md"
    ).is_visible()

    page.get_by_role("button", name="Add files and more").click()
    page.wait_for_selector("#discussContextPicker", state="visible")
    page.locator("#discussInput").press_sequentially("plans")
    page.locator("#discussContextOptions [data-path='plans']").click()
    assert "plans" in page.locator("#discussContext").inner_text()

    page.fill("#discussInput", "REQUEST_APPROVAL")
    page.press("#discussInput", "Enter")
    page.wait_for_selector(".discuss-approval button", state="visible")
    assert page.evaluate("document.activeElement === document.querySelector('.discuss-approval button')")
    page.keyboard.press("Enter")
    wait_for_discuss_answer(page, "Approval resolved")
    # A decision you already made is a settled line in the turn's trail, not a
    # card still wearing the amber "needs you" treatment.
    assert page.locator(".discuss-approval").count() == 0
    page.click("#discussTurnTrailToggle")
    assert "You allowed a command" in page.locator("#discussTurnTrail").inner_text()

    page.fill("#discussInput", "REQUEST_APPROVAL again")
    page.press("#discussInput", "Enter")
    page.locator(".discuss-approval button", has_text="Decline").wait_for(state="visible")
    page.locator(".discuss-approval button", has_text="Decline").click()
    page.wait_for_function("() => document.querySelector('#discussLog').innerText.includes('Approval resolved: decline')")

    page.click("#discussPanel .utility-tab:text-is('Terminal')")
    page.wait_for_selector("#terminalPanel", state="visible")
    page.wait_for_function("() => document.getElementById('terminalPanel').classList.contains('dock-right')")
    page.wait_for_selector(".terminal-tab-mount .xterm", timeout=20_000)
    page.click(".terminal-tab-mount .xterm-screen")
    _wait_until(lambda: any(ch in _terminal_text(page) for ch in ("$", "%", "#")), timeout=25)
    run_in_terminal(page, "echo discuss-terminal-alive", "discuss-terminal-alive")
    page.click("#terminalPanel button:text-is('Codex')")
    page.wait_for_selector("#discussPanel", state="visible")
    assert "Approval resolved" in page.locator("#discussLog").inner_text()
    page.click("#discussPanel .utility-tab:text-is('Terminal')")
    assert "discuss-terminal-alive" in _terminal_text(page)


def test_the_terminal_never_buries_the_way_back_to_the_other_tabs(
    page: Page, server: ProseviewServer
):
    """Session chips used to share a row with the dock tabs.

    Open two shells in a right-docked terminal and "Scene" and "Analysis" were
    pushed off the end of the header, with no way back to them. The dock tabs
    have a row of their own now, and they survive any number of sessions.
    """
    open_scene(page, server)
    open_discuss(page)
    page.click("#discussPanel .utility-tab:text-is('Terminal')")
    page.wait_for_selector("#terminalPanel", state="visible")
    page.wait_for_selector(".terminal-tab-mount .xterm", timeout=20_000)
    page.evaluate("() => { openShellTerminal(); openShellTerminal(); }")
    page.wait_for_function(
        "() => document.querySelectorAll('#terminalTabs .terminal-tab').length >= 3"
    )

    tabs = page.locator("#terminalPanel .terminal-dock-tabs .utility-tab")
    assert tabs.count() == 6
    for name in ("Scene", "Analysis", "History", "Codex", "Claude", "Terminal"):
        tab = page.locator(f"#terminalPanel .terminal-dock-tabs button:text-is('{name}')")
        assert tab.is_visible(), f"{name} is unreachable from a terminal with two shells"
        box = tab.bounding_box()
        assert box and box["x"] >= 0 and box["width"] > 0

    # And the way back actually works.
    page.click("#terminalPanel .terminal-dock-tabs button:text-is('Analysis')")
    page.wait_for_selector("#sceneAnalysisPane:not([hidden])")
    assert page.locator("#terminalPanel").is_hidden()


def test_discuss_responsive_dark_zoom_and_keyboard_flow(page: Page, server: ProseviewServer):
    page.set_viewport_size({"width": 1400, "height": 1000})
    open_scene(page, server)
    # The toolbar button opens the dock from the keyboard; the tab row then
    # takes you to Codex, also from the keyboard. The tabs live inside the dock,
    # so the toolbar button is the only one that can open it.
    page.locator("#sceneModal .scene-toolbar-button.discuss-open-btn").focus()
    page.keyboard.press("Enter")
    page.wait_for_selector("#discussPanel", state="visible")
    page.locator("#utilityTabCodex").focus()
    page.keyboard.press("Enter")
    page.wait_for_function("() => document.querySelector('#discussConnection').innerText.startsWith('Live')")
    box = page.locator("#discussPanel").bounding_box()
    assert box and box["x"] + box["width"] <= 1401

    open_scene_appearance(page)
    page.select_option("#modalThemeSelect", "dark")
    page.set_viewport_size({"width": 1024, "height": 768})
    page.evaluate("document.body.style.zoom = '2'")
    page.wait_for_timeout(100)
    box = page.locator("#discussPanel").bounding_box()
    assert box and box["x"] >= 0 and box["width"] <= 1024
    assert page.locator("#discussInput").is_visible()
    assert page.evaluate("document.documentElement.dataset.theme") == "dark"
    page.locator("#discussInput").press("@")
    page.wait_for_selector("#discussContextPicker", state="visible")
    menu_box = page.locator("#discussContextPicker").bounding_box()
    assert menu_box
    assert menu_box["x"] >= 0 and menu_box["x"] + menu_box["width"] <= 1024
    assert menu_box["y"] >= 0 and menu_box["y"] + menu_box["height"] <= 768
    page.locator("#discussInput").press("Escape")
    assert page.locator("#discussPanel").is_visible()

    page.evaluate("document.body.style.zoom = '1'")
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(100)
    phone_box = page.locator("#discussPanel").bounding_box()
    assert phone_box and phone_box["x"] == 0 and phone_box["width"] <= 390
    page.fill("#discussInput", "")
    page.get_by_role("button", name="Add files and more").click()
    page.wait_for_selector("#discussContextPicker", state="visible")
    phone_menu_box = page.locator("#discussContextPicker").bounding_box()
    assert phone_menu_box and phone_menu_box["x"] >= 0 and phone_menu_box["x"] + phone_menu_box["width"] <= 390
    page.locator("#discussInput").press("Escape")

    # Escape closes the context picker and stops there. It no longer closes the
    # dock, so a writer who dismisses the picker keeps the conversation they
    # were in, and the key stays available to the editor underneath.
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    assert page.locator("#discussPanel").is_visible()
    assert page.locator("#discussContextPicker").is_hidden()


def test_the_turn_strip_reports_work_a_stop_and_an_answer(page: Page, server: ProseviewServer):
    """The panel can be asked "is it still working?" and answer it.

    Before this, a running turn and a finished one looked identical: the only
    difference was a Stop button in a row of other buttons.
    """
    open_scene(page, server)
    open_discuss(page)
    page.wait_for_function("() => document.querySelector('#discussConnection').innerText.startsWith('Live')")

    page.fill("#discussInput", "HOLD_FOR_STOP")
    page.press("#discussInput", "Enter")
    page.wait_for_selector("#discussTurnStatus[data-state='working']")
    assert "Codex is working" in page.locator("#discussTurnState").inner_text()
    # The clock is the difference between "thinking" and "wedged".
    page.wait_for_function(r"() => /^\d+:\d\d$/.test(document.querySelector('#discussTurnClock').innerText)")
    # The tab carries the same state for anyone not looking at the panel.
    assert page.locator("#utilityTabCodex.utility-tab-busy").count() == 1

    page.click("#discussStop")
    page.wait_for_selector("#discussTurnStatus[data-state='failed']")
    assert "Stopped after" in page.locator("#discussTurnState").inner_text()

    page.fill("#discussInput", "Explain this scene")
    page.press("#discussInput", "Enter")
    wait_for_discuss_answer(page, "Fake answer")
    page.wait_for_selector("#discussTurnStatus[data-state='done']")
    assert "Answered in" in page.locator("#discussTurnState").inner_text()
    assert page.locator("#utilityTabCodex.utility-tab-busy").count() == 0


def test_the_dock_offers_the_scene_before_the_repository(page: Page, server: ProseviewServer):
    """Opening the dock on a scene should offer something about that scene.

    The passes writers repeat were reachable only after selecting prose, so the
    empty state led with two repository scans, one of which will not run until
    you type a paragraph describing a canon change.
    """
    open_scene(page, server)
    open_discuss(page)
    page.wait_for_function("() => document.querySelector('#discussConnection').innerText.startsWith('Live')")
    page.wait_for_selector(".discuss-story-action")

    labels = page.locator(".discuss-story-action-title").all_inner_texts()
    assert labels[:2] == ["Quick critique", "Style and consistency"]
    assert "Trace a canon change" in labels

    page.locator(".discuss-story-action", has_text="Quick critique").click()
    page.wait_for_selector(".discuss-message.user")
    # One click: nothing selected, nothing typed.
    assert page.locator("#discussInput").input_value() == ""
    wait_for_discuss_answer(page, "Fake answer")
    assert page.locator(".discuss-task").count() == 0


def test_discuss_queues_stops_and_continues(page: Page, server: ProseviewServer):
    open_scene(page, server)
    open_discuss(page)
    page.wait_for_function("() => document.querySelector('#discussConnection').innerText.startsWith('Live')")
    page.fill("#discussInput", "HOLD_FOR_STOP")
    page.press("#discussInput", "Enter")
    page.wait_for_selector("#discussStop", state="visible")

    page.fill("#discussInput", "Continue after the stopped turn")
    page.press("#discussInput", "Enter")
    page.wait_for_function("() => document.querySelector('#discussLog').innerText.includes('item queued')")

    page.reload(wait_until="load")
    page.wait_for_function("() => !!window._PM")
    page.wait_for_selector("#sceneModal", state="visible")
    open_discuss(page)
    page.wait_for_function("() => document.querySelectorAll('.discuss-message.user').length === 2")
    page.wait_for_selector("#discussStop", state="visible")
    page.click("#discussStop")
    wait_for_discuss_answer(page, "Fake answer")
    assert page.locator(".discuss-message.user").count() == 2
    page.wait_for_selector("#discussStop", state="hidden")


def test_discuss_stop_recovers_when_codex_unloads_thread(page: Page, server: ProseviewServer):
    open_scene(page, server)
    open_discuss(page)
    page.wait_for_function("() => document.querySelector('#discussConnection').innerText.startsWith('Live')")
    page.fill("#discussInput", "HOLD_UNLOAD_ON_STOP")
    page.press("#discussInput", "Enter")
    page.wait_for_selector("#discussStop", state="visible")

    page.fill("#discussInput", "Continue in a fresh conversation")
    page.press("#discussInput", "Enter")
    page.wait_for_function("() => document.querySelector('#discussLog').innerText.includes('item queued')")
    page.click("#discussStop")

    wait_for_discuss_answer(page, "Fake answer")
    page.wait_for_selector("#discussStop", state="hidden")
    connection = page.locator("#discussConnection").inner_text()
    assert connection == "Live"
    assert "thread not loaded" not in connection


def test_discuss_pending_queue_item_can_be_removed(page: Page, server: ProseviewServer):
    open_scene(page, server)
    open_discuss(page)
    page.wait_for_function("() => document.querySelector('#discussConnection').innerText.startsWith('Live')")
    page.fill("#discussInput", "HOLD_FOR_STOP")
    page.press("#discussInput", "Enter")
    page.wait_for_selector("#discussStop", state="visible")
    page.fill("#discussInput", "Remove this queued request")
    page.press("#discussInput", "Enter")
    remove = page.get_by_role("button", name="Remove Question from queue")
    remove.wait_for(state="visible")
    remove.click()
    page.wait_for_function("() => !document.querySelector('.discuss-queue-remove')")
    assert page.locator(".discuss-message.user").count() == 1
    page.click("#discussStop")
    page.wait_for_selector("#discussStop", state="hidden")


def test_active_codex_turn_explains_new_conversation_and_has_explicit_stop(
    page: Page, server: ProseviewServer
):
    open_scene(page, server)
    open_discuss(page)
    page.wait_for_function("() => document.querySelector('#discussConnection').innerText.startsWith('Live')")
    page.fill("#discussInput", "HOLD_FOR_STOP")
    page.press("#discussInput", "Enter")

    stop = page.get_by_role("button", name="Stop Codex")
    stop.wait_for(state="visible")
    assert page.locator("#discussNewConversation").is_disabled()
    hint = page.locator("#discussNewConversationHint")
    assert hint.is_visible()
    assert "Stop Codex before starting a new conversation" in hint.inner_text()

    stopping = page.evaluate(
        """() => new Promise(resolve => {
            const button = document.getElementById('discussStop');
            const observer = new MutationObserver(() => {
                if (button.textContent === 'Stopping…') {
                    observer.disconnect();
                    resolve(button.textContent);
                }
            });
            observer.observe(button, {childList: true, subtree: true});
            button.click();
        })"""
    )
    assert stopping == "Stopping…"
    page.wait_for_selector("#discussStop", state="hidden")
    page.wait_for_function("() => !document.getElementById('discussNewConversation').disabled")
    assert hint.is_hidden()


def test_discuss_refresh_recovers_missing_thread_and_new_conversation_is_explicit(
    page: Page,
    server: ProseviewServer,
):
    open_scene(page, server)
    open_discuss(page)
    page.wait_for_function("() => document.querySelector('#discussConnection').innerText.startsWith('Live')")
    page.fill("#discussInput", "FORGET_THREAD_AFTER_TURN")
    page.press("#discussInput", "Enter")
    wait_for_discuss_answer(page)

    page.reload(wait_until="load")
    page.wait_for_function("() => !!window._PM")
    page.wait_for_selector("#sceneModal", state="visible")
    open_discuss(page)
    page.wait_for_function("() => document.querySelector('#discussConnection').innerText.startsWith('Live')")
    panel_text = page.locator("#discussPanel").inner_text()
    assert "next question will start a new conversation" in panel_text.lower()
    assert "thread not found" not in panel_text.lower()

    page.fill("#discussInput", "Continue after refresh")
    page.press("#discussInput", "Enter")
    page.wait_for_function("() => document.querySelectorAll('.discuss-message.assistant').length === 2")
    page.wait_for_function("() => !document.getElementById('discussNewConversation').disabled")
    assert page.locator("#discussConnection").inner_text().startswith("Live")

    # new_conversation refuses while a turn is still winding down. The rendered
    # snapshot lags the server, so ask the server directly -- believing the
    # browser here is what made this fail roughly half the time.
    wait_for_discuss_idle(page)
    open_new_discuss_conversation_dialog(page)
    assert page.evaluate("document.activeElement === document.getElementById('discussNewConversationCancel')")
    assert "reopen the current conversation later from History" in page.locator("#discussNewConversationDialog").inner_text()
    page.keyboard.press("Escape")
    page.wait_for_selector("#discussNewConversationDialog", state="hidden")
    page.wait_for_selector("#discussPanel", state="visible")
    assert page.evaluate("document.activeElement === document.getElementById('discussNewConversation')")

    page.keyboard.press("Enter")
    # Wait for the dialog to come back before confirming: clicking into it while
    # it was still opening is what made this racy.
    page.wait_for_selector("#discussNewConversationDialog", state="visible")
    # The reset is refused while the worker is still draining, and the browser
    # cannot see that moment: the snapshot reports the turn finished before the
    # server stops calling itself busy. The dialog is built for this -- it keeps
    # the writer in place and offers "Try again" -- so exercise that path rather
    # than pretend the window does not exist.
    confirm_new_discuss_conversation(page)
    page.wait_for_function("() => document.querySelectorAll('#discussLog .discuss-message').length === 0")
    assert "Ask about what you are reading" in page.locator("#discussLog").inner_text()
    assert page.evaluate("document.activeElement === document.getElementById('discussInput')")

    page.fill("#discussInput", "A fresh browser conversation")
    page.press("#discussInput", "Enter")
    page.wait_for_function(
        "() => document.querySelectorAll('.discuss-message.user').length === 1 && "
        "document.querySelector('#discussLog').innerText.includes('Fake answer')"
    )
    assert page.locator(".discuss-message.user").count() == 1


def test_missing_thread_notice_stays_chronological_is_dismissible_and_does_not_trap_scroll(
    page: Page,
    server: ProseviewServer,
):
    page.set_viewport_size({"width": 1024, "height": 520})
    open_scene(page, server)
    open_discuss(page)
    page.wait_for_function("() => document.querySelector('#discussConnection').innerText.startsWith('Live')")

    page.fill("#discussInput", "FORGET_THREAD_AFTER_TURN")
    page.press("#discussInput", "Enter")
    page.wait_for_function(
        "() => document.querySelectorAll('.discuss-message.assistant').length === 1"
        " && !window._discussSnapshot.active_turn_id"
    )
    page.fill("#discussInput", "Continue after the missing thread")
    page.press("#discussInput", "Enter")
    page.wait_for_function(
        "() => document.querySelectorAll('.discuss-message.assistant').length === 2"
        " && !window._discussSnapshot.active_turn_id"
    )

    entries = page.locator("#discussLog > .discuss-message, #discussLog > .discuss-notice")
    rendered = entries.all_inner_texts()
    question_index = next(i for i, text in enumerate(rendered) if "Continue after the missing thread" in text)
    notice_index = next(i for i, text in enumerate(rendered) if "retried your question" in text)
    answer_index = max(i for i, text in enumerate(rendered) if "Fake answer" in text)
    assert question_index < notice_index < answer_index

    for expected in range(3, 7):
        page.fill("#discussInput", f"Follow-up {expected}")
        page.press("#discussInput", "Enter")
        page.wait_for_function(
            "count => document.querySelectorAll('.discuss-message.assistant').length === count"
            " && !window._discussSnapshot.active_turn_id",
            arg=expected,
        )

    scroll = page.locator("#discussLog")
    page.evaluate(
        """() => {
            const log = document.getElementById('discussLog');
            log.style.scrollBehavior = 'auto';
            log.scrollTop = log.scrollHeight;
        }"""
    )
    bottom = scroll.evaluate("node => node.scrollTop")
    page.evaluate("document.getElementById('discussLog').scrollTop -= 20")
    before = scroll.evaluate("node => node.scrollTop")
    assert before > 0 and 0 < bottom - before <= 21

    after = page.evaluate(
        """() => new Promise(resolve => {
            renderDiscussSnapshot();
            requestAnimationFrame(() => requestAnimationFrame(() => {
                resolve(document.getElementById('discussLog').scrollTop);
            }));
        })"""
    )
    assert abs(after - before) <= 1

    page.get_by_role("button", name="Dismiss notice").click()
    page.wait_for_selector("#discussLog .discuss-notice", state="detached")
    assert page.evaluate("document.activeElement === document.getElementById('discussLog')")


def test_conversation_history_reopens_a_previous_thread(page: Page, server: ProseviewServer):
    open_scene(page, server)
    open_discuss(page)
    page.wait_for_function("() => document.querySelector('#discussConnection').innerText.startsWith('Live')")
    page.fill("#discussInput", "Why is the opening quiet?")
    page.press("#discussInput", "Enter")
    wait_for_discuss_answer(page)

    page.click("#discussNewConversation")
    page.click("#discussNewConversationConfirm")
    page.wait_for_selector("#discussNewConversationDialog", state="hidden")
    page.fill("#discussInput", "What changes in the second conversation?")
    page.press("#discussInput", "Enter")
    wait_for_discuss_answer(page)

    page.click("#discussHistory")
    page.wait_for_selector("#discussHistoryDialog", state="visible")
    previous = page.locator(".discuss-history-row").filter(has_text="Why is the opening quiet?")
    previous.wait_for(state="visible")
    assert previous.count() == 1
    assert "Saved conversation" in previous.inner_text()
    previous.get_by_role("button", name="Open").click()
    page.wait_for_selector("#discussHistoryDialog", state="hidden")
    page.wait_for_function("() => document.querySelector('#discussLog').innerText.includes('Why is the opening quiet?')")
    assert "What changes in the second conversation?" not in page.locator("#discussLog").inner_text()

    page.fill("#discussInput", "Continue this earlier thought")
    page.press("#discussInput", "Enter")
    page.wait_for_function("() => document.querySelectorAll('.discuss-message.user').length === 2")
    page.wait_for_function("() => document.querySelectorAll('.discuss-message.assistant').length === 2")

    page.click("#discussHistory")
    current = page.locator(".discuss-history-row").filter(has_text="Why is the opening quiet?")
    current.wait_for(state="visible")
    current.locator("summary").click()
    current.get_by_role("button", name="Rename").click()
    current.locator("input").fill("Opening rhythm")
    current.get_by_role("button", name="Save").click()
    renamed = page.locator(".discuss-history-row").filter(has_text="Opening rhythm")
    renamed.wait_for(state="visible")
    renamed.locator("summary").click()
    with page.expect_download() as download_info:
        renamed.get_by_role("button", name="Export JSON").click()
    exported = Path(download_info.value.path()).read_text(encoding="utf-8")
    assert '"title": "Opening rhythm"' in exported
    assert "BEGIN UNTRUSTED DOCUMENT" not in exported
    assert "RAW SECRET" not in exported

    page.click("#discussHistoryClose")
    page.click("#discussNewConversation")
    page.click("#discussNewConversationConfirm")
    page.wait_for_selector("#discussNewConversationDialog", state="hidden")
    page.click("#discussHistory")
    saved = page.locator(".discuss-history-row").filter(has_text="Opening rhythm")
    saved.wait_for(state="visible")
    saved.locator("summary").click()
    page.once("dialog", lambda dialog: dialog.accept())
    saved.get_by_role("button", name="Remove from history").click()
    saved.wait_for(state="detached")


def test_new_conversation_dialog_announces_pending_and_recovers_from_failure(
    page: Page, server: ProseviewServer
):
    open_scene(page, server)
    open_scene_appearance(page)
    page.select_option("#modalThemeSelect", "dark")
    open_discuss(page)
    page.wait_for_function("() => document.querySelector('#discussConnection').innerText.startsWith('Live')")
    page.click("#discussNewConversation")
    page.wait_for_selector("#discussNewConversationDialog", state="visible")
    page.evaluate(
        """() => {
            const originalFetch = window.fetch.bind(window);
            window.fetch = function(...args) {
                if (String(args[0]).endsWith('/new')) {
                    return new Promise((resolve, reject) => {
                        window.__releaseConversationReset = () => {
                            window.fetch = originalFetch;
                            originalFetch(...args).then(resolve, reject);
                        };
                    });
                }
                return originalFetch(...args);
            };
        }"""
    )

    pending = page.evaluate(
        """() => new Promise(resolve => {
            const dialog = document.getElementById('discussNewConversationDialog');
            const button = document.getElementById('discussNewConversationConfirm');
            const observer = new MutationObserver(() => {
                if (button.textContent === 'Starting…') {
                    observer.disconnect();
                    resolve({
                        label: button.textContent,
                        disabled: button.disabled,
                        busy: dialog.getAttribute('aria-busy'),
                        announcement: document.getElementById('discussAnnouncement').textContent,
                    });
                }
            });
            observer.observe(button, {childList: true, subtree: true});
            button.click();
        })"""
    )
    assert pending == {
        "label": "Starting…",
        "disabled": True,
        "busy": "true",
        "announcement": "Starting a new conversation",
    }
    page.keyboard.press("Escape")
    assert page.locator("#discussNewConversationDialog").is_visible()
    slow_status = page.locator("#discussNewConversationStatus")
    slow_status.wait_for(state="visible", timeout=3_000)
    assert "Still starting" in slow_status.inner_text()
    page.evaluate("window.__releaseConversationReset()")
    page.wait_for_selector("#discussNewConversationDialog", state="hidden")

    page.click("#discussNewConversation")
    page.wait_for_selector("#discussNewConversationDialog", state="visible")
    page.route(
        "**/api/discuss/conversations/*/new",
        lambda route: route.fulfill(
            status=409,
            content_type="application/json",
            body='{"error":"The local reset could not finish safely."}',
        ),
    )
    page.click("#discussNewConversationConfirm")
    error = page.locator("#discussNewConversationError")
    error.wait_for(state="visible")
    assert "could not finish safely" in error.inner_text()
    retry = page.get_by_role("button", name="Try again")
    assert retry.is_enabled()
    assert retry.evaluate("button => document.activeElement === button")
    assert page.locator("#discussNewConversationDialog").get_attribute("aria-busy") == "false"


def test_new_conversation_dialog_remains_operable_at_dark_200_percent_zoom(
    page: Page, server: ProseviewServer
):
    page.set_viewport_size({"width": 1024, "height": 768})
    open_scene(page, server)
    open_scene_appearance(page)
    page.select_option("#modalThemeSelect", "dark")
    open_discuss(page)
    page.wait_for_function("() => document.querySelector('#discussConnection').innerText.startsWith('Live')")
    page.evaluate("document.body.style.zoom = '2'")
    page.click("#discussNewConversation")
    page.wait_for_selector("#discussNewConversationDialog", state="visible")

    assert_fully_inside_viewport(page, "#discussNewConversationDialog")
    assert page.get_by_role("button", name="Keep conversation").is_visible()
    assert page.get_by_role("button", name="Start new conversation").is_visible()
    page.keyboard.press("Escape")
    page.wait_for_selector("#discussNewConversationDialog", state="hidden")


def open_scene_details(page: Page) -> None:
    """Open the scene panel on its Scene tab.

    Replaces the two ``<details>`` disclosures that used to sit above the prose.
    Frontmatter, story fields, links and tasks now live in the dock, so a test
    that wants the scene card opens the panel rather than expanding a summary.
    """
    page.evaluate("() => showScenePanelTab('scene')")
    page.wait_for_selector("#sceneDetailsPane:not([hidden])")


def open_scene_analysis(page: Page) -> None:
    """Open the scene panel on its Analysis tab: measures and highlight passes."""
    page.evaluate("() => showScenePanelTab('analysis')")
    page.wait_for_selector("#sceneAnalysisPane:not([hidden])")


def open_discuss(page: Page) -> None:
    """Open the dock on its Codex tab.

    The toolbar button owns the whole dock now rather than Discuss alone, so
    reaching Codex is "open the panel, choose the tab". Tests go through this
    helper so the tab order can change again without touching thirty of them.
    """
    # The tabs live inside the dock, so they cannot be clicked to open it --
    # this helper used to hunt for one in #sceneModal and #file-preview-panel,
    # where it has never been. Go through the same entry point the tab buttons
    # call, which opens the panel and selects the agent in one step.
    page.evaluate("() => showDiscussAgentTab('codex')")
    page.wait_for_selector("#discussPanel:not([hidden])")
    page.wait_for_selector("#discussLog:not([hidden])")
    page.wait_for_function("() => _discussAgent === 'codex'")


def wait_for_discuss_idle(page: Page) -> None:
    """Block until the server agrees the conversation has nothing in flight."""
    page.wait_for_function(
        """async () => {
            if (!window._discussConversationId) return false;
            const response = await fetch(
                '/api/discuss/conversations/' + encodeURIComponent(_discussConversationId) + '/snapshot',
                {cache: 'no-store'}
            );
            if (!response.ok) return false;
            const snapshot = (await response.json()).snapshot || {};
            return !snapshot.active_turn_id
                && !snapshot.active_request_id
                && !(snapshot.queue || []).length;
        }"""
    )


def confirm_new_discuss_conversation(page: Page, attempts: int = 5) -> None:
    """Confirm the reset, retrying the busy refusal the dialog invites."""
    for _ in range(attempts):
        page.click("#discussNewConversationConfirm")
        try:
            page.wait_for_selector("#discussNewConversationDialog", state="hidden", timeout=5000)
            return
        except Exception:
            error = page.locator("#discussNewConversationError")
            if not error.is_visible() or "busy" not in error.inner_text():
                raise
            wait_for_discuss_idle(page)
    raise AssertionError("the conversation never became resettable")


def open_new_discuss_conversation_dialog(page: Page, attempts: int = 5) -> None:
    """Open the reset dialog from the keyboard, tolerating the busy window.

    The dialog refuses to open while the server still considers the
    conversation busy, and the browser cannot see the moment that ends. press()
    also re-resolves the button, which matters because a snapshot re-render
    between a separate focus() and keypress rebuilds it.
    """
    for _ in range(attempts):
        wait_for_discuss_idle(page)
        page.locator("#discussNewConversation").press("Enter")
        try:
            page.wait_for_selector("#discussNewConversationDialog", state="visible", timeout=5000)
            return
        except Exception:
            continue
    raise AssertionError("the new-conversation dialog never opened")


def open_selection_menu(page: Page, needle: str) -> None:
    select_prose(page, needle)
    page.click("#selectionPillBtn")
    page.wait_for_selector("#selectionPillMenu", state="visible")


def _editor_text(page: Page) -> str:
    return page.locator("#sceneProseHost .ProseMirror").inner_text()


def frontmatter(text: str) -> str:
    match = re.match(r"^---\n.*?\n---\n", text, re.DOTALL)
    assert match, "scene file lost its frontmatter block"
    return match.group(0)


def paragraphs(text: str) -> list[str]:
    body = re.sub(r"^---\n.*?\n---\n", "", text, flags=re.DOTALL)
    return [p.strip() for p in body.split("\n\n") if p.strip()]


# ── dashboard, navigation, preferences ──────────────────────────────────────


def test_dashboard_renders_the_scene_table_and_charts(page: Page, server: ProseviewServer):
    open_dashboard(page, server)

    assert page.locator("#sceneTable tbody tr").count() >= 6
    table = page.locator("#sceneTable").inner_text()
    assert SCENE_REL in table
    assert LARGE_SCENE_REL in table
    assert "10,069" in table, "word counts are not rendered in the scene table"



def test_analysis_initializes_every_owned_chart(
    page: Page,
    server: ProseviewServer,
):
    open_dashboard(page, server)
    page.click('.tab-nav button[data-tab="analysis"]')
    page.wait_for_selector("#analysisContent:not([hidden])")

    for chart_id in ("presenceChart", "locationChart", "coOccurChart", "lexicalScatterChart"):
        page.wait_for_function(
            "chartId => !!window.Chart.getChart(document.getElementById(chartId))",
            arg=chart_id,
        )
        # Count plotted points, not labels. A scatter chart is built from
        # {x, y} pairs and carries no labels at all, so a labels-based check
        # reported the Lexical Health Map as empty while it was drawing 12
        # scenes -- a red test that said nothing about the app.
        chart = page.evaluate(
            "chartId => { const c = Chart.getChart(document.getElementById(chartId)); "
            "return {datasets: c.data.datasets.length, "
            "points: c.data.datasets.reduce((n, d) => n + ((d.data || []).length), 0)}; }",
            chart_id,
        )
        assert chart["datasets"] > 0 and chart["points"] > 0, (
            f"{chart_id} initialized without its fixture data: {chart}"
        )


def test_every_chart_exposes_its_values_without_reading_canvas_pixels(
    page: Page,
    server: ProseviewServer,
):
    open_dashboard(page, server)

    page.click('.tab-nav button[data-tab="analysis"]')
    page.wait_for_selector("#analysisContent:not([hidden])")
    for chart_id in ("presenceChart", "locationChart", "coOccurChart", "lexicalScatterChart"):
        figure = page.locator(f"figure:has(#{chart_id})")
        assert figure.get_attribute("aria-labelledby")
        figure.get_by_text("View chart data", exact=True).click()
        assert figure.get_by_role("table").locator("tbody tr").count() > 0


def test_lexical_chart_alternative_names_axes_and_target_ranges(
    page: Page,
    server: ProseviewServer,
):
    open_dashboard(page, server)
    page.click('.tab-nav button[data-tab="analysis"]')
    page.wait_for_selector("#analysisContent:not([hidden])")
    figure = page.locator("figure:has(#lexicalScatterChart)")
    figure.get_by_text("View chart data", exact=True).click()
    alternative = figure.locator(".chart-data").inner_text()
    assert "Local Variety (MATTR)" in alternative
    assert "Whole-Scene Variety (MTLD)" in alternative
    assert "Target range" in alternative


def test_dashboard_lexical_health_cards_format_values_as_percentages_and_words(
    page: Page,
    server: ProseviewServer,
):
    open_dashboard(page, server)
    page.click('.tab-nav button[data-tab="analysis"]')
    page.wait_for_selector("#analysisContent:not([hidden])")
    
    # Wait for the lexical cards to populate
    page.wait_for_function("() => document.querySelector('#analysisMattrText').innerText.includes('%')")
    
    mattr_text = page.locator("#analysisMattrText").inner_text()
    mtld_text = page.locator("#analysisMtldText").inner_text()
    
    # Should be formatted as 69.5% instead of 0.695
    assert "%" in mattr_text
    assert "." in mattr_text  # Should have decimal precision, e.g. 69.5%
    
    # Should be formatted as "75 words" instead of just "75.6"
    assert "words" in mtld_text


def test_dashboard_has_landmarks_headings_and_no_horizontal_page_overflow(
    page: Page,
    server: ProseviewServer,
):
    for viewport in ({"width": 1400, "height": 1000}, {"width": 1024, "height": 768}):
        page.set_viewport_size(viewport)
        open_dashboard(page, server)
        assert page.get_by_role("main").count() == 1
        assert page.get_by_role("heading", level=1).count() == 1
        assert page.get_by_role("heading", level=2).count() >= 1
        assert page.evaluate(
            "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
        ), f"dashboard overflowed at {viewport}"
        assert_fully_inside_viewport(page, "#themeToggle")


def test_dashboard_charts_remain_contained_after_resize_and_css_zoom(
    page: Page,
    server: ProseviewServer,
):
    page.set_viewport_size({"width": 1400, "height": 1000})
    open_dashboard(page, server)
    page.click('.tab-nav button[data-tab="analysis"]')
    page.wait_for_selector("#analysisContent:not([hidden])")
    page.set_viewport_size({"width": 1024, "height": 768})
    page.evaluate("document.body.style.zoom = '2'")
    page.wait_for_function("() => document.documentElement.dataset.cssZoom === 'true'")

    assert page.evaluate(
        """() => Array.from(document.querySelectorAll('#tab-analysis .chart-frame')).every(frame => {
            const canvas = frame.querySelector('canvas');
            const outer = frame.getBoundingClientRect();
            const inner = canvas.getBoundingClientRect();
            return inner.left >= outer.left - 1 && inner.right <= outer.right + 1;
        })"""
    )


def test_dashboard_tabs_announce_the_current_route(page: Page, server: ProseviewServer):
    open_dashboard(page, server)
    overview = page.locator('.tab-nav button[data-tab="overview"]')
    timeline = page.locator('.tab-nav button[data-tab="timeline"]')
    assert overview.get_attribute("aria-current") == "page"
    assert timeline.get_attribute("aria-current") is None

    timeline.click()
    assert timeline.get_attribute("aria-current") == "page"
    assert overview.get_attribute("aria-current") is None


def test_dashboard_settings_tab_renders_and_reads_config(page: Page, server: ProseviewServer):
    open_dashboard(page, server)
    settings = page.locator('.tab-nav button[data-tab="settings"]')
    settings.click()
    assert settings.get_attribute("aria-current") == "page"
    assert page.locator("#tab-settings").is_visible()
    
    settings_text = page.locator("#tab-settings").inner_text()
    assert "Project Configuration" in settings_text
    assert "Target Words:" in settings_text


@pytest.mark.allow_http_errors("/api/discuss/conversations/open")
def test_discuss_tab_shows_ai_not_connected_empty_state_without_codex(page: Page, server: ProseviewServer):
    open_scene(page, server)

    # Mock the API to simulate Codex not being installed/reachable
    def handle_route(route):
        route.fulfill(
            status=503,
            content_type="application/json",
            body='{"ok": false, "error": "Codex CLI is not installed or is not on PATH"}'
        )

    page.route("**/api/discuss/conversations/open", handle_route)

    # Click the discuss/panel button
    page.evaluate("openDiscuss(document.querySelector('#utilityTabCodex'))")
    page.wait_for_selector("#discussPanel", state="visible")
    
    # Verify the empty state renders
    page.wait_for_function("() => document.querySelector('#discussConnection').innerText.includes('Not connected')")

    log_text = page.locator("#discussLog").inner_text()
    # Named per agent now that there are two tabs, and reporting the real
    # reason rather than assuming a missing Codex CLI.
    assert "Codex is not connected" in log_text
    assert "Proseview runs entirely locally" in log_text
    assert "Codex CLI is not installed or is not on PATH" in log_text
    
    # Verify the composer is hidden
    assert page.locator("#discussComposerArea").is_hidden()


def test_repository_metadata_and_bios_render_as_content_not_executable_html(
    page: Page,
    server: ProseviewServer,
):
    payload = '<img data-pv-xss src=x onerror="window.__pvXss=true">'
    scene = server.scene_path()
    scene_text = scene.read_text().replace("chapter: Chapter 1", f"chapter: '{payload}'")
    scene_text = scene_text.replace(
        "goal: Rena needs to clear a weekly ledger before the shop opens.",
        f"goal: '{payload}'",
    )
    scene.write_text(scene_text, encoding="utf-8")
    bio = server.root / "story-bible" / "characters" / "rena.md"
    bio.write_text(bio.read_text(encoding="utf-8") + "\n\n" + payload + "\n", encoding="utf-8")
    server.restart()

    page.goto(server.base_url, wait_until="load")
    assert page.locator("img[data-pv-xss]").count() == 0
    assert page.evaluate("window.__pvXss !== true")
    assert payload in page.locator("#sceneTable").inner_text()

    open_scene(page, server)
    open_scene_details(page)
    assert payload in page.locator(".scene-card").inner_text()
    assert page.locator(".scene-card img[data-pv-xss]").count() == 0
    page.locator(".sc-char-tag", has_text="Rena").click()
    assert page.locator("img[data-pv-xss]").count() == 0
    assert page.evaluate("window.__pvXss !== true")
    assert payload in page.locator(".bio-card").inner_text()


def test_repository_filenames_and_editor_config_cannot_escape_their_html_contexts(
    page: Page,
    server: ProseviewServer,
):
    filename = 'evil" autofocus onfocus="window.__pvRecentXss=true.md'
    hostile = server.root / "plans" / filename
    hostile.write_text("# Hostile filename\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=server.root, check=True)
    subprocess.run(["git", "add", "--", f"plans/{filename}"], cwd=server.root, check=True)
    subprocess.run(
        [
            "git", "-c", "user.name=Proseview E2E", "-c", "user.email=e2e@example.invalid",
            "commit", "-qm", "hostile filename fixture",
        ],
        cwd=server.root,
        check=True,
    )
    (server.root / ".proseview.yaml").write_text(
        "editor:\n"
        "  scheme: custom\n"
        '  url_template: "safe</script><script>window.__pvConfigXss=true</script>:{abs_path}"\n',
        encoding="utf-8",
    )
    server.restart()

    page.goto(server.base_url, wait_until="load")
    recent = page.locator(".recent-file-link", has_text=filename)
    recent.wait_for(state="visible")
    assert recent.get_attribute("onfocus") is None
    assert recent.get_attribute("data-repo-path") == f"plans/{filename}"
    assert page.evaluate("window.__pvRecentXss !== true && window.__pvConfigXss !== true")

    recent.click()
    page.wait_for_function(
        "expected => document.getElementById('filePreviewTitle').innerText === expected",
        arg=f"plans/{filename}",
    )


def test_overview_does_not_ship_the_lexical_analysis(page: Page, server: ProseviewServer):
    """The expensive pass must not be paid at first paint.

    The Overview scene table drops the four analysis columns, and the two
    analysis charts render only once their tab is opened.
    """
    open_dashboard(page, server)

    # The header row is upper-cased by CSS, so compare case-insensitively.
    headers = [h.strip().lower() for h in page.locator("#sceneTable thead th").all_inner_texts()]
    assert headers == ["scene", "chapter", "words", "keywords"]
    for chart_id in ("presenceChart", "locationChart", "coOccurChart", "lexicalScatterChart"):
        assert page.locator(f"#{chart_id}").bounding_box() is None, \
            f"{chart_id} should not be laid out before the Analysis tab is opened"


def test_analysis_tab_loads_on_demand_and_renders_every_panel(page: Page, server: ProseviewServer):
    """End-to-end cover for the lazy Analysis tab: fetch, inject, chart."""
    open_dashboard(page, server)
    page.click('.tab-nav button[data-tab="analysis"]')

    page.wait_for_selector("#analysisContent:not([hidden])")
    page.wait_for_function("() => document.querySelectorAll('#analysisSceneTable tbody tr').length > 0")

    # The four analysis columns are back on this table.
    headers = [h.strip().lower() for h in page.locator("#analysisSceneTable thead th").all_inner_texts()]
    assert headers == ["scene", "chapter", "words", "lexical health", "keywords", "top repeat", "dialogue %", "avg sent length"]

    # Book-wide lexical health is filled in from the payload, not left blank.
    assert page.locator("#analysisMattrText").inner_text().strip()
    assert page.locator("#analysisMtldText").inner_text().strip()

    for chart_id in ("presenceChart", "locationChart", "coOccurChart", "lexicalScatterChart"):
        box = page.locator(f"#{chart_id}").bounding_box()
        assert box and box["width"] > 0, f"{chart_id} did not render on the Analysis tab"

    # The tab is a real route, so a deep link lands on it directly.
    assert page.evaluate("decodeURIComponent(location.hash)") == "#/tab/analysis"


def test_deep_link_opens_a_scene_and_back_returns_to_the_dashboard(page: Page, server: ProseviewServer):
    open_dashboard(page, server)
    page.locator("#sceneTable .scene-table-link", has_text=SCENE_REL).click()
    page.wait_for_selector("#sceneModal", state="visible")
    # The router percent-encodes the path segment, so compare decoded.
    assert page.evaluate("decodeURIComponent(location.hash)") == f"#/scene/{SCENE_REL}"

    page.go_back()
    page.wait_for_selector("#sceneModal", state="hidden")

    # And the URL alone is enough to restore the view.
    open_scene(page, server)
    assert SCENE_REL in page.locator("#modalTitle").inner_text()


def test_routed_documents_expose_a_primary_literary_heading(
    page: Page,
    server: ProseviewServer,
):
    open_scene(page, server)
    assert page.get_by_role("main").count() == 1
    scene_main = page.get_by_role("main", name=re.compile("Opening Ledger", re.I))
    assert scene_main.count() == 1
    scene_heading = scene_main.get_by_role("heading", level=1)
    assert "Opening Ledger" in scene_heading.inner_text()
    assert SCENE_REL in scene_heading.inner_text()

    page.goto(f"{server.base_url}#/file/plans/book-plan.md", wait_until="load")
    page.wait_for_selector("#file-preview-panel", state="visible")
    file_heading = page.get_by_role("heading", level=1, name=re.compile("book-plan", re.I))
    assert file_heading.count() == 1


def test_tab_routes_survive_navigation(page: Page, server: ProseviewServer):
    open_dashboard(page, server)
    page.click(".tab-nav button[data-tab='todos']")
    assert page.evaluate("location.hash") == "#/tab/todos"

    page.reload(wait_until="load")
    page.wait_for_selector("#tab-todos.active")
    assert page.locator("#tab-todos").is_visible()


def test_todo_and_note_tabs_list_scene_annotations(page: Page, server: ProseviewServer):
    open_dashboard(page, server)

    page.click(".tab-nav button[data-tab='todos']")
    page.wait_for_selector("#tab-todos.active")
    assert "Tighten this opening beat" in page.locator("#tab-todos").inner_text()

    page.click(".tab-nav button[data-tab='notes']")
    page.wait_for_selector("#tab-notes.active")
    assert "Patel should not know about the safe yet" in page.locator("#notesTabContent").inner_text()


def test_scene_table_sorts_by_column(page: Page, server: ProseviewServer):
    open_dashboard(page, server)
    first_column = "#sceneTable tbody tr td:first-child"

    before = page.locator(first_column).all_inner_texts()
    page.click("#sceneTable thead th:first-child")
    after = page.locator(first_column).all_inner_texts()

    assert sorted(before) == sorted(after), "sorting must not add or drop rows"
    assert before != after, "clicking the header did not reorder the table"


def test_scene_table_sorting_is_keyboard_operable_and_announces_direction(
    page: Page,
    server: ProseviewServer,
):
    open_dashboard(page, server)
    first_column = "#sceneTable tbody tr td:first-child"
    before = page.locator(first_column).all_inner_texts()
    button = page.get_by_role("button", name="Sort by Scene")
    button.focus()
    page.keyboard.press("Enter")

    after = page.locator(first_column).all_inner_texts()
    assert before != after
    assert page.locator("#sceneTable thead th:first-child").get_attribute("aria-sort") in {
        "ascending",
        "descending",
    }


def test_theme_choice_survives_a_reload(page: Page, server: ProseviewServer):
    """Theme is written to localStorage and re-applied on load."""
    open_scene(page, server)
    open_scene_appearance(page)
    page.select_option("#modalThemeSelect", "dark")
    _wait_until(lambda: page.evaluate("document.documentElement.dataset.theme") == "dark")

    page.reload(wait_until="load")
    page.wait_for_function("() => !!window._PM")

    assert page.evaluate("document.documentElement.dataset.theme") == "dark"
    assert page.locator("#modalThemeSelect").input_value() == "dark"


def test_scene_toolbar_is_compact_and_exposes_grouped_actions(page: Page, server: ProseviewServer):
    open_scene(page, server)

    header = page.locator("#sceneModal .modal-header")
    box = header.bounding_box()
    assert box and box["height"] <= 52
    assert page.locator("#modalTitle").bounding_box()["width"] > 0
    assert page.get_by_role("button", name="Show or hide the scene panel").is_visible()
    assert page.get_by_role("button", name="Edit scene").is_visible()

    page.locator("#sceneAppearanceBtn").click()
    appearance = page.locator("#sceneAppearanceMenu")
    assert appearance.is_visible()
    assert page.locator("#modalFontSize").is_visible()
    assert page.locator("#modalFontSelect").is_visible()
    assert page.locator("#modalThemeSelect").is_visible()
    assert page.locator("#modalLineNumbersBtn").is_visible()

    page.locator("#sceneMoreBtn").click()
    more = page.locator("#sceneMoreMenu")
    assert more.is_visible()
    assert page.locator("#modalRefreshBtn").is_visible()
    assert page.locator("#modalEditorBtn").is_visible()
    assert page.locator("#agentMenuSceneBtn").is_visible()
    assert more.get_by_role("button", name="Open shell").is_visible()
    page.keyboard.press("Escape")
    assert more.is_hidden()
    assert page.evaluate("document.activeElement.id") == "sceneMoreBtn"


def test_scene_toolbar_visibility_mode_persists_and_has_keyboard_recovery(
    page: Page,
    server: ProseviewServer,
):
    open_scene(page, server)
    header = page.locator("#sceneModal .modal-header")

    page.locator("#sceneAppearanceBtn").click()
    page.locator("input[name='sceneToolbarMode'][value='hidden']").check()
    page.wait_for_function(
        "() => document.querySelector('#sceneModal .modal-header').dataset.toolbarHidden === 'true'"
    )
    assert page.evaluate("localStorage.getItem('proseview-scene-toolbar-mode')") == "hidden"

    page.reload(wait_until="load")
    page.wait_for_function("() => !!window._PM")
    page.wait_for_selector("#sceneModal", state="visible")
    page.wait_for_function(
        "() => document.querySelector('#sceneModal .modal-header').dataset.toolbarHidden === 'true'"
    )
    # With the toolbar hidden the prose owns the top of the window -- 60px is
    # the reading column's own gutter, and nothing else sits above it.
    prose_box = page.locator("#sceneProseHost").bounding_box()
    assert prose_box and prose_box["y"] <= 80

    reveal_box = page.locator("#sceneToolbarReveal").bounding_box()
    assert reveal_box
    page.mouse.move(
        reveal_box["x"] + reveal_box["width"] / 2,
        reveal_box["y"] + reveal_box["height"] / 2,
    )
    page.wait_for_function(
        "() => document.querySelector('#sceneModal .modal-header').dataset.toolbarHidden === 'false'"
    )
    page.mouse.move(500, 500)
    page.wait_for_function(
        "() => document.querySelector('#sceneModal .modal-header').dataset.toolbarHidden === 'true'"
    )

    page.locator("#sceneToolbarReveal").focus()
    page.wait_for_function(
        "() => document.querySelector('#sceneModal .modal-header').dataset.toolbarHidden === 'false'"
    )
    assert page.evaluate("document.activeElement.id") == "sceneToolbarReveal"
    assert header.get_attribute("data-toolbar-mode") == "hidden"


def test_scene_toolbar_auto_hides_on_scroll_and_reveals_on_reverse_scroll(
    page: Page,
    server: ProseviewServer,
):
    open_scene(page, server, LARGE_SCENE_REL)
    scroller = page.locator("#sceneModal .modal-content")
    # Route scroll restoration retries for 260ms while the editor settles.
    page.wait_for_timeout(350)
    scroller.hover(position={"x": 400, "y": 500})
    page.mouse.wheel(0, 1400)
    page.wait_for_function(
        "() => document.querySelector('#sceneModal .modal-header').dataset.toolbarHidden === 'true'"
    )

    page.mouse.wheel(0, -500)
    page.wait_for_function(
        "() => document.querySelector('#sceneModal .modal-header').dataset.toolbarHidden === 'false'"
    )


def test_scene_toolbar_auto_mode_does_not_move_with_reduced_motion(
    page: Page,
    server: ProseviewServer,
):
    page.emulate_media(reduced_motion="reduce")
    open_scene(page, server, LARGE_SCENE_REL)
    scroller = page.locator("#sceneModal .modal-content")
    page.wait_for_timeout(350)
    scroller.evaluate(
        "node => { node.style.scrollBehavior = 'auto'; "
        "node.scrollTop = node.scrollHeight - node.clientHeight; "
        "node.dispatchEvent(new Event('scroll')); }"
    )

    assert page.locator("#sceneModal .modal-header").get_attribute("data-toolbar-hidden") == "false"


def test_focus_layout_uses_the_toolbar_visibility_state(page: Page, server: ProseviewServer):
    open_scene(page, server)

    page.keyboard.press("f")
    page.wait_for_function(
        "() => document.querySelector('#sceneModal .modal-header').dataset.toolbarHidden === 'true'"
    )
    assert page.locator("#modalFocusBtn").get_attribute("aria-pressed") == "true"

    page.keyboard.press("f")
    page.wait_for_function(
        "() => document.querySelector('#sceneModal .modal-header').dataset.toolbarHidden === 'false'"
    )
    assert page.locator("#modalFocusBtn").get_attribute("aria-pressed") == "false"


def test_focus_mode_closes_the_dock_and_gives_it_back(page: Page, server: ProseviewServer):
    """Focus mode used to hide the two disclosures above the prose.

    They live in the dock now, so focus mode closes the dock -- and reopens it
    on the way out, on the tab it was showing. A reading mode that quietly
    discards your panel is a mode you stop using.
    """
    open_scene(page, server)
    open_scene_analysis(page)

    page.keyboard.press("f")
    page.wait_for_selector("#discussPanel", state="hidden")

    page.keyboard.press("f")
    page.wait_for_selector("#sceneAnalysisPane:not([hidden])")


def test_focus_mode_leaves_a_closed_dock_closed(page: Page, server: ProseviewServer):
    open_scene(page, server)
    assert page.locator("#discussPanel").is_hidden()

    page.keyboard.press("f")
    page.keyboard.press("f")
    page.wait_for_function(
        "() => document.querySelector('#sceneModal .modal-header').dataset.toolbarHidden === 'false'"
    )
    assert page.locator("#discussPanel").is_hidden(), (
        "leaving focus mode must not conjure a panel the reader never opened"
    )


def test_scene_toolbar_mode_change_invalidates_temporary_hide_timer(
    page: Page,
    server: ProseviewServer,
):
    open_scene(page, server)
    page.evaluate("setSceneToolbarMode('hidden'); revealSceneToolbar(true)")
    page.locator("#sceneAppearanceBtn").focus()
    page.keyboard.press("Enter")
    page.locator("input[name='sceneToolbarMode'][value='pinned']").check()
    page.locator("#sceneProseHost .ProseMirror").focus()
    page.wait_for_timeout(2000)

    header = page.locator("#sceneModal .modal-header")
    assert header.get_attribute("data-toolbar-mode") == "pinned"
    assert header.get_attribute("data-toolbar-hidden") == "false"


def test_scene_toolbar_stays_single_row_with_dock_and_at_two_hundred_percent_zoom(
    page: Page,
    server: ProseviewServer,
):
    page.set_viewport_size({"width": 1400, "height": 800})
    open_scene(page, server)
    open_discuss(page)
    page.wait_for_selector("#discussPanel", state="visible")

    header = page.locator("#sceneModal .modal-header")
    box = header.bounding_box()
    assert box and box["height"] <= 52
    assert page.locator("#modalTitle").bounding_box()["width"] > 0

    page.evaluate("document.body.style.zoom = '2'")
    page.wait_for_function("() => document.documentElement.dataset.cssZoom === 'true'")
    box = header.bounding_box()
    assert box and box["height"] <= 104
    assert page.evaluate(
        "() => { const h = document.querySelector('#sceneModal .modal-header'); "
        "return h.scrollWidth <= h.clientWidth; }"
    )
    for selector in ("#sceneMoreBtn", "#sceneAppearanceBtn", "#sceneEditBtn"):
        action = page.locator(selector).bounding_box()
        assert action
        assert action["x"] >= 0 and action["x"] + action["width"] <= 1400

    for trigger, menu in (
        ("#sceneAppearanceBtn", "#sceneAppearanceMenu"),
        ("#sceneMoreBtn", "#sceneMoreMenu"),
    ):
        page.locator(trigger).click()
        menu_box = page.locator(menu).bounding_box()
        assert menu_box
        assert menu_box["x"] >= 0
        assert menu_box["x"] + menu_box["width"] <= 1400
        assert menu_box["y"] >= 0
        assert menu_box["y"] + menu_box["height"] <= 800
        page.keyboard.press("Escape")


def test_scene_toolbar_actions_remain_clickable_beside_compact_dock(
    page: Page,
    server: ProseviewServer,
):
    page.set_viewport_size({"width": 1024, "height": 768})
    open_scene(page, server)
    open_discuss(page)
    page.wait_for_selector("#discussPanel", state="visible")

    header = page.locator("#sceneModal .modal-header")
    assert header.bounding_box()["height"] <= 52
    assert page.evaluate(
        "() => { const h = document.querySelector('#sceneModal .modal-header'); "
        "return h.scrollWidth <= h.clientWidth; }"
    )
    page.locator("#sceneAppearanceBtn").click()
    assert page.locator("#sceneAppearanceMenu").is_visible()


def test_compact_right_dock_never_covers_scene_content_or_toolbar_controls(
    page: Page,
    server: ProseviewServer,
):
    page.set_viewport_size({"width": 1024, "height": 768})
    open_scene(page, server)
    open_discuss(page)
    page.wait_for_selector("#discussPanel", state="visible")

    geometry = page.evaluate(
        """() => {
            const dock = document.querySelector('#discussPanel').getBoundingClientRect();
            const content = document.querySelector('#sceneModal .modal-content').getBoundingClientRect();
            const controls = ['sceneAppearanceBtn', 'sceneMoreBtn'].map(id => document.getElementById(id))
                .concat([document.querySelector('#sceneModal .modal-close')]);
            return {
                dockLeft: dock.left,
                contentRight: content.right,
                controls: controls.map(control => {
                    const rect = control.getBoundingClientRect();
                    const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
                    return {id: control.id || 'close', right: rect.right, ownsHit: hit === control || control.contains(hit)};
                })
            };
        }"""
    )
    assert geometry["contentRight"] <= geometry["dockLeft"] + 1
    assert all(control["right"] <= geometry["dockLeft"] + 1 for control in geometry["controls"])
    assert all(control["ownsHit"] for control in geometry["controls"])


def test_one_dock_shows_one_thing_at_a_time(
    page: Page,
    server: ProseviewServer,
):
    """Switching to Codex puts the scene panes away, and the prose stays put.

    The dock is a single surface with four tabs. Two of them showing at once
    was the confusion that made the old disclosures unreadable.
    """
    page.set_viewport_size({"width": 1400, "height": 1000})
    open_scene(page, server)
    open_scene_details(page)
    open_discuss(page)
    page.wait_for_selector("#discussPanel", state="visible")
    assert page.locator("#sceneDetailsPane").is_hidden()
    assert page.locator("#sceneAnalysisPane").is_hidden()

    open_scene_analysis(page)
    assert page.locator("#sceneDetailsPane").is_hidden()
    assert page.locator("#discussLog").is_hidden()

    prose = page.locator("#sceneProseHost").bounding_box()
    assert prose and prose["y"] < 400


def test_unavailable_terminal_hides_every_terminal_backed_entry_point(
    page: Page,
    server: ProseviewServer,
):
    open_scene(page, server)
    open_discuss(page)
    page.wait_for_selector("#discussPanel", state="visible")
    page.evaluate("_hideTerminalEntryPointsWhenUnavailable(false)")

    assert page.locator('[onclick*="openShellTerminal"]:visible').count() == 0
    assert page.locator(".agent-menu-wrap:visible").count() == 0
    assert page.locator('#discussPanel [onclick="showRightTerminal()"]:visible').count() == 0


def test_compact_scene_leads_with_prose_and_context_reflows_beside_the_dock(
    page: Page,
    server: ProseviewServer,
):
    page.set_viewport_size({"width": 1024, "height": 768})
    open_scene(page, server)

    prose = page.locator("#sceneProseHost").bounding_box()
    assert prose and prose["y"] < 400, "secondary UI still pushes prose out of the opening viewport"

    open_scene_details(page)
    page.wait_for_selector("#discussPanel", state="visible")
    # The card's three columns stack in a dock this narrow: same left edge,
    # descending down the pane rather than squeezed side by side.
    columns = page.evaluate(
        "() => ['.scene-card-meta', '.scene-card-arc', '.scene-card-related']"
        ".map(sel => document.querySelector(sel))"
        ".filter(Boolean)"
        ".map(el => el.getBoundingClientRect())"
        ".map(r => ({left: Math.round(r.left), top: Math.round(r.top)}))"
    )
    assert len(columns) >= 2
    assert len({c["left"] for c in columns}) == 1, "the card is still laid out in columns"
    assert [c["top"] for c in columns] == sorted(c["top"] for c in columns)
    assert page.evaluate(
        "() => Array.from(document.querySelectorAll('.scene-card .sc-value')).every(el => "
        "el.getBoundingClientRect().width > 120 && el.scrollWidth <= el.clientWidth + 1)"
    )


def test_the_dock_overlays_rather_than_squeezing_the_prose_to_a_ribbon(
    page: Page,
    server: ProseviewServer,
):
    """At a wide viewport the dock splits the screen; at a narrow one it covers it.

    The rule is not a breakpoint but a question: can the reading column still
    hold the measure the reader chose? Splitting an 820px window would leave
    prose too narrow to read, so the dock overlays until it is closed.
    """
    page.set_viewport_size({"width": 1600, "height": 1000})
    open_scene(page, server)
    open_scene_details(page)
    page.wait_for_selector("#discussPanel", state="visible")

    assert page.evaluate("() => document.documentElement.dataset.utilityOverlay") != "true"
    wide = page.evaluate(
        "() => ({prose: document.getElementById('sceneProseHost').getBoundingClientRect(),"
        " dock: document.getElementById('discussPanel').getBoundingClientRect()})"
    )
    assert wide["prose"]["right"] <= wide["dock"]["left"] + 1, "the dock and the prose share the width"

    page.set_viewport_size({"width": 820, "height": 900})
    page.wait_for_function(
        "() => document.documentElement.dataset.utilityOverlay === 'true'"
    )
    narrow = page.evaluate(
        "() => document.getElementById('discussPanel').getBoundingClientRect().width"
    )
    assert narrow > 700, "the dock takes the window rather than halving it"

    # The point of overlaying: the prose underneath keeps its measure instead of
    # reflowing to a ribbon, so closing the dock costs no relayout.
    covered = page.locator("#sceneProseHost").bounding_box()
    assert covered and covered["width"] >= 700

    page.evaluate("() => closeScenePanel()")
    page.wait_for_selector("#discussPanel", state="hidden")
    page.wait_for_function(
        "() => document.documentElement.dataset.utilityOverlay !== 'true'"
    )
    prose = page.locator("#sceneProseHost").bounding_box()
    assert prose and prose["y"] < 400 and prose["width"] > 400
    assert page.evaluate("() => getComputedStyle(document.body).marginRight") == "0px"


def test_switching_theme_does_not_raise(page: Page, server: ProseviewServer):
    """Re-theming the charts must not throw.

    Regression guard: this used to recurse to a stack overflow because the
    theme was written through Chart.js's resolved options proxy rather than the
    raw config. Charts must still repaint, so assert both.
    """
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    open_scene(page, server)
    open_scene_appearance(page)
    for theme in ("dark", "docsify", "hopscotch", "light"):
        page.select_option("#modalThemeSelect", theme)
        _wait_until(lambda t=theme: page.evaluate("document.documentElement.dataset.theme") == t)

    assert not errors, f"switching theme raised: {errors}"

    # Charts live behind the modal, so close it before checking they repainted
    # rather than being torn down by the re-theme.
    page.click("#sceneModal .modal-close")
    page.wait_for_selector("#sceneModal", state="hidden")
    box = page.locator("#sceneTable").bounding_box()
    assert box and box["width"] > 0, "dashboard did not survive the theme switch"


@pytest.mark.parametrize("font", ["reader", "literary", "inter", "georgia", "baskerville", "sans", "mono"])
def test_every_font_choice_survives_a_reload(page: Page, server: ProseviewServer, font: str):
    """All seven fonts, because the boot allow-list once knew only four.

    Inter, Georgia, and Baskerville were accepted by the picker, written to
    localStorage, then silently rejected on load and reset to Reader.
    """
    open_scene(page, server)
    open_scene_appearance(page)
    page.select_option("#modalFontSelect", font)
    _wait_until(lambda: page.evaluate("document.documentElement.dataset.font") == font)

    page.reload(wait_until="load")
    page.wait_for_function("() => !!window._PM")

    assert page.evaluate("document.documentElement.dataset.font") == font
    assert page.locator("#modalFontSelect").input_value() == font


#: Every editorial pass, and the CSS class its marks carry. Mirrors
#: ``PASS_CLASSES`` in ``00-state.js``; a rename there should fail these tests.
PASS_CLASSES = {
    "passive_voice": "hl-passive",
    "filter_verbs": "hl-filter",
    "crutch_words": "hl-crutch",
    "hyperbole": "hl-hyperbole",
    "lyrical": "hl-lyrical",
    "sensory": "hl-sensory",
    "comedy_beats": "hl-comedy",
    "repeats": "hl-repeat",
    "first_person": "hl-first-person",
}


def _scene_with_hits(server: ProseviewServer, pass_name: str) -> str:
    """Pick the scene where *pass_name* fires hardest.

    Chosen from the server's own highlight payload rather than hard-coded, so
    the test follows the fixture instead of silently going vacuous when the
    prose changes.
    """
    highlights = server.get_json("/data.json")["highlightsByPath"]

    def hits(entry: dict) -> int:
        value = entry["highlights"].get(pass_name, 0)
        return len(value) if hasattr(value, "__len__") else int(value or 0)

    best = max(highlights.items(), key=lambda kv: hits(kv[1]))
    assert hits(best[1]) > 0, f"no fixture scene exercises the {pass_name} pass"
    return best[0]


@pytest.mark.parametrize("pass_name", list(PASS_CLASSES))
def test_every_highlight_pass_marks_the_prose(page: Page, shared_server: ProseviewServer, pass_name: str):
    """All nine passes, each on a scene that actually triggers it."""
    css = PASS_CLASSES[pass_name]
    scene = _scene_with_hits(shared_server, pass_name)

    open_scene(page, shared_server, scene)
    open_scene_analysis(page)
    marks = page.locator(f"#sceneProseHost .{css}")
    assert marks.count() == 0, f"{pass_name} marks rendered before the pass was enabled"

    toggle = page.locator(f"#pass-row-{pass_name}")
    toggle.wait_for(state="visible")
    toggle.click()

    _wait_until(
        lambda: toggle.get_attribute("aria-pressed") == "true",
        message=f"{pass_name} row did not activate",
    )
    # The row lighting up is not the feature; the marks are.
    _wait_until(
        lambda: marks.count() > 0,
        message=f"{pass_name} enabled but no .{css} marks rendered in {scene}",
    )


def test_highlight_pass_choice_persists_across_a_reload(page: Page, shared_server: ProseviewServer):
    open_scene(page, shared_server)
    open_scene_analysis(page)
    page.click("#pass-row-repeats")
    _wait_until(lambda: page.locator("#sceneProseHost .hl-repeat").count() > 0)

    page.reload(wait_until="load")
    page.wait_for_selector("#sceneProseHost .ProseMirror")

    open_scene_analysis(page)
    assert page.locator("#pass-row-repeats").get_attribute("aria-pressed") == "true"
    _wait_until(
        lambda: page.locator("#sceneProseHost .hl-repeat").count() > 0,
        message="pass was remembered but its marks were not re-rendered",
    )


def test_clear_all_turns_every_active_pass_off(page: Page, shared_server: ProseviewServer):
    open_scene(page, shared_server)
    open_scene_analysis(page)
    page.click("#pass-row-repeats")
    page.click("#pass-row-sensory")
    _wait_until(lambda: page.locator("#sceneProseHost .hl-repeat").count() > 0)

    # The button reads "All" when everything is off, "Clear" once any pass is on.
    assert page.locator("#scenePassAllBtn").inner_text().strip() == "Clear"
    page.click("#scenePassAllBtn")

    _wait_until(
        lambda: page.locator("#sceneProseHost .hl-repeat").count() == 0
        and page.locator("#sceneProseHost .hl-sensory").count() == 0,
        message="Clear did not remove the rendered marks",
    )


def test_highlight_passes_are_keyboard_toggle_buttons(page: Page, shared_server: ProseviewServer):
    open_scene(page, shared_server, _scene_with_hits(shared_server, "repeats"))
    open_scene_analysis(page)
    toggle = page.locator("#pass-row-repeats")
    assert toggle.get_attribute("aria-pressed") == "false"
    toggle.focus()
    page.keyboard.press("Space")
    _wait_until(lambda: page.locator("#sceneProseHost .hl-repeat").count() > 0)
    assert toggle.get_attribute("aria-pressed") == "true"


def test_repo_tree_previews_a_non_manuscript_file(page: Page, server: ProseviewServer):
    page.goto(f"{server.base_url}#/file/plans/book-plan.md", wait_until="load")
    page.wait_for_selector("#file-preview-panel", state="visible")

    assert "book-plan.md" in page.locator("#filePreviewTitle").inner_text()
    assert page.locator("#filePreviewBody").inner_text().strip()


def test_repo_tree_supports_keyboard_traversal_and_activation(page: Page, server: ProseviewServer):
    open_dashboard(page, server)
    tree = page.get_by_role("tree", name="Repository files")
    first = tree.get_by_role("treeitem").first
    first.focus()
    first_text = first.inner_text()
    page.keyboard.press("ArrowDown")
    assert page.evaluate("document.activeElement.getAttribute('role')") == "treeitem"
    assert page.evaluate("document.activeElement.innerText") != first_text

    file_item = tree.locator(".file-link:visible").first
    file_item.focus()
    expected_path = file_item.get_attribute("data-scene-path") or file_item.get_attribute("data-path")
    page.keyboard.press("Enter")
    page.wait_for_function("() => ['scene', 'file'].includes(document.documentElement.dataset.view)")
    assert expected_path and expected_path in page.evaluate("decodeURIComponent(location.hash)")


def test_file_explorer_creates_an_empty_scene_and_opens_it_for_editing(
    page: Page,
    server: ProseviewServer,
):
    open_dashboard(page, server)
    page.get_by_role("button", name="Create a file or folder").click()
    menu = page.get_by_role("menu")
    assert menu.get_by_role("menuitem").all_inner_texts() == ["New file", "New folder"]
    menu.get_by_role("menuitem", name="New file", exact=True).click()

    dialog = page.get_by_role("dialog", name="New file")
    dialog.get_by_label("Name").fill("03-new-arrival")
    dialog.get_by_label("Location").select_option("manuscript/ch01")
    assert "manuscript/ch01/03-new-arrival.md" in dialog.locator("#sidebarCreatePreview").inner_text()
    dialog.get_by_role("button", name="Create", exact=True).click()

    page.wait_for_function(
        "() => location.hash === '#/scene/ch01%2F03-new-arrival.md' && window._pmEditMode === true"
    )
    assert (server.root / "manuscript/ch01/03-new-arrival.md").read_bytes() == b""
    assert page.locator("#sceneModal").is_visible()


def test_file_explorer_folder_menu_renames_and_trashes_nonempty_folders(
    page: Page,
    server: ProseviewServer,
):
    open_dashboard(page, server)
    tree = page.get_by_role("tree", name="Repository files")
    story_bible = tree.locator('.dir-toggle').filter(has_text=re.compile(r"^story-bible$")).first
    story_bible.click(button="right")
    root_menu = page.locator("#sidebarContextMenu")
    assert root_menu.get_by_role("menuitem").all_inner_texts() == ["New file here", "New folder here"]
    root_menu.get_by_role("menuitem", name="New folder here").click()

    create_folder = page.get_by_role("dialog", name="New folder")
    assert create_folder.get_by_label("Location").input_value() == "story-bible"
    create_folder.get_by_label("Name").fill("Locations")
    create_folder.get_by_role("button", name="Create", exact=True).click()
    page.wait_for_function(
        "() => !!document.querySelector('#sidebarTree .dir-toggle + .sidebar-row-more[aria-label=\"More actions for Locations\"]')"
    )

    locations = tree.locator('.dir-toggle').filter(has_text=re.compile(r"^Locations$")).first
    locations.click(button="right")
    page.locator("#sidebarContextMenu").get_by_role("menuitem", name="New file here").click()
    create_file = page.get_by_role("dialog", name="New file")
    assert create_file.get_by_label("Location").input_value() == "story-bible/Locations"
    create_file.get_by_label("Name").fill("market")
    create_file.get_by_role("button", name="Create", exact=True).click()
    page.wait_for_function("() => location.hash === '#/file/story-bible%2FLocations%2Fmarket.md'")
    assert (server.root / "story-bible/Locations/market.md").read_bytes() == b""

    locations = tree.locator('.dir-toggle').filter(has_text=re.compile(r"^Locations$")).first
    locations.click(button="right")
    page.locator("#sidebarContextMenu").get_by_role("menuitem", name="Rename").click()
    rename = page.get_by_label("New name for Locations")
    rename.fill("Places")
    rename.press("Enter")
    page.wait_for_function(
        "() => !!document.querySelector('#sidebarTree .dir-toggle + .sidebar-row-more[aria-label=\"More actions for Places\"]')"
    )
    assert (server.root / "story-bible/Places/market.md").read_bytes() == b""

    places = tree.locator('.dir-toggle').filter(has_text=re.compile(r"^Places$")).first
    places.click(button="right")
    page.locator("#sidebarContextMenu").get_by_role("menuitem", name="Delete…").click()
    delete_dialog = page.get_by_role("dialog", name="Delete folder?")
    assert "everything inside" in delete_dialog.inner_text()
    assert ".proseview/trash" in delete_dialog.inner_text()
    delete_dialog.get_by_role("button", name="Delete", exact=True).click()
    page.wait_for_function(
        "() => ![...document.querySelectorAll('#sidebarTree .dir-toggle')].some(el => el.textContent === 'Places')"
    )
    assert not (server.root / "story-bible/Places").exists()
    trashed = list((server.root / ".proseview/trash").rglob("Places/market.md"))
    assert len(trashed) == 1 and trashed[0].read_bytes() == b""


def test_file_creation_never_discards_a_dirty_scene_without_confirmation(
    page: Page,
    server: ProseviewServer,
):
    open_scene(page, server)
    enter_edit_mode(page)
    append_to_paragraph(page, "The loft smelled of cold coffee", " Unsaved sidebar test.")

    page.get_by_role("button", name="Create a file or folder").click()
    page.get_by_role("menuitem", name="New folder", exact=True).click()

    unsaved = page.get_by_role("dialog", name="Unsaved changes")
    unsaved.wait_for(state="visible")
    unsaved.get_by_role("button", name="Cancel").click()
    assert page.get_by_role("dialog", name="New folder").is_hidden()
    assert page.evaluate("window._pmEditMode && window._pmDirty")
    assert not (server.root / "story-bible/Should not exist").exists()


def test_repo_tree_auto_reveal_keeps_expansion_semantics_in_sync(
    page: Page,
    server: ProseviewServer,
):
    open_scene(page, server)
    tree = page.get_by_role("tree", name="Repository files")
    tree.wait_for(state="visible")
    expanded = tree.locator("li.expanded > .dir-toggle")
    assert expanded.count() > 0
    assert all(value == "true" for value in expanded.evaluate_all(
        "items => items.map(item => item.getAttribute('aria-expanded'))"
    ))


def test_file_explorer_uses_roomy_targets_and_coherent_vector_icons(
    page: Page,
    server: ProseviewServer,
):
    """The file tree belongs beside the manuscript, not in a 20px code gutter."""
    page.set_viewport_size({"width": 1400, "height": 1000})
    open_dashboard(page, server)
    tree = page.get_by_role("tree", name="Repository files")
    tree.locator(".dir-toggle:visible").filter(has_text=re.compile(r"^ch01$")).click()
    tree.locator(f'.file-link[data-scene-path="{SCENE_REL}"]').click()
    page.wait_for_selector("#sceneModal", state="visible")
    page.wait_for_selector("#sidebarTree .file-link.active", state="visible")

    measurements = page.evaluate(
        """() => {
            const rows = [...document.querySelectorAll('#sidebarTree [role="treeitem"]')]
                .filter(el => el.getClientRects().length);
            const nodeIcons = [...document.querySelectorAll('#sidebarTree .sidebar-node-icon')]
                .filter(el => el.getClientRects().length);
            const firstStyle = getComputedStyle(rows[0]);
            const active = document.querySelector('#sidebarTree .file-link.active');
            const activeStyle = getComputedStyle(active);
            const activeMarker = getComputedStyle(active, '::before');
            const close = document.querySelector('.sidebar-close-btn').getBoundingClientRect();
            const title = getComputedStyle(document.querySelector('.sidebar-title'));
            return {
                rowHeights: rows.map(el => el.getBoundingClientRect().height),
                fontFamily: firstStyle.fontFamily,
                fontSize: parseFloat(firstStyle.fontSize),
                iconCount: nodeIcons.length,
                iconSizes: nodeIcons.map(el => {
                    const box = el.getBoundingClientRect();
                    return [box.width, box.height];
                }),
                iconsDecorative: nodeIcons.every(el => el.getAttribute('aria-hidden') === 'true'),
                close: [close.width, close.height],
                titleFont: title.fontFamily,
                titleSize: parseFloat(title.fontSize),
                activeBackground: activeStyle.backgroundColor,
                activeWeight: parseInt(activeStyle.fontWeight, 10),
                activeMarkerWidth: parseFloat(activeMarker.width),
                activeMarkerContent: activeMarker.content,
                horizontalOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
            };
        }"""
    )

    assert min(measurements["rowHeights"]) >= 32
    assert measurements["fontSize"] >= 13
    assert "mono" not in measurements["fontFamily"].lower()
    assert measurements["iconCount"] >= len(measurements["rowHeights"])
    assert all(width >= 18 and height >= 18 for width, height in measurements["iconSizes"]), measurements["iconSizes"]
    assert measurements["iconsDecorative"]
    assert min(measurements["close"]) >= 34
    assert measurements["titleSize"] >= 16
    assert any(name in measurements["titleFont"] for name in ("Iowan", "Palatino", "Georgia"))
    assert measurements["activeBackground"] not in ("rgba(0, 0, 0, 0)", "transparent")
    assert measurements["activeWeight"] >= 600
    assert measurements["activeMarkerWidth"] >= 3
    assert measurements["activeMarkerContent"] != "none"
    assert not measurements["horizontalOverflow"]

    assert page.locator(".sidebar-close-btn .sidebar-chrome-icon").count() == 1
    assert page.locator("#sidebarOpenBtn .sidebar-chrome-icon").count() == 1
    assert page.locator("#modalSidebarBtn .sidebar-chrome-icon").count() == 1

    page.evaluate("selectTheme('dark')")
    assert page.locator("#sidebarTree .file-link.active").is_visible()
    assert not page.evaluate(
        "document.documentElement.scrollWidth > document.documentElement.clientWidth"
    )


# ── editor round-trip fidelity ──────────────────────────────────────────────


def test_cmd_s_saves_without_exiting_edit_mode(page: Page, server: ProseviewServer):
    path = server.scene_path()
    before = path.read_text(encoding="utf-8")

    open_scene(page, server)
    enter_edit_mode(page)
    append_to_paragraph(page, "The loft smelled of cold coffee", " The kettle ticked.")
    save_scene(page)

    _wait_until(lambda: "The kettle ticked." in path.read_text(encoding="utf-8"),
                message="Mod-S did not reach the file")
                
    # Check that it did NOT drop out of edit mode
    assert page.evaluate("window._pmEditMode") is True, "Cmd+S exited edit mode"
    page.wait_for_selector(".scene-edit-bar.is-saved")


def test_typing_saves_the_edit_and_preserves_everything_else(page: Page, server: ProseviewServer):
    path = server.scene_path()
    before = path.read_text(encoding="utf-8")

    open_scene(page, server)
    enter_edit_mode(page)
    append_to_paragraph(page, "The loft smelled of cold coffee", " The kettle ticked.")
    save_scene(page)

    _wait_until(lambda: "The kettle ticked." in path.read_text(encoding="utf-8"),
                message="Mod-S did not reach the file")
    after = path.read_text(encoding="utf-8")

    # Frontmatter is rebuilt server-side from the live file; a serializer that
    # round-trips the whole document could silently reformat it.
    assert frontmatter(after) == frontmatter(before)
    # Every paragraph the user did not touch must come back byte-identical.
    untouched = [p for p in paragraphs(before) if "The loft smelled of cold coffee" not in p]
    after_paras = paragraphs(after)
    for para in untouched:
        assert para in after_paras, f"paragraph was rewritten by the save:\n{para}"


def test_saving_preserves_inline_todo_and_note_annotations(page: Page, server: ProseviewServer):
    path = server.scene_path(ANNOTATED_SCENE_REL)
    before = path.read_text(encoding="utf-8")

    open_scene(page, server, ANNOTATED_SCENE_REL)
    enter_edit_mode(page)
    append_to_paragraph(page, "Patel arrived with the ledger", " He did not sit.")
    save_scene(page)

    _wait_until(lambda: "He did not sit." in path.read_text(encoding="utf-8"))
    after = path.read_text(encoding="utf-8")

    # Annotations are atom nodes in ProseMirror. If the round trip degraded them
    # they would come back as escaped text or vanish entirely.
    assert "<!-- TODO: Tighten this opening beat -->" in after
    assert "<!-- NOTE[continuity]: Patel should not know about the safe yet -->" in after
    assert after.count("<!--") == before.count("<!--")
    assert "&lt;!--" not in after, "annotation was HTML-escaped on save"
    assert frontmatter(after) == frontmatter(before)


def test_emphasis_survives_a_save_without_reformatting(page: Page, server: ProseviewServer):
    path = server.scene_path(ANNOTATED_SCENE_REL)

    open_scene(page, server, ANNOTATED_SCENE_REL)
    enter_edit_mode(page)
    append_to_paragraph(page, "Patel arrived with the ledger", " Noted.")
    save_scene(page)

    _wait_until(lambda: "Noted." in path.read_text(encoding="utf-8"))
    assert "*quiet*" in path.read_text(encoding="utf-8"), "emphasis marker was rewritten"


def test_undo_before_saving_leaves_the_file_untouched(page: Page, server: ProseviewServer):
    path = server.scene_path()
    before = path.read_text(encoding="utf-8")

    open_scene(page, server)
    enter_edit_mode(page)
    append_to_paragraph(page, "The loft smelled of cold coffee", " Typed then regretted.")
    page.keyboard.press("ControlOrMeta+z")
    page.wait_for_function(
        "() => !document.querySelector('#sceneProseHost .ProseMirror').textContent"
        ".includes('Typed then regretted')"
    )
    save_scene(page)
    page.wait_for_timeout(1000)

    assert path.read_text(encoding="utf-8") == before


def test_editing_a_large_scene_leaves_untouched_prose_intact(page: Page, server: ProseviewServer):
    """~10k words through the editor.

    A serializer that reflows or normalises on save would show up here as
    hundreds of rewritten paragraphs rather than the one that was edited.
    """
    path = server.scene_path(LARGE_SCENE_REL)
    before = path.read_text(encoding="utf-8")
    before_paras = paragraphs(before)

    open_scene(page, server, LARGE_SCENE_REL)
    enter_edit_mode(page)
    append_to_paragraph(page, before_paras[1][:40], " EDITED-MARKER.")
    save_scene(page)

    _wait_until(lambda: "EDITED-MARKER." in path.read_text(encoding="utf-8"), timeout=20,
                message="large-scene save never landed")
    after_paras = paragraphs(path.read_text(encoding="utf-8"))

    assert len(after_paras) == len(before_paras)
    rewritten = [
        p for p in before_paras
        if p not in after_paras and "EDITED-MARKER." not in p and p != before_paras[1]
    ]
    assert not rewritten, f"{len(rewritten)} untouched paragraphs were rewritten on save"


def test_conflicting_save_is_refused_in_the_browser(page: Page, server: ProseviewServer):
    """Someone edits the file in vim while the editor is open."""
    path = server.scene_path()
    open_scene(page, server)
    enter_edit_mode(page)

    path.write_text(path.read_text(encoding="utf-8") + "\nChanged underneath.\n", encoding="utf-8")
    on_disk = path.read_text(encoding="utf-8")

    append_to_paragraph(page, "The loft smelled of cold coffee", " Browser wins?")
    save_scene(page)
    page.wait_for_timeout(1500)

    assert path.read_text(encoding="utf-8") == on_disk
    assert "Browser wins?" not in on_disk


def test_conflict_recovery_preserves_draft_and_offers_explicit_disk_reload(
    page: Page,
    server: ProseviewServer,
):
    path = server.scene_path()
    open_scene(page, server)
    enter_edit_mode(page)
    path.write_text(path.read_text(encoding="utf-8") + "\nExternal version.\n", encoding="utf-8")
    append_to_paragraph(page, "The loft smelled of cold coffee", " Browser draft.")
    save_scene(page)

    dialog = page.get_by_role("alertdialog", name="Scene changed on disk")
    dialog.wait_for(state="visible")
    assert "Browser draft." in _editor_text(page)
    dialog.get_by_role("button", name="Keep editing").click()
    assert "Browser draft." in _editor_text(page)
    assert page.get_by_role("button", name="Resolve save conflict").is_visible()

    page.get_by_role("button", name="Resolve save conflict").click()
    dialog.get_by_role("button", name="Reload disk version").click()
    page.wait_for_function(
        "() => document.querySelector('#sceneProseHost .ProseMirror').innerText.includes('External version.')"
    )
    assert "Browser draft." not in _editor_text(page)
    assert path.read_text(encoding="utf-8").endswith("External version.\n")


def test_conflict_recovery_copies_the_latest_draft_and_guards_scene_exit(
    page: Page,
    server: ProseviewServer,
):
    path = server.scene_path()
    open_scene(page, server)
    enter_edit_mode(page)
    path.write_text(path.read_text(encoding="utf-8") + "\nExternal version.\n", encoding="utf-8")
    append_to_paragraph(page, "The loft smelled of cold coffee", " First browser draft.")
    save_scene(page)

    conflict = page.get_by_role("alertdialog", name="Scene changed on disk")
    conflict.get_by_role("button", name="Keep editing").click()
    append_to_paragraph(page, "The loft smelled of cold coffee", " Latest browser draft.")

    page.evaluate(
        """() => Object.defineProperty(navigator, 'clipboard', {
            configurable: true,
            value: {writeText: text => { window.__copiedConflictDraft = text; return Promise.resolve(); }}
        })"""
    )
    page.get_by_role("button", name="Resolve save conflict").click()
    conflict.get_by_role("button", name="Copy draft").click()
    page.wait_for_function("() => !!window.__copiedConflictDraft")
    assert "Latest browser draft." in page.evaluate("window.__copiedConflictDraft")
    conflict.get_by_role("button", name="Keep editing").click()

    original_hash = page.evaluate("location.hash")
    page.locator("#sceneModal .nav-btn").nth(1).click()
    page.get_by_role("dialog", name="Unsaved changes").wait_for(state="visible")
    assert page.evaluate("location.hash") == original_hash
    assert "Latest browser draft." in _editor_text(page)

    page.get_by_role("dialog", name="Unsaved changes").get_by_role(
        "button", name="Cancel", exact=True
    ).click()
    page.get_by_role("button", name="Close scene and return to dashboard").first.click()
    page.get_by_role("dialog", name="Unsaved changes").wait_for(state="visible")
    assert page.evaluate("location.hash") == original_hash
    assert "Latest browser draft." in _editor_text(page)


def test_dirty_scene_guards_history_file_routes_and_beforeunload(
    page: Page,
    server: ProseviewServer,
):
    open_dashboard(page, server)
    open_scene(page, server)
    enter_edit_mode(page)
    append_to_paragraph(page, "The loft smelled of cold coffee", " Guarded browser draft.")
    original_hash = page.evaluate("location.hash")

    page.go_back(wait_until="commit")
    dialog = page.get_by_role("dialog", name="Unsaved changes")
    dialog.wait_for(state="visible")
    assert page.evaluate("location.hash") == original_hash
    assert "Guarded browser draft." in _editor_text(page)
    dialog.get_by_role("button", name="Cancel").click()

    file_item = page.get_by_role("tree", name="Repository files").locator(
        ".file-link[data-path]:not([data-scene-path]):visible"
    ).first
    file_item.click()
    dialog.wait_for(state="visible")
    assert page.evaluate("document.documentElement.dataset.view") == "scene"
    assert "Guarded browser draft." in _editor_text(page)
    dialog.get_by_role("button", name="Cancel").click()

    unload = page.evaluate(
        """() => {
            const event = new Event('beforeunload', {cancelable: true});
            window.dispatchEvent(event);
            return {prevented: event.defaultPrevented, returnValue: event.returnValue};
        }"""
    )
    assert unload["prevented"] or unload["returnValue"] is False

    file_item.click()
    dialog.wait_for(state="visible")
    dialog.get_by_role("button", name="Discard").click()
    page.wait_for_function("() => document.documentElement.dataset.view === 'file'")
    assert page.evaluate("location.hash").startswith("#/file/")

    open_scene(page, server)
    enter_edit_mode(page)
    append_to_paragraph(page, "The loft smelled of cold coffee", " Saved transition draft.")
    file_item.click()
    dialog.wait_for(state="visible")
    dialog.get_by_role("button", name="Save").click()
    page.wait_for_function("() => document.documentElement.dataset.view === 'file'")
    assert "Saved transition draft." in server.scene_path().read_text()


def test_unsaved_dialog_enter_activates_the_focused_action(
    page: Page,
    server: ProseviewServer,
):
    before = server.scene_path().read_text()
    open_scene(page, server)
    enter_edit_mode(page)
    append_to_paragraph(page, "The loft smelled of cold coffee", " Discard-only draft.")
    page.locator("#sceneModal .nav-btn").nth(1).click()
    dialog = page.get_by_role("dialog", name="Unsaved changes")
    discard = dialog.get_by_role("button", name="Discard")
    discard.focus()
    page.keyboard.press("Enter")

    page.wait_for_function("() => !document.querySelector('.unsaved-dialog')")
    assert server.scene_path().read_text() == before
    assert "Discard-only draft." not in _editor_text(page)


def test_typing_during_a_delayed_save_remains_visible_and_dirty(
    page: Page,
    server: ProseviewServer,
):
    open_scene(page, server)
    enter_edit_mode(page)
    append_to_paragraph(page, "The loft smelled of cold coffee", " Saved snapshot.")
    page.evaluate(
        """() => {
            const realFetch = window.fetch.bind(window);
            let intercepted = false;
            window.fetch = function(input, init) {
                const url = typeof input === 'string' ? input : input.url;
                if (!intercepted && url === '/save-scene') {
                    intercepted = true;
                    window.__saveStarted = true;
                    return new Promise(resolve => {
                        window.__releaseSave = () => realFetch(input, init).then(resolve);
                    });
                }
                return realFetch(input, init);
            };
        }"""
    )
    page.locator(".scene-edit-save").click()
    page.wait_for_function("() => window.__saveStarted === true")
    append_to_paragraph(page, "Saved snapshot.", " Typed during save.")
    assert page.locator(".scene-edit-cancel").is_disabled()
    assert page.evaluate("cancelSceneEdit()") is False
    assert page.evaluate("window._pmEditMode && window._pmDirty")
    assert "Typed during save." in _editor_text(page)
    page.evaluate("window.__releaseSave()")
    # The status readout became an icon whose tooltip carries the state, so
    # there is no #sceneEditState element to read text out of any more.
    page.wait_for_function(
        "() => (document.querySelector('.scene-edit-status')?.title || '').includes('unsaved')"
    )

    assert "Typed during save." in _editor_text(page)
    assert "Typed during save." not in server.scene_path().read_text()
    assert page.locator(".scene-edit-save").is_enabled()

    page.locator(".scene-edit-save").click()
    page.wait_for_function("() => !window._pmEditMode")
    assert "Typed during save." in server.scene_path().read_text()


def test_guarded_browser_back_preserves_history_direction(
    page: Page,
    server: ProseviewServer,
):
    open_dashboard(page, server)
    page.locator('.tab-nav button[data-tab="analysis"]').click()
    page.locator('.tab-nav button[data-tab="overview"]').click()
    page.locator(f'.scene-table-link[data-scene-path="{SCENE_REL}"]').click()
    page.wait_for_function("() => document.documentElement.dataset.view === 'scene'")
    enter_edit_mode(page)
    append_to_paragraph(page, "The loft smelled of cold coffee", " Discarded history draft.")

    page.go_back(wait_until="commit")
    dialog = page.get_by_role("dialog", name="Unsaved changes")
    dialog.get_by_role("button", name="Discard").click()
    page.wait_for_function(
        "() => !document.documentElement.dataset.view"
        " && location.hash === '#/tab/overview'"
    )

    page.go_back(wait_until="commit")
    page.wait_for_function("() => location.hash === '#/tab/analysis'")
    assert page.locator("#tab-analysis").get_attribute("class").endswith("active")
    assert page.evaluate("document.documentElement.dataset.view") is None

    page.go_forward(wait_until="commit")
    page.wait_for_function("() => location.hash === '#/tab/overview'")
    page.go_forward(wait_until="commit")
    page.wait_for_function("() => document.documentElement.dataset.view === 'scene'")
    assert "Discarded history draft." not in _editor_text(page)


def test_dirty_related_document_navigation_keeps_its_exact_destination(
    page: Page,
    server: ProseviewServer,
):
    open_scene(page, server)
    target = page.evaluate("Object.keys(repoFileByPath)[0]")
    assert target
    enter_edit_mode(page)
    append_to_paragraph(page, "The loft smelled of cold coffee", " Related-route draft.")
    page.evaluate("path => openRelatedDoc(path)", target)
    dialog = page.get_by_role("dialog", name="Unsaved changes")
    dialog.get_by_role("button", name="Discard").click()
    page.wait_for_function("() => document.documentElement.dataset.view === 'file'")
    assert page.locator("#filePreviewTitle").inner_text() == target
    page.go_back()
    page.wait_for_function("() => document.documentElement.dataset.view === 'scene'")
    assert page.evaluate("decodeURIComponent(location.hash)").startswith("#/scene/")


def test_unsaved_dialog_traps_keyboard_focus(page: Page, server: ProseviewServer):
    open_scene(page, server)
    enter_edit_mode(page)
    append_to_paragraph(page, "The loft smelled of cold coffee", " Unsaved focus draft.")
    page.locator("#sceneModal .nav-btn").nth(1).click()
    dialog = page.get_by_role("dialog", name="Unsaved changes")
    dialog.wait_for(state="visible")
    assert dialog.locator(":focus").count() == 1

    for _ in range(5):
        page.keyboard.press("Tab")
        assert page.evaluate(
            "document.querySelector('.unsaved-dialog').contains(document.activeElement)"
        )


def test_dashboard_appearance_listboxes_are_keyboard_operable_and_persist(
    page: Page,
    server: ProseviewServer,
):
    open_dashboard(page, server)
    font = page.get_by_role("button", name="Pick reading font")
    font.focus()
    page.keyboard.press("Enter")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    assert page.evaluate("document.documentElement.dataset.font") == "literary"
    assert page.evaluate("document.activeElement.id") == "fontToggle"

    font.focus()
    page.keyboard.press("Enter")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Tab")
    assert page.evaluate("localStorage.getItem('proseview-font')") == "inter"
    page.reload(wait_until="load")
    assert page.evaluate("document.documentElement.dataset.font") == "inter"

    theme = page.locator("#themeToggle")
    theme.focus()
    page.keyboard.press("Enter")
    page.keyboard.press("End")
    page.keyboard.press("Enter")
    assert page.evaluate("document.documentElement.dataset.theme") == "graphite-dark"
    assert page.evaluate("document.activeElement.id") == "themeToggle"

    page.reload(wait_until="load")
    assert page.evaluate("document.documentElement.dataset.font") == "inter"
    assert page.evaluate("document.documentElement.dataset.theme") == "graphite-dark"

    font.focus()
    page.keyboard.press("Enter")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Escape")
    assert page.evaluate("document.documentElement.dataset.font") == "inter"
    assert page.evaluate("document.activeElement.id") == "fontToggle"


def test_scene_analysis_and_character_controls_are_keyboard_operable(
    page: Page,
    server: ProseviewServer,
):
    open_dashboard(page, server)
    scene_link = page.locator(".scene-table-link").first
    scene_link.focus()
    page.keyboard.press("Enter")
    page.wait_for_function("() => document.documentElement.dataset.view === 'scene'")

    open_scene_details(page)
    context = page.locator("#sceneDetailsPane")
    character = context.locator(".sc-char-tag").first
    assert character.count() == 1
    character.focus()
    page.keyboard.press("Enter")
    back = page.get_by_role("button", name="Back to scene")
    back.wait_for(state="visible")
    back.focus()
    page.keyboard.press("Enter")
    page.locator("#sceneProseHost").wait_for(state="visible")

    page.get_by_role("button", name="Close scene and return to dashboard").first.click()
    page.get_by_role("button", name="Analysis").click()
    page.wait_for_selector("#analysisSceneTable tbody tr")
    scene_link = page.locator("#analysisSceneTable .scene-table-link").first
    scene_link.focus()
    page.keyboard.press("Enter")
    page.wait_for_function("() => document.documentElement.dataset.view === 'scene'")


def test_workspace_resizers_are_keyboard_operable_and_preserve_writing_space(
    page: Page,
    server: ProseviewServer,
):
    page.set_viewport_size({"width": 1400, "height": 1000})
    open_scene(page, server)

    sidebar = page.get_by_role("separator", name="Resize file browser")
    before_sidebar = page.locator("#repoSidebar").bounding_box()["width"]
    sidebar.focus()
    page.keyboard.press("ArrowRight")
    assert page.locator("#repoSidebar").bounding_box()["width"] > before_sidebar

    open_discuss(page)
    discuss = page.get_by_role("separator", name="Resize Discuss")
    discuss.focus()
    page.keyboard.press("End")
    assert page.locator("#sceneModal .modal-content").bounding_box()["width"] >= 420
    handle_box = discuss.bounding_box()
    assert handle_box and handle_box["width"] >= 24
    page.mouse.move(handle_box["x"] + handle_box["width"] / 2, 300)
    page.mouse.down()
    page.mouse.move(0, 300)
    page.mouse.up()
    assert page.locator("#sceneModal .modal-content").bounding_box()["width"] >= 420

    page.evaluate("closeDiscuss(); _termDock = 'bottom'; document.getElementById('terminalPanel').hidden = false; _applyTerminalDock()")
    terminal = page.get_by_role("separator", name="Resize Terminal")
    before_height = page.locator("#terminalPanel").bounding_box()["height"]
    terminal.focus()
    page.keyboard.press("ArrowUp")
    assert page.locator("#terminalPanel").bounding_box()["height"] > before_height
    page.evaluate("toggleTerminalDock()")
    assert terminal.get_attribute("aria-orientation") == "vertical"
    terminal.focus()
    page.keyboard.press("End")
    assert page.locator("#sceneModal .modal-content").bounding_box()["width"] >= 420


def test_resizer_values_match_rendered_bounds_at_compact_and_zoom(
    page: Page,
    server: ProseviewServer,
):
    page.set_viewport_size({"width": 800, "height": 768})
    open_scene(page, server)
    sidebar = page.get_by_role("separator", name="Resize file browser")
    sidebar.focus()
    page.keyboard.press("End")
    sidebar_width = page.locator("#repoSidebar").bounding_box()["width"]
    assert abs(float(sidebar.get_attribute("aria-valuenow")) - sidebar_width) <= 1
    assert page.locator("#sceneModal .modal-content").bounding_box()["width"] >= 360

    page.set_viewport_size({"width": 1024, "height": 768})
    open_discuss(page)
    discuss = page.get_by_role("separator", name="Resize Discuss")
    discuss.focus()
    page.keyboard.press("End")
    discuss_width = page.locator("#discussPanel").bounding_box()["width"]
    assert abs(float(discuss.get_attribute("aria-valuenow")) - discuss_width) <= 1

    page.evaluate("document.body.style.zoom = '2'; syncCssZoomViewport()")
    page.wait_for_function("() => document.documentElement.dataset.utilityOverlay === 'true'")
    assert discuss.is_hidden()
    zoom_width = page.locator("#discussPanel").bounding_box()["width"]
    assert zoom_width >= 1020
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")

    page.evaluate("closeDiscuss(); _termDock = 'bottom'; document.getElementById('terminalPanel').hidden = false; _applyTerminalDock()")
    terminal = page.get_by_role("separator", name="Resize Terminal")
    terminal.focus()
    page.keyboard.press("End")
    terminal_height = page.locator("#terminalPanel").bounding_box()["height"]
    assert abs(float(terminal.get_attribute("aria-valuenow")) - terminal_height) <= 1
    handle = terminal.bounding_box()
    assert handle and handle["y"] >= 240

    page.evaluate("toggleTerminalDock()")
    page.wait_for_function("() => document.getElementById('terminalPanel').classList.contains('dock-right')")
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    assert terminal.is_hidden()
    panel = page.locator("#terminalPanel").bounding_box()
    assert panel and panel["x"] <= 1 and panel["width"] >= 1020


def test_compact_utility_docks_remove_retracted_sidebar_from_keyboard_order(
    page: Page,
    server: ProseviewServer,
):
    page.set_viewport_size({"width": 1024, "height": 768})
    open_scene(page, server)
    open_discuss(page)
    sidebar = page.locator("#repoSidebar")
    assert sidebar.get_attribute("inert") is not None
    # `inert` already removes the subtree from the accessibility tree, so a
    # second `aria-hidden="true"` is redundant -- and screen readers treat the
    # pair inconsistently. Assert it is *absent*, not present.
    assert sidebar.get_attribute("aria-hidden") is None

    page.locator("#discussSend").focus()
    for _ in range(20):
        page.keyboard.press("Tab")
        focused = page.evaluate(
            """() => {
                const el = document.activeElement;
                const box = el && el.getBoundingClientRect();
                return {
                    inSidebar: !!(el && document.getElementById('repoSidebar').contains(el)),
                    visible: !!(box && box.width > 0 && box.height > 0
                        && box.right > 0 && box.bottom > 0
                        && box.left < innerWidth && box.top < innerHeight),
                };
            }"""
        )
        assert not focused["inSidebar"]
        assert focused["visible"]

    page.evaluate("closeDiscuss()")
    page.wait_for_function("() => !document.getElementById('repoSidebar').inert")


def test_wide_scene_defaults_to_manuscript_first(page: Page, server: ProseviewServer):
    page.set_viewport_size({"width": 1400, "height": 1000})
    open_scene(page, server)
    prose = page.locator("#sceneProseHost").bounding_box()
    assert prose and prose["y"] < 300
    assert prose["width"] <= 780


# ── repo-wide search ────────────────────────────────────────────────────────
#
# `_runSearch` scans five categories -- FILES, SCENES, PROSE, TODOS, NOTES --
# with a two-character minimum and a 30-result cap. Results render into
# #searchResults, which stays `hidden` until a query qualifies.


def search_for(page: Page, query: str) -> None:
    box = page.locator("#searchBox")
    if box.is_hidden():
        page.get_by_role("button", name="Search files").first.click()
    box.fill(query)


def search_rows(page: Page):
    return page.locator("#searchResults .search-row")


def search_groups(page: Page) -> list[str]:
    """Displayed group labels, upper-cased.

    The headings read "Files" / "Scenes" / "In prose" / "TODOs" / "Notes" and
    are upper-cased by CSS, so normalise rather than depend on which form a
    given text-extraction path returns.
    """
    labels = page.locator("#searchResults .search-group-label").all_inner_texts()
    return [t.strip().upper() for t in labels]


def test_dashboard_search_is_large_and_inline(page: Page, shared_server: ProseviewServer):
    open_dashboard(page, shared_server)

    box = page.locator("#searchBox")
    palette = page.locator("#searchPalette")
    assert box.is_visible()
    assert palette.is_hidden()
    bounds = box.bounding_box()
    assert bounds and bounds["width"] >= 300

    box.fill("Rena")
    page.wait_for_selector("#searchResults .search-row")
    assert palette.is_hidden(), "dashboard search should not open a second modal"


def test_scene_has_an_explicit_return_to_dashboard(page: Page, shared_server: ProseviewServer):
    open_scene(page, shared_server)

    back = page.get_by_role("button", name="Close scene and return to dashboard").first
    assert back.is_visible()
    assert "Dashboard" in back.inner_text()
    back.click()

    page.wait_for_selector("#sceneModal", state="hidden")
    assert page.evaluate("document.documentElement.dataset.view || ''") == ""
    assert page.locator("#tab-overview").is_visible()


def test_scene_search_modal_has_pointer_close(page: Page, shared_server: ProseviewServer):
    open_scene(page, shared_server)
    page.locator("#sceneModal button[aria-label='Search files']").click()

    palette = page.locator("#searchPalette")
    palette.wait_for(state="visible")
    close = page.get_by_role("button", name="Close search")
    assert close.is_visible()
    close.click()

    palette.wait_for(state="hidden")
    assert page.locator("#sceneModal").is_visible()


def test_search_needs_two_characters_before_it_offers_anything(page: Page, shared_server: ProseviewServer):
    open_dashboard(page, shared_server)
    panel = page.locator("#searchResults")

    search_for(page, "R")
    page.wait_for_timeout(400)
    assert panel.is_hidden(), "a single character should not open the results panel"

    search_for(page, "Rena")
    panel.wait_for(state="visible")
    assert search_rows(page).count() > 0


def test_search_finds_a_scene_by_path_and_opens_it(page: Page, shared_server: ProseviewServer):
    open_dashboard(page, shared_server)
    search_for(page, "01-opening")

    page.wait_for_selector("#searchResults .search-row")
    assert "FILES" in search_groups(page)
    assert SCENE_REL in page.locator("#searchResults").inner_text()

    page.keyboard.press("Enter")
    page.wait_for_selector("#sceneModal", state="visible")
    assert SCENE_REL in page.locator("#modalTitle").inner_text()


def test_search_finds_prose_and_jumps_into_the_scene(page: Page, shared_server: ProseviewServer):
    """A phrase that appears only in the prose, not in any path or annotation."""
    open_dashboard(page, shared_server)
    search_for(page, "slow algebra")

    page.wait_for_selector("#searchResults .search-row")
    assert "IN PROSE" in search_groups(page)

    page.keyboard.press("Enter")
    page.wait_for_selector("#sceneModal", state="visible")
    _wait_until(lambda: "slow algebra" in _editor_text(page),
                message="activating a prose hit did not open the right scene")


def test_search_finds_todos(page: Page, shared_server: ProseviewServer):
    open_dashboard(page, shared_server)
    search_for(page, "Tighten this opening")

    page.wait_for_selector("#searchResults .search-row")
    assert "TODOS" in search_groups(page)
    assert ANNOTATED_SCENE_REL in page.locator("#searchResults").inner_text()


def test_search_finds_tagged_notes(page: Page, shared_server: ProseviewServer):
    open_dashboard(page, shared_server)
    search_for(page, "should not know about the safe")

    page.wait_for_selector("#searchResults .search-row")
    assert "NOTES" in search_groups(page)
    # The tag rides along in the result row, so a writer can tell why it matched.
    assert "[continuity]" in page.locator("#searchResults").inner_text()


def test_search_finds_non_scene_repo_files(page: Page, shared_server: ProseviewServer):
    open_dashboard(page, shared_server)
    search_for(page, "book-plan")

    page.wait_for_selector("#searchResults .search-row")
    assert "plans/book-plan.md" in page.locator("#searchResults").inner_text()

    page.keyboard.press("Enter")
    page.wait_for_selector("#file-preview-panel", state="visible")
    assert "book-plan.md" in page.locator("#filePreviewTitle").inner_text()


def test_search_opens_manuscript_files_outside_the_scene_index(
    page: Page,
    shared_server: ProseviewServer,
):
    """Manuscript Markdown nested below a chapter dir is not a scene.

    It used to be flagged as one, which routed the click to a scene the
    client had no entry for and left the result dead.
    """
    open_dashboard(page, shared_server)
    search_for(page, "reader-pass-notes")

    page.wait_for_selector("#searchResults .search-row")
    assert NESTED_MANUSCRIPT_NOTE in page.locator("#searchResults").inner_text()

    page.keyboard.press("Enter")
    page.wait_for_selector("#file-preview-panel", state="visible")
    page.wait_for_function(
        "path => document.getElementById('filePreviewTitle').innerText === path",
        arg=NESTED_MANUSCRIPT_NOTE,
    )
    # The title is set before the body is fetched, so wait on the body itself.
    page.wait_for_function(
        "() => document.getElementById('filePreviewBody')"
        ".innerText.includes('safe reveal lands too early')"
    )


def test_opening_a_file_reveals_and_highlights_it_in_the_sidebar(
    page: Page,
    shared_server: ProseviewServer,
):
    """Opening from search points the sidebar at the file, VS Code style:
    ancestor folders expand, the row is highlighted and scrolled into view."""
    open_dashboard(page, shared_server)
    # Collapse everything first, so an expanded ancestor proves the reveal.
    page.evaluate(
        "() => document.querySelectorAll('#sidebarTree li')"
        ".forEach(li => li.classList.remove('expanded'))"
    )

    search_for(page, "reader-pass-notes")
    page.wait_for_selector("#searchResults .search-row")
    page.keyboard.press("Enter")
    page.wait_for_selector("#file-preview-panel", state="visible")

    active = page.locator("#sidebarTree .file-link.active")
    active.wait_for(state="visible")
    assert active.get_attribute("data-path") == NESTED_MANUSCRIPT_NOTE
    # Every folder on the way down is open, so the row is actually on screen.
    expanded = page.evaluate(
        "() => [...document.querySelectorAll('#sidebarTree li.expanded > .dir-toggle')]"
        ".map(el => el.textContent)"
    )
    assert {"manuscript", "ch01", "review"} <= set(expanded)

    # A scene reveals the same way, matched on its scene path. Typing and
    # pressing Enter without pause also lands inside the search debounce, so
    # this doubles as a guard that activation uses the query on screen rather
    # than the previous keystroke's results.
    search_for(page, "02-walk")
    page.wait_for_selector("#searchResults .search-row")
    page.keyboard.press("Enter")
    page.wait_for_selector("#sceneModal", state="visible")
    scene_active = page.locator("#sidebarTree .file-link.active")
    scene_active.wait_for(state="visible")
    assert scene_active.get_attribute("data-path") == "manuscript/ch01/02-walk.md"



@pytest.mark.parametrize("start_view", ["scene", "file"])
def test_global_search_opens_unconfigured_file_from_every_reader_view(
    page: Page,
    shared_server: ProseviewServer,
    start_view: str,
):
    if start_view == "scene":
        open_scene(page, shared_server)
    else:
        page.goto(f"{shared_server.base_url}#/file/plans/book-plan.md", wait_until="load")
        page.wait_for_selector("#file-preview-panel", state="visible")

    page.keyboard.press("ControlOrMeta+k")

    palette = page.locator("#searchPalette")
    box = page.locator("#searchBox")
    palette.wait_for(state="visible")
    assert box.is_visible()
    assert page.evaluate("document.activeElement === document.getElementById('searchBox')")
    assert box.evaluate("node => node.getClientRects().length > 0")

    box.fill("check_continuity")
    page.wait_for_selector("#searchResults .search-row")
    assert "scripts/check_continuity.py" in page.locator("#searchResults").inner_text()
    page.keyboard.press("Enter")

    page.wait_for_selector("#file-preview-panel", state="visible")
    page.wait_for_function("() => document.getElementById('filePreviewTitle').innerText === 'scripts/check_continuity.py'")
    page.wait_for_function("() => document.getElementById('filePreviewBody').innerText.includes('def check_continuity')")
    assert "def check_continuity" in page.locator("#filePreviewBody").inner_text()

    page.go_back(wait_until="load")
    if start_view == "scene":
        page.wait_for_selector("#sceneModal", state="visible")
    else:
        page.wait_for_function("() => document.getElementById('filePreviewTitle').innerText === 'plans/book-plan.md'")
    page.go_forward(wait_until="load")
    page.wait_for_function("() => document.getElementById('filePreviewTitle').innerText === 'scripts/check_continuity.py'")

    page.keyboard.press("ControlOrMeta+k")
    palette.wait_for(state="visible")
    assert box.input_value() == "check_continuity"
    assert "scripts/check_continuity.py" in page.locator("#searchResults").inner_text()
    page.keyboard.press("Escape")
    palette.wait_for(state="hidden")


def test_global_search_has_visible_pointer_entry_and_lazy_deep_link(page: Page, shared_server: ProseviewServer):
    page.goto(f"{shared_server.base_url}#/file/scripts/check_continuity.py", wait_until="load")
    page.wait_for_selector("#file-preview-panel", state="visible")
    page.wait_for_function("() => document.getElementById('filePreviewTitle').innerText === 'scripts/check_continuity.py'")
    page.wait_for_function("() => document.getElementById('filePreviewBody').innerText.includes('def check_continuity')")
    assert "def check_continuity" in page.locator("#filePreviewBody").inner_text()

    search_button = page.locator("#file-preview-panel").get_by_role("button", name="Search files")
    assert search_button.is_visible()
    page.select_option("#filePreviewThemeSelect", "dark")
    page.set_viewport_size({"width": 1024, "height": 768})
    page.evaluate("document.body.style.zoom = '2'")
    search_button.click()
    page.wait_for_selector("#searchPalette", state="visible")
    assert page.evaluate("document.activeElement === document.getElementById('searchBox')")
    palette_box = page.locator("#searchMenu").bounding_box()
    assert palette_box
    assert palette_box["x"] >= 0 and palette_box["x"] + palette_box["width"] <= 1024
    assert palette_box["y"] >= 0 and palette_box["y"] + palette_box["height"] <= 768
    page.keyboard.press("Escape")
    assert page.evaluate("document.activeElement === document.querySelector('#file-preview-panel button[aria-label=\"Search files\"]')")


def test_search_does_not_navigate_away_from_an_unsaved_scene(page: Page, server: ProseviewServer):
    before = _stage_unsaved_edit(page, server)
    page.keyboard.press("ControlOrMeta+k")
    page.locator("#searchBox").fill("check_continuity")
    page.wait_for_selector("#searchResults .search-row")

    page.keyboard.press("Enter")

    assert page.locator("#searchPalette").is_visible()
    assert page.locator("#searchNavigationWarning").is_visible()
    assert "Save or cancel" in page.locator("#searchNavigationWarning").inner_text()
    assert page.evaluate("document.documentElement.dataset.view") == "scene"
    assert TYPED.strip() in _editor_text(page)
    assert server.scene_path().read_text(encoding="utf-8") == before
    page.keyboard.press("Escape")
    page.wait_for_selector("#searchPalette", state="hidden")
    assert page.evaluate("document.activeElement.classList.contains('ProseMirror')")
    assert page.locator(DIALOG).count() == 0
    assert TYPED.strip() in _editor_text(page)


def test_scene_search_pointer_entry_reflows_at_two_hundred_percent_zoom(
    page: Page,
    shared_server: ProseviewServer,
):
    page.set_viewport_size({"width": 1024, "height": 768})
    open_scene(page, shared_server)
    page.evaluate("document.body.style.zoom = '2'")
    page.wait_for_function("() => document.documentElement.dataset.cssZoom === 'true'")

    trigger = page.locator("#sceneModal button[aria-label='Search files']")
    box = trigger.bounding_box()
    assert box
    assert box["x"] >= 0 and box["x"] + box["width"] <= 1024
    assert box["y"] >= 0 and box["y"] + box["height"] <= 768
    trigger.click()
    page.wait_for_selector("#searchPalette", state="visible")


def test_scene_search_pointer_entry_reflows_for_compact_css_viewport(
    page: Page,
    shared_server: ProseviewServer,
):
    page.set_viewport_size({"width": 512, "height": 500})
    open_scene(page, shared_server)

    trigger = page.locator("#sceneModal button[aria-label='Search files']")
    box = trigger.bounding_box()
    assert box
    assert box["x"] >= 0 and box["x"] + box["width"] <= 512
    assert box["y"] >= 0 and box["y"] + box["height"] <= 500
    trigger.click()
    page.wait_for_selector("#searchPalette", state="visible")


def test_lazy_markdown_preview_strips_script_from_hostile_html_and_links(
    page: Page,
    shared_server: ProseviewServer,
):
    page.goto(f"{shared_server.base_url}#/file/scripts/hostile-preview.md", wait_until="load")
    page.wait_for_function(
        "() => document.getElementById('filePreviewTitle').innerText === 'scripts/hostile-preview.md'"
    )
    # `attached`, not `visible`: the src deliberately 404s, so the element has
    # no intrinsic size and never satisfies a visibility check.
    page.wait_for_selector("#filePreviewBody img", state="attached")

    body = page.locator("#filePreviewBody")

    # The <img> now renders -- images are a supported feature -- but only its
    # allowlisted attributes survive. Previously the whole tag stayed as text,
    # which was safe but also meant no images at all.
    assert body.locator("img").count() == 1
    assert body.locator("img").get_attribute("onerror") is None
    assert body.locator("img").get_attribute("src") == "/repo-asset/scripts/x"

    # The src 404s, so the browser fires an error event. Nothing must be
    # listening for it.
    assert page.evaluate("window.__previewPwned === true") is False

    # Everything else hostile is untouched: a javascript: URL yields no anchor,
    # and the URL itself is discarded rather than shown.
    assert body.locator("a").count() == 0
    assert "Unsafe link" in body.inner_text()
    assert "javascript:" not in body.inner_text()


def test_hidden_repository_deep_link_never_displays_its_contents(
    page: Page,
    shared_server: ProseviewServer,
):
    page.goto(f"{shared_server.base_url}#/file/.private/token.txt", wait_until="load")
    page.wait_for_function("() => document.getElementById('filePreviewMeta').innerText === 'Preview unavailable'")

    assert "fixture secret" not in page.locator("#filePreviewBody").inner_text()


def test_newer_lazy_preview_wins_when_an_older_request_finishes_late(
    page: Page,
    shared_server: ProseviewServer,
):
    def delay_first_preview(route):
        if "check_continuity.py" in route.request.url:
            time.sleep(0.25)
        route.continue_()

    page.route("**/repo-file?*", delay_first_preview)
    open_dashboard(page, shared_server)
    page.evaluate(
        """() => {
            previewRepoFile('scripts/check_continuity.py');
            previewRepoFile('scripts/hostile-preview.md');
        }"""
    )

    page.wait_for_function(
        "() => document.getElementById('filePreviewBody').innerText.includes('Safe heading')"
    )
    page.wait_for_timeout(400)
    assert page.locator("#filePreviewTitle").inner_text() == "scripts/hostile-preview.md"
    assert "def check_continuity" not in page.locator("#filePreviewBody").inner_text()


def test_arrow_keys_move_the_search_cursor(page: Page, shared_server: ProseviewServer):
    open_dashboard(page, shared_server)
    search_for(page, "Rena")
    page.wait_for_selector("#searchResults .search-row")
    assert search_rows(page).count() > 1

    def active_index() -> int:
        classes = [r.get_attribute("class") or "" for r in search_rows(page).all()]
        return next((i for i, c in enumerate(classes) if "active" in c), -1)

    first = active_index()
    page.keyboard.press("ArrowDown")
    _wait_until(lambda: active_index() != first, message="ArrowDown did not move the cursor")
    moved = active_index()

    page.keyboard.press("ArrowUp")
    _wait_until(lambda: active_index() != moved, message="ArrowUp did not move the cursor back")


def test_escape_closes_the_search_panel(page: Page, shared_server: ProseviewServer):
    open_dashboard(page, shared_server)
    search_for(page, "Rena")
    page.wait_for_selector("#searchResults", state="visible")

    page.keyboard.press("Escape")

    page.wait_for_selector("#searchResults", state="hidden")


@pytest.mark.parametrize("query", ["the", "a ", "e", "Rena", ".md"])
def test_search_never_exceeds_the_result_cap(page: Page, shared_server: ProseviewServer, query: str):
    """SEARCH_RESULT_CAP bounds the dropdown however broad the query.

    This asserts the invariant, not the overflow banner: prose hits are deduped
    to one row per scene, so a six-scene fixture cannot actually reach 30 rows.
    A test for the "+N more hits hidden" label would need a repo an order of
    magnitude larger and could never fail here.
    """
    open_dashboard(page, shared_server)
    search_for(page, query)
    page.wait_for_timeout(500)

    assert search_rows(page).count() <= 30, f"{query!r} returned more rows than the cap"


def test_search_reports_when_nothing_matches(page: Page, shared_server: ProseviewServer):
    open_dashboard(page, shared_server)
    search_for(page, "zzzznotinthisrepo")

    page.wait_for_selector("#searchResults .search-empty")
    assert search_rows(page).count() == 0
    assert "No matches" in page.locator("#searchResults").inner_text()


# ── unsaved-changes guard ───────────────────────────────────────────────────
#
# The highest-consequence path in the app. A save that breaks fails loudly; a
# discard guard that breaks throws away a writer's prose with no error and no
# way to get it back. Esc is wired at capture phase in 60-selection.js and
# routes through tryEscapeEditMode() in 50-discard-confirm.js.


DIALOG = ".unsaved-dialog-overlay"
TYPED = " An unsaved sentence."


def _dialog(page: Page):
    return page.locator(DIALOG)


def _stage_unsaved_edit(page: Page, server: ProseviewServer) -> str:
    """Enter edit mode, type, and return the file's untouched contents."""
    before = server.scene_path().read_text(encoding="utf-8")
    open_scene(page, server)
    enter_edit_mode(page)
    append_to_paragraph(page, "The loft smelled of cold coffee", TYPED)
    _wait_until(lambda: TYPED.strip() in _editor_text(page))
    return before


def test_escape_with_no_unsaved_edits_leaves_edit_mode_silently(page: Page, server: ProseviewServer):
    before = server.scene_path().read_text(encoding="utf-8")
    open_scene(page, server)
    enter_edit_mode(page)

    page.keyboard.press("Escape")

    page.wait_for_function("() => window._pmEditMode === false")
    assert _dialog(page).count() == 0, "guard prompted despite there being nothing to lose"
    assert server.scene_path().read_text(encoding="utf-8") == before


def test_escape_with_unsaved_edits_prompts_instead_of_discarding(page: Page, server: ProseviewServer):
    before = _stage_unsaved_edit(page, server)

    page.keyboard.press("Escape")

    page.wait_for_selector(DIALOG, state="visible")
    # Nothing may be lost or written while the writer is still deciding.
    assert TYPED.strip() in _editor_text(page)
    assert server.scene_path().read_text(encoding="utf-8") == before


def test_discarding_drops_the_edit_and_never_writes_the_file(page: Page, server: ProseviewServer):
    before = _stage_unsaved_edit(page, server)
    page.keyboard.press("Escape")
    page.wait_for_selector(DIALOG, state="visible")

    page.click(".unsaved-dialog-discard")

    page.wait_for_selector(DIALOG, state="detached")
    _wait_until(lambda: TYPED.strip() not in _editor_text(page),
                message="discard left the edit in the editor")
    page.wait_for_function("() => window._pmEditMode === false")
    assert server.scene_path().read_text(encoding="utf-8") == before


def test_saving_from_the_guard_persists_the_edit(page: Page, server: ProseviewServer):
    path = server.scene_path()
    _stage_unsaved_edit(page, server)
    page.keyboard.press("Escape")
    page.wait_for_selector(DIALOG, state="visible")

    page.click(".unsaved-dialog-save")

    _wait_until(lambda: TYPED.strip() in path.read_text(encoding="utf-8"),
                message="Save in the guard did not reach the file")
    assert frontmatter(path.read_text(encoding="utf-8"))


def test_cancelling_the_guard_keeps_the_editor_open_and_dirty(page: Page, server: ProseviewServer):
    before = _stage_unsaved_edit(page, server)
    page.keyboard.press("Escape")
    page.wait_for_selector(DIALOG, state="visible")

    page.click(".unsaved-dialog-cancel")

    page.wait_for_selector(DIALOG, state="detached")
    assert page.evaluate("window._pmEditMode") is True, "cancel dropped out of edit mode"
    assert TYPED.strip() in _editor_text(page)
    assert server.scene_path().read_text(encoding="utf-8") == before


def test_escape_inside_the_guard_dismisses_it_without_discarding(page: Page, server: ProseviewServer):
    """Esc opened the guard; Esc again must not be read as 'yes, throw it away'."""
    before = _stage_unsaved_edit(page, server)
    page.keyboard.press("Escape")
    page.wait_for_selector(DIALOG, state="visible")

    page.keyboard.press("Escape")

    page.wait_for_selector(DIALOG, state="detached")
    assert TYPED.strip() in _editor_text(page), "second Esc discarded the edit"
    assert page.evaluate("window._pmEditMode") is True
    assert server.scene_path().read_text(encoding="utf-8") == before


def test_enter_inside_the_guard_saves(page: Page, server: ProseviewServer):
    path = server.scene_path()
    _stage_unsaved_edit(page, server)
    page.keyboard.press("Escape")
    page.wait_for_selector(DIALOG, state="visible")

    page.keyboard.press("Enter")

    _wait_until(lambda: TYPED.strip() in path.read_text(encoding="utf-8"),
                message="Enter did not take the guard's default action")


def test_clicking_the_backdrop_dismisses_the_guard_without_discarding(page: Page, server: ProseviewServer):
    before = _stage_unsaved_edit(page, server)
    page.keyboard.press("Escape")
    page.wait_for_selector(DIALOG, state="visible")

    # Corner of the overlay, well clear of the centred dialog box.
    _dialog(page).click(position={"x": 5, "y": 5})

    page.wait_for_selector(DIALOG, state="detached")
    assert TYPED.strip() in _editor_text(page), "a stray backdrop click discarded the edit"
    assert server.scene_path().read_text(encoding="utf-8") == before


# ── inline annotation editing ───────────────────────────────────────────────
#
# TODO/NOTE comments render as atom nodes with their own node view. Clicking one
# in read mode enters edit mode with autoSave on, so Save and Resolve write
# straight through to the file.


TODO_BODY = "Tighten this opening beat"
NOTE_BODY = "Patel should not know about the safe yet"


def open_annotated_scene(page: Page, server: ProseviewServer) -> Path:
    open_scene(page, server, ANNOTATED_SCENE_REL)
    page.wait_for_selector(".pm-annotation")
    return server.scene_path(ANNOTATED_SCENE_REL)


def wait_for_annotation_editor(page: Page):
    """Wait until the annotation's box is not just present but focused.

    The node view focuses the field and selects its contents from a
    ``setTimeout(..., 0)``. Typing before that lands appends instead of
    replacing, so waiting on the element alone is a race.
    """
    page.wait_for_selector(".pm-annotation-editing .pm-annotation-edit-text")
    page.wait_for_function(
        "() => document.activeElement"
        " && document.activeElement.classList.contains('pm-annotation-edit-text')"
    )
    return page.locator(".pm-annotation-editing .pm-annotation-edit-text")


def click_annotation(page: Page, body: str):
    marker = page.locator(f".pm-annotation:has-text('{body}')")
    marker.wait_for(state="visible")
    marker.click()
    return wait_for_annotation_editor(page)


def test_annotations_render_as_markers_not_raw_comments(page: Page, server: ProseviewServer):
    open_annotated_scene(page, server)

    assert page.locator(".pm-annotation-todo").count() == 1
    assert page.locator(".pm-annotation-note").count() == 1
    assert TODO_BODY in page.locator(".pm-annotation-todo").inner_text()
    # The raw comment syntax must never be visible to the writer.
    assert "<!--" not in _editor_text(page)


def test_clicking_a_todo_opens_it_for_editing_and_enters_edit_mode(page: Page, server: ProseviewServer):
    open_annotated_scene(page, server)
    assert page.evaluate("window._pmEditMode") is not True

    editable = click_annotation(page, TODO_BODY)

    assert editable.inner_text().strip() == TODO_BODY
    assert page.evaluate("window._pmEditMode") is True, "editing an annotation did not enter edit mode"
    assert page.locator(".pm-annotation-save").is_visible()


def test_editing_a_todo_inline_writes_it_back_to_the_file(page: Page, server: ProseviewServer):
    path = open_annotated_scene(page, server)
    click_annotation(page, TODO_BODY)

    # The node view selects the existing body on focus, so typing replaces it.
    page.keyboard.type("Cut the opening beat entirely")
    page.click(".pm-annotation-save")

    _wait_until(lambda: "Cut the opening beat entirely" in path.read_text(encoding="utf-8"),
                message="inline TODO edit never reached the file")
    after = path.read_text(encoding="utf-8")
    assert "<!-- TODO: Cut the opening beat entirely -->" in after
    assert TODO_BODY not in after
    # The neighbouring note and the frontmatter must be untouched.
    assert f"<!-- NOTE[continuity]: {NOTE_BODY} -->" in after


def test_editing_a_note_inline_preserves_its_tag(page: Page, server: ProseviewServer):
    path = open_annotated_scene(page, server)
    click_annotation(page, NOTE_BODY)

    page.keyboard.type("Patel learns about the safe in chapter three")
    page.click(".pm-annotation-save")

    # Wait on a phrase that appears nowhere else -- "chapter three" is already
    # in this scene's frontmatter `todos:`, so it would match before the save.
    _wait_until(lambda: "Patel learns about the safe" in path.read_text(encoding="utf-8"),
                message="inline note edit never reached the file")
    after = path.read_text(encoding="utf-8")
    assert "<!-- NOTE[continuity]: Patel learns about the safe in chapter three -->" in after
    assert "NOTE[note]" not in after, "the tag was reset to the default on save"


def test_resolving_a_todo_removes_it_from_the_file(page: Page, server: ProseviewServer):
    path = open_annotated_scene(page, server)
    before = path.read_text(encoding="utf-8")
    click_annotation(page, TODO_BODY)

    # Labelled "Resolve" for TODOs and "Delete" for notes; same control.
    page.click(".pm-annotation-delete")

    _wait_until(lambda: "<!-- TODO:" not in path.read_text(encoding="utf-8"),
                message="resolving the TODO did not remove it from the file")
    after = path.read_text(encoding="utf-8")
    assert f"<!-- NOTE[continuity]: {NOTE_BODY} -->" in after
    assert "Patel arrived with the ledger" in after, "resolving an annotation ate the prose"
    assert frontmatter(after) == frontmatter(before)


def test_cancelling_an_inline_annotation_edit_leaves_the_file_untouched(page: Page, server: ProseviewServer):
    path = open_annotated_scene(page, server)
    before = path.read_text(encoding="utf-8")
    click_annotation(page, TODO_BODY)

    page.keyboard.type("This should never be written")
    page.click(".pm-annotation-cancel")

    page.wait_for_selector(".pm-annotation-editing", state="detached")
    page.wait_for_timeout(1000)
    assert path.read_text(encoding="utf-8") == before
    assert TODO_BODY in page.locator(".pm-annotation-todo").inner_text()


def test_insert_affordance_adds_a_new_todo_to_a_paragraph(page: Page, server: ProseviewServer):
    """Hovering a paragraph in edit mode offers a TODO insertion point."""
    path = open_annotated_scene(page, server)
    enter_edit_mode(page)

    page.hover("#sceneProseHost .ProseMirror p")
    affordance = page.locator("#pmInsertAffordance")
    affordance.wait_for(state="visible")
    affordance.click()

    wait_for_annotation_editor(page)
    page.keyboard.type("Added from the gutter affordance")
    page.click(".pm-annotation-save")

    # Inserting does not autosave -- the affordance path leaves the writer in
    # control, so the edit only lands once they save the scene.
    page.wait_for_timeout(500)
    assert "Added from the gutter affordance" not in path.read_text(encoding="utf-8")

    # Mod-S is a ProseMirror keymap binding, so the editor needs focus back --
    # rendering the annotation read-only left it on the body.
    page.click("#sceneProseHost .ProseMirror p")
    save_scene(page)
    _wait_until(lambda: "Added from the gutter affordance" in path.read_text(encoding="utf-8"),
                message="the inserted TODO never reached the file")
    assert "<!-- TODO: Added from the gutter affordance -->" in path.read_text(encoding="utf-8")


# ── selection menu ──────────────────────────────────────────────────────────


def test_selection_pill_exposes_every_action(page: Page, server: ProseviewServer):
    open_scene(page, server)
    open_selection_menu(page, "the slow algebra")

    menu = page.locator("#selectionPillMenu")
    for control in ("selectionEditorBtn", "selectionTodoBtn", "selectionNoteBtn",
                    "selectionCodexBtn", "selectionSkillsBtn"):
        assert menu.locator(f"#{control}").is_visible(), f"{control} missing from the pill"


def test_keyboard_selection_exposes_the_same_action_menu(page: Page, server: ProseviewServer):
    open_scene(page, server)
    enter_edit_mode(page)
    page.evaluate(
        """() => {
            const text = document.querySelector('#sceneProseHost .ProseMirror p').firstChild;
            const range = document.createRange();
            range.setStart(text, 0);
            range.collapse(true);
            const selection = window.getSelection();
            selection.removeAllRanges();
            selection.addRange(range);
            document.querySelector('#sceneProseHost .ProseMirror').focus();
        }"""
    )
    page.keyboard.press("Shift+ArrowRight")
    assert page.evaluate("window.getSelection().toString().length") == 1
    page.wait_for_selector("#selectionPillBtn", state="visible")
    page.keyboard.press("ControlOrMeta+k")
    page.wait_for_selector("#selectionPillMenu", state="visible")
    assert page.get_by_role("menuitem", name="Add TODO").is_visible()


def test_collapsing_a_keyboard_selection_clears_its_action_trigger(
    page: Page,
    server: ProseviewServer,
):
    open_scene(page, server)
    enter_edit_mode(page)
    paragraph = page.locator("#sceneProseHost .ProseMirror p").first
    paragraph.click(position={"x": 8, "y": 8})
    page.keyboard.press("Shift+ArrowRight")
    page.wait_for_selector("#selectionPillBtn", state="visible")

    page.keyboard.press("ArrowRight")
    assert page.evaluate("window.getSelection().isCollapsed")
    page.wait_for_selector("#selectionPill", state="hidden")


def test_page_navigation_keyboard_selection_exposes_the_action_trigger(
    page: Page,
    server: ProseviewServer,
):
    open_scene(page, server)
    enter_edit_mode(page)
    editor = page.locator("#sceneProseHost .ProseMirror")
    editor.focus()
    page.keyboard.press("ControlOrMeta+Home")
    page.keyboard.press("Shift+PageDown")
    assert len(page.evaluate("window.getSelection().toString()")) > 0
    page.wait_for_selector("#selectionPillBtn", state="visible")


def test_selection_menu_has_keyboard_semantics_and_restores_focus(
    page: Page,
    server: ProseviewServer,
):
    open_scene(page, server)
    select_prose(page, "the slow algebra")

    trigger = page.locator("#selectionPillBtn")
    menu = page.locator("#selectionPillMenu")
    assert trigger.get_attribute("aria-label") == "Work with selected text"
    assert trigger.get_attribute("aria-haspopup") == "menu"
    assert trigger.get_attribute("aria-controls") == "selectionPillMenu"
    assert trigger.get_attribute("aria-expanded") == "false"
    assert menu.get_attribute("role") == "menu"

    page.keyboard.press("ControlOrMeta+k")

    menu.wait_for(state="visible")
    assert page.locator("#searchPalette").is_hidden()
    assert trigger.get_attribute("aria-expanded") == "true"
    assert page.evaluate("document.activeElement === document.getElementById('selectionRewriteBtn')")
    assert page.locator("#selectionRewriteBtn").get_attribute("aria-controls") == "selectionRewriteMenu"
    assert page.locator("#selectionRewriteBtn").get_attribute("aria-expanded") == "false"
    page.keyboard.press("ArrowRight")
    assert page.locator("#selectionRewriteBtn").get_attribute("aria-expanded") == "true"
    assert page.locator("#selectionRewriteMenu").is_visible()
    page.keyboard.press("ArrowLeft")
    assert page.locator("#selectionRewriteBtn").get_attribute("aria-expanded") == "false"
    assert page.evaluate("document.activeElement === document.getElementById('selectionRewriteBtn')")

    for _ in range(4):
        page.keyboard.press("ArrowDown")
    assert page.evaluate("document.activeElement === document.getElementById('selectionTodoBtn')")
    page.keyboard.press("Enter")
    assert page.evaluate("document.activeElement === document.getElementById('selectionTodoText')")

    page.keyboard.press("Escape")
    assert menu.is_visible()
    assert page.locator("#selectionTodoForm").is_hidden()
    assert page.evaluate("document.activeElement === document.getElementById('selectionTodoBtn')")

    page.keyboard.press("Escape")
    menu.wait_for(state="hidden")
    assert trigger.is_visible()
    assert trigger.get_attribute("aria-expanded") == "false"
    assert page.evaluate("document.activeElement === document.getElementById('selectionPillBtn')")


def test_selection_skills_open_managed_searchable_surface(
    page: Page,
    server: ProseviewServer,
):
    open_scene(page, server)
    open_selection_menu(page, "the slow algebra")

    page.locator("#selectionSkillsBtn").click()
    page.wait_for_selector("#discussPanel", state="visible")
    page.wait_for_function("() => document.querySelectorAll('#discussSkillsPicker .discuss-skill').length >= 2")
    assert "Tighten Prose" in page.locator("#discussSkillsPicker").inner_text()
    page.locator("#discussSkillsPicker .discuss-skill").first.click()
    page.fill("#discussInput", "Review this selected passage")
    page.click("#discussSend")
    wait_for_discuss_answer(page)

    received = (server.env["HOME"] and Path(server.env["HOME"]) / "fake-codex-received.jsonl").read_text(encoding="utf-8")
    assert '"type": "skill"' in received


def test_selection_shortcut_falls_back_to_search_after_selection_is_cleared(
    page: Page,
    server: ProseviewServer,
):
    open_scene(page, server)
    select_prose(page, "the slow algebra")
    page.keyboard.press("ControlOrMeta+k")
    page.keyboard.press("Escape")
    page.keyboard.press("Escape")

    page.locator("#sceneProseHost .ProseMirror p").first.click()
    assert page.evaluate("window.getSelection().isCollapsed")
    assert page.locator("#selectionPill").is_hidden()

    page.keyboard.press("ControlOrMeta+k")

    page.wait_for_selector("#searchPalette", state="visible")
    assert page.locator("#selectionPillMenu").is_hidden()
    assert page.evaluate("document.activeElement === document.getElementById('searchBox')")


def test_selection_pill_reanchors_with_the_scene_scroll_container(
    page: Page,
    server: ProseviewServer,
):
    page.set_viewport_size({"width": 1024, "height": 768})
    open_scene(page, server)
    page.evaluate(
        "document.querySelector('#sceneModal .modal-content').style.scrollBehavior = 'auto'"
    )
    select_prose(page, "the slow algebra", block="center")
    page.wait_for_timeout(50)

    before = page.evaluate(
        """() => ({
            anchorTop: currentSelectionRange.getBoundingClientRect().top,
            pillTop: document.getElementById('selectionPillBtn').getBoundingClientRect().top,
        })"""
    )
    page.evaluate(
        """() => {
            const scroller = document.querySelector('#sceneModal .modal-content');
            scroller.scrollTop += 40;
        }"""
    )
    page.wait_for_function(
        "before => Math.abs(currentSelectionRange.getBoundingClientRect().top - before) > 20",
        arg=before["anchorTop"],
    )
    page.wait_for_timeout(50)
    after = page.evaluate(
        """() => ({
            anchorTop: currentSelectionRange.getBoundingClientRect().top,
            pillTop: document.getElementById('selectionPillBtn').getBoundingClientRect().top,
        })"""
    )

    anchor_delta = after["anchorTop"] - before["anchorTop"]
    pill_delta = after["pillTop"] - before["pillTop"]
    assert abs(anchor_delta - pill_delta) < 2
    assert_fully_inside_viewport(page, "#selectionPillBtn")


def test_selection_form_is_a_separate_accessible_surface(
    page: Page,
    server: ProseviewServer,
):
    open_scene(page, server)
    open_selection_menu(page, "the slow algebra")

    page.locator("#selectionTodoBtn").click()

    assert page.locator("#selectionPillMenu").is_hidden()
    form = page.locator("#selectionTodoForm")
    assert form.is_visible()
    assert form.get_attribute("role") == "dialog"
    assert form.get_attribute("aria-label") == "Add TODO to selected text"
    assert page.locator("#selectionPillBtn").get_attribute("aria-expanded") == "false"
    assert page.locator("#selectionPill [role='menuitem']:visible").count() == 0

    page.keyboard.press("Escape")
    assert form.is_hidden()
    assert page.locator("#selectionPillMenu").is_visible()
    assert page.locator("#selectionPillMenu").get_attribute("role") == "menu"
    assert page.evaluate("document.activeElement === document.getElementById('selectionTodoBtn')")


def test_selection_menu_tab_dismisses_to_a_page_control(
    page: Page,
    server: ProseviewServer,
):
    open_scene(page, server)
    select_prose(page, "the slow algebra")
    page.keyboard.press("ControlOrMeta+k")
    assert page.evaluate("document.activeElement === document.getElementById('selectionRewriteBtn')")

    page.keyboard.press("Tab")
    assert page.locator("#selectionPillMenu").is_hidden()
    assert page.evaluate(
        "document.activeElement !== document.body && !document.getElementById('selectionPill').contains(document.activeElement)"
    )


@pytest.mark.parametrize(
    ("opener_id", "form_id", "first_id", "last_id"),
    [
        ("selectionTodoBtn", "selectionTodoForm", "selectionTodoText", "selectionTodoCancel"),
        ("selectionNoteBtn", "selectionNoteForm", "selectionNoteTag", "selectionNoteCancel"),
    ],
)
def test_selection_dialogs_trap_tab_focus(
    page: Page,
    server: ProseviewServer,
    opener_id: str,
    form_id: str,
    first_id: str,
    last_id: str,
):
    open_scene(page, server)
    open_selection_menu(page, "the slow algebra")
    page.locator(f"#{opener_id}").click()
    assert page.locator(f"#{form_id}").is_visible()

    page.locator(f"#{first_id}").focus()
    page.keyboard.press("Shift+Tab")
    assert page.evaluate(
        "lastId => document.activeElement === document.getElementById(lastId)",
        last_id,
    )

    page.keyboard.press("Tab")
    assert page.evaluate(
        "firstId => document.activeElement === document.getElementById(firstId)",
        first_id,
    )
    assert page.locator(f"#{form_id}").is_visible()


@pytest.mark.parametrize("dirty", [False, True])
def test_selection_escape_precedes_edit_mode_escape(
    page: Page,
    server: ProseviewServer,
    dirty: bool,
):
    open_scene(page, server)
    enter_edit_mode(page)
    if dirty:
        append_to_paragraph(page, "The loft smelled of cold coffee", TYPED)
        assert TYPED.strip() in _editor_text(page)
    select_prose(page, "the slow algebra")
    page.keyboard.press("ControlOrMeta+k")
    page.locator("#selectionTodoBtn").click()
    assert page.locator("#selectionTodoForm").is_visible()

    page.keyboard.press("Escape")
    assert page.locator("#selectionTodoForm").is_hidden()
    assert page.locator("#selectionPillMenu").is_visible()
    assert page.evaluate("window._pmEditMode === true")
    assert page.locator(DIALOG).count() == 0

    page.keyboard.press("Escape")
    assert page.locator("#selectionPillMenu").is_hidden()
    assert page.locator("#selectionPillBtn").is_visible()
    assert page.evaluate("window._pmEditMode === true")
    assert page.locator(DIALOG).count() == 0

    page.keyboard.press("Escape")
    assert page.locator("#selectionPill").is_hidden()
    assert page.evaluate("window._pmEditMode === true")
    assert page.locator(DIALOG).count() == 0

    page.keyboard.press("Escape")
    if dirty:
        page.wait_for_selector(DIALOG, state="visible")
        assert page.evaluate("window._pmEditMode === true")
    else:
        page.wait_for_function("() => window._pmEditMode === false")
        assert page.locator(DIALOG).count() == 0


@pytest.mark.parametrize(
    ("width", "height", "zoom", "theme"),
    [(1400, 1000, 1, "light"), (1024, 768, 2, "dark")],
)
def test_selection_menu_and_managed_dock_stay_inside_the_visual_viewport(
    page: Page,
    server: ProseviewServer,
    width: int,
    height: int,
    zoom: int,
    theme: str,
):
    page.set_viewport_size({"width": width, "height": height})
    open_scene(page, server)
    open_scene_appearance(page)
    page.select_option("#modalThemeSelect", theme)
    page.evaluate("zoom => { document.body.style.zoom = String(zoom); }", zoom)
    if zoom > 1:
        page.wait_for_function("() => document.documentElement.dataset.cssZoom === 'true'")

    select_prose(page, "the slow algebra", block="end")
    assert_fully_inside_viewport(page, "#selectionPillBtn")

    page.locator("#selectionPillBtn").click()
    page.wait_for_selector("#selectionPillMenu", state="visible")
    assert_fully_inside_viewport(page, "#selectionPillMenu")

    page.locator("#selectionCodexBtn").click()
    page.wait_for_selector("#discussPanel", state="visible")
    page.wait_for_function("() => document.activeElement === document.getElementById('discussInput')")
    assert page.locator("#selectionPillMenu").is_hidden()
    assert_fully_inside_viewport(page, "#discussPanel")
    assert_fully_inside_viewport(page, "#discussInput")
    assert_fully_inside_viewport(page, "#discussSend")


def test_selection_add_todo_writes_the_comment_to_the_file(page: Page, server: ProseviewServer):
    path = server.scene_path()
    open_scene(page, server)
    open_selection_menu(page, "It is sticking again")

    page.click("#selectionTodoBtn")
    page.fill("#selectionTodoText", "Sharpen Lowe's entrance")
    page.click("#selectionTodoCopy")

    # The selected passage is appended to the note, so the TODO still reads as
    # an instruction once the paragraph around it has moved on.
    expected = '<!-- TODO: Sharpen Lowe\'s entrance | "It is sticking again" -->'
    _wait_until(lambda: expected in path.read_text(encoding="utf-8"),
                message="TODO from the selection menu never reached the file")

    text = path.read_text(encoding="utf-8")
    assert text.index("<!-- TODO:") < text.index("It is sticking again")


def test_selection_add_note_writes_a_tagged_comment(page: Page, server: ProseviewServer):
    path = server.scene_path()
    open_scene(page, server)
    open_selection_menu(page, "It is not the safe")

    page.click("#selectionNoteBtn")
    page.fill("#selectionNoteText", "Safe brand must match chapter three")
    page.select_option("#selectionNoteTag", "continuity")
    page.click("#selectionNoteCopy")

    _wait_until(
        lambda: "<!-- NOTE[continuity]: Safe brand must match chapter three -->"
        in path.read_text(encoding="utf-8"),
        message="tagged note never reached the file",
    )


@pytest.mark.allow_http_errors("/insert-todo")
def test_stale_selection_reports_an_error_instead_of_annotating_the_wrong_paragraph(
    page: Page, server: ProseviewServer
):
    """A selection that no longer matches the file must not silently relocate.

    The annotation used to fall back to the first paragraph of the scene, so a
    passage edited in another editor since the page loaded put the comment at
    the top of the file with a success message. Now the endpoint answers 500 and
    the page surfaces it -- hence the ``allow_http_errors`` marker, which also
    proves the fixture's 5xx guard fires.
    """
    path = server.scene_path()
    before = path.read_text(encoding="utf-8")
    open_scene(page, server)

    # Post the selection the page would send if its anchor paragraph had been
    # rewritten in another editor. Editing the file on disk instead would trip
    # live reload and tear the selection down before the request went out.
    result = page.evaluate(
        """async (absPath) => {
            const r = await fetch('/insert-todo', {
                method: 'POST',
                headers: pvHeaders(),
                body: JSON.stringify({
                    abs_path: absPath,
                    selection_text: 'a paragraph that no longer exists in this scene',
                    txt_line_offset: 0,
                    todo_text: 'should never land',
                }),
            });
            return {status: r.status, body: await r.json()};
        }""",
        str(path),
    )

    assert result["status"] == 500
    assert "Could not find the selected passage" in result["body"]["error"]
    assert path.read_text(encoding="utf-8") == before, "a rejected annotation must not touch the file"


def test_file_preview_renders_tables_and_rules_rather_than_raw_source(
    page: Page, server: ProseviewServer
):
    """Block Markdown in a non-scene document must actually render.

    ``renderSafeMarkdown`` walks an allowlist of token types and falls back to
    dumping ``token.raw`` for anything it does not know. ``table`` and ``hr``
    were missing, so a planning document showed its tables as one run-on line of
    pipes and its rules as literal ``---``.
    """
    page.goto(f"{server.base_url}#/file/plans/structure-notes.md", wait_until="load")
    page.wait_for_selector("#filePreviewBody", state="visible")
    page.wait_for_selector("#filePreviewBody table")

    body = page.locator("#filePreviewBody")
    assert body.locator("hr").count() == 1
    assert body.locator("table thead th").count() == 3
    assert body.locator("table tbody tr").count() == 2
    assert "ch01" in body.locator("table tbody tr").first.inner_text()

    # Nothing may remain as raw pipe-table source.
    assert "| --- |" not in body.inner_text()
    assert "\n---\n" not in body.inner_text()

    # Right-aligned column from the `---:` marker survives.
    assert "right" in body.locator("table thead th").last.get_attribute("style")


def test_repo_images_render_and_raw_img_tags_cannot_carry_script(
    page: Page, server: ProseviewServer
):
    """Markdown and raw ``<img>`` both render, through the contained route.

    The raw-tag path is the sharp one: the token is literal HTML, so it is
    parsed attribute by attribute against an allowlist rather than handed to
    ``innerHTML``. An ``onerror`` must not survive that.
    """
    page.goto(f"{server.base_url}#/file/plans/images-demo.md", wait_until="load")
    page.wait_for_selector("#filePreviewBody img")

    images = page.locator("#filePreviewBody img")
    assert images.count() == 3, "markdown, raw tag, and remote should all produce an <img>"

    # The relative reference resolved against the document's own directory.
    markdown_img = images.nth(0)
    assert markdown_img.get_attribute("src") == "/repo-asset/img/cover.png"
    assert markdown_img.get_attribute("alt") == "The cover"
    # It actually decoded, so the route really served the bytes.
    assert page.evaluate(
        "() => { const i = document.querySelectorAll('#filePreviewBody img')[0];"
        " return i.complete && i.naturalWidth > 0; }"
    )

    raw_img = images.nth(1)
    assert raw_img.get_attribute("src") == "/repo-asset/img/cover.png"
    assert raw_img.get_attribute("alt") == "Raw tag"
    assert raw_img.get_attribute("width") == "10", "allowlisted attribute should survive"
    assert raw_img.get_attribute("onerror") is None, "event handler must be stripped"
    assert page.evaluate("() => window.__pwned === undefined"), "no handler may have fired"

    # Remote images load at the default `images: all`, with no referrer leak.
    assert images.nth(2).get_attribute("src") == "https://example.invalid/remote.png"
    assert images.nth(2).get_attribute("referrerpolicy") == "no-referrer"


@pytest.mark.allow_js_errors("__pv_images_off")
def test_images_off_falls_back_to_alt_text(page: Page, server: ProseviewServer):
    """With ``images: off`` nothing loads and the alt text shows instead."""
    page.goto(f"{server.base_url}#/file/plans/images-demo.md", wait_until="load")
    page.wait_for_selector("#filePreviewBody img")

    rendered = page.evaluate(
        """() => {
            imagesConfig.mode = 'off';
            const host = document.createElement('div');
            renderSafeMarkdown(host, '![The cover](../img/cover.png)',
                               {basePath: 'plans/images-demo.md'});
            return {imgs: host.querySelectorAll('img').length,
                    text: host.innerText.trim()};
        }"""
    )
    assert rendered["imgs"] == 0
    assert "The cover" in rendered["text"]


def test_agent_output_images_follow_the_remote_setting(page: Page, server: ProseviewServer):
    """Remote images in Discuss require opt-in, and the switch really works.

    The gate exists so this one surface can be turned off on its own: there the
    URL is the model's choice, and fetching it tells that host the reader opened
    the document.
    """
    page.goto(f"{server.base_url}#/file/plans/images-demo.md", wait_until="load")
    page.wait_for_selector("#filePreviewBody img", state="attached")

    rendered = page.evaluate(
        """() => {
            const run = () => {
                const host = document.createElement('div');
                renderDiscussMarkdown(host, '![shot](https://example.invalid/tracker.png)');
                return {imgs: host.querySelectorAll('img').length,
                        placeholders: host.querySelectorAll('.md-image-placeholder').length};
            };
            imagesConfig.remoteInAgentOutput = false;
            const defaultOff = run();
            imagesConfig.remoteInAgentOutput = true;
            const optedIn = run();
            imagesConfig.remoteInAgentOutput = false;
            return {defaultOff, optedIn};
        }"""
    )
    assert rendered["defaultOff"] == {"imgs": 0, "placeholders": 1}
    assert rendered["optedIn"] == {"imgs": 1, "placeholders": 0}


def test_image_paths_cannot_escape_the_repository(page: Page, server: ProseviewServer):
    """Client-side resolution refuses what the server would refuse anyway."""
    page.goto(f"{server.base_url}#/file/plans/images-demo.md", wait_until="load")
    page.wait_for_selector("#filePreviewBody img")

    escaped = page.evaluate(
        """() => ['../../../../etc/passwd', '../../outside.png', '..']
                  .map(src => repoAssetUrl(src, 'plans/images-demo.md'))"""
    )
    assert escaped == [None, None, None], f"a path escaped containment: {escaped}"


def test_chronology_boxes_never_overlap_their_slot(page: Page, server: ProseviewServer):
    """The strip sizes boxes from the slot, and scrolls rather than squashing.

    Box width used to be a fixed 58px while the step shrank with scene count.
    At 39 scenes that gave a 29px step, so every box overlapped its neighbour
    and all the labels were clipped mid-word.
    """
    page.goto(f"{server.base_url}#/tab/timeline", wait_until="load")
    page.wait_for_selector("#timelineContent .story-section")

    layout = page.evaluate(
        """() => {
            const svgs = [...document.querySelectorAll('#timelineContent .story-svg')];
            const svg = svgs[svgs.length - 1];
            if (!svg) return null;
            const rects = [...svg.querySelectorAll('g.story-node rect')];
            if (rects.length < 2) return null;
            const xs = rects.map(r => parseFloat(r.getAttribute('x')));
            const w = parseFloat(rects[0].getAttribute('width'));
            const step = xs[1] - xs[0];
            return {step, boxWidth: w, hasExplicitWidth: svg.hasAttribute('width')};
        }"""
    )
    assert layout, "expected a chronology strip with at least two scenes"
    assert layout["boxWidth"] <= layout["step"], \
        f"box {layout['boxWidth']} is wider than its {layout['step']}px slot"
    assert layout["hasExplicitWidth"], \
        "without an explicit width the SVG scales to fit instead of scrolling"


def test_character_charts_render_multi_word_names(page: Page, server: ProseviewServer):
    """The gap that let the empty co-occurrence chart ship.

    Character names came from ``path.stem.capitalize()``, so ``harbour-master.md``
    became ``Harbour-master`` and matched nothing in the prose. Presence and
    co-occurrence then rendered as empty axes with no explanation. The fixture's
    other characters are all single words, so nothing here could catch it.
    """
    open_dashboard(page, server)
    page.click('.tab-nav button[data-tab="analysis"]')
    page.wait_for_selector("#analysisContent:not([hidden])")

    counts = page.evaluate(
        """() => {
            const rows = (id) => {
                const c = chartRefs[id];
                if (!c) return 0;
                return (c.data.datasets || []).reduce((n, d) => n + (d.data || []).length, 0);
            };
            return {presence: rows('presenceChart'), coOccur: rows('coOccurChart'),
                    labels: (chartRefs.presenceChart || {data: {}}).data.datasets
                        ? chartRefs.presenceChart.data.datasets.map(d => d.label) : []};
        }"""
    )
    assert counts["presence"] > 0, "character presence chart is empty"
    assert "Harbour Master" in counts["labels"], \
        f"multi-word character missing from presence: {counts['labels']}"


def test_no_panel_is_ever_silently_empty(page: Page, bare_server: ProseviewServer):
    """The invariant behind a whole class of bugs.

    Against a manuscript with no metadata, every chart has nothing to plot. Each
    one must either draw rows or say why it cannot -- an empty axis box looks
    identical whether the data is absent or the code is broken, which is exactly
    how the character-name derivation bug survived review and the test suite.
    """
    page.goto(bare_server.base_url, wait_until="load")
    page.wait_for_selector("#sceneTable tbody tr")
    page.click('.tab-nav button[data-tab="analysis"]')
    page.wait_for_selector("#analysisContent:not([hidden])")
    page.wait_for_function("() => typeof chartRefs === 'object'")
    page.wait_for_timeout(600)

    verdicts = page.evaluate(
        """() => {
            const out = {};
            ['presenceChart', 'locationChart', 'coOccurChart'].forEach(function(id) {
                const chart = chartRefs[id];
                const rows = chart
                    ? (chart.data.datasets || []).reduce((n, d) => n + (d.data || []).length, 0)
                    : 0;
                // The note replaces the canvas, so look for either.
                const canvas = document.getElementById(id);
                const frame = canvas ? canvas.parentElement : null;
                const note = document.querySelectorAll('.chart-empty');
                out[id] = {rows: rows, explained: !canvas && note.length > 0};
            });
            return out;
        }"""
    )
    for chart, verdict in verdicts.items():
        assert verdict["rows"] > 0 or verdict["explained"], \
            f"{chart} rendered empty with no explanation"


def test_a_manuscript_with_no_frontmatter_is_still_usable(
    page: Page, bare_server: ProseviewServer
):
    """Reading, searching, and the scene table work on prose alone."""
    page.goto(bare_server.base_url, wait_until="load")
    page.wait_for_selector("#sceneTable tbody tr")

    assert page.locator("#sceneTable tbody tr.scene-row").count() == 3
    table = page.locator("#sceneTable").inner_text()
    assert "one.md" in table

    page.goto(f"{bare_server.base_url}#/scene/one.md", wait_until="load")
    page.wait_for_selector("#sceneProseHost .ProseMirror")
    assert "counted the boats" in page.locator("#sceneProseHost .ProseMirror").inner_text()


def test_line_width_control_resizes_the_reading_column_and_persists(
    page: Page, server: ProseviewServer
):
    """One control for measure, not two for margins.

    The reading column is centred, so its width is the only real variable; the
    margins are whatever is left, split evenly. The readout is in characters
    because that is the number a reader is actually choosing.

    Wide viewport on purpose: this is about clamping the *preference*, and at
    the default 1280 the sidebar leaves less room than the 1100px maximum, so
    the container would do the clamping and the assertion would prove nothing.
    """
    page.set_viewport_size({"width": 1700, "height": 900})
    open_scene(page, server)

    width = lambda: page.evaluate(
        "() => Math.round(document.getElementById('sceneProseHost').getBoundingClientRect().width)"
    )
    assert width() == 760, "an unset preference must not clamp to the minimum"
    assert "chars" in page.locator("#modalMeasureOut").inner_text()

    page.evaluate("() => updateReadingMeasure(1000)")
    assert width() == 1000

    page.reload(wait_until="load")
    page.wait_for_selector("#sceneProseHost .ProseMirror")
    assert width() == 1000, "the choice did not survive a reload"

    # Out-of-range values clamp rather than throwing.
    page.evaluate("() => updateReadingMeasure(99999)")
    assert width() == 1100
    page.evaluate("() => updateReadingMeasure('nonsense')")
    assert width() == 760


def test_the_file_preview_uses_the_same_measure(page: Page, server: ProseviewServer):
    """Scenes and documents must not disagree about line length."""
    page.goto(f"{server.base_url}#/scene/{SCENE_REL}", wait_until="load")
    page.wait_for_selector("#sceneProseHost .ProseMirror")
    page.evaluate("() => updateReadingMeasure(620)")

    page.goto(f"{server.base_url}#/file/plans/structure-notes.md", wait_until="load")
    page.wait_for_selector("#filePreviewBody")

    assert page.evaluate(
        "() => Math.round(document.getElementById('filePreviewBody').getBoundingClientRect().width)"
    ) == 620


def test_scene_stats_carry_their_units(page: Page, server: ProseviewServer):
    """A rate with no denominator cannot be read.

    "Sensory 1.4" says nothing; "1.4 per 1,000 words" says what was counted.
    """
    open_scene(page, server)
    page.evaluate("() => document.querySelectorAll('details').forEach(d => d.open = true)")

    units = page.evaluate(
        "() => [...document.querySelectorAll('#modalStats .unit')].map(u => u.textContent)"
    )
    assert units.count("per 1,000 words") == 4, f"rate units missing: {units}"


def test_stat_tiles_are_readouts_not_a_second_set_of_controls(
    page: Page, server: ProseviewServer
):
    """One surface toggles a pass, and it is the pass row.

    Making four of the eight tiles clickable put the same state behind two
    controls, and left tiles that looked alike behaving differently. The tiles
    echo which pass is on; they do not switch it.
    """
    open_scene(page, server)
    open_scene_analysis(page)

    tiles = page.locator("#sceneAnalysisPane .scene-stat-box")
    assert tiles.count() == 10
    assert page.evaluate(
        "() => [...document.querySelectorAll('#sceneAnalysisPane .scene-stat-box')]"
        ".every(el => el.tagName === 'DIV')"
    ), "no tile should be a button"

    # Turning a pass on from its own row tints the matching tile.
    page.evaluate("() => toggleHighlight('sensory')")
    page.wait_for_function(
        "() => document.querySelector('#sceneAnalysisPane [data-pass=\"sensory\"]')"
        ".classList.contains('scene-stat-on')"
    )


def test_the_scene_tabs_do_not_wear_the_discuss_heading(
    page: Page, server: ProseviewServer
):
    """"Codex / Live" names the agent connection, not a pane of counts."""
    open_scene(page, server)
    open_scene_details(page)

    assert page.locator("#discussTitle").inner_text().strip() == "Scene"
    assert page.locator("#discussConnection").is_hidden()

    open_scene_analysis(page)
    assert page.locator("#discussTitle").inner_text().strip() == "Analysis"
    assert page.locator("#discussConnection").is_hidden()

    page.evaluate("() => showDiscussTab()")
    page.wait_for_function(
        "() => !['Scene', 'Analysis'].includes("
        "document.getElementById('discussTitle').innerText.trim())"
    )
    assert page.locator("#discussConnection").is_visible()


def test_every_pass_is_listed_with_its_count_and_an_example(
    page: Page, server: ProseviewServer
):
    """The pass list is the one place a pass can be switched on.

    Each row carries a line of examples, because "felt, saw, heard, noticed"
    identifies Filter Verbs faster than a definition does, and a tooltip with
    the longer note for anyone who wants it. A pass with no matches still
    appears, so "nothing here" costs no click.
    """
    open_scene(page, server)
    open_scene_analysis(page)

    rows = page.locator("#scenePassList .scene-pass-row")
    assert rows.count() == 9, "every pass is listed, including the empty ones"

    for row in rows.all():
        assert row.locator(".scene-pass-example").inner_text().strip(), (
            "a pass row without examples is a label the reader has to guess at"
        )
        assert (row.get_attribute("title") or "").strip(), "the longer note is the hover layer"
        assert row.locator(".scene-pass-count").inner_text().strip().isdigit()

    # Ordered by how much there is to look at.
    counts = [
        int(n)
        for n in page.evaluate(
            "() => [...document.querySelectorAll('#scenePassList .scene-pass-count')]"
            ".map(el => el.textContent)"
        )
    ]
    assert counts == sorted(counts, reverse=True)

    filter_row = page.locator("#pass-row-filter_verbs")
    assert "felt" in filter_row.locator(".scene-pass-example").inner_text()
    assert filter_row.get_attribute("aria-pressed") == "false"
    filter_row.click()
    assert filter_row.get_attribute("aria-pressed") == "true"
    page.wait_for_selector("#sceneProseHost .hl-filter")


def test_clicking_a_pass_row_is_the_only_way_its_state_changes(
    page: Page, server: ProseviewServer
):
    """All / Clear drives the rows, and the rows drive the prose."""
    open_scene(page, server)
    open_scene_analysis(page)

    page.click("#scenePassAllBtn")
    page.wait_for_function(
        "() => [...document.querySelectorAll('#scenePassList .scene-pass-row')]"
        ".every(el => el.getAttribute('aria-pressed') === 'true')"
    )
    assert page.locator("#scenePassAllBtn").inner_text().strip() == "Clear"

    page.click("#scenePassAllBtn")
    page.wait_for_function(
        "() => [...document.querySelectorAll('#scenePassList .scene-pass-row')]"
        ".every(el => el.getAttribute('aria-pressed') === 'false')"
    )
    assert page.locator("#scenePassAllBtn").inner_text().strip() == "All"


def test_managed_skills_come_from_app_server(page: Page, server: ProseviewServer):
    open_scene(page, server)
    open_selection_menu(page, "the slow algebra")

    page.click("#selectionSkillsBtn")
    page.wait_for_function("() => document.querySelectorAll('#discussSkillsPicker .discuss-skill').length >= 2")
    listed = page.locator("#discussSkillsPicker").inner_text()

    assert "Continuity Check" in listed
    assert "Tighten Prose" in listed


# ── agents and terminal ─────────────────────────────────────────────────────


def _terminal_text(page: Page) -> str:
    """Read the visible xterm buffer.

    xterm renders into ``.xterm-rows``, so ``inner_text`` on the panel works --
    but only for rows currently on screen, which is all these tests need.
    """
    return page.locator("#terminalPanel").inner_text()


def open_shell_terminal(page: Page) -> None:
    """Open a shell tab, focus xterm, and wait until the shell can take input.

    Two separate races here. The click is needed because keystrokes otherwise
    go to the document and never reach the PTY. The prompt wait is needed
    because the ``$ Shell`` button spawns a *login interactive* shell: until it
    has finished starting up and drawn a prompt, anything typed is swallowed
    and the test hangs waiting for output that will never come.
    """
    page.click("#sceneMoreBtn")
    page.get_by_role("button", name="Open shell").click()
    page.wait_for_selector("#terminalPanel", state="visible")
    page.wait_for_selector(".terminal-tab-mount .xterm", timeout=20_000)
    page.click(".terminal-tab-mount .xterm-screen")
    _wait_until(
        lambda: any(ch in _terminal_text(page) for ch in ("$", "%", "#")),
        timeout=25,
        message="shell never drew a prompt",
    )


def run_in_terminal(page: Page, command: str, marker: str, attempts: int = 4) -> None:
    """Type *command* into the focused terminal until *marker* comes back.

    xterm wires its ``onData`` handler to ``/terminal-input`` a beat after the
    element appears, so the first keystrokes are occasionally dropped on the
    floor with no error. Retrying is what a user does when nothing echoes, and
    it makes the test deterministic; re-running an ``echo`` is harmless.
    """
    for _ in range(attempts):
        page.keyboard.type(command + "\n")
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            if marker in _terminal_text(page):
                return
            time.sleep(0.15)
    raise AssertionError(f"{marker!r} never appeared after {attempts} attempts")


def test_shell_terminal_opens_and_runs_a_command(page: Page, server: ProseviewServer):
    open_scene(page, server)
    open_shell_terminal(page)

    run_in_terminal(page, "echo proseview-browser-marker", "proseview-browser-marker")

    terminal_input = page.locator(".terminal-tab-mount:not([hidden]) .xterm-helper-textarea")
    assert terminal_input.get_attribute("aria-describedby") == "terminalKeyboardHelp"
    assert terminal_input.get_attribute("aria-keyshortcuts") == "Shift+Tab"
    terminal_input.focus()
    page.keyboard.press("Shift+Tab")
    active_tab = page.locator('#terminalTabs [role="tab"][aria-selected="true"]')
    assert active_tab.evaluate("el => el === document.activeElement")


def test_terminal_session_tabs_are_named_and_keyboard_operable(
    page: Page,
    server: ProseviewServer,
):
    open_scene(page, server)
    page.evaluate(
        """() => {
            const panel = document.getElementById('terminalPanel');
            panel.hidden = false;
            panel.style.display = 'flex';
            const mounts = document.getElementById('terminalMounts');
            const makeSession = (id, label) => {
                const mountEl = document.createElement('div');
                mountEl.className = 'terminal-tab-mount';
                mounts.appendChild(mountEl);
                return {id, label, type: 'shell', termId: null, xterm: null, fit: null,
                        es: null, send: null, contextFile: null, contextSel: null, mountEl};
            };
            _termSessions = [makeSession('keyboard-one', 'Shell 1'),
                             makeSession('keyboard-two', 'Shell 2')];
            _termActiveId = 'keyboard-one';
            _renderTabs();
        }"""
    )

    tabs = page.locator("#terminalTabs").get_by_role("tab")
    assert tabs.count() == 2
    assert tabs.nth(0).get_attribute("aria-selected") == "true"
    tabs.nth(0).focus()
    page.keyboard.press("ArrowRight")
    assert tabs.nth(1).get_attribute("aria-selected") == "true"
    assert tabs.nth(1).evaluate("el => el === document.activeElement")

    tabs.nth(0).locator("xpath=following-sibling::button").focus()
    page.keyboard.press("Enter")
    assert page.locator("#terminalTabs").get_by_role("tab").count() == 1
    assert page.locator("#terminalTabs").get_by_role("tab").evaluate(
        "el => el === document.activeElement"
    )
    close = page.locator("#terminalTabs .terminal-tab-close")
    close_box = close.bounding_box()
    assert close_box and close_box["width"] >= 24 and close_box["height"] >= 24
    page.evaluate(
        "_termReturnFocus = Array.from(document.querySelectorAll('#sceneMoreMenu button'))"
        ".find(button => button.textContent.includes('Shell'))"
    )
    close.focus()
    page.keyboard.press("Enter")
    assert page.locator("#terminalPanel").is_hidden()
    assert page.evaluate("document.activeElement.id") == "sceneMoreBtn"


@pytest.mark.parametrize(("label", "agent"), [("Codex", "codex"), ("Claude", "claude"), ("Gemini", "gemini")])
def test_scene_agent_menu_launches_the_agent(page: Page, server: ProseviewServer, label: str, agent: str):
    """The agent menu spawns the agent's own binary in a terminal tab.

    A stub on PATH stands in for the real tool and announces itself, so this
    proves the click reaches an actual process.
    """
    open_scene(page, server)
    page.click("#sceneMoreBtn")
    page.click("#agentMenuSceneBtn")
    page.wait_for_selector("#agentMenuScene", state="visible")
    page.click(f"#agentMenuScene button:has-text('{label}')")

    page.wait_for_selector("#terminalPanel", state="visible")
    _wait_until(lambda: f"{AGENT_MARKER} {agent}" in _terminal_text(page), timeout=25,
                message=f"{agent} stub never announced itself")

    sessions = server.get_json("/terminal-list")["sessions"]
    assert any(s["type"] == agent for s in sessions)


def _terminal_flat(page: Page) -> str:
    """Terminal text with runs of whitespace collapsed.

    xterm hard-wraps at the column width and each visual row is its own DOM
    node, so a long prompt is split across lines. Collapsing whitespace lets a
    test match the prompt as the user wrote it.
    """
    return " ".join(_terminal_text(page).split())


def test_ask_about_selection_is_normal_chat_and_keeps_context_for_followups(
    page: Page, server: ProseviewServer
):
    quote = "the slow algebra of yesterday's receipts"
    path = server.scene_path()
    before = path.read_bytes()
    open_scene(page, server)
    open_selection_menu(page, quote)

    assert page.locator("#selectionCodexBtn").inner_text() == "Ask about selection"
    page.click("#selectionCodexBtn")
    page.wait_for_selector("#discussPanel", state="visible")
    page.wait_for_function("() => document.querySelector('#discussConnection').innerText.startsWith('Live')")
    assert quote in page.locator("#discussSelectionChip").inner_text()
    assert page.locator("#discussContext .discuss-chip-current").count() == 0
    assert page.locator("#discussInput").get_attribute("placeholder") == "Ask anything about this selection…"
    assert page.locator("#discussSend").inner_text() == "Send"

    page.fill("#discussInput", "Explain how this image affects the voice")
    page.click("#discussSend")
    wait_for_discuss_answer(page)
    assert quote in page.locator("#discussSelectionChip").inner_text()
    assert page.locator(".discuss-task").count() == 0
    assert page.locator(".ai-proposal-panel:visible").count() == 0

    page.fill("#discussInput", "What does it reveal about the narrator?")
    page.click("#discussSend")
    page.wait_for_function("() => document.querySelectorAll('.discuss-message.assistant').length === 2")
    assert quote in page.locator("#discussSelectionChip").inner_text()

    records = [
        json.loads(line)
        for line in (server.home / "fake-codex-received.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    questions = ("Explain how this image affects the voice", "What does it reveal about the narrator?")
    prompts = [
        record["params"]["input"][0]["text"]
        for record in records
        if record["params"].get("input")
        and any(question in record["params"]["input"][0]["text"] for question in questions)
    ]
    assert len(prompts) == 2
    assert all(f"BEGIN USER SELECTION\n{quote}\nEND USER SELECTION" in prompt for prompt in prompts)

    page.locator("#discussSelectionChip button").click()
    assert page.locator("#discussSelectionChip").is_hidden()
    assert page.locator("#discussInput").get_attribute("placeholder") == "Ask anything about your story…"
    assert path.read_bytes() == before


def test_discuss_presets_merge_configured_and_starred_prompts_without_inline_recents(
    page: Page, server: ProseviewServer
):
    page.set_viewport_size({"width": 1024, "height": 768})
    (server.root / ".proseview.yaml").write_text(
        "discuss:\n"
        "  selection_presets:\n"
        "    - Is the grammar correct?\n"
        "    - Make this more direct.\n"
        "    - Check the point of view.\n",
        encoding="utf-8",
    )
    server.restart()
    page.goto(f"{server.base_url}#/scene/{SCENE_REL}", wait_until="load")
    page.evaluate(
        """() => {
            localStorage.setItem('proseview-codex-favorite-instructions', JSON.stringify([
                'Favorite prompt', 'Make this more direct.'
            ]));
            localStorage.setItem('proseview-codex-recent-instructions', JSON.stringify([
                'Recent only prompt', 'Favorite prompt'
            ]));
        }"""
    )
    page.reload(wait_until="load")
    page.wait_for_selector("#sceneProseHost .ProseMirror")
    open_selection_menu(page, "the slow algebra")
    page.click("#selectionCodexBtn")
    page.wait_for_selector("#discussTaskMode:not([hidden])")

    presets = page.locator("#discussTaskMode .discuss-preset-inline")
    assert presets.all_inner_texts() == [
        "Favorite prompt", "Make this more direct.", "Is the grammar correct?"
    ]
    assert "Recent only prompt" not in page.locator("#discussTaskMode").inner_text()

    more = page.get_by_role("button", name="More presets and recent instructions")
    more.focus()
    page.keyboard.press("Enter")
    popover = page.locator("#discussPresetsPopover")
    assert popover.is_visible()
    assert "Check the point of view." in popover.inner_text()
    assert "Recent only prompt" in popover.inner_text()

    popover.get_by_role("button", name="Add to favorites: Recent only prompt").click()
    assert popover.is_visible()
    assert page.evaluate(
        "document.activeElement?.getAttribute('aria-label')"
    ) == "Remove from favorites: Recent only prompt"
    assert page.locator("#discussTaskMode .discuss-preset-inline").all_inner_texts() == [
        "Recent only prompt", "Favorite prompt", "Make this more direct."
    ]
    assert page.evaluate(
        "JSON.parse(localStorage.getItem('proseview-codex-favorite-instructions'))[0]"
    ) == "Recent only prompt"

    page.locator("#discussTaskMode .discuss-preset-inline").first.click()
    assert page.input_value("#discussInput") == "Recent only prompt"
    assert page.locator("#discussTaskMode .discuss-preset-inline").count() == 3
    assert page.locator("#discussTaskMode").bounding_box()["height"] < 45


def test_selection_dock_close_returns_focus_to_visible_selection_trigger(
    page: Page, server: ProseviewServer
):
    open_scene(page, server)
    open_selection_menu(page, "the slow algebra")
    page.click("#selectionCodexBtn")
    page.wait_for_selector("#discussPanel", state="visible")
    assert page.locator("#selectionPillBtn").is_visible()
    page.click(".discuss-close")
    assert page.evaluate("document.activeElement === document.getElementById('selectionPillBtn')")


def test_unsent_selection_instruction_survives_panel_close_and_reload(
    page: Page, server: ProseviewServer
):
    quote = "the slow algebra of yesterday's receipts"
    open_scene(page, server)
    open_selection_menu(page, quote)

    page.click("#selectionCodexBtn")
    page.fill("#discussInput", "Make the waiting feel more ominous")
    page.click(".discuss-close")
    page.evaluate("openDiscuss(document.querySelector('#utilityTabCodex'))")
    assert page.input_value("#discussInput") == "Make the waiting feel more ominous"
    page.reload()
    open_scene(page, server)
    page.evaluate("openDiscuss(document.querySelector('#utilityTabCodex'))")
    assert page.input_value("#discussInput") == "Make the waiting feel more ominous"


def test_selection_quick_flow_never_offers_auto_approve(page: Page, server: ProseviewServer):
    open_scene(page, server)
    open_selection_menu(page, "the slow algebra")

    page.click("#selectionCodexBtn")
    page.wait_for_selector("#discussPanel", state="visible")
    assert page.locator("text=Auto-approve changes").count() == 0
    assert page.locator("#selectionCodexAutoApprove").count() == 0


def test_new_conversation_clears_configured_selection_action_mode(
    page: Page, server: ProseviewServer
):
    open_scene(page, server)
    open_selection_menu(page, "the slow algebra of yesterday's receipts")
    page.click("#selectionRewriteBtn")
    page.click("[data-selection-action='custom_rewrite']")
    page.wait_for_selector("#discussPanel", state="visible")
    page.wait_for_function("() => document.querySelector('#discussConnection').innerText.startsWith('Live')")
    assert "Custom rewrite" in page.locator("#discussTaskMode").inner_text()
    page.fill("#discussInput", "Make the diction more formal")

    page.click("#discussNewConversation")
    page.wait_for_selector("#discussNewConversationDialog", state="visible")
    page.click("#discussNewConversationConfirm")
    page.wait_for_selector("#discussNewConversationDialog", state="hidden")

    assert page.locator("#discussTaskMode").is_hidden()
    assert page.locator("#discussSelectionChip").is_hidden()
    page.fill("#discussInput", "Fresh question after reset")
    page.press("#discussInput", "Enter")
    page.wait_for_function(
        "() => document.querySelector('.discuss-message.user')?.textContent.includes('Fresh question after reset')"
    )


def test_selection_action_started_from_dirty_editor_sends_the_live_target(
    page: Page, server: ProseviewServer
):
    """An action on an unsaved buffer must be about the buffer, not the file."""
    question_requests: list[dict] = []
    page.on(
        "request",
        lambda request: question_requests.append(request.post_data_json)
        if "/api/discuss/conversations/" in request.url and request.url.endswith("/questions")
        else None,
    )
    quote = "the slow algebra of yesterday's receipts"
    open_scene(page, server)
    page.click("#sceneEditBtn")
    page.evaluate(
        """() => {
            _pmView.dispatch(_pmView.state.tr.insertText('Local preface. ', 1));
            setPmDirty(true);
        }"""
    )
    open_selection_menu(page, quote)
    page.click("#selectionRewriteBtn")
    page.click("[data-selection-action='tighten']")

    wait_for_discuss_answer(page, "Fake answer")
    sent = question_requests[-1]
    assert "Local preface." in sent["live_document"]["content"]
    assert sent["selection"] == quote


def test_a_critique_answers_in_the_conversation_and_keeps_its_subject(
    page: Page, server: ProseviewServer
):
    """A critique writes nothing, so it is a message and not a card.

    It also leaves the passage attached: "say more about that" is the obvious
    next move and should not require reselecting the prose.
    """
    quote = "the slow algebra of yesterday's receipts"
    open_scene(page, server)
    open_selection_menu(page, quote)
    page.click("#selectionCritiqueBtn")
    page.click("[data-selection-action='quick_critique']")

    wait_for_discuss_answer(page, "Fake answer")
    assert page.locator(".discuss-task").count() == 0
    asked = page.locator(".discuss-message.user").last.inner_text()
    assert "Critique the provided text" in asked
    assert "Provide a clear suggested fix" in asked
    assert "Selection" in page.locator("#discussSelectionChip").inner_text()

def test_quick_critique_queues_while_another_tab_restores_history(
    page: Page, server: ProseviewServer
):
    """A slow thread/read must not hold the queue endpoint past its deadline."""
    quote = "the slow algebra of yesterday's receipts"
    open_scene(page, server)
    open_discuss(page)
    page.wait_for_function("() => document.querySelector('#discussConnection').innerText.startsWith('Live')")
    page.fill("#discussInput", "Prime this conversation")
    page.press("#discussInput", "Enter")
    wait_for_discuss_answer(page)

    other = page.context.new_page()
    _install_esm_cache(other)
    try:
        with server.hold_codex_request("thread/read") as restore_reached:
            open_scene(other, server)
            open_discuss(other)
            _wait_until(
                restore_reached.exists,
                message="the second tab never began restoring Codex history",
            )

            # Keep this shorter than the held restore. Before the lock fix, the
            # browser aborts this POST and renders the same timeout users saw.
            page.evaluate("window._discussRequestTimeoutMs = 500")
            open_selection_menu(page, quote)
            page.click("#selectionCritiqueBtn")
            page.click("[data-selection-action='quick_critique']")
            page.wait_for_selector(".discuss-message.user", state="visible", timeout=1_500)
            assert page.locator("#discussError", has_text="Request timed out").count() == 0
    finally:
        other.close()

    page.wait_for_function(
        "() => document.querySelectorAll('.discuss-message.assistant').length > 0",
        timeout=15_000,
    )


def test_quick_critique_queues_before_a_slow_codex_turn_starts(
    page: Page, server: ProseviewServer
):
    """Queue acknowledgement is independent of Codex accepting the turn."""
    quote = "the slow algebra of yesterday's receipts"
    open_scene(page, server)

    with server.hold_codex_request("turn/start") as turn_start_reached:
        page.evaluate("window._discussRequestTimeoutMs = 500")
        open_selection_menu(page, quote)
        page.click("#selectionCritiqueBtn")
        page.click("[data-selection-action='quick_critique']")
        _wait_until(
            turn_start_reached.exists,
            message="Quick Critique never reached the Codex turn boundary",
        )
        page.wait_for_selector(".discuss-message.user", state="visible", timeout=1_500)
        assert page.locator("#discussError", has_text="Request timed out").count() == 0

    page.wait_for_function(
        "() => document.querySelectorAll('.discuss-message.assistant').length > 0",
        timeout=15_000,
    )


def test_quick_critique_runs_immediately_after_restart_with_retained_history(
    page: Page, server: ProseviewServer
):
    """A fresh process must retain history without delaying the next action."""
    quote = "the slow algebra of yesterday's receipts"
    open_scene(page, server)
    open_discuss(page)
    page.wait_for_function("() => document.querySelector('#discussConnection').innerText.startsWith('Live')")
    page.fill("#discussInput", "Prime retained history")
    page.press("#discussInput", "Enter")
    wait_for_discuss_answer(page)

    server.restart()
    page.context.new_cdp_session(page).send("Network.setCacheDisabled", {"cacheDisabled": True})
    page.goto("about:blank")
    open_scene(page, server)
    open_selection_menu(page, quote)
    page.click("#selectionCritiqueBtn")
    page.click("[data-selection-action='quick_critique']")

    page.wait_for_function(
        "() => document.querySelectorAll('.discuss-message.assistant').length > 0",
        timeout=15_000,
    )
    assert page.locator("#discussConnection").inner_text().startswith("Live")
    assert page.locator("#discussError").count() == 0
    assert "Fake answer" in page.locator("#discussLog").inner_text()


def test_quick_critique_queues_while_an_active_turn_is_stopping(
    page: Page, server: ProseviewServer
):
    """Stopping one request must not make the next managed action disappear."""
    quote = "the slow algebra of yesterday's receipts"
    open_scene(page, server)
    open_discuss(page)
    page.wait_for_function("() => document.querySelector('#discussConnection').innerText.startsWith('Live')")
    page.fill("#discussInput", "HOLD_FOR_STOP")
    page.press("#discussInput", "Enter")
    page.wait_for_selector("#discussStop", state="visible")

    with server.hold_codex_request("turn/interrupt") as interrupt_reached:
        page.click("#discussStop")
        _wait_until(
            interrupt_reached.exists,
            message="the stop request never reached Codex",
        )
        assert page.locator("#discussStop").inner_text() == "Stopping…"
        page.evaluate("window._discussRequestTimeoutMs = 500")
        open_selection_menu(page, quote)
        page.click("#selectionCritiqueBtn")
        page.click("[data-selection-action='quick_critique']")
        page.wait_for_selector(".discuss-message.user", state="visible", timeout=1_500)
        assert page.locator("#discussError", has_text="Request timed out").count() == 0

    page.wait_for_function(
        "() => document.querySelectorAll('.discuss-message.assistant').length > 0",
        timeout=15_000,
    )
    page.wait_for_selector("#discussStop", state="hidden")


def test_terminal_survives_a_page_reload(page: Page, server: ProseviewServer):
    """A reload must reattach live sessions rather than orphan running agents."""
    open_scene(page, server)
    open_shell_terminal(page)
    run_in_terminal(page, "echo before-reload", "before-reload")

    before = {s["id"] for s in server.get_json("/terminal-list")["sessions"]}
    assert before

    page.reload(wait_until="load")
    page.wait_for_selector("#terminalPanel", state="visible")
    page.wait_for_selector(".terminal-tab-mount .xterm")

    after = {s["id"] for s in server.get_json("/terminal-list")["sessions"]}
    assert before <= after, "reload killed a live terminal session"
    _wait_until(lambda: "before-reload" in _terminal_text(page), timeout=20,
                message="scrollback was not replayed after reload")


# ── AI proposal bridge ──────────────────────────────────────────────────────


QUOTE = "the slow algebra of yesterday's receipts"
REPLACEMENT = "the arithmetic of yesterday's receipts"


def _raise_proposal(page: Page, server: ProseviewServer) -> None:
    server.cli(
        "propose", "--root", str(server.root), "--file", SCENE_REL,
        "--quote", QUOTE, "--message", "Too ornate for a cold open",
        "--option", REPLACEMENT,
    )
    # Pushed over SSE: the panel and the inline decoration appear without a
    # reload. (Playwright timeouts are milliseconds.)
    page.wait_for_selector(".ai-proposal-panel", timeout=20_000)
    page.wait_for_selector(".pm-ai-proposal-highlight", timeout=20_000)


def test_proposal_from_the_cli_is_highlighted_in_the_open_scene(page: Page, server: ProseviewServer):
    open_scene(page, server)
    _raise_proposal(page, server)

    assert QUOTE in page.locator(".pm-ai-proposal-highlight").inner_text()
    assert "Too ornate for a cold open" in page.locator(".ai-proposal-panel").inner_text()


def test_using_a_proposal_applies_the_edit_without_writing_the_file(page: Page, server: ProseviewServer):
    """Accepting is not committing.

    The edit lands in the editor and the file is left alone until the writer
    confirms -- the guarantee that nothing rewrites prose behind their back.
    """
    path = server.scene_path()
    before = path.read_text(encoding="utf-8")

    open_scene(page, server)
    _raise_proposal(page, server)
    page.click(".ai-proposal-panel button:has-text('Use this version')")

    _wait_until(lambda: REPLACEMENT in _editor_text(page),
                message="the replacement never appeared in the editor")
    page.wait_for_timeout(1500)
    assert path.read_text(encoding="utf-8") == before, "accepting wrote to disk without confirmation"


def test_undo_restores_the_original_passage(page: Page, server: ProseviewServer):
    open_scene(page, server)
    _raise_proposal(page, server)
    page.click(".ai-proposal-panel button:has-text('Use this version')")
    _wait_until(lambda: REPLACEMENT in _editor_text(page))

    page.click("button:has-text('Undo')")
    _wait_until(lambda: QUOTE in _editor_text(page),
                message="undo did not restore the original passage")
    assert REPLACEMENT not in _editor_text(page)


def test_proposal_undo_restores_original_inline_emphasis(page: Page, server: ProseviewServer):
    path = open_annotated_scene(page, server)
    original = path.read_text(encoding="utf-8")
    server.cli(
        "propose", "--root", str(server.root), "--file", ANNOTATED_SCENE_REL,
        "--quote", "shop stayed quiet", "--message", "Test marked selection", "--option", "shop fell silent",
    )
    page.wait_for_selector(".ai-proposal-panel", timeout=20_000)
    page.wait_for_selector(".pm-ai-proposal-highlight", timeout=20_000)
    page.click(".ai-proposal-panel button:has-text('Use this version')")
    _wait_until(lambda: "shop fell silent" in _editor_text(page))
    page.click("button:has-text('Undo')")
    _wait_until(lambda: page.locator("#sceneProseHost em", has_text="quiet").count() == 1)
    assert path.read_text(encoding="utf-8") == original


def test_applied_proposal_requires_normal_save_to_reach_disk(page: Page, server: ProseviewServer):
    """The bridge applies locally; only the editor's normal Save writes the file."""
    path = server.scene_path()
    original = path.read_text(encoding="utf-8")

    open_scene(page, server)
    _raise_proposal(page, server)
    page.click(".ai-proposal-panel button:has-text('Use this version')")
    _wait_until(lambda: REPLACEMENT in _editor_text(page))

    page.click("button:has-text('Close')")
    page.wait_for_timeout(500)
    assert path.read_text(encoding="utf-8") == original
    page.click("#sceneProseHost .ProseMirror")
    save_scene(page)
    _wait_until(lambda: REPLACEMENT in path.read_text(encoding="utf-8"), timeout=20,
                message="normal Save did not persist the applied proposal")

    after = path.read_text(encoding="utf-8")
    assert QUOTE not in after
    assert frontmatter(after) == frontmatter(original)


def test_timeline_tab_shows_shape_threads_and_chronology(page: Page, shared_server: ProseviewServer):
    """The three story layers render from frontmatter, and the chronology view
    names the scene that is read out of the order it happens."""
    open_dashboard(page, shared_server)
    page.click('.tab-nav button[data-tab="timeline"]')
    page.wait_for_selector("#tab-timeline.active")

    # Layer 1 is always available: one segment and one bar per scene.
    scenes = page.evaluate("() => storyModel.scenes.length")
    assert scenes > 0
    assert page.locator(".story-seg").count() == scenes
    assert page.locator(".story-barwrap").count() == scenes

    # Layer 2: one lane per thread the fixture seeds.
    lanes = page.locator(".story-lane-row")
    lanes.first.wait_for(state="visible")
    lane_text = page.locator("#timelineContent").inner_text().lower()
    assert "present" in lane_text and "recollection" in lane_text

    # Layer 3: the seeded flashback happens first but is read last.
    assert "reading order vs story order" in lane_text
    assert page.locator(".story-svg").count() == 1
    assert "read far from where they happen" in lane_text
    assert "flashback" in lane_text


def test_timeline_scene_click_opens_the_scene(page: Page, shared_server: ProseviewServer):
    open_dashboard(page, shared_server)
    page.click('.tab-nav button[data-tab="timeline"]')
    page.wait_for_selector("#tab-timeline.active")

    first = page.locator(".story-barwrap[data-scene]").first
    expected = page.evaluate("() => storyModel.scenes[+document.querySelector('.story-barwrap[data-scene]').dataset.scene].path")
    first.click()

    page.wait_for_selector("#sceneModal", state="visible")
    assert expected in page.locator("#modalTitle").inner_text()


def test_every_timeline_scene_mark_is_named_and_keyboard_operable(
    page: Page,
    shared_server: ProseviewServer,
):
    open_dashboard(page, shared_server)
    page.click('.tab-nav button[data-tab="timeline"]')
    page.wait_for_selector("#tab-timeline.active")

    marks = page.locator("#timelineContent [data-scene]")
    assert marks.count() > 0
    states = marks.evaluate_all(
        """items => items.map(item => ({
            role: item.getAttribute('role'),
            name: item.getAttribute('aria-label'),
            tabIndex: item.tabIndex
        }))"""
    )
    assert all(state["role"] == "button" for state in states)
    assert all(state["name"] for state in states)
    assert all(state["tabIndex"] == 0 for state in states)
    assert "percent of manuscript words" in page.locator(".story-seg[data-scene]").first.get_attribute("aria-label")
    assert "words" in page.locator(".story-barwrap[data-scene]").first.get_attribute("aria-label")
    assert "storyline" in page.locator(".story-slot[data-scene]").first.get_attribute("aria-label")
    assert "order position" in page.locator(".story-node[data-scene]").first.get_attribute("aria-label")

    first = marks.first
    first.focus()
    page.keyboard.press("Enter")
    page.wait_for_selector("#sceneModal", state="visible")


def test_timeline_says_what_is_missing_rather_than_guessing(page: Page, shared_server: ProseviewServer):
    """A manuscript with no story fields still gets the shape view, and the
    other two layers name the field they would need instead of guessing."""
    open_dashboard(page, shared_server)
    page.click('.tab-nav button[data-tab="timeline"]')
    page.wait_for_selector("#tab-timeline.active")

    # Re-render against a model with no thread or day data, which is what an
    # untagged manuscript produces (proseview/story.py decides that; this
    # asserts what the renderer does with it).
    page.evaluate("""() => {
        storyModel.threads = [];
        storyModel.has_threads = false;
        storyModel.has_chronology = false;
        _timelineBuilt = false;
        buildTimelineTab();
    }""")

    text = page.locator("#timelineContent").inner_text().lower()
    assert "proportion of the book" in text, "the shape layer must survive with no story fields"
    assert "no storylines yet" in text
    assert "thread" in text, "the empty state names the field to add"
    assert "no chronology yet" in text
    assert page.locator(".story-lane-row").count() == 0
    assert page.locator(".story-svg").count() == 0
    # Still navigable: the shape layer keeps its per-scene marks.
    assert page.locator(".story-barwrap").count() > 0


def test_timeline_hover_shows_a_scene_card(page: Page, shared_server: ProseviewServer):
    """Hovering a scene mark shows its title, metadata, and what happens in it."""
    open_dashboard(page, shared_server)
    page.click('.tab-nav button[data-tab="timeline"]')
    page.wait_for_selector("#tab-timeline.active")

    card = page.locator("#storyCard")
    assert card.count() == 0, "the card is created on first hover, not up front"

    page.locator(".story-barwrap[data-scene]").first.hover()
    page.wait_for_selector("#storyCard.on")

    text = card.inner_text()
    expected = page.evaluate(
        "() => storyModel.scenes[+document.querySelector('.story-barwrap[data-scene]').dataset.scene]")
    assert expected["title"] in text
    assert "words" in text
    if expected["blurb"]:
        assert expected["blurb"][:40] in text

    # It goes away again, and never covers what it describes.
    page.locator("#timelineContent .story-h").first.hover()
    page.wait_for_function("() => !document.getElementById('storyCard').classList.contains('on')")


def test_timeline_hover_card_reaches_the_chronology_blocks(page: Page, shared_server: ProseviewServer):
    open_dashboard(page, shared_server)
    page.click('.tab-nav button[data-tab="timeline"]')
    page.wait_for_selector(".story-svg")

    page.locator(".story-node[data-scene]").first.hover()
    page.wait_for_selector("#storyCard.on")
    assert page.locator("#storyCard").inner_text().strip() != ""


def test_timeline_shows_untagged_scenes_as_their_own_lane(page: Page, shared_server: ProseviewServer):
    """Untagged scenes are a state, not a gap.

    Drawn only as holes in the real lanes they read as a rendering fault, so
    they get a lane of their own that says how many and which.
    """
    open_dashboard(page, shared_server)
    page.click('.tab-nav button[data-tab="timeline"]')
    page.wait_for_selector("#tab-timeline.active")

    untagged = page.evaluate("() => storyModel.scenes.filter(s => !s.thread).length")
    assert untagged > 0, "the fixture must have some untagged scenes for this to mean anything"

    row = page.locator(".story-lane-untagged")
    row.wait_for(state="visible")
    assert str(untagged) in row.inner_text()
    assert storyModel_field(page) in row.inner_text()
    # Its marks are real scenes: hoverable and clickable like any other.
    assert page.locator(".story-lane-untagged .story-slot.none[data-scene]").count() == untagged

    page.locator(".story-lane-untagged .story-slot.none[data-scene]").first.hover()
    page.wait_for_selector("#storyCard.on")


def storyModel_field(page: Page) -> str:
    return page.evaluate("() => storyModel.thread_field")


def test_timeline_hides_the_untagged_lane_when_everything_is_tagged(page: Page, shared_server: ProseviewServer):
    open_dashboard(page, shared_server)
    page.click('.tab-nav button[data-tab="timeline"]')
    page.wait_for_selector("#tab-timeline.active")

    page.evaluate("""() => {
        storyModel.scenes.forEach(s => { s.thread = s.thread || 'present'; });
        _timelineBuilt = false;
        buildTimelineTab();
    }""")

    assert page.locator(".story-lane-untagged").count() == 0
    assert "every scene belongs to a storyline" in page.locator("#timelineContent").inner_text().lower()


# ── dock scope, dock seam, toolbar overflow ─────────────────────────────────


def test_leaving_a_scene_closes_the_dock(page: Page, shared_server: ProseviewServer):
    """Three of the four tabs describe the document that just closed, so there
    is nothing left worth showing -- and closing costs nothing, because a
    terminal session is hidden rather than killed."""
    open_scene(page, shared_server)
    open_scene_details(page)
    assert page.locator("#sceneDetailsPane").is_visible()

    page.click(".scene-back-btn")
    page.wait_for_selector("#sceneModal", state="hidden")

    page.wait_for_selector("#discussPanel", state="hidden")
    assert page.locator("#terminalPanel").is_hidden()
    for tab in ("#utilityTabScene", "#utilityTabAnalysis", "#utilityTabCodex"):
        assert page.locator(tab).is_hidden(), tab


@POSIX_ONLY_BROWSER
def test_leaving_a_scene_never_spawns_a_terminal(page: Page, shared_server: ProseviewServer):
    """The regression this replaces: falling back to the Terminal tab called
    ``showRightTerminal``, which spawns a shell when none is running. Clicking
    "Dashboard" created a PTY process nobody asked for."""
    open_scene(page, shared_server)
    open_scene_details(page)
    assert page.evaluate("() => _termSessions.length") == 0

    page.click(".scene-back-btn")
    page.wait_for_selector("#discussPanel", state="hidden")

    assert page.evaluate("() => _termSessions.length") == 0, "navigation spawned a shell"


@POSIX_ONLY_BROWSER
def test_closing_the_dock_hides_a_running_shell_rather_than_killing_it(
    page: Page, shared_server: ProseviewServer
):
    """This is why closing is safe. The session survives and comes back."""
    open_scene(page, shared_server)
    page.evaluate("() => showRightTerminal()")
    page.wait_for_function("() => _termSessions.length === 1")

    page.click(".scene-back-btn")
    page.wait_for_selector("#terminalPanel", state="hidden")
    assert page.evaluate("() => _termSessions.length") == 1, "closing the dock killed the shell"

    # The dashboard button brings the same session back, without spawning another.
    page.click("#dashboardPanelBtn")
    page.wait_for_selector("#terminalPanel:not([hidden])")
    assert page.evaluate("() => _termSessions.length") == 1


@POSIX_ONLY_BROWSER
def test_the_dashboard_panel_button_opens_the_terminal_on_purpose(
    page: Page, shared_server: ProseviewServer
):
    """An explicit click is consent, unlike navigation: opening -- and spawning
    -- a shell here is exactly what was asked for."""
    open_dashboard(page, shared_server)
    button = page.locator("#dashboardPanelBtn")
    assert button.count() == 1 and button.is_visible()

    button.click()
    page.wait_for_selector("#terminalPanel:not([hidden])")
    button.click()
    page.wait_for_selector("#terminalPanel", state="hidden")


def test_leaving_a_scene_does_not_forget_the_preferred_dock_tab(
    page: Page, shared_server: ProseviewServer
):
    """Closing the dock is not a tab choice, so the stored tab is untouched and
    the pane comes straight back on the next scene."""
    open_scene(page, shared_server)
    open_scene_details(page)
    assert page.evaluate("() => localStorage.getItem('proseview-scene-panel-tab')") == "scene"

    page.click(".scene-back-btn")
    page.wait_for_selector("#discussPanel", state="hidden")
    assert page.evaluate("() => localStorage.getItem('proseview-scene-panel-tab')") == "scene"

    # Reopening a scene and the dock brings the remembered tab back.
    open_scene(page, shared_server)
    page.evaluate("() => toggleScenePanel(null)")
    page.wait_for_selector("#sceneDetailsPane:not([hidden])")
    assert page.locator("#utilityTabScene").is_visible()


def test_dock_resize_bar_sits_on_the_seam_not_inside_the_dock(
    page: Page, shared_server: ProseviewServer
):
    """The handle is an in-flow flex child, so the offset the absolutely
    positioned handles use pushed its bar 10px inside the dock."""
    open_scene(page, shared_server)
    open_scene_details(page)

    edges = page.evaluate("""() => {
        const panel = document.getElementById('discussPanel');
        const handle = document.getElementById('discussResizeHandle');
        const bar = getComputedStyle(handle, '::after');
        return {
            panelLeft: panel.getBoundingClientRect().left,
            handleLeft: handle.getBoundingClientRect().left,
            barOffset: parseFloat(bar.left),
            barWidth: parseFloat(bar.width),
        };
    }""")
    bar_left = edges["handleLeft"] + edges["barOffset"]
    bar_centre = bar_left + edges["barWidth"] / 2
    # The bar straddles the dock's own left edge rather than sitting inside it.
    assert abs(bar_centre - edges["panelLeft"]) <= 2.5, edges


def test_the_dashboard_toolbar_never_overflows_its_container(
    page: Page, shared_server: ProseviewServer
):
    """The stacking breakpoints key off the viewport, but the space this row
    has is the viewport minus the sidebar minus the dock. Wrapping is driven by
    the real width, so the theme toggle stays inside the body at every size."""
    open_dashboard(page, shared_server)

    for width, height in ((1600, 900), (1280, 900), (1100, 900), (1000, 900)):
        page.set_viewport_size({"width": width, "height": height})
        page.wait_for_timeout(120)
        overflow = page.evaluate("""() => {
            const body = document.body;
            const limit = body.getBoundingClientRect().right
                - parseFloat(getComputedStyle(body).paddingRight);
            const toggle = document.getElementById('themeToggle');
            return {
                over: Math.round(toggle.getBoundingClientRect().right - limit),
                pageScroll: document.documentElement.scrollWidth
                    - document.documentElement.clientWidth,
            };
        }""")
        assert overflow["over"] <= 1, f"theme toggle overflows at {width}px: {overflow}"
        assert overflow["pageScroll"] <= 1, f"page scrolls sideways at {width}px: {overflow}"


def test_scene_card_shows_the_story_fields_when_present(page: Page, shared_server: ProseviewServer):
    """A scene's storyline and day belong on the scene card, not only in the
    Timeline, and are labelled with the keys this repo actually uses."""
    rel, thread, day = STORY_SCENES[0]
    open_scene(page, shared_server, rel.split("manuscript/")[-1] if "manuscript/" in rel else rel)
    open_scene_details(page)

    card = page.locator(".scene-card").inner_text().lower()
    assert thread in card
    assert str(day) in card
    assert page.evaluate("() => storyModel.thread_field") in card
    assert page.evaluate("() => storyModel.day_field") in card


def test_scene_card_omits_story_rows_when_the_scene_has_none(page: Page, shared_server: ProseviewServer):
    """A manuscript that does not use these fields sees no row at all, rather
    than a line of 'Unknown' for something it never opted into."""
    open_scene(page, shared_server, SCENE_REL)
    open_scene_details(page)

    card = page.locator(".scene-card").inner_text().lower()
    thread_field = page.evaluate("() => storyModel.thread_field")
    assert thread_field not in card
    # The fields this scene *does* set are untouched.
    assert "pov" in card and "when" in card and "goal" in card


def test_scene_card_with_no_frontmatter_explains_instead_of_showing_unknowns(
    page: Page, shared_server: ProseviewServer
):
    """Plain Markdown -- an Obsidian vault, an imported draft -- is the case
    the panel used to handle worst.

    Every story row fell back to "Unknown" or "Not defined", so a writer who
    had simply never opted into frontmatter saw seven rows of nothing and read
    the panel as broken. Now the rows are dropped and one hint takes their
    place.
    """
    open_scene(page, shared_server, BARE_SCENE_REL)
    open_scene_details(page)

    card = page.locator(".scene-card").inner_text().lower()
    assert "unknown" not in card
    assert "not defined" not in card

    # The empty-state hint replaces them, and names the fields to add.
    hint = page.locator(".scene-card-fm-empty")
    assert hint.count() == 1
    hint_text = hint.inner_text().lower()
    assert "no scene details yet" in hint_text
    for field in ("characters", "where", "goal", "conflict", "outcome"):
        assert field in hint_text
    # The arc column is gone entirely rather than rendered empty.
    assert page.locator(".scene-card-arc").count() == 0


def test_add_frontmatter_button_writes_the_block_and_the_panel_fills_in(
    page: Page, server: ProseviewServer
):
    """The offer has to actually land on disk, and the panel has to change.

    Uses the per-test ``server`` fixture rather than the shared one, because it
    writes into the manuscript.
    """
    scene = server.root / "manuscript" / BARE_SCENE_REL
    assert not scene.read_text(encoding="utf-8").startswith("---")

    open_scene(page, server, BARE_SCENE_REL)
    open_scene_details(page)
    page.click(".scene-card-fm-add")

    # The button reloads the page once the write lands.
    page.wait_for_function(
        "() => !document.querySelector('.scene-card-fm-add')", timeout=10_000
    )

    written = scene.read_text(encoding="utf-8")
    assert written.startswith("---\n")
    assert "goal:\n" in written and "characters:\n" in written
    # Keys only: nothing was guessed on the writer's behalf.
    assert "goal: " not in written


def test_add_frontmatter_button_is_absent_when_a_scene_already_has_one(
    page: Page, shared_server: ProseviewServer
):
    """Nothing to offer, so nothing is offered."""
    open_scene(page, shared_server, SCENE_REL)
    open_scene_details(page)
    assert page.locator(".scene-card-fm-add").count() == 0


def test_scene_card_with_no_frontmatter_keeps_what_does_not_need_it(
    page: Page, shared_server: ProseviewServer
):
    """The file path and the related-docs column come from the file itself, so
    they survive when the frontmatter does not."""
    open_scene(page, shared_server, BARE_SCENE_REL)
    open_scene_details(page)

    card = page.locator(".scene-card")
    assert card.count() == 1
    assert "04-bare.md" in card.inner_text()
    assert page.locator(".scene-card-related").count() == 1


def test_timeline_names_a_bare_chapter_number(page: Page, shared_server: ProseviewServer):
    """A frontmatter `chapter: 2` renders as "Chapter 2", while a value that
    already names itself is left alone."""
    open_dashboard(page, shared_server)
    page.click('.tab-nav button[data-tab="timeline"]')
    page.wait_for_selector("#tab-timeline.active")

    labels = page.evaluate(
        "() => ['2', 2, 'ch00-prolog', 'Chapter 3', ''].map(v => _storyChapterLabel(v))")

    assert labels == ["Chapter 2", "Chapter 2", "ch00-prolog", "Chapter 3", ""]

def test_scroll_position_preserved_on_save(page: Page, server: ProseviewServer):
    # Use the standard scene, we'll force it to be scrollable
    open_scene(page, server, SCENE_REL)
    enter_edit_mode(page)
    
    # Wait for editor to render fully
    page.wait_for_selector(".ProseMirror")
    page.wait_for_timeout(500)
    
    # Force the container to be tall enough to scroll
    page.evaluate("document.querySelector('#sceneModal .modal-content').style.paddingBottom = '3000px'")
    
    # Scroll down to ensure we are not at the top
    page.mouse.wheel(0, 500)
    page.wait_for_timeout(200)
    
    # Verify we scrolled
    scroll_before = page.evaluate("document.querySelector('#sceneModal .modal-content').scrollTop")
    assert scroll_before > 0, "Failed to scroll"
    
    # Append text and save without moving cursor
    page.evaluate("window._pmDirty = true; window.contents = window.contents || {}; window.contents[Object.keys(window.contents)[0]] += ' test';")
    with page.expect_response("**/save-scene*"):
        save_scene(page)
        
    # Wait for save state to settle
    page.wait_for_timeout(1000)
    
    # Verify scroll is preserved
    scroll_after = page.evaluate("document.querySelector('#sceneModal .modal-content').scrollTop")
    
    # Allow 1px difference for browser sub-pixel rendering or exact match
    assert abs(scroll_after - scroll_before) <= 1, f"Scroll jumped! Before: {scroll_before}, After: {scroll_after}"
@pytest.mark.e2e_browser
def test_external_change_highlight(page: Page, server: ProseviewServer):
    open_scene(page, server, SCENE_REL)
    
    # Wait for the scene to load
    page.wait_for_selector(".ProseMirror")
    
    # Give the app a moment to settle
    page.wait_for_timeout(500)
    
    # Get the file path
    abs_path = server.scene_path(SCENE_REL)
    
    # Read the file
    content = abs_path.read_text()
        
    # Append a new paragraph externally
    abs_path.write_text(content + "\n\nThis is a brand new externally added paragraph.")
        
    # Wait for the frontend to reload the file via SSE
    # The new paragraph should get the highlight class
    page.wait_for_selector(".ProseMirror > p.external-change-highlight")
    
    highlighted_paras = page.locator(".ProseMirror > p.external-change-highlight").all()
    assert len(highlighted_paras) > 0, "No paragraphs were highlighted"
    
    # Verify the highlighted paragraph contains our text
    text = highlighted_paras[-1].inner_text()
    assert "brand new externally added paragraph" in text, f"Text was: {text}"

def test_editor_list_and_quote_formatting(page: Page, server: ProseviewServer):
    open_scene(page, server)
    page.wait_for_selector(".ProseMirror")
    
    # Empty editor, type something
    page.click(".ProseMirror")
    page.keyboard.type("List item")
    
    # Select text
    page.keyboard.press("Shift+ArrowLeft")
    page.keyboard.press("Shift+ArrowLeft")
    
    # Reveal toolbar if hidden
    page.click("#sceneToolbarReveal", force=True)
    
    # Click bullet list
    page.click("button[aria-label='Bullet List']")
    page.wait_for_selector(".ProseMirror ul li")
    assert page.locator(".ProseMirror ul li").count() > 0, "Bullet list was not created"
    
    # Click ordered list
    page.click("button[aria-label='Numbered List']")
    page.wait_for_selector(".ProseMirror ol li")
    assert page.locator(".ProseMirror ol li").count() > 0, "Ordered list was not created"
    
    # Click quote
    page.click("button[aria-label='Quote']")
    page.wait_for_selector(".ProseMirror blockquote")
    assert page.locator(".ProseMirror blockquote").count() > 0, "Blockquote was not created"

def test_editor_list_enter_splits_item(page: Page, server: ProseviewServer):
    open_scene(page, server)
    page.wait_for_selector(".ProseMirror")
    
    page.click(".ProseMirror")
    page.keyboard.type("List item 1")
    
    page.keyboard.press("Shift+ArrowLeft")
    page.keyboard.press("Shift+ArrowLeft")
    
    page.click("#sceneToolbarReveal", force=True)
    page.click("button[aria-label='Bullet List']")
    page.wait_for_selector(".ProseMirror ul li")
    
    # Go to end of the line
    page.evaluate("() => { const sel = window.getSelection(); sel.modify('move', 'forward', 'lineboundary'); }")
    
    # Press Enter
    page.keyboard.press("Enter")
    page.keyboard.type("List item 2")
    
    # Verify there are two list items
    page.wait_for_function("() => document.querySelectorAll('.ProseMirror ul li').length === 2")
    list_items = page.locator(".ProseMirror ul li").all_inner_texts()
    assert len(list_items) == 2

def test_discuss_panel_state_preserved_on_reload(page: Page, server: ProseviewServer):
    open_scene(page, server, SCENE_REL)
    
    # Open the discuss panel
    page.locator(".scene-toolbar-button.discuss-open-btn").click()
    page.locator("#utilityTabCodex").click()
    page.wait_for_selector("#discussPanel:not([hidden])")
    
    # Reload the page
    page.reload()
    
    # Verify the discuss panel opens automatically
    page.wait_for_selector("#discussPanel:not([hidden])")
    
    # Close the panel
    page.locator(".discuss-close").click()
    page.wait_for_selector("#discussPanel", state="hidden")
    
    # Reload the page
    page.reload()
    
    # Verify it stays closed
    page.wait_for_selector("#discussPanel", state="hidden")
    page.wait_for_timeout(500) # Give it some time to ensure it doesn't pop open
    assert page.locator("#discussPanel").is_hidden(), "Discuss panel should remain closed after reload"
