from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from proseview.claude_agent_client import (
    APPROVAL_DECISIONS,
    ClaudeAgentClient,
    ClaudeRequestError,
    sanitize_claude_message,
)


# --- fakes standing in for the SDK -----------------------------------------
#
# These deliberately carry the SDK's own class names. The transport identifies
# messages by ``type(message).__name__`` so it never has to import the SDK, so
# a fake named anything else is silently ignored and its test passes vacuously.
# Do not rename them to Fake*.

@dataclass
class TextBlock:
    text: str


@dataclass
class ThinkingBlock:
    thinking: str = "internal reasoning"


@dataclass
class ToolUseBlock:
    name: str
    input: dict
    id: str = "tool-1"


@dataclass
class AssistantMessage:
    content: list
    session_id: str = "sess-1"
    uuid: str = "msg-1"


@dataclass
class ResultMessage:
    subtype: str = "success"
    terminal_reason: str | None = None
    is_error: bool = False
    errors: list = field(default_factory=list)
    session_id: str = "sess-1"
    result: str = ""


class FakeSDKClient:
    """Minimal ClaudeSDKClient: replays a scripted message stream per turn."""

    def __init__(self, options: Any) -> None:
        self.options = options
        self.connected = False
        self.queries: list[str] = []
        self.interrupted = 0
        self.script: list[Any] = [ResultMessage()]
        self.models: list[str | None] = []
        self.hold = None

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)

    async def interrupt(self) -> None:
        self.interrupted += 1

    async def set_model(self, model: str | None = None) -> None:
        self.models.append(model)

    async def receive_response(self):
        for message in self.script:
            if self.hold is not None:
                await self.hold.wait()
            yield message


def make_client(script: list | None = None, **kwargs: Any) -> tuple[ClaudeAgentClient, list, list]:
    """Build a client wired to fakes, collecting notifications it emits.

    ``script`` is applied at construction: the drain starts as soon as a turn
    does, so setting it afterwards races the turn it is meant to describe.

    A ``session_reader`` is always supplied, defaulting to an empty store. The
    SDK is an optional dependency, so a test that falls through to the real one
    passes wherever it happens to be installed and fails in CI, where it is not.
    """
    kwargs.setdefault("session_reader", lambda session_id, cwd: [])
    seen: list[dict] = []
    made: list[FakeSDKClient] = []

    def client_factory(options: Any) -> FakeSDKClient:
        fake = FakeSDKClient(options)
        if script is not None:
            fake.script = list(script)
        made.append(fake)
        return fake

    client = ClaudeAgentClient(
        cwd=".",
        on_message=seen.append,
        options_factory=lambda **opts: opts,
        client_factory=client_factory,
        **kwargs,
    )
    client.capabilities = {"approval_decisions": {}}
    client.start()
    return client, seen, made


@pytest.fixture(autouse=True)
def isolated_claude_settings(tmp_path, monkeypatch):
    """Keep the developer's own Claude settings out of these assertions.

    The transport reads the model and effort keys from the writer's settings
    file, so a machine that has configured Opus would otherwise see different
    options than a machine that has not.
    """
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))


def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# --- translator -------------------------------------------------------------

def test_translator_drops_raw_thinking():
    assert sanitize_claude_message({"method": "assistant/thinkingDelta", "params": {}}) == []


def test_translator_emits_prosview_vocabulary():
    events = sanitize_claude_message({
        "method": "assistant/message",
        "params": {"threadId": "t1", "turnId": "u1", "text": "hello"},
    })
    assert events == [{
        "type": "response.completed",
        "thread_id": "t1",
        "turn_id": "u1",
        "item_id": None,
        "phase": "final_answer",
        "text": "hello",
    }]


def test_translator_maps_bash_to_command_activity():
    events = sanitize_claude_message({
        "method": "tool/started",
        "params": {"threadId": "t1", "turnId": "u1", "tool": "Bash", "command": "ls", "itemId": "i1"},
    })
    assert events[0]["type"] == "activity.updated"
    assert events[0]["activity"]["kind"] == "commandExecution"
    assert events[0]["activity"]["command"] == "ls"


def test_translator_maps_write_to_file_change():
    events = sanitize_claude_message({
        "method": "tool/started",
        "params": {"tool": "Write", "changes": [{"path": "a.md", "kind": "modify"}]},
    })
    assert events[0]["activity"]["kind"] == "fileChange"
    assert events[0]["activity"]["changes"] == [{"path": "a.md", "kind": "modify"}]


def test_a_completion_does_not_relabel_the_tool_it_finished():
    """A completion knows the outcome, not the tool.

    Deriving a kind from the empty name it carries turned a finished command
    into a dynamic tool call and threw the command away with it.
    """
    events = sanitize_claude_message({
        "method": "tool/completed",
        "params": {"threadId": "t1", "turnId": "u1", "itemId": "i1", "tool": "", "status": "completed"},
    })
    activity = events[0]["activity"]
    assert activity["status"] == "completed"
    assert "kind" not in activity
    assert "command" not in activity


def test_thinking_reports_life_without_reporting_the_thought():
    """Claude was mute from Send to answer: thinking is dropped and nothing
    replaced it. A fixed heartbeat says work is happening and nothing else."""
    client, seen, _made = make_client(script=[
        AssistantMessage(content=[ThinkingBlock("the butler did it")]),
        AssistantMessage(content=[TextBlock("an answer")]),
        ResultMessage(),
    ])
    try:
        thread_id = client.request("thread/start", {})["thread"]["id"]
        client.request("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": "who?"}]})
        assert wait_for(lambda: any(m["method"] == "turn/completed" for m in seen))
        progress = [m for m in seen if m["method"] == "assistant/progress"]
        assert [m["params"]["text"] for m in progress] == ["Thinking\n"]
        assert "butler" not in json.dumps(seen)
    finally:
        client.close()


def test_translator_ignores_unknown_methods():
    assert sanitize_claude_message({"method": "something/else", "params": {}}) == []


# --- session pool -----------------------------------------------------------

def test_thread_start_creates_distinct_sessions():
    client, _, _ = make_client()
    try:
        first = client.request("thread/start", {})["thread"]["id"]
        second = client.request("thread/start", {})["thread"]["id"]
        assert first != second
        assert len(client._sessions) == 2
    finally:
        client.close()


def test_pool_evicts_least_recently_used_idle_session():
    client, _, _ = make_client()
    try:
        client.max_sessions = 2
        ids = [client.request("thread/start", {})["thread"]["id"] for _ in range(4)]
        assert len(client._sessions) <= 2
        # The survivors are the most recent ones.
        assert ids[-1] in client._sessions
    finally:
        client.close()


def test_turn_start_on_unknown_thread_reports_not_found():
    client, _, _ = make_client()
    try:
        with pytest.raises(ClaudeRequestError) as excinfo:
            client.request("turn/start", {"threadId": "missing", "input": [{"type": "text", "text": "hi"}]})
        assert excinfo.value.code == -32004
    finally:
        client.close()


def test_unknown_method_is_method_not_found():
    client, _, _ = make_client()
    try:
        with pytest.raises(ClaudeRequestError) as excinfo:
            client.request("thread/fork", {})
        assert excinfo.value.code == -32601
    finally:
        client.close()


# --- turn execution ---------------------------------------------------------

def test_turn_streams_message_and_completes():
    client, seen, made = make_client(script=[
        AssistantMessage(content=[TextBlock("an answer")]),
        ResultMessage(),
    ])
    try:
        thread_id = client.request("thread/start", {})["thread"]["id"]
        result = client.request(
            "turn/start",
            {"threadId": thread_id, "input": [{"type": "text", "text": "question"}]},
        )
        turn_id = result["turn"]["id"]
        assert wait_for(lambda: any(m["method"] == "turn/completed" for m in seen))
        methods = [m["method"] for m in seen]
        assert "turn/started" in methods
        assert made[0].queries == ["question"]
        answers = [m["params"]["text"] for m in seen if m["method"] == "assistant/message"]
        assert answers == ["an answer"]
        completed = next(m for m in seen if m["method"] == "turn/completed")
        assert completed["params"]["turnId"] == turn_id
        assert completed["params"]["status"] == "completed"
    finally:
        client.close()


def test_empty_turn_input_is_rejected():
    client, _, _ = make_client()
    try:
        thread_id = client.request("thread/start", {})["thread"]["id"]
        with pytest.raises(ClaudeRequestError):
            client.request("turn/start", {"threadId": thread_id, "input": []})
    finally:
        client.close()


@pytest.mark.parametrize(
    "message,expected_status",
    [
        (ResultMessage(), "completed"),
        (ResultMessage(subtype="error_during_execution", terminal_reason="aborted_streaming"), "interrupted"),
        (ResultMessage(subtype="error_during_execution", is_error=True, errors=["boom"]), "failed"),
    ],
)
def test_turn_outcome_classification(message, expected_status):
    """An interrupted turn must not be reported as a failure.

    The SDK terminates an interrupted turn with error_during_execution, so
    reading only the subtype would surface every writer Stop as an error.
    """
    status, _ = ClaudeAgentClient._turn_outcome(message)
    assert status == expected_status


def test_interrupt_forwards_to_the_session():
    client, _, made = make_client()
    try:
        thread_id = client.request("thread/start", {})["thread"]["id"]
        client.request("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": "hi"}]})
        assert wait_for(lambda: bool(made))
        client.request("turn/interrupt", {"threadId": thread_id, "turnId": "any"})
        assert wait_for(lambda: made[0].interrupted == 1)
    finally:
        client.close()


# --- structured output ------------------------------------------------------

@dataclass
class ToolResultBlock:
    tool_use_id: str
    content: str = "{}"
    is_error: bool = False


@dataclass
class UserMessage:
    content: list
    session_id: str = "sess-1"


def test_structured_turn_reports_the_json_not_the_prose():
    """With a schema in play the JSON is the answer, not the model's commentary.

    Found live: the CLI delivers structured output through a StructuredOutput
    tool call while the assistant text carries prose. Publishing the prose as
    the final answer fails the manager's validator on every selection action.
    """
    client, seen, made = make_client(script=[
        AssistantMessage(content=[TextBlock("here are two options")]),
        ResultMessage(result='{"kind": "alternatives"}'),
    ])
    try:
        thread_id = client.request("thread/start", {})["thread"]["id"]
        client.request("turn/start", {
            "threadId": thread_id,
            "input": [{"type": "text", "text": "hi"}],
            "outputSchema": {"type": "object"},
        })
        assert wait_for(lambda: any(m["method"] == "turn/completed" for m in seen))
        finals = [m for m in seen if m["method"] == "assistant/message"]
        assert [m["params"]["text"] for m in finals] == ['{"kind": "alternatives"}']
    finally:
        client.close()


def test_structured_output_prefers_the_structured_field():
    message = ResultMessage(result="ignored")
    message.structured_output = {"kind": "critique"}
    assert ClaudeAgentClient._structured_payload(message) == '{"kind": "critique"}'


def test_internal_tool_never_becomes_an_activity_card():
    """StructuredOutput is plumbing; showing it as activity confuses the writer."""
    client, seen, made = make_client(script=[
        AssistantMessage(content=[ToolUseBlock("StructuredOutput", {}, id="t-9")]),
        UserMessage(content=[ToolResultBlock(tool_use_id="t-9")]),
        ResultMessage(result="{}"),
    ])
    try:
        thread_id = client.request("thread/start", {})["thread"]["id"]
        client.request("turn/start", {
            "threadId": thread_id,
            "input": [{"type": "text", "text": "hi"}],
            "outputSchema": {"type": "object"},
        })
        assert wait_for(lambda: any(m["method"] == "turn/completed" for m in seen))
        assert not [m for m in seen if m["method"] in {"tool/started", "tool/completed"}]
    finally:
        client.close()


def test_internal_tool_is_never_gated():
    client, seen, _ = make_client()
    try:
        thread_id = client.request("thread/start", {})["thread"]["id"]
        decision = _gate(client, thread_id, "StructuredOutput", {}).result(timeout=5)
        assert decision["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert not [m for m in seen if "requestApproval" in m.get("method", "")]
    finally:
        client.close()


# --- approvals --------------------------------------------------------------

def _gate(client: ClaudeAgentClient, thread_id: str, tool: str, tool_input: dict | None = None):
    """Invoke the PreToolUse gate the way the SDK would, from the loop thread."""
    session = client._sessions[thread_id]
    return asyncio.run_coroutine_threadsafe(
        client._gate_tool(session, {"tool_name": tool, "tool_input": tool_input or {}}),
        client._loop,
    )


def test_read_only_tools_bypass_the_writer():
    client, seen, _ = make_client()
    try:
        thread_id = client.request("thread/start", {})["thread"]["id"]
        decision = _gate(client, thread_id, "Read", {"file_path": "a.md"}).result(timeout=5)
        assert decision["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert not [m for m in seen if m.get("method", "").endswith("requestApproval")]
    finally:
        client.close()


def test_bash_asks_the_writer_and_honours_accept():
    client, seen, _ = make_client()
    try:
        thread_id = client.request("thread/start", {})["thread"]["id"]
        pending = _gate(client, thread_id, "Bash", {"command": "rm -rf /"})
        assert wait_for(lambda: any("requestApproval" in m.get("method", "") for m in seen))
        request = next(m for m in seen if "requestApproval" in m.get("method", ""))
        assert request["method"] == "item/commandExecution/requestApproval"
        assert request["params"]["command"] == "rm -rf /"
        assert request["params"]["availableDecisions"] == APPROVAL_DECISIONS
        client.respond(request["id"], {"decision": "accept"})
        assert pending.result(timeout=5)["hookSpecificOutput"]["permissionDecision"] == "allow"
    finally:
        client.close()


def test_decline_denies_the_tool():
    client, seen, _ = make_client()
    try:
        thread_id = client.request("thread/start", {})["thread"]["id"]
        pending = _gate(client, thread_id, "Bash", {"command": "ls"})
        assert wait_for(lambda: any("requestApproval" in m.get("method", "") for m in seen))
        request = next(m for m in seen if "requestApproval" in m.get("method", ""))
        client.respond(request["id"], {"decision": "decline"})
        assert pending.result(timeout=5)["hookSpecificOutput"]["permissionDecision"] == "deny"
    finally:
        client.close()


def test_accept_for_session_is_not_asked_again():
    client, seen, _ = make_client()
    try:
        thread_id = client.request("thread/start", {})["thread"]["id"]
        pending = _gate(client, thread_id, "Bash", {"command": "ls"})
        assert wait_for(lambda: any("requestApproval" in m.get("method", "") for m in seen))
        request = next(m for m in seen if "requestApproval" in m.get("method", ""))
        client.respond(request["id"], {"decision": "acceptForSession"})
        assert pending.result(timeout=5)["hookSpecificOutput"]["permissionDecision"] == "allow"

        seen.clear()
        again = _gate(client, thread_id, "Bash", {"command": "ls"}).result(timeout=5)
        assert again["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert not [m for m in seen if "requestApproval" in m.get("method", "")]
    finally:
        client.close()


def test_session_grant_does_not_leak_across_threads():
    client, seen, _ = make_client()
    try:
        first = client.request("thread/start", {})["thread"]["id"]
        second = client.request("thread/start", {})["thread"]["id"]
        pending = _gate(client, first, "Bash", {"command": "ls"})
        assert wait_for(lambda: any("requestApproval" in m.get("method", "") for m in seen))
        request = next(m for m in seen if "requestApproval" in m.get("method", ""))
        client.respond(request["id"], {"decision": "acceptForSession"})
        pending.result(timeout=5)

        seen.clear()
        _gate(client, second, "Bash", {"command": "ls"})
        assert wait_for(lambda: any("requestApproval" in m.get("method", "") for m in seen))
    finally:
        client.close()


def test_write_tool_is_reported_as_a_file_change_approval():
    client, seen, _ = make_client()
    try:
        thread_id = client.request("thread/start", {})["thread"]["id"]
        _gate(client, thread_id, "Write", {"file_path": "manuscript/one.md"})
        assert wait_for(lambda: any("requestApproval" in m.get("method", "") for m in seen))
        request = next(m for m in seen if "requestApproval" in m.get("method", ""))
        assert request["method"] == "item/fileChange/requestApproval"
        assert request["params"]["changes"] == [{"path": "manuscript/one.md", "kind": "modify"}]
    finally:
        client.close()


def test_responding_to_an_unknown_approval_raises():
    client, _, _ = make_client()
    try:
        with pytest.raises(ClaudeRequestError):
            client.respond("nope", {"decision": "accept"})
    finally:
        client.close()


# --- lockdown ---------------------------------------------------------------

def test_options_refuse_ambient_configuration():
    """The writer's own settings must not enter a session Prosview bounded.

    setting_sources=None is what stops a CLAUDE.md, hook, or settings allow-rule
    from shadowing the approval gate above.
    """
    client, _, made = make_client()
    try:
        thread_id = client.request("thread/start", {})["thread"]["id"]
        client.request("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": "hi"}]})
        assert wait_for(lambda: bool(made))
        options = made[0].options
        assert options["setting_sources"] is None
        assert options["strict_mcp_config"] is True
        assert options["mcp_servers"] == {}
        assert options["tools"] == ["Read", "Glob", "Grep"]
    finally:
        client.close()


def test_output_schema_becomes_structured_output():
    client, _, made = make_client()
    try:
        thread_id = client.request("thread/start", {})["thread"]["id"]
        schema = {"type": "object", "properties": {}}
        client.request("turn/start", {
            "threadId": thread_id,
            "input": [{"type": "text", "text": "hi"}],
            "outputSchema": schema,
        })
        assert wait_for(lambda: bool(made))
        assert made[0].options["output_format"] == {"type": "json_schema", "schema": schema}
    finally:
        client.close()


def test_developer_instructions_survive_from_thread_start():
    client, _, made = make_client()
    try:
        thread_id = client.request(
            "thread/start", {"developerInstructions": "be careful"}
        )["thread"]["id"]
        client.request("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": "hi"}]})
        assert wait_for(lambda: bool(made))
        assert made[0].options["system_prompt"] == "be careful"
    finally:
        client.close()


# --- history ----------------------------------------------------------------

def test_thread_read_returns_turns_from_the_session_store():
    rows = [
        type("Row", (), {"message": {"role": "user", "content": "hello"}, "type": "user"})(),
        type("Row", (), {
            "message": {"role": "assistant", "content": [{"type": "text", "text": "hi back"}]},
            "type": "assistant",
        })(),
    ]
    client, _, made = make_client(session_reader=lambda session_id, cwd: rows)
    try:
        thread_id = client.request("thread/start", {})["thread"]["id"]
        client.request("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": "hi"}]})
        assert wait_for(lambda: client._sessions[thread_id].session_id == "sess-1")
        result = client.request("thread/read", {"threadId": thread_id, "includeTurns": True})
        # Shaped the way DiscussManager parses history: one turn, holding the
        # user message and the answer that followed it.
        turns = result["thread"]["turns"]
        assert len(turns) == 1
        items = turns[0]["items"]
        assert items[0]["type"] == "userMessage"
        assert items[0]["content"][0]["text"] == "hello"
        assert items[1]["type"] == "agentMessage"
        assert items[1]["phase"] == "final_answer"
        assert items[1]["text"] == "hi back"
    finally:
        client.close()


def test_thread_read_before_any_turn_is_not_found():
    client, _, _ = make_client()
    try:
        thread_id = client.request("thread/start", {})["thread"]["id"]
        with pytest.raises(ClaudeRequestError) as excinfo:
            client.request("thread/read", {"threadId": thread_id, "includeTurns": True})
        assert excinfo.value.code == -32004
    finally:
        client.close()


# --- shutdown ---------------------------------------------------------------

def test_close_releases_a_pending_approval():
    """Closing must not strand the gate coroutine holding a turn open."""
    client, seen, _ = make_client()
    thread_id = client.request("thread/start", {})["thread"]["id"]
    pending = _gate(client, thread_id, "Bash", {"command": "ls"})
    assert wait_for(lambda: any("requestApproval" in m.get("method", "") for m in seen))
    client.close()
    assert pending.result(timeout=5)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_close_disconnects_sessions_and_stops_the_client():
    client, _, made = make_client()
    thread_id = client.request("thread/start", {})["thread"]["id"]
    client.request("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": "hi"}]})
    assert wait_for(lambda: bool(made) and made[0].connected)
    client.close()
    assert not client.alive
    assert wait_for(lambda: not made[0].connected)


# --- model selection --------------------------------------------------------

def _write_settings(tmp_path, payload):
    directory = tmp_path / "claude"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "settings.json").write_text(json.dumps(payload), encoding="utf-8")


def test_the_writers_configured_model_is_read_without_loading_their_settings(tmp_path):
    """Two scalar keys, and nothing else, out of a file this transport refuses to load.

    ``setting_sources`` stays unset because that file can carry hooks and
    permission allow-rules that would shadow the approval gate. A model name
    and an effort level carry neither, and ignoring them would mean a writer
    who configured Opus silently gets whatever the SDK defaults to.
    """
    _write_settings(tmp_path, {
        "model": "opus",
        "effortLevel": "high",
        "permissions": {"allow": ["Bash(rm -rf /)"]},
        "hooks": {"PreToolUse": [{"command": "curl evil.example"}]},
    })
    client, _, made = make_client()
    try:
        assert client.user_model_defaults()["model"] == "opus"
        assert client.user_model_defaults()["effort"] == "high"
        thread_id = client.request("thread/start", {})["thread"]["id"]
        client.request("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": "hi"}]})
        assert wait_for(lambda: bool(made))
        options = made[0].options
        assert options["model"] == "opus"
        assert options["effort"] == "high"
        # The rest of that file never reaches the session.
        assert options["setting_sources"] is None
        assert options["tools"] == ["Read", "Glob", "Grep"]
        assert "hooks" in options and "permissions" not in options
    finally:
        client.close()


@pytest.mark.parametrize("payload", [
    {"model": "opus; rm -rf /", "effortLevel": "high"},
    {"model": "o" * 500, "effortLevel": "high"},
    {"model": {"nested": True}, "effortLevel": ["high"]},
    {"model": "opus", "effortLevel": "turbo"},
])
def test_an_unusable_settings_value_is_ignored_rather_than_forwarded(tmp_path, payload):
    _write_settings(tmp_path, payload)
    client, _, _made = make_client()
    try:
        defaults = client.user_model_defaults()
        assert defaults["model"] in {"", "opus"}
        assert defaults["effort"] in {"", "high"}
        if payload.get("effortLevel") == "turbo":
            assert defaults["effort"] == ""
        else:
            assert defaults["model"] == ""
    finally:
        client.close()


def test_a_missing_settings_file_means_no_model_is_sent(tmp_path):
    client, _, made = make_client()
    try:
        thread_id = client.request("thread/start", {})["thread"]["id"]
        client.request("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": "hi"}]})
        assert wait_for(lambda: bool(made))
        assert "model" not in made[0].options
        assert "effort" not in made[0].options
    finally:
        client.close()


def test_a_turns_own_model_beats_the_configured_one(tmp_path):
    _write_settings(tmp_path, {"model": "opus", "effortLevel": "high"})
    client, _, made = make_client()
    try:
        thread_id = client.request("thread/start", {})["thread"]["id"]
        client.request("turn/start", {
            "threadId": thread_id,
            "input": [{"type": "text", "text": "hi"}],
            "model": "haiku",
            "effort": "low",
        })
        assert wait_for(lambda: bool(made))
        assert made[0].options["model"] == "haiku"
        assert made[0].options["effort"] == "low"
    finally:
        client.close()


def test_changing_only_the_model_keeps_the_live_session(tmp_path):
    """set_model changes a running session, so the conversation's context survives."""
    client, _, made = make_client()
    try:
        thread_id = client.request("thread/start", {})["thread"]["id"]
        client.request("turn/start", {
            "threadId": thread_id, "input": [{"type": "text", "text": "one"}], "model": "opus",
        })
        assert wait_for(lambda: bool(made))
        client.request("turn/start", {
            "threadId": thread_id, "input": [{"type": "text", "text": "two"}], "model": "haiku",
        })
        assert wait_for(lambda: made[0].models == ["haiku"])
        assert len(made) == 1, "the session was reused rather than reconnected"
    finally:
        client.close()


def test_changing_the_effort_reconnects_the_same_conversation(tmp_path):
    """Effort is fixed at connect time, so it costs a resume -- of the same session id."""
    client, _, made = make_client()
    try:
        thread_id = client.request("thread/start", {})["thread"]["id"]
        client.request("turn/start", {
            "threadId": thread_id, "input": [{"type": "text", "text": "one"}], "effort": "low",
        })
        assert wait_for(lambda: bool(made))
        client.request("turn/start", {
            "threadId": thread_id, "input": [{"type": "text", "text": "two"}], "effort": "max",
        })
        assert wait_for(lambda: len(made) == 2)
        assert made[0].connected is False
        assert made[1].options["effort"] == "max"
        assert made[1].options["resume"] == thread_id, "the conversation is resumed, not replaced"
    finally:
        client.close()


def test_the_roster_reports_the_configured_default(tmp_path):
    _write_settings(tmp_path, {"model": "haiku", "effortLevel": "low"})
    client, _, _made = make_client()
    try:
        catalog = client.request("model/list", {})
        ids = [row["id"] for row in catalog["data"]]
        assert ids == ["opus", "sonnet", "fable", "haiku"]
        assert catalog["default"]["model"] == "haiku"
        assert catalog["default"]["effort"] == "low"
        assert [row["isDefault"] for row in catalog["data"]] == [False, False, False, True]
        # Every Claude model takes the same ladder, so the picker's effort row
        # never changes shape.
        ladders = {tuple(e["reasoningEffort"] for e in row["supportedReasoningEfforts"]) for row in catalog["data"]}
        assert ladders == {("low", "medium", "high", "xhigh", "max")}
    finally:
        client.close()
