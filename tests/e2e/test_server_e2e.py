"""End-to-end tests against a running Proseview server.

Every test here talks to a real ``python -m proseview`` subprocess over HTTP and
asserts on observable effects -- bytes on disk, SSE frames, PTY output -- rather
than on status codes alone. See ``conftest.py`` for the harness.

No browser is involved; the browser-only surface lives in ``test_browser_e2e.py``.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from pathlib import Path

import pytest

from .conftest import (
    AGENT_MARKER,
    ANNOTATED_SCENE_REL,
    BARE_SCENE_REL,
    LARGE_SCENE_REL,
    SCENE_REL,
    ProseviewServer,
)

POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="PTY terminals are POSIX-only")


def _discuss_token(server: ProseviewServer) -> str:
    match = re.search(r"const pageSessionToken = \"([a-f0-9]+)\";", server.get("/").text)
    assert match, "page session token was not embedded"
    return match.group(1)


def _discuss_headers(server: ProseviewServer) -> dict[str, str]:
    return {"X-Proseview-Session": _discuss_token(server), "Origin": server.base_url}


def _wait_discuss(server: ProseviewServer, conversation_id: str, predicate, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = server.get_json(f"/api/discuss/conversations/{conversation_id}/snapshot")["snapshot"]
        if predicate(snapshot):
            return snapshot
        time.sleep(0.03)
    raise AssertionError("Discuss snapshot did not reach expected state")


def _frontmatter(text: str) -> str:
    """The leading ``---`` block, or a clear failure if the save dropped it."""
    match = re.match(r"^---\n.*?\n---\n", text, re.DOTALL)
    assert match, f"scene file has no frontmatter block; starts with {text[:80]!r}"
    return match.group(0)


def _line_col(text: str, index: int) -> tuple[int, int]:
    """Line and column for *index*, both 1-based.

    Matches ``server._line_col_to_offset``, which rejects anything below 1.
    """
    line = text.count("\n", 0, index) + 1
    line_start = text.rfind("\n", 0, index) + 1
    return line, index - line_start + 1


# ── boot and discovery ──────────────────────────────────────────────────────


def test_dashboard_renders_every_fixture_scene(shared_server: ProseviewServer):
    resp = shared_server.get("/")
    assert resp.status == 200
    html = resp.text
    # Titles come from frontmatter, so their presence proves the scene pipeline
    # ran end to end, not merely that a template rendered.
    assert "Opening Ledger" in html
    assert "Long Haul" in html
    assert "Annotated Ledger" in html


def test_runtime_file_advertises_this_server(shared_server: ProseviewServer):
    """``proseview propose`` finds the server through this file."""
    payload = json.loads((shared_server.root / ".proseview" / "server.json").read_text())
    assert payload["url"] == shared_server.base_url
    assert Path(payload["repo_root"]).resolve() == shared_server.root.resolve()


def test_discuss_http_flow_is_document_aware_private_and_idempotent(server: ProseviewServer, fake_home: Path):
    headers = _discuss_headers(server)
    # Explicitly tokenless: the harness attaches a valid token by default, and
    # this case exists to prove the server refuses a request without one.
    denied = server.post_json(
        "/api/discuss/conversations/open",
        {"kind": "scene", "path": SCENE_REL},
        headers={"X-Proseview-Session": ""},
    )
    assert denied.status == 403

    opened = server.post_json(
        "/api/discuss/conversations/open",
        {"kind": "scene", "path": SCENE_REL},
        headers=headers,
    )
    assert opened.status == 200
    conversation_id = opened.json()["conversation_id"]
    payload = {
        "client_request_id": "http-one",
        "question": "Explain the ledger",
        "selection": "selection sentinel",
        "attachments": [{"kind": "file", "path": "plans/book-plan.md"}],
        "include_current_document": True,
    }
    first = server.post_json(f"/api/discuss/conversations/{conversation_id}/questions", payload, headers=headers)
    duplicate = server.post_json(f"/api/discuss/conversations/{conversation_id}/questions", payload, headers=headers)
    assert first.status == duplicate.status == 202
    assert first.json()["client_request_id"] == duplicate.json()["client_request_id"]

    snapshot = _wait_discuss(server, conversation_id, lambda value: any(m["role"] == "assistant" for m in value["messages"]))
    serialized = json.dumps(snapshot)
    assert "Fake answer" in serialized
    assert "Reviewing the attached document" in serialized
    assert "PRIVATE RAW REASONING" not in serialized
    assert len([m for m in snapshot["messages"] if m["role"] == "user"]) == 1

    records = [json.loads(line) for line in (fake_home / "fake-codex-received.jsonl").read_text(encoding="utf-8").splitlines()]
    prompt = records[-1]["params"]["input"][0]["text"]
    assert "Opening Ledger" in prompt
    assert "selection sentinel" in prompt
    assert "book-plan.md" in prompt
    assert "Explain the ledger" in prompt
    assert records[-1]["params"]["sandboxPolicy"] == {"type": "workspaceWrite", "networkAccess": False}


def test_the_action_list_carries_the_wording_the_writer_owns(server: ProseviewServer):
    """The panel asks the server what its buttons say, and the answer is a file.

    Rewriting the skill in the repository has to reach the running server: the
    skills directory is inside ``.proseview``, which the watcher ignores, so
    nothing invalidates a cache on the writer's behalf.
    """
    headers = _discuss_headers(server)
    actions = server.post_json("/api/discuss/actions", {}, headers=headers).json()["actions"]
    by_id = {row["id"]: row for row in actions}
    assert by_id["quick_critique"]["label"] == "Quick critique"
    assert by_id["quick_critique"]["scene_pass"] is True
    shipped = by_id["quick_critique"]["description"]
    assert shipped and "Five things" not in shipped
    assert by_id["canon_refactor"]["description"]

    skill = server.root / ".proseview" / "skills" / "quick_critique" / "SKILL.md"
    assert shipped in skill.read_text(encoding="utf-8")
    skill.write_text(
        "---\nname: quick_critique\ndescription: Whatever I decide it is.\n---\n\nBody.\n",
        encoding="utf-8",
    )

    reread = server.post_json("/api/discuss/actions", {}, headers=headers).json()["actions"]
    assert {row["id"]: row for row in reread}["quick_critique"]["description"] == "Whatever I decide it is."


def test_discuss_model_choice_reaches_the_agent_and_defaults_to_its_own_configuration(
    server: ProseviewServer, fake_home: Path
):
    """The picker's promise, end to end: nothing sent until something is pinned."""
    headers = _discuss_headers(server)
    catalog = server.post_json("/api/discuss/models", {"agent": "codex"}, headers=headers).json()["catalog"]
    assert [row["id"] for row in catalog["models"]] == ["gpt-5.6-sol", "gpt-5.6-luna"]
    assert catalog["default"] == {
        "model": "gpt-5.6-sol",
        "effort": "xhigh",
        "source": "Codex settings (~/.codex/config.toml)",
        "label": "GPT-5.6-Sol",
    }
    # Luna's ladder stops short of Sol's, which is what makes the effort row
    # a property of the selected model rather than a fixed list.
    assert [e["id"] for e in catalog["models"][1]["efforts"]] == ["low", "medium", "high"]

    conversation_id = server.post_json(
        "/api/discuss/conversations/open", {"kind": "scene", "path": SCENE_REL}, headers=headers
    ).json()["conversation_id"]

    server.post_json(
        f"/api/discuss/conversations/{conversation_id}/questions",
        {"client_request_id": "unpinned", "question": "Ask on the default"},
        headers=headers,
    )
    _wait_discuss(server, conversation_id, lambda value: any(m["role"] == "assistant" for m in value["messages"]))
    records = [json.loads(line) for line in (fake_home / "fake-codex-received.jsonl").read_text(encoding="utf-8").splitlines()]
    assert "model" not in records[-1]["params"]
    assert "effort" not in records[-1]["params"]

    refused = server.post_json(
        f"/api/discuss/conversations/{conversation_id}/model",
        {"model": "gpt-5.6-luna", "effort": "turbo"},
        headers=headers,
    )
    assert refused.status == 400

    pinned = server.post_json(
        f"/api/discuss/conversations/{conversation_id}/model",
        {"model": "gpt-5.6-luna", "effort": "high"},
        headers=headers,
    )
    assert pinned.status == 200
    assert pinned.json()["model"] == {"model": "gpt-5.6-luna", "effort": "high"}
    snapshot = server.get_json(f"/api/discuss/conversations/{conversation_id}/snapshot")["snapshot"]
    assert snapshot["model"] == {"model": "gpt-5.6-luna", "effort": "high"}

    server.post_json(
        f"/api/discuss/conversations/{conversation_id}/questions",
        {"client_request_id": "pinned", "question": "Ask on Luna"},
        headers=headers,
    )
    _wait_discuss(
        server, conversation_id,
        lambda value: len([m for m in value["messages"] if m["role"] == "assistant"]) == 2,
    )
    records = [json.loads(line) for line in (fake_home / "fake-codex-received.jsonl").read_text(encoding="utf-8").splitlines()]
    assert records[-1]["params"]["model"] == "gpt-5.6-luna"
    assert records[-1]["params"]["effort"] == "high"


def test_a_preset_may_write_only_when_it_was_asked_to_change_something(server: ProseviewServer, fake_home: Path):
    headers = _discuss_headers(server)
    opened = server.post_json(
        "/api/discuss/conversations/open", {"kind": "scene", "path": SCENE_REL}, headers=headers
    )
    conversation_id = opened.json()["conversation_id"]
    quote = "the slow algebra of yesterday's receipts"
    # Every preset is an ordinary question now. A rewrite edits the scene, so
    # its turn is sandboxed to allow it; a pass that exists to read and report
    # cannot write even if it decided to try.
    rewrites = ["rephrase", "tighten", "clarify", "sensory_detail", "show_moment", "custom_rewrite"]
    readings = ["quick_critique", "voice_character", "pacing_tension", "clarity_flow", "continuity"]
    actions = rewrites + readings
    for index, action_id in enumerate(actions):
        response = server.post_json(
            f"/api/discuss/conversations/{conversation_id}/questions",
            {
                "client_request_id": f"preset-{index}", "question": "", "selection": quote,
                "action_id": action_id,
                "custom_instruction": "Make the diction more formal" if action_id == "custom_rewrite" else "",
            },
            headers=headers,
        )
        assert response.status == 202, response.text
        if index in {4, 8}:
            _wait_discuss(
                server, conversation_id,
                lambda value, expected=index: len([
                    m for m in value["messages"] if m["role"] == "assistant"
                ]) >= expected,
                timeout=15.0,
            )

    snapshot = _wait_discuss(
        server, conversation_id,
        lambda value: len([m for m in value["messages"] if m["role"] == "assistant"]) == len(actions),
        timeout=20.0,
    )
    assert snapshot["tasks"] == []
    records = [json.loads(line) for line in (fake_home / "fake-codex-received.jsonl").read_text(encoding="utf-8").splitlines()]
    preset_records = [row for row in records if str(row["params"].get("clientUserMessageId", "")).startswith("preset-")]
    assert len(preset_records) == len(actions)
    assert not any("outputSchema" in row["params"] for row in preset_records)
    sandbox_by_request = {
        str(row["params"]["clientUserMessageId"]): row["params"]["sandboxPolicy"]["type"]
        for row in preset_records
    }
    assert all(sandbox_by_request[f"preset-{i}"] == "workspaceWrite" for i in range(len(rewrites)))
    assert all(
        sandbox_by_request[f"preset-{i}"] == "readOnly"
        for i in range(len(rewrites), len(actions))
    )
    assert all(row["params"]["networkAccess"] is False for row in preset_records
               if "networkAccess" in row["params"])


def test_continuity_refactor_http_flow_scans_without_writing_and_hands_off_one_proposal(server: ProseviewServer):
    headers = _discuss_headers(server)
    opened = server.post_json(
        "/api/discuss/conversations/open", {"kind": "scene", "path": SCENE_REL}, headers=headers
    ).json()
    conversation_id = opened["conversation_id"]
    before = server.scene_path().read_bytes()

    submitted = server.post_json(
        f"/api/discuss/conversations/{conversation_id}/questions",
        {
            "client_request_id": "canon-http-1",
            "question": "Rena changed the safe code this spring.",
            "action_id": "canon_refactor",
        },
        headers=headers,
    )
    assert submitted.status == 202, submitted.text
    task_id = submitted.json()["task_id"]
    snapshot = _wait_discuss(
        server,
        conversation_id,
        lambda value: any(task["id"] == task_id and task["status"] == "ready" for task in value["tasks"]),
    )
    task = next(task for task in snapshot["tasks"] if task["id"] == task_id)
    finding = task["result"]["findings"][0]
    assert task["scope"]["files_scanned"] >= 4
    assert finding["file"] == "manuscript/ch01/01-opening.md"
    assert server.scene_path().read_bytes() == before

    intentional = server.post_json(
        f"/api/discuss/conversations/{conversation_id}/tasks/{task_id}/findings/{finding['id']}/decision",
        {"decision": "intentional"},
        headers=headers,
    )
    assert intentional.status == 200
    assert intentional.json()["decision"] == "intentional"

    proposal = server.post_json(
        f"/api/discuss/conversations/{conversation_id}/tasks/{task_id}/findings/{finding['id']}/proposal",
        {},
        headers=headers,
    )
    assert proposal.status == 200, proposal.text
    assert proposal.json()["proposal"]["origin"] == "managed_continuity_refactor"
    assert proposal.json()["proposal"]["file"] == SCENE_REL
    assert server.scene_path().read_bytes() == before

    repeated = server.post_json(
        f"/api/discuss/conversations/{conversation_id}/tasks/{task_id}/findings/{finding['id']}/proposal",
        {},
        headers=headers,
    )
    assert repeated.status == 200, repeated.text
    assert repeated.json()["proposal"]["id"] == proposal.json()["proposal"]["id"]

    verification = server.post_json(
        f"/api/discuss/conversations/{conversation_id}/questions",
        {
            "client_request_id": "canon-http-verify",
            "question": "",
            "action_id": "verify_refactor",
            "verify_of_task_id": task_id,
        },
        headers=headers,
    )
    assert verification.status == 202, verification.text
    verify_id = verification.json()["task_id"]
    verified = _wait_discuss(
        server,
        conversation_id,
        lambda value: any(task["id"] == verify_id and task["status"] == "ready" for task in value["tasks"]),
    )
    verify_task = next(task for task in verified["tasks"] if task["id"] == verify_id)
    assert verify_task["verify_of"] == task_id
    assert server.scene_path().read_bytes() == before


def test_discuss_http_omits_document_context_unless_requested(server: ProseviewServer, fake_home: Path):
    headers = _discuss_headers(server)
    opened = server.post_json(
        "/api/discuss/conversations/open",
        {"kind": "scene", "path": SCENE_REL},
        headers=headers,
    )
    conversation_id = opened.json()["conversation_id"]
    question = "HTTP OMIT CURRENT DOCUMENT SENTINEL"

    response = server.post_json(
        f"/api/discuss/conversations/{conversation_id}/questions",
        {
            "client_request_id": "without-current-document",
            "question": question,
            "attachments": [{"kind": "file", "path": "plans/book-plan.md"}],
        },
        headers=headers,
    )
    assert response.status == 202
    _wait_discuss(
        server,
        conversation_id,
        lambda value: any(m["role"] == "assistant" for m in value["messages"]),
    )

    records = [json.loads(line) for line in (fake_home / "fake-codex-received.jsonl").read_text(encoding="utf-8").splitlines()]
    prompt = next(
        record["params"]["input"][0]["text"]
        for record in reversed(records)
        if question in json.dumps(record)
    )
    assert "Opening Ledger" not in prompt
    assert "book-plan.md" in prompt
    assert question in prompt

    resumed = server.post_json(
        "/api/discuss/conversations/open",
        {"kind": "scene", "path": SCENE_REL},
        headers=headers,
    ).json()
    assert resumed["conversation_id"] == conversation_id
    assert any(message["role"] == "assistant" for message in resumed["snapshot"]["messages"])


def test_discuss_http_approval_and_event_stream(server: ProseviewServer):
    headers = _discuss_headers(server)
    opened = server.post_json(
        "/api/discuss/conversations/open",
        {"kind": "scene", "path": SCENE_REL},
        headers=headers,
    ).json()
    conversation_id = opened["conversation_id"]

    with server.sse(f"/api/discuss/conversations/{conversation_id}/events") as stream:
        submitted = server.post_json(
            f"/api/discuss/conversations/{conversation_id}/questions",
            {"client_request_id": "approval-one", "question": "REQUEST_APPROVAL", "attachments": []},
            headers=headers,
        )
        assert submitted.status == 202
        frame = stream.wait_for(lambda value: "Test approval" in value)
        approval = json.loads(frame)
        assert approval["kind"] == "command"
        resolved = server.post_json(
            f"/api/discuss/conversations/{conversation_id}/approvals/{approval['request_id']}",
            {"decision": "decline"},
            headers=headers,
        )
        assert resolved.status == 200
        assert resolved.json()["approval"]["decision"] == "decline"

    snapshot = _wait_discuss(server, conversation_id, lambda value: any("Approval resolved" in m["text"] for m in value["messages"]))
    assert snapshot["approvals"][0]["status"] == "resolved"
    stale = server.post_json(
        f"/api/discuss/conversations/{conversation_id}/approvals/{approval['request_id']}",
        {"decision": "decline"},
        headers=headers,
    )
    assert stale.status == 409


def test_event_streams_reject_a_stale_server_session_without_subscribing(
    server: ProseviewServer,
):
    stale_query = "?session=stale-server-session"

    assert server.get("/events" + stale_query).status == 204
    assert server.get(
        "/api/discuss/conversations/not-open/events" + stale_query
    ).status == 204
    assert server.get("/terminal-output/not-open" + stale_query).status == 204
    assert server.get("/events").status == 204


def test_discuss_stop_preserves_and_runs_queued_question(server: ProseviewServer):
    headers = _discuss_headers(server)
    conversation_id = server.post_json(
        "/api/discuss/conversations/open",
        {"kind": "scene", "path": SCENE_REL},
        headers=headers,
    ).json()["conversation_id"]
    server.post_json(
        f"/api/discuss/conversations/{conversation_id}/questions",
        {"client_request_id": "hold", "question": "HOLD_FOR_STOP"},
        headers=headers,
    )
    active = _wait_discuss(server, conversation_id, lambda value: bool(value["active_turn_id"]))
    server.post_json(
        f"/api/discuss/conversations/{conversation_id}/questions",
        {"client_request_id": "after", "question": "Answer after stop"},
        headers=headers,
    )
    stopped = server.post_json(
        f"/api/discuss/conversations/{conversation_id}/turns/{active['active_turn_id']}/stop",
        {},
        headers=headers,
    )
    assert stopped.status == 200
    snapshot = _wait_discuss(
        server,
        conversation_id,
        lambda value: any("Fake answer" in message["text"] for message in value["messages"]),
    )
    assert len([message for message in snapshot["messages"] if message["role"] == "user"]) == 2


def test_discuss_can_remove_one_pending_queue_item(server: ProseviewServer):
    headers = _discuss_headers(server)
    conversation_id = server.post_json(
        "/api/discuss/conversations/open",
        {"kind": "scene", "path": SCENE_REL},
        headers=headers,
    ).json()["conversation_id"]
    server.post_json(
        f"/api/discuss/conversations/{conversation_id}/questions",
        {"client_request_id": "held", "question": "HOLD_FOR_STOP"},
        headers=headers,
    )
    active = _wait_discuss(server, conversation_id, lambda value: bool(value["active_turn_id"]))
    server.post_json(
        f"/api/discuss/conversations/{conversation_id}/questions",
        {"client_request_id": "remove-me", "question": "Do not run this"},
        headers=headers,
    )
    cancelled = server.post_json(
        f"/api/discuss/conversations/{conversation_id}/queue/remove-me/cancel",
        {},
        headers=headers,
    )
    assert cancelled.status == 200
    assert cancelled.json()["status"] == "cancelled"
    snapshot = _wait_discuss(server, conversation_id, lambda value: not value["queue"])
    assert not any(message.get("client_request_id") == "remove-me" for message in snapshot["messages"])
    server.post_json(
        f"/api/discuss/conversations/{conversation_id}/turns/{active['active_turn_id']}/stop",
        {},
        headers=headers,
    )


def test_discuss_sse_replays_strictly_after_last_event_id(server: ProseviewServer):
    headers = _discuss_headers(server)
    conversation_id = server.post_json(
        "/api/discuss/conversations/open",
        {"kind": "scene", "path": SCENE_REL},
        headers=headers,
    ).json()["conversation_id"]
    path = f"/api/discuss/conversations/{conversation_id}/events"
    with server.sse(path) as stream:
        first = stream.next_event()
        assert first["id"] is not None
        server.post_json(
            f"/api/discuss/conversations/{conversation_id}/questions",
            {"client_request_id": "replay", "question": "Replay this"},
            headers=headers,
        )
        completed = None
        while completed is None:
            event = stream.next_event()
            if event["type"] == "turn.completed":
                completed = event
        assert completed["id"] > first["id"]

    with server.sse(path, headers={"Last-Event-ID": str(first["id"])}) as replay:
        next_event = replay.next_event()
        assert next_event["id"] == first["id"] + 1
        assert next_event["type"] != "snapshot"


def test_discuss_queue_overflow_and_context_validation(server: ProseviewServer):
    headers = _discuss_headers(server)
    conversation_id = server.post_json(
        "/api/discuss/conversations/open",
        {"kind": "scene", "path": SCENE_REL},
        headers=headers,
    ).json()["conversation_id"]
    server.post_json(
        f"/api/discuss/conversations/{conversation_id}/questions",
        {"client_request_id": "active", "question": "HOLD_FOR_STOP"},
        headers=headers,
    )
    _wait_discuss(server, conversation_id, lambda value: bool(value["active_turn_id"]))
    for index in range(10):
        response = server.post_json(
            f"/api/discuss/conversations/{conversation_id}/questions",
            {"client_request_id": f"queued-{index}", "question": f"Queued {index}"},
            headers=headers,
        )
        assert response.status == 202
    overflow = server.post_json(
        f"/api/discuss/conversations/{conversation_id}/questions",
        {"client_request_id": "overflow", "question": "Too many"},
        headers=headers,
    )
    assert overflow.status == 429

    invalid = server.post_json(
        "/api/discuss/conversations/open",
        {"kind": "file", "path": "../outside.md"},
        headers=headers,
    )
    assert invalid.status == 400
    malformed = server.post_json(
        f"/api/discuss/conversations/{conversation_id}/questions",
        {"client_request_id": "bad-attachments", "question": "Bad", "attachments": {"path": "plans"}},
        headers=headers,
    )
    assert malformed.status == 400


def test_discuss_process_failure_is_honest_and_restarts_on_next_action(server: ProseviewServer):
    headers = _discuss_headers(server)
    conversation_id = server.post_json(
        "/api/discuss/conversations/open",
        {"kind": "scene", "path": SCENE_REL},
        headers=headers,
    ).json()["conversation_id"]
    server.post_json(
        f"/api/discuss/conversations/{conversation_id}/questions",
        {"client_request_id": "crash", "question": "CRASH_PROCESS"},
        headers=headers,
    )
    failed = _wait_discuss(server, conversation_id, lambda value: value["connection"] == "Unavailable")
    assert "exited" in failed["unavailable_reason"].lower() or "closed" in failed["unavailable_reason"].lower()

    restarted = server.post_json(
        f"/api/discuss/conversations/{conversation_id}/questions",
        {"client_request_id": "restart", "question": "Continue after failure"},
        headers=headers,
    )
    assert restarted.status == 202
    recovered = _wait_discuss(
        server,
        conversation_id,
        lambda value: value["connection"] == "Live" and any("Fake answer" in message["text"] for message in value["messages"]),
    )
    assert recovered["connection"] == "Live"


def test_discuss_refresh_recovers_a_missing_thread_and_can_start_new_conversation(
    server: ProseviewServer,
    fake_home: Path,
):
    headers = _discuss_headers(server)
    document = {"kind": "scene", "path": SCENE_REL}
    opened = server.post_json("/api/discuss/conversations/open", document, headers=headers).json()
    conversation_id = opened["conversation_id"]
    server.post_json(
        f"/api/discuss/conversations/{conversation_id}/questions",
        {"client_request_id": "forget", "question": "FORGET_THREAD_AFTER_TURN"},
        headers=headers,
    )
    # The answer text lands before the turn is finished, and a turn that fails
    # after that point flips the conversation to Unavailable from the worker
    # thread. Wait for the turn to settle so the refresh below observes a
    # stable connection state rather than racing the worker.
    _wait_discuss(
        server,
        conversation_id,
        lambda value: value["active_turn_id"] is None
        and any("Fake answer" in message["text"] for message in value["messages"]),
    )

    refreshed = server.post_json("/api/discuss/conversations/open", document, headers=headers).json()["snapshot"]
    assert refreshed["connection"] == "Live"
    assert any("next question will start a new conversation" in notice["message"].lower() for notice in refreshed["notices"])

    server.post_json(
        f"/api/discuss/conversations/{conversation_id}/questions",
        {"client_request_id": "after-refresh", "question": "Continue after refresh"},
        headers=headers,
    )
    recovered = _wait_discuss(
        server,
        conversation_id,
        lambda value: value["active_turn_id"] is None
        and len([message for message in value["messages"] if message["role"] == "assistant"]) == 2,
    )
    assert recovered["connection"] == "Live"
    records = [json.loads(line) for line in (fake_home / "fake-codex-received.jsonl").read_text(encoding="utf-8").splitlines()]
    assert records[-2]["threadId"] != records[-1]["threadId"]

    reset = server.post_json(
        f"/api/discuss/conversations/{conversation_id}/new",
        {},
        headers=headers,
    )
    assert reset.status == 200
    assert reset.json()["snapshot"]["messages"] == []
    server.post_json(
        f"/api/discuss/conversations/{conversation_id}/questions",
        {"client_request_id": "after-reset", "question": "A deliberately fresh conversation"},
        headers=headers,
    )
    fresh = _wait_discuss(
        server,
        conversation_id,
        lambda value: any("Fake answer" in message["text"] for message in value["messages"]),
    )
    assert len([message for message in fresh["messages"] if message["role"] == "user"]) == 1
    records = [json.loads(line) for line in (fake_home / "fake-codex-received.jsonl").read_text(encoding="utf-8").splitlines()]
    assert records[-2]["threadId"] != records[-1]["threadId"]


def test_discuss_history_endpoints_resume_rename_export_and_remove(server: ProseviewServer):
    headers = _discuss_headers(server)
    document = {"kind": "scene", "path": SCENE_REL}
    opened = server.post_json("/api/discuss/conversations/open", document, headers=headers).json()
    conversation_id = opened["conversation_id"]
    server.post_json(
        f"/api/discuss/conversations/{conversation_id}/questions",
        {"client_request_id": "history-first", "question": "Why does this opening feel quiet?"},
        headers=headers,
    )
    _wait_discuss(
        server,
        conversation_id,
        lambda value: value["active_turn_id"] is None
        and any("Fake answer" in message["text"] for message in value["messages"]),
    )

    listed = server.post_json(
        f"/api/discuss/conversations/{conversation_id}/history/list", {}, headers=headers
    )
    assert listed.status == 200
    first = listed.json()["conversations"][0]
    assert first["title"] == "Why does this opening feel quiet?"
    assert first["current"] is True

    server.post_json(f"/api/discuss/conversations/{conversation_id}/new", {}, headers=headers)
    reopened = server.post_json(
        f"/api/discuss/conversations/{conversation_id}/history/{first['thread_id']}/open", {}, headers=headers
    )
    assert reopened.status == 200
    assert any(message["text"] == "Why does this opening feel quiet?" for message in reopened.json()["snapshot"]["messages"])

    renamed = server.post_json(
        f"/api/discuss/conversations/{conversation_id}/history/{first['thread_id']}/rename",
        {"title": "Quiet opening"}, headers=headers,
    )
    assert renamed.json()["conversation"]["title"] == "Quiet opening"
    exported = server.post_json(
        f"/api/discuss/conversations/{conversation_id}/history/{first['thread_id']}/export", {}, headers=headers
    ).json()["export"]
    assert exported["conversation"]["title"] == "Quiet opening"
    assert "BEGIN UNTRUSTED DOCUMENT" not in json.dumps(exported)

    server.post_json(f"/api/discuss/conversations/{conversation_id}/new", {}, headers=headers)
    removed = server.post_json(
        f"/api/discuss/conversations/{conversation_id}/history/{first['thread_id']}/remove", {}, headers=headers
    )
    assert removed.status == 200
    assert removed.json()["removed"] is True


def test_discuss_new_conversation_refuses_to_discard_active_work(server: ProseviewServer):
    headers = _discuss_headers(server)
    conversation_id = server.post_json(
        "/api/discuss/conversations/open",
        {"kind": "scene", "path": SCENE_REL},
        headers=headers,
    ).json()["conversation_id"]
    server.post_json(
        f"/api/discuss/conversations/{conversation_id}/questions",
        {"client_request_id": "busy-reset", "question": "HOLD_FOR_STOP"},
        headers=headers,
    )
    active = _wait_discuss(server, conversation_id, lambda value: bool(value["active_turn_id"]))

    refused = server.post_json(
        f"/api/discuss/conversations/{conversation_id}/new",
        {},
        headers=headers,
    )
    assert refused.status == 409
    assert "busy" in refused.json()["error"]
    server.post_json(
        f"/api/discuss/conversations/{conversation_id}/turns/{active['active_turn_id']}/stop",
        {},
        headers=headers,
    )


# ── data endpoints ──────────────────────────────────────────────────────────


def test_data_json_carries_contents_meta_and_highlights(shared_server: ProseviewServer):
    data = shared_server.get_json("/data.json")
    assert set(data) == {"contents", "meta", "highlightsByPath", "medians"}

    meta = data["meta"][SCENE_REL]
    assert meta["words"] > 0
    assert meta["mtime"] > 0
    assert Path(meta["abs_path"]).is_file()
    assert data["highlightsByPath"][SCENE_REL]["paragraphs"]


def test_scene_data_returns_only_the_requested_scene(shared_server: ProseviewServer):
    data = shared_server.get_json(f"/scene-data?path={SCENE_REL}")
    assert list(data["contents"]) == [SCENE_REL]
    assert data["contents"][SCENE_REL] == shared_server.get_json("/data.json")["contents"][SCENE_REL]


def test_scene_data_rejects_paths_outside_the_manuscript(shared_server: ProseviewServer):
    resp = shared_server.get("/scene-data?path=../../../etc/passwd")
    assert resp.status == 403
    assert resp.json()["ok"] is False


def test_large_scene_is_analysed(shared_server: ProseviewServer):
    """A ~10k-word scene still produces sane analytics.

    Guards against silent truncation or a quadratic blow-up in the lexical
    passes, which the small committed fixture would never surface.
    """
    started = time.monotonic()
    data = shared_server.get_json("/data.json")
    elapsed = time.monotonic() - started

    meta = data["meta"][LARGE_SCENE_REL]
    assert meta["words"] > 9_000
    assert meta["read_min"] > 0
    assert data["highlightsByPath"][LARGE_SCENE_REL]["paragraphs"]
    assert elapsed < 30, f"/data.json took {elapsed:.1f}s over a 10k-word scene"


# ── saving ──────────────────────────────────────────────────────────────────


def test_save_scene_writes_body_and_preserves_frontmatter(server: ProseviewServer):
    path = server.scene_path()
    before = path.read_text(encoding="utf-8")
    body = server.get_json("/data.json")["contents"][SCENE_REL]

    resp = server.save_scene(body.replace("cold coffee", "burnt coffee"))
    assert resp.status == 200
    payload = resp.json()
    assert payload["ok"] is True

    after = path.read_text(encoding="utf-8")
    assert "burnt coffee" in after
    # The header (frontmatter + title) is reconstructed by the server, not sent
    # by the client, so a regression there would silently eat metadata.
    assert _frontmatter(after) == _frontmatter(before)
    assert "# Opening Ledger" in after
    # The returned mtime is the client's next conflict baseline.
    assert payload["mtime"] == pytest.approx(path.stat().st_mtime, abs=0.01)
    assert re.fullmatch(r"[0-9a-f]{64}", payload["revision"])
    assert server.scene_meta()["revision"] == payload["revision"]


def test_save_scene_with_stale_mtime_conflicts_and_changes_nothing(server: ProseviewServer):
    path = server.scene_path()
    body = server.get_json("/data.json")["contents"][SCENE_REL]
    stale_mtime = server.scene_meta()["mtime"]

    assert server.save_scene(body + "\nFirst writer wins.\n").status == 200
    after_first = path.read_text(encoding="utf-8")

    # Second writer opened the file before the first save landed.
    resp = server.save_scene(body + "\nSecond writer clobbers.\n", mtime=stale_mtime)
    assert resp.status == 409
    assert resp.json() == {"conflict": True}
    assert path.read_text(encoding="utf-8") == after_first
    assert "Second writer clobbers." not in after_first


def test_save_scene_refuses_paths_outside_the_manuscript(server: ProseviewServer):
    outside = server.root / "plans" / "book-plan.md"
    before = outside.read_text(encoding="utf-8")
    resp = server.post_json("/save-scene", {
        "abs_path": str(outside),
        "content": "overwritten",
        "open_mtime": outside.stat().st_mtime,
    })
    assert resp.status == 500
    assert outside.read_text(encoding="utf-8") == before


# ── frontmatter scaffold ────────────────────────────────────────────────────


def _bare_scene_meta(server: ProseviewServer) -> dict:
    return server.get_json("/data.json")["meta"][BARE_SCENE_REL]


def test_add_frontmatter_writes_an_empty_block_over_the_wire(server: ProseviewServer):
    meta = _bare_scene_meta(server)
    path = Path(meta["abs_path"])
    before = path.read_text(encoding="utf-8")
    assert not before.startswith("---")

    resp = server.post_json("/add-frontmatter", {
        "abs_path": meta["abs_path"],
        "open_mtime": meta["mtime"],
    })
    assert resp.status == 200
    assert resp.json()["ok"] is True

    after = path.read_text(encoding="utf-8")
    assert after.startswith("---\n")
    # Keys only -- the writer fills the values.
    assert "goal:\n" in after and "characters:\n" in after
    assert "goal: " not in after
    assert before.strip() in after


def test_add_frontmatter_refuses_a_scene_that_already_has_one(server: ProseviewServer):
    meta = server.scene_meta()
    path = server.scene_path()
    before = path.read_text(encoding="utf-8")

    resp = server.post_json("/add-frontmatter", {
        "abs_path": meta["abs_path"],
        "open_mtime": meta["mtime"],
    })
    assert resp.status == 409
    assert path.read_text(encoding="utf-8") == before


def test_add_frontmatter_refuses_paths_outside_the_repository(server: ProseviewServer):
    """The endpoint takes an absolute path, so it needs the containment check."""
    outside = server.root.parent / "escape.md"
    outside.write_text("# Not part of the repo\n", encoding="utf-8")
    try:
        resp = server.post_json("/add-frontmatter", {"abs_path": str(outside)})
        assert resp.status == 403
        assert not outside.read_text(encoding="utf-8").startswith("---")
    finally:
        outside.unlink(missing_ok=True)


def test_delete_fm_todo_refuses_paths_outside_the_repository(server: ProseviewServer):
    """``/delete-fm-todo`` takes an ``abs_path`` like its siblings, but was left
    out of ``_ABS_PATH_ENDPOINTS``, so it never got the containment check."""
    outside = server.root.parent / "escape-fm-todo.md"
    outside.write_text(
        "---\ntodos:\n  - something\n---\n\n# Not part of the repo\n", encoding="utf-8"
    )
    try:
        before = outside.read_text(encoding="utf-8")
        resp = server.post_json("/delete-fm-todo", {
            "abs_path": str(outside),
            "todo_text": "something",
        })
        assert resp.status == 403
        assert outside.read_text(encoding="utf-8") == before
    finally:
        outside.unlink(missing_ok=True)


def test_add_frontmatter_conflicts_on_a_stale_mtime(server: ProseviewServer):
    meta = _bare_scene_meta(server)
    path = Path(meta["abs_path"])
    before = path.read_text(encoding="utf-8")

    resp = server.post_json("/add-frontmatter", {
        "abs_path": meta["abs_path"],
        "open_mtime": meta["mtime"] - 100,
    })
    assert resp.status == 409
    assert resp.json().get("conflict") is True
    assert path.read_text(encoding="utf-8") == before


# ── TODOs and notes ─────────────────────────────────────────────────────────


def test_todo_lifecycle_round_trips_the_file(server: ProseviewServer):
    path = server.scene_path()
    original = path.read_text(encoding="utf-8")
    meta = server.scene_meta()

    assert server.post_json("/insert-todo", {
        "abs_path": meta["abs_path"],
        "selection_text": "It is sticking again",
        "txt_line_offset": meta["txt_line_offset"],
        "todo_text": "Sharpen Lowe's entrance",
    }).json() == {"ok": True}

    with_todo = path.read_text(encoding="utf-8")
    assert "<!-- TODO: Sharpen Lowe's entrance -->" in with_todo
    # It must land above the paragraph holding the selection, not at the top.
    todo_line = with_todo.index("<!-- TODO:")
    assert with_todo.index("It is sticking again") > todo_line
    assert with_todo.index("The loft smelled") < todo_line

    assert server.post_json("/edit-todo", {
        "abs_path": meta["abs_path"],
        "old_todo_text": "Sharpen Lowe's entrance",
        "new_todo_text": "Cut Lowe's entrance entirely",
    }).json() == {"ok": True}
    assert "<!-- TODO: Cut Lowe's entrance entirely -->" in path.read_text(encoding="utf-8")

    assert server.post_json("/delete-todo", {
        "abs_path": meta["abs_path"],
        "todo_text": "Cut Lowe's entrance entirely",
    }).json() == {"ok": True}
    assert "<!-- TODO:" not in path.read_text(encoding="utf-8")


def test_note_lifecycle_preserves_its_tag(server: ProseviewServer):
    path = server.scene_path()
    meta = server.scene_meta()

    assert server.post_json("/add-note", {
        "abs_path": meta["abs_path"],
        "selection_text": "It is not the safe",
        "txt_line_offset": meta["txt_line_offset"],
        "note_text": "Safe brand must match chapter three",
        "tag": "continuity",
    }).json() == {"ok": True}
    assert "<!-- NOTE[continuity]: Safe brand must match chapter three -->" in path.read_text(encoding="utf-8")

    assert server.post_json("/edit-note", {
        "abs_path": meta["abs_path"],
        "old_note_text": "Safe brand must match chapter three",
        "old_tag": "continuity",
        "new_note_text": "Safe brand is established in chapter three",
        "new_tag": "question",
    }).json() == {"ok": True}
    assert "<!-- NOTE[question]: Safe brand is established in chapter three -->" in path.read_text(encoding="utf-8")

    assert server.post_json("/delete-note", {
        "abs_path": meta["abs_path"],
        "note_text": "Safe brand is established in chapter three",
        "tag": "question",
    }).json() == {"ok": True}
    assert "<!-- NOTE[" not in path.read_text(encoding="utf-8")


def test_annotations_surface_in_scene_metadata(shared_server: ProseviewServer):
    """The seeded annotated scene's inline comments reach the Tasks/Notes data."""
    meta = shared_server.get_json("/data.json")["meta"][ANNOTATED_SCENE_REL]
    todo_text = json.dumps(meta["todos"])
    note_text = json.dumps(meta["notes"])
    assert "Tighten this opening beat" in todo_text
    assert "Patel should not know about the safe yet" in note_text


# ── AI proposal bridge (through the real CLI) ───────────────────────────────


QUOTE = "the slow algebra of yesterday's receipts"


def test_cli_propose_resolves_a_quote_to_an_editor_range(server: ProseviewServer):
    proc = server.cli(
        "propose", "--root", str(server.root),
        "--file", SCENE_REL,
        "--quote", QUOTE,
        "--message", "Too ornate for a cold open",
        "--option", "the arithmetic of yesterday's receipts",
    )
    assert "created for" in proc.stdout

    proposals = server.get_json("/ai/proposals")["proposals"]
    assert len(proposals) == 1
    prop = proposals[0]
    assert prop["file"] == SCENE_REL
    assert prop["status"] == "created"
    assert prop["resolved_quote"] == QUOTE
    assert prop["range"]["end"] - prop["range"]["start"] == len(QUOTE)
    assert prop["options"][0]["text"] == "the arithmetic of yesterday's receipts"


def test_quote_and_line_col_targeting_resolve_identically(server: ProseviewServer):
    """The two targeting paths must agree.

    ``--quote`` searches the flattened editor text; ``--start-line/--start-col``
    walks raw Markdown offsets through ``_scene_to_editor_offset``. They are
    independent implementations of the same mapping, so cross-checking them
    catches the off-by-one class of bug without reimplementing either here.
    """
    raw = server.scene_path().read_text(encoding="utf-8")
    start = raw.index(QUOTE)
    start_line, start_col = _line_col(raw, start)
    end_line, end_col = _line_col(raw, start + len(QUOTE))

    server.cli(
        "propose", "--root", str(server.root), "--file", SCENE_REL,
        "--quote", QUOTE, "--message", "by quote", "--option", "a plainer phrase",
    )
    server.cli(
        "propose", "--root", str(server.root), "--file", SCENE_REL,
        "--start-line", str(start_line), "--start-col", str(start_col),
        "--end-line", str(end_line), "--end-col", str(end_col),
        "--message", "by line/col", "--option", "a plainer phrase",
    )

    by_message = {p["message"]: p for p in server.get_json("/ai/proposals")["proposals"]}
    assert by_message["by quote"]["range"] == by_message["by line/col"]["range"]
    assert by_message["by quote"]["resolved_quote"] == by_message["by line/col"]["resolved_quote"]


def test_propose_rejects_a_quote_that_spans_an_annotation(server: ProseviewServer):
    """Annotations are atoms in the editor; a range crossing one cannot be applied."""
    proc = server.cli(
        "propose", "--root", str(server.root),
        "--file", ANNOTATED_SCENE_REL,
        "--quote", "<!-- TODO: Tighten this opening beat -->",
        "--message", "should be refused", "--option", "nothing",
        check=False,
    )
    assert proc.returncode != 0 or "failed" in proc.stdout + proc.stderr
    assert server.get_json("/ai/proposals")["proposals"] == []


def test_apply_requests_the_edit_and_publishes_it_over_sse(server: ProseviewServer):
    """Applying is a *request*, not a write.

    The server marks the proposal ``apply_requested`` and broadcasts it; the
    browser performs the edit in ProseMirror and saves. Asserting a file change
    here would encode the wrong contract -- that assertion belongs in the
    browser tier.
    """
    server.cli(
        "propose", "--root", str(server.root), "--file", SCENE_REL,
        "--quote", QUOTE, "--message", "Too ornate",
        "--option", "the arithmetic of yesterday's receipts",
    )
    prop_id = server.get_json("/ai/proposals")["proposals"][0]["id"]
    before = server.scene_path().read_text(encoding="utf-8")

    with server.sse() as events:
        server.cli("proposal", "apply", prop_id, "--root", str(server.root))
        frame = events.wait_for(lambda f: "apply" in f and prop_id in f)

    payload = json.loads(frame)
    assert payload["proposal"]["status"] == "apply_requested"
    assert payload["proposal"]["selected_option"] == 0
    assert server.get_json(f"/ai/proposals/{prop_id}")["proposal"]["status"] == "apply_requested"
    assert server.scene_path().read_text(encoding="utf-8") == before


def test_proposal_can_be_skipped(server: ProseviewServer):
    server.cli(
        "propose", "--root", str(server.root), "--file", SCENE_REL,
        "--quote", QUOTE, "--message", "Never mind", "--option", "leave it alone",
    )
    prop_id = server.get_json("/ai/proposals")["proposals"][0]["id"]
    server.cli("proposal", "skip", prop_id, "--root", str(server.root))
    assert server.get_json(f"/ai/proposals/{prop_id}")["proposal"]["status"] == "skipped"


# ── live reload ─────────────────────────────────────────────────────────────


def test_editing_a_scene_on_disk_pushes_a_reload_event(server: ProseviewServer):
    with server.sse() as events:
        assert events.next(timeout=5) == "connected"
        path = server.scene_path()
        path.write_text(path.read_text(encoding="utf-8") + "\nA line typed in vim.\n", encoding="utf-8")

        frame = events.wait_for(lambda f: "reload" in f, timeout=15)

    # Content changes carry the changed paths so the client can re-render a
    # single scene instead of reloading the whole page.
    if frame.startswith("{"):
        payload = json.loads(frame)
        assert payload["type"] == "reload"
        assert any(SCENE_REL.split("/")[-1] in p for p in payload["paths"])
    else:
        assert frame == "reload"


# ── terminal and agents ─────────────────────────────────────────────────────


def _terminal_text(server: ProseviewServer, tid: str, needle: str, timeout: float = 15.0) -> str:
    """Accumulate PTY output until *needle* appears.

    Frames are base64-encoded chunks of raw terminal bytes, so a marker can be
    split across frames -- always match against the accumulation.
    """
    seen = ""
    with server.sse(f"/terminal-output/{tid}") as stream:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = stream.next(timeout=max(0.1, deadline - time.monotonic()))
            if frame in ("connected", "__exit__"):
                if frame == "__exit__":
                    break
                continue
            try:
                seen += base64.b64decode(frame).decode("utf-8", errors="replace")
            except Exception:
                continue
            if needle in seen:
                return seen
    raise AssertionError(f"{needle!r} never appeared in terminal output; saw {seen!r}")


@POSIX_ONLY
def test_terminal_streams_output_from_a_spawned_command(server: ProseviewServer):
    resp = server.post_json("/terminal-spawn", {
        "command": ["echo", "proseview-e2e-marker"],
        "type": "shell", "label": "Shell 1", "rows": 24, "cols": 80,
    })
    assert resp.status == 200
    assert "proseview-e2e-marker" in _terminal_text(
        server, resp.json()["id"], "proseview-e2e-marker"
    )


@POSIX_ONLY
def test_terminal_session_is_listed_while_alive_and_killable(server: ProseviewServer):
    """``/terminal-list`` reports only live sessions, so the command has to
    outlive the request -- ``echo`` would already be reaped."""
    tid = server.post_json("/terminal-spawn", {
        "command": ["sh", "-c", "echo proseview-ready; cat"],
        "type": "shell", "label": "Shell 1",
    }).json()["id"]
    _terminal_text(server, tid, "proseview-ready")

    sessions = server.get_json("/terminal-list")["sessions"]
    assert any(s["id"] == tid and s["label"] == "Shell 1" and s["alive"] for s in sessions)

    assert server.post_json("/terminal-kill", {"id": tid}).json() == {"ok": True}
    assert all(s["id"] != tid for s in server.get_json("/terminal-list")["sessions"])


@POSIX_ONLY
@pytest.mark.parametrize("agent", ["codex", "claude", "gemini"])
def test_agent_launch_runs_the_real_binary_from_path(server: ProseviewServer, agent: str):
    """The agent handoff is a plain terminal spawn of the agent's own command.

    A stub on PATH stands in for the real tool, so this proves the spawn reaches
    an executable and is tagged with the right session type -- the part
    Proseview owns.
    """
    resp = server.post_json("/terminal-spawn", {
        "command": [agent], "type": agent, "label": f"{agent.title()} 1",
    })
    assert resp.status == 200
    tid = resp.json()["id"]

    assert f"{AGENT_MARKER} {agent}" in _terminal_text(server, tid, f"{AGENT_MARKER} {agent}")
    assert any(s["type"] == agent for s in server.get_json("/terminal-list")["sessions"])


@POSIX_ONLY
def test_codex_auto_approve_passes_the_full_auto_flag(server: ProseviewServer):
    """Auto-approve is expressed purely as argv, so the stub can echo it back."""
    tid = server.post_json("/terminal-spawn", {
        "command": ["codex", "--full-auto"], "type": "codex",
    }).json()["id"]
    assert "argv:--full-auto" in _terminal_text(server, tid, "argv:--full-auto")


@POSIX_ONLY
def test_selection_and_instruction_reach_the_agent_process(server: ProseviewServer):
    """The prompt is delivered as keystrokes, not as a spawn argument.

    ``/terminal-input`` is the only channel carrying the selected passage to the
    agent, so a regression there would silently strip the user's context while
    still appearing to launch the agent correctly.
    """
    tid = server.post_json("/terminal-spawn", {"command": ["codex"], "type": "codex"}).json()["id"]
    _terminal_text(server, tid, AGENT_MARKER)

    prompt = "Tighten this passage: the slow algebra of yesterday's receipts\n"
    assert server.post_json("/terminal-input", {
        "id": tid,
        "data": base64.b64encode(prompt.encode()).decode(),
    }).json() == {"ok": True}

    assert "STDIN:Tighten this passage" in _terminal_text(server, tid, "STDIN:Tighten this passage")


@POSIX_ONLY
def test_terminal_sessions_outlive_a_page_reload(server: ProseviewServer):
    """``/terminal-list`` is what lets a reloaded page reattach instead of
    losing every running agent."""
    tid = server.post_json("/terminal-spawn", {"command": ["codex"], "type": "codex"}).json()["id"]
    _terminal_text(server, tid, AGENT_MARKER)

    # A reload re-fetches the page; the PTY must be untouched by it.
    assert server.get("/").status == 200
    session = next(s for s in server.get_json("/terminal-list")["sessions"] if s["id"] == tid)
    assert session["alive"] is True
    assert session["command"] == ["codex"]

    # Scrollback replays to the reattaching client.
    assert AGENT_MARKER in _terminal_text(server, tid, AGENT_MARKER)


# ── static assets and rejections ────────────────────────────────────────────


def test_file_api_creates_renames_and_trashes_a_scene(server: ProseviewServer):
    created = server.post_json(
        "/api/files/create",
        {"parent": "manuscript/ch01", "name": "03-new-arrival", "kind": "file"},
    )
    assert created.status == 201, created.text
    assert created.json()["path"] == "manuscript/ch01/03-new-arrival.md"
    assert created.json()["scene_path"] == "ch01/03-new-arrival.md"
    new_scene = server.root / created.json()["path"]
    assert new_scene.read_bytes() == b""

    new_scene.write_bytes(b"A first line.\n")
    renamed = server.post_json(
        "/api/files/rename",
        {"path": created.json()["path"], "name": "03-renamed"},
    )
    assert renamed.status == 200, renamed.text
    assert renamed.json()["path"] == "manuscript/ch01/03-renamed.md"
    assert renamed.json()["scene_path"] == "ch01/03-renamed.md"
    renamed_scene = server.root / renamed.json()["path"]
    assert renamed_scene.read_bytes() == b"A first line.\n"
    assert not new_scene.exists()

    removed = server.post_json("/api/files/delete", {"path": renamed.json()["path"]})
    assert removed.status == 200, removed.text
    assert removed.json()["kind"] == "file"
    assert removed.json()["entry_count"] == 1
    assert not renamed_scene.exists()
    assert (server.root / removed.json()["trash_path"]).read_bytes() == b"A first line.\n"


def test_file_api_handles_folders_without_overwriting_content(server: ProseviewServer):
    made = server.post_json(
        "/api/files/create",
        {"parent": "story-bible", "name": "Locations", "kind": "folder"},
    )
    assert made.status == 201, made.text
    assert made.json()["path"] == "story-bible/Locations"
    folder = server.root / made.json()["path"]
    (folder / "market.md").write_text("Keep this.\n", encoding="utf-8")

    duplicate = server.post_json(
        "/api/files/create",
        {"parent": "story-bible", "name": "Locations", "kind": "folder"},
    )
    assert duplicate.status == 409
    assert (folder / "market.md").read_text(encoding="utf-8") == "Keep this.\n"

    renamed = server.post_json(
        "/api/files/rename", {"path": "story-bible/Locations", "name": "Places"}
    )
    assert renamed.status == 200, renamed.text
    assert (server.root / "story-bible/Places/market.md").read_text(encoding="utf-8") == "Keep this.\n"

    removed = server.post_json("/api/files/delete", {"path": "story-bible/Places"})
    assert removed.status == 200, removed.text
    assert removed.json()["kind"] == "folder"
    assert removed.json()["entry_count"] == 1
    assert (server.root / removed.json()["trash_path"] / "market.md").read_text(encoding="utf-8") == "Keep this.\n"


def test_file_api_rejects_unsafe_or_unmanaged_mutations(server: ProseviewServer):
    tokenless = server.post_json(
        "/api/files/create",
        {"parent": "manuscript/ch01", "name": "blocked", "kind": "file"},
        headers={"X-Proseview-Session": ""},
    )
    assert tokenless.status == 403

    outside = server.post_json(
        "/api/files/create", {"parent": "scripts", "name": "blocked", "kind": "file"}
    )
    assert outside.status == 403
    assert not (server.root / "scripts/blocked.md").exists()

    traversal = server.post_json(
        "/api/files/create",
        {"parent": "manuscript/../../outside", "name": "blocked", "kind": "file"},
    )
    assert traversal.status == 400

    protected = server.post_json(
        "/api/files/rename", {"path": "manuscript", "name": "draft"}
    )
    assert protected.status == 403

    linked = server.root / "story-bible/linked"
    try:
        linked.symlink_to(server.root / "scripts", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")
    through_link = server.post_json(
        "/api/files/create", {"parent": "story-bible/linked", "name": "blocked", "kind": "file"}
    )
    assert through_link.status == 400
    assert not (server.root / "scripts/blocked.md").exists()


def test_stylesheet_and_vendored_assets_are_served(shared_server: ProseviewServer):
    css = shared_server.get("/app.css")
    assert css.status == 200 and b"{" in css.body

    xterm = shared_server.get("/vendor/xterm.js")
    assert xterm.status == 200 and len(xterm.body) > 1000


def test_repo_file_returns_a_preview_node(shared_server: ProseviewServer):
    payload = shared_server.get_json("/repo-file?path=plans/book-plan.md")
    assert payload["ok"] is True
    assert payload["node"]["is_text"] is True
    assert payload["node"]["body"].strip()


def test_repo_file_previews_utf8_source_outside_configured_folders(shared_server: ProseviewServer):
    payload = shared_server.get_json("/repo-file?path=scripts/check_continuity.py")
    assert payload["ok"] is True
    assert payload["node"]["is_text"] is True
    assert "def check_continuity" in payload["node"]["body"]


def test_repo_file_rejects_traversal_outside_the_repo(shared_server: ProseviewServer):
    resp = shared_server.get("/repo-file?path=../../../../etc/passwd")
    assert resp.status == 403


def test_repo_file_rejects_hidden_internal_paths(shared_server: ProseviewServer):
    resp = shared_server.get("/repo-file?path=.private/token.txt")
    assert resp.status == 403
    assert b"fixture secret" not in resp.body


def test_unknown_paths_404(shared_server: ProseviewServer):
    assert shared_server.get("/definitely-not-a-route").status == 404
