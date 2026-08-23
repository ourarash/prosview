from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from proseview.discuss import (
    ContextBuilder,
    ContextError,
    DiscussStateStore,
    EventBuffer,
    sanitize_agent_message,
)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "novel"
    (root / "manuscript" / "ch01").mkdir(parents=True)
    (root / "manuscript" / "ch01" / "opening.md").write_text(
        "---\ntitle: Opening\n---\n\n# Opening\n\nThe first line.\n",
        encoding="utf-8",
    )
    (root / "plans").mkdir()
    (root / "plans" / "arc.md").write_text("# Arc\n\nA plan.\n", encoding="utf-8")
    (root / "plans" / "notes.txt").write_text("notes\n", encoding="utf-8")
    return root


def test_context_builder_includes_document_selection_and_sorted_attachments(tmp_path: Path):
    root = _repo(tmp_path)
    (root / "plans" / "validator.py").write_text("def validate():\n    return True\n", encoding="utf-8")
    bundle = ContextBuilder(root).build(
        {"kind": "scene", "path": "ch01/opening.md"},
        "Why does this work?",
        selection="The first line.",
        attachments=[{"kind": "folder", "path": "plans"}],
    )

    assert bundle.question == "Why does this work?"
    assert [item.path for item in bundle.items] == [
        "manuscript/ch01/opening.md",
        "plans/arc.md",
        "plans/notes.txt",
        "plans/validator.py",
    ]
    assert bundle.selection == "The first line."
    assert "BEGIN UNTRUSTED DOCUMENT" in bundle.prompt
    assert "use its repository-relative path" in bundle.prompt
    assert "never use an absolute filesystem path" in bundle.prompt
    assert bundle.prompt.index("plans/arc.md") < bundle.prompt.index("plans/notes.txt")
    assert "def validate" in bundle.prompt


def test_context_builder_can_omit_default_document_and_attach_it_explicitly(tmp_path: Path):
    root = _repo(tmp_path)
    builder = ContextBuilder(root)

    omitted = builder.build(
        {"kind": "scene", "path": "ch01/opening.md"},
        "Answer without the current document",
        include_current_document=False,
        attachments=[{"kind": "file", "path": "plans/arc.md"}],
    )
    assert [item.path for item in omitted.items] == ["plans/arc.md"]
    assert "The first line." not in omitted.prompt

    reattached = builder.build(
        {"kind": "scene", "path": "ch01/opening.md"},
        "Use it again",
        include_current_document=False,
        attachments=[{"kind": "file", "path": "manuscript/ch01/opening.md"}],
    )
    assert [item.path for item in reattached.items] == ["manuscript/ch01/opening.md"]


def test_context_builder_rejects_non_boolean_current_document_flag(tmp_path: Path):
    root = _repo(tmp_path)
    with pytest.raises(ContextError, match="include_current_document must be a boolean"):
        ContextBuilder(root).build(
            {"kind": "scene", "path": "ch01/opening.md"},
            "Question",
            include_current_document="false",  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("path", ["../secret.md", "/tmp/secret.md"])
def test_context_builder_rejects_paths_outside_repository(tmp_path: Path, path: str):
    root = _repo(tmp_path)
    with pytest.raises(ContextError, match="outside|relative"):
        ContextBuilder(root).build(
            {"kind": "file", "path": "plans/arc.md"},
            "Question",
            attachments=[{"kind": "file", "path": path}],
        )


def test_context_builder_rejects_symlink_escape_and_binary(tmp_path: Path):
    root = _repo(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    (root / "plans" / "escape.md").symlink_to(outside)
    (root / "plans" / "image.png").write_bytes(b"\x89PNG")

    builder = ContextBuilder(root)
    with pytest.raises(ContextError, match="outside"):
        builder.build(
            {"kind": "file", "path": "plans/arc.md"},
            "Question",
            attachments=[{"kind": "file", "path": "plans/escape.md"}],
        )
    with pytest.raises(ContextError, match="text"):
        builder.build(
            {"kind": "file", "path": "plans/arc.md"},
            "Question",
            attachments=[{"kind": "file", "path": "plans/image.png"}],
        )


def test_context_builder_rejects_direct_hidden_and_internal_attachments(tmp_path: Path):
    root = _repo(tmp_path)
    hidden = root / ".private"
    hidden.mkdir()
    (hidden / "token.txt").write_text("secret", encoding="utf-8")

    with pytest.raises(ContextError, match="safe visible repository"):
        ContextBuilder(root).build(
            {"kind": "file", "path": "plans/arc.md"},
            "Question",
            attachments=[{"kind": "file", "path": ".private/token.txt"}],
        )


def test_context_builder_enforces_total_limit_without_truncating(tmp_path: Path):
    root = _repo(tmp_path)
    (root / "plans" / "large.txt").write_text("x" * 100, encoding="utf-8")
    builder = ContextBuilder(root, max_file_bytes=200, max_total_bytes=80)
    with pytest.raises(ContextError, match="total context"):
        builder.build(
            {"kind": "file", "path": "plans/arc.md"},
            "Question",
            attachments=[{"kind": "file", "path": "plans/large.txt"}],
        )


def test_context_builder_can_fit_whole_files_under_an_agent_prompt_limit(tmp_path: Path):
    root = _repo(tmp_path)
    (root / "plans" / "a-large.md").write_text("a" * 180, encoding="utf-8")
    (root / "plans" / "b-small.md").write_text("b" * 20, encoding="utf-8")
    builder = ContextBuilder(
        root,
        max_file_bytes=300,
        max_total_bytes=1_000,
        # The shared preamble is fixed overhead on every turn; this budget is
        # the content room left over, so it moves when the preamble does.
        max_prompt_chars=1010,
        allow_partial=True,
    )

    bundle = builder.build(
        {"kind": "scene", "path": "ch01/opening.md"},
        "Check continuity.",
        attachments=[{"kind": "folder", "path": "plans"}],
    )

    assert len(bundle.prompt) <= 1010
    assert bundle.items[0].path == "manuscript/ch01/opening.md"
    assert "plans/a-large.md" in bundle.omitted_paths
    assert "plans/b-small.md" in [item.path for item in bundle.items]
    assert "CONTEXT LIMIT NOTICE" in bundle.prompt


def test_context_builder_rejects_malformed_utf8_and_deduplicates_overlap(tmp_path: Path):
    root = _repo(tmp_path)
    (root / "plans" / "bad.txt").write_bytes(b"\xff\xfe")
    builder = ContextBuilder(root)
    with pytest.raises(ContextError, match="UTF-8"):
        builder.build(
            {"kind": "file", "path": "plans/arc.md"},
            "Question",
            attachments=[{"kind": "file", "path": "plans/bad.txt"}],
        )
    (root / "plans" / "bad.txt").unlink()

    bundle = builder.build(
        {"kind": "file", "path": "plans/arc.md"},
        "Question",
        attachments=[
            {"kind": "file", "path": "plans/arc.md"},
            {"kind": "folder", "path": "plans"},
        ],
    )
    assert [item.path for item in bundle.items].count("plans/arc.md") == 1


def test_context_builder_enforces_question_and_file_count_limits(tmp_path: Path):
    root = _repo(tmp_path)
    (root / "plans" / "extra.md").write_text("extra", encoding="utf-8")
    with pytest.raises(ContextError, match="question exceeds"):
        ContextBuilder(root, max_question_bytes=4).build(
            {"kind": "file", "path": "plans/arc.md"}, "long question"
        )
    with pytest.raises(ContextError, match="more than 1"):
        ContextBuilder(root, max_files=1).build(
            {"kind": "file", "path": "plans/arc.md"},
            "Q",
            attachments=[{"kind": "file", "path": "plans/extra.md"}],
        )


def test_state_store_is_external_atomic_and_user_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = _repo(tmp_path)
    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    store = DiscussStateStore(root)

    store.set("scene", "ch01/opening.md", "thread-1")

    assert store.get("scene", "ch01/opening.md") == "thread-1"
    assert store.path == state_home / "proseview" / "discuss.json"
    assert not store.path.is_relative_to(root)
    if os.name != "nt":
        # Windows has no POSIX mode bits to assert on; chmod there only
        # toggles the read-only flag.
        assert store.path.stat().st_mode & 0o777 == 0o600
    data = json.loads(store.path.read_text(encoding="utf-8"))
    assert str(root.resolve()) not in json.dumps(data)


def test_state_store_recovers_from_malformed_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = _repo(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    store = DiscussStateStore(root)
    store.path.parent.mkdir(parents=True)
    store.path.write_text("not-json", encoding="utf-8")

    assert store.get("scene", "ch01/opening.md") is None
    store.set("scene", "ch01/opening.md", "thread-2")
    assert store.get("scene", "ch01/opening.md") == "thread-2"


def test_state_store_keeps_a_model_choice_with_its_own_conversation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The pin belongs to one conversation, not to the project or the document."""
    root = _repo(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    store = DiscussStateStore(root)
    store.set("scene", "ch01/opening.md", "thread-1")
    store.set("scene", "ch01/opening.md", "thread-2")

    store.set_model("scene", "ch01/opening.md", "thread-1", {"model": "gpt-5.6-luna", "effort": "high"})

    assert store.model("scene", "ch01/opening.md", "thread-1") == {
        "model": "gpt-5.6-luna", "effort": "high",
    }
    assert store.model("scene", "ch01/opening.md", "thread-2") == {"model": "", "effort": ""}
    assert store.model("scene", "ch01/opening.md", "thread-1", "claude") == {"model": "", "effort": ""}
    # A conversation that no longer exists is not a conversation to pin.
    store.set_model("scene", "ch01/opening.md", "gone", {"model": "gpt-5.6-luna", "effort": ""})
    assert store.model("scene", "ch01/opening.md", "gone") == {"model": "", "effort": ""}


def test_state_store_refuses_an_unusable_model_and_survives_a_stored_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A hand-edited state file must not make a conversation unopenable."""
    root = _repo(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    store = DiscussStateStore(root)
    store.set("scene", "ch01/opening.md", "thread-1")

    with pytest.raises(ContextError):
        store.set_model("scene", "ch01/opening.md", "thread-1", {"model": "", "effort": "turbo"})

    data = json.loads(store.path.read_text(encoding="utf-8"))
    entry = data["repositories"][store.root_key]["agents"]["codex"]
    entry["threads"][0]["model"] = {"model": "x" * 500, "effort": "turbo"}
    store.path.write_text(json.dumps(data), encoding="utf-8")

    assert store.model("scene", "ch01/opening.md", "thread-1") == {"model": "", "effort": ""}
    assert store.get("scene", "ch01/opening.md") == "thread-1"


def test_state_store_is_project_scoped_and_isolates_repositories_and_agents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    one = _repo(tmp_path / "one")
    two = _repo(tmp_path / "two")
    first = DiscussStateStore(one)
    second = DiscussStateStore(two)
    first.set("scene", "same.md", "scene-thread")
    assert first.get("scene", "same.md") == "scene-thread"
    assert first.get("file", "another.md") == "scene-thread"
    assert first.get("scene", "same.md", "claude") is None
    assert second.get("scene", "same.md") is None


def test_event_buffer_replays_or_requests_snapshot_when_gap_was_evicted():
    buffer = EventBuffer(max_events=2, max_bytes=10_000)
    one = buffer.publish("turn.started", {"n": 1})
    two = buffer.publish("response.delta", {"n": 2})
    three = buffer.publish("turn.completed", {"n": 3})

    assert [event.id for event in buffer.replay(two.id)] == [three.id]
    assert [event.id for event in buffer.replay(one.id)] == [two.id, three.id]
    assert buffer.replay(0) is None


def test_protocol_adapter_never_forwards_raw_reasoning():
    started = sanitize_agent_message({
        "method": "item/started",
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "item": {
                "id": "reason-1",
                "type": "reasoning",
                "summary": ["Safe summary"],
                "content": ["SECRET RAW CHAIN OF THOUGHT"],
            },
        },
    })
    raw_delta = sanitize_agent_message({
        "method": "item/reasoning/textDelta",
        "params": {"delta": "SECRET RAW CHAIN OF THOUGHT"},
    })
    summary_delta = sanitize_agent_message({
        "method": "item/reasoning/summaryTextDelta",
        "params": {"threadId": "thread-1", "turnId": "turn-1", "delta": "Checking context"},
    })

    assert started == []
    assert raw_delta == []
    assert summary_delta[0]["type"] == "progress.delta"
    assert "SECRET" not in json.dumps(started + raw_delta + summary_delta)
