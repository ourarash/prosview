"""Shared harness for the Proseview end-to-end suite.

Unlike the rest of ``tests/``, these tests start the real application: a
``python -m proseview`` subprocess serving a throwaway copy of
``fixtures/demo-repo``. Nothing here imports ``proseview`` in-process, so the
suite exercises the same boot path a user gets from the CLI.

Everything in this module is stdlib-only. The browser tier layers Playwright on
top of these same fixtures.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import random
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_REPO = REPO_ROOT / "fixtures" / "demo-repo"

#: How long to wait for the server subprocess to answer its first request.
# Hosted runners are slow enough that a fixed 30s starves the server before
# it binds: the macOS leg spends 21 minutes where Ubuntu spends two, and
# every e2e test there errors with "did not write port". Local runs keep the
# short deadline so a genuinely stuck server still fails fast.
BOOT_TIMEOUT = 90.0 if os.environ.get("CI") else 30.0
#: Watch interval handed to the server. Low so live-reload tests stay quick.
WATCH_INTERVAL = 0.5

#: Scene the mutating tests edit. Small, has frontmatter, no annotations.
SCENE_REL = "ch01/01-opening.md"
#: Scene carrying inline TODO/NOTE comments (created by ``_seed_annotated_scene``).
ANNOTATED_SCENE_REL = "ch01/03-annotated.md"
#: Scene reproducing a book layout where raw HTML precedes the H1. The browser
#: renders the HTML as a non-text atom, so Markdown character offsets cannot be
#: used as ProseMirror selection offsets.
HTML_LEAD_SCENE_REL = "ch01/05-html-lead.md"
#: Generated ~10k-word scene used by the large-file cases.
LARGE_SCENE_REL = "ch03/01-long-haul.md"
#: Scene with no frontmatter at all -- what an Obsidian vault or an imported
#: draft looks like. The Scene tab has to read as "these fields are optional"
#: here, not as a broken panel full of "Unknown".
BARE_SCENE_REL = "ch01/04-bare.md"
#: Manuscript Markdown nested two levels below the manuscript root. It *is* a
#: scene now -- scene discovery accepts any depth -- but it is still reachable
#: as a repo file, which is what the search and sidebar tests here exercise.
#: The name is deliberately unlike any scene's: search matches files on path
#: substring, and folders sort above files, so a shared prefix would outrank
#: the real scene.
NESTED_MANUSCRIPT_NOTE = "manuscript/ch01/review/reader-pass-notes.md"

#: Scenes carrying story-layer fields, so the Timeline tab has threads and a
#: chronology to draw. Deliberately told out of order: day 5 is read before
#: day 4, which is the crossing the chronology view exists to show.
STORY_SCENES = (
    ("ch03/01-present-a.md", "present", 5),
    ("ch03/02-present-b.md", "present", 6),
    ("ch03/03-present-c.md", "present", 7),
    ("ch03/04-flashback.md", "recollection", 1),
)

#: Printed by every stub agent so tests can recognise a real spawn.
AGENT_MARKER = "PROSEVIEW_FAKE_AGENT"


# ── repo construction ───────────────────────────────────────────────────────


def _seed_skills(root: Path) -> None:
    """Give the repo a ``skills/`` tree.

    ``fixtures/demo-repo`` has none, and ``generator._load_skills`` returns ``[]``
    for a missing directory -- which makes the template omit the Skills button
    entirely. Without this the selection-menu tests would silently assert
    against a control that was never rendered.

    Three skills, chosen to cover every branch of ``_load_skills``. Note the
    selection menu lists only ``snippet-`` prefixed skills, so the one carrying
    ``agents/openai.yaml`` -- the display-name / default-prompt path -- has to
    be a snippet for that path to be observable in the browser.
    """
    skills = root / ".proseview/skills"

    # Scene skill: no snippet prefix, so it is absent from the selection menu.
    tighten = skills / "tighten-prose"
    tighten.mkdir(parents=True, exist_ok=True)
    (tighten / "SKILL.md").write_text(
        "---\nname: tighten-prose\n---\n\nRemove filler from the selected passage.\n",
        encoding="utf-8",
    )

    # Snippet skill whose label comes from openai.yaml, not the directory name.
    continuity = skills / "snippet-continuity"
    (continuity / "agents").mkdir(parents=True, exist_ok=True)
    (continuity / "SKILL.md").write_text(
        "---\nname: snippet-continuity\n---\n\nCheck the passage against the story bible.\n",
        encoding="utf-8",
    )
    (continuity / "agents" / "openai.yaml").write_text(
        "interface:\n"
        '  display_name: "Continuity Check"\n'
        '  short_description: "Flag contradictions with the story bible"\n'
        '  default_prompt: "Check this passage for continuity errors."\n',
        encoding="utf-8",
    )

    # Snippet skill with no openai.yaml: display name falls back to the name.
    snippet = skills / "snippet-sensory"
    snippet.mkdir(parents=True, exist_ok=True)
    (snippet / "SKILL.md").write_text(
        "---\nname: snippet-sensory\n---\n\nAdd one concrete sensory detail.\n",
        encoding="utf-8",
    )


def _seed_annotated_scene(root: Path) -> None:
    """A scene that already contains inline TODO/NOTE comments.

    The editor round-trip tests need a document where annotations are parsed as
    ProseMirror atom nodes, so they can prove a save doesn't degrade them back
    into literal ``<!-- ... -->`` text.
    """
    path = root / "manuscript" / ANNOTATED_SCENE_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "title: Annotated Ledger\n"
        "chapter: Chapter 1\n"
        "status: revision\n"
        "characters:\n"
        "  - Rena\n"
        "  - Patel\n"
        "todos:\n"
        "  - Verify the safe's brand against chapter three\n"
        "---\n"
        "\n"
        "# Annotated Ledger\n"
        "\n"
        "<!-- TODO: Tighten this opening beat -->\n"
        "\n"
        "Patel arrived with the ledger already open, thumb wedged at a column of "
        "numbers that refused to reconcile. He set it on the counter without a word.\n"
        "\n"
        "<!-- NOTE[continuity]: Patel should not know about the safe yet -->\n"
        "\n"
        "Rena read the column twice. The second reading did not improve it. She "
        "found a pencil, crossed out a figure, and wrote a smaller one above it.\n"
        "\n"
        "The shop stayed *quiet* in the way a held breath is quiet.\n",
        encoding="utf-8",
    )


def _seed_html_lead_scene(root: Path) -> None:
    """A scene whose rendered text deliberately diverges from its Markdown."""
    path = root / "manuscript" / HTML_LEAD_SCENE_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "title: The King\n"
        "chapter: Chapter 1\n"
        "status: revision\n"
        "---\n"
        "\n"
        '<img src="/repo-asset/manuscript/ch01/king.png" alt="The king at dusk">\n'
        "\n"
        "# The King\n"
        "\n"
        "But I have another theory.\n"
        "\n"
        "What if the whole thing was a rumor the king started to save his pride? "
        "After all, who kills a young girl every day if he can get the fear for free?\n",
        encoding="utf-8",
    )


def _seed_bare_scene(root: Path) -> None:
    """A scene that is plain Markdown, with no YAML block of any kind."""
    path = root / "manuscript" / BARE_SCENE_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# The Long Way Round\n"
        "\n"
        "The shop was shut by the time she reached it, and the lights in the "
        "upstairs window had already gone out.\n"
        "\n"
        "She waited on the step anyway, counting the cars that did not stop.\n",
        encoding="utf-8",
    )


def _seed_large_scene(root: Path) -> int:
    """Generate a ~10k-word scene and return its word count.

    Deterministically seeded, with a wide enough vocabulary that MATTR/MTLD land
    in a plausible range rather than degenerating on repeated filler.
    """
    rng = random.Random(20260802)
    subjects = [
        "Rena", "Lowe", "Patel", "the ledger", "the pier boy", "the harbour master",
        "the dockhand", "the auditor", "the clerk", "the tide", "the river",
    ]
    verbs = [
        "counted", "questioned", "abandoned", "recovered", "measured", "doubted",
        "annotated", "misplaced", "reconciled", "defended", "postponed", "revisited",
    ]
    objects = [
        "the weekly total", "a column of figures", "the safe's dial", "a torn receipt",
        "the morning delivery", "an unsigned invoice", "the shop's account",
        "a promise from spring", "the last honest number", "a stack of manifests",
    ]
    codas = [
        "and said nothing afterward", "before the kettle boiled", "against her better sense",
        "while the market woke", "with the patience of a creditor", "twice, then once more",
        "as though it were arithmetic", "under a grey and unhelpful sky",
    ]

    paragraphs: list[str] = []
    words = 0
    index = 0
    while words < 10_000:
        sentences = []
        for _ in range(rng.randint(4, 7)):
            sentences.append(
                f"{rng.choice(subjects)} {rng.choice(verbs)} {rng.choice(objects)} "
                f"{rng.choice(codas)}."
            )
        para = " ".join(sentences)
        paragraphs.append(para)
        words += len(para.split())
        index += 1
        # Sprinkle a few annotations so the large-file round-trip has atoms to
        # preserve, not just prose.
        if index % 40 == 0:
            paragraphs.append(f"<!-- NOTE[question]: Does beat {index} earn its place? -->")

    body = "\n\n".join(paragraphs)
    path = root / "manuscript" / LARGE_SCENE_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "title: Long Haul\n"
        "chapter: Chapter 3\n"
        "status: draft\n"
        "characters:\n"
        "  - Rena\n"
        "  - Patel\n"
        "---\n"
        "\n"
        "# Long Haul\n"
        "\n" + body + "\n",
        encoding="utf-8",
    )
    return words


def _build_repo(dest: Path) -> Path:
    """Copy the committed fixture into *dest* and enrich it for E2E use."""
    shutil.copytree(FIXTURE_REPO, dest, dirs_exist_ok=True)
    # The committed fixture is gitignored but a stale .proseview/server.json can
    # linger from a local run. Leaving it would let `proseview propose` resolve
    # to a dead server instead of ours.
    shutil.rmtree(dest / ".proseview", ignore_errors=True)
    _seed_skills(dest)
    _seed_annotated_scene(dest)
    _seed_html_lead_scene(dest)
    _seed_bare_scene(dest)
    _seed_large_scene(dest)
    for rel, thread, day in STORY_SCENES:
        scene = dest / "manuscript" / rel
        scene.parent.mkdir(parents=True, exist_ok=True)
        scene.write_text(
            f"---\ntitle: {rel.split('/')[-1][:-3].replace('-', ' ').title()}\n"
            f"chapter: Chapter 3\nthread: {thread}\nday: {day}\n"
            f"when: Day {day}\nwhere: The shop\n---\n\nA scene on day {day}.\n",
            encoding="utf-8",
        )

    note = dest / NESTED_MANUSCRIPT_NOTE
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(
        "# Opening review\n\nThe safe reveal lands too early in this draft.\n",
        encoding="utf-8",
    )
    # An image plus a document exercising every path the renderer takes:
    # a relative Markdown reference, a raw <img> carrying an event handler, and
    # a remote URL.
    (dest / "img").mkdir(parents=True, exist_ok=True)
    (dest / "img" / "cover.png").write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAoAAAAKCAYAAACNMs+9AAAAFUlEQVR42mNk+M+ACzDiVDBUFAAAxgQDAeVX6gYAAAAASUVORK5CYII="
        )
    )
    images_doc = dest / "plans" / "images-demo.md"
    images_doc.parent.mkdir(parents=True, exist_ok=True)
    images_doc.write_text(
        "# Images\n\n"
        "![The cover](../img/cover.png)\n\n"
        '<img src="../img/cover.png" alt="Raw tag" width="10" onerror="window.__pwned = true">\n\n'
        "![Remote](https://example.invalid/remote.png)\n",
        encoding="utf-8",
    )

    # A non-scene document with block Markdown the preview has to render:
    # a GFM table and a horizontal rule. Both used to be dumped as raw source.
    notes = dest / "plans" / "structure-notes.md"
    notes.parent.mkdir(parents=True, exist_ok=True)
    notes.write_text(
        "# Structure Notes\n\n"
        "| Chapter | Status | Words |\n"
        "| --- | --- | ---: |\n"
        "| ch01 | Drafted | 1200 |\n"
        "| ch02 | Revising | 900 |\n\n"
        "---\n\n"
        "Prose after the rule.\n",
        encoding="utf-8",
    )

    scripts = dest / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "check_continuity.py").write_text(
        "def check_continuity(scene):\n    return bool(scene)\n",
        encoding="utf-8",
    )
    (scripts / "hostile-preview.md").write_text(
        "# Safe heading\n\n"
        "<img src=x onerror=\"window.__previewPwned = true\">\n\n"
        "[Unsafe link](javascript:window.__previewPwned=true)\n",
        encoding="utf-8",
    )
    private = dest / ".private"
    private.mkdir(exist_ok=True)
    (private / "token.txt").write_text("fixture secret\n", encoding="utf-8")
    return dest


# ── agent stubs ─────────────────────────────────────────────────────────────


def _make_runnable(bin_dir: Path, name: str) -> None:
    """Make a stub launchable by name on this platform.

    POSIX needs the execute bit and honours the shebang. Windows honours
    neither: a bare `codex` is not on PATHEXT, so shutil.which never finds it
    and every agent looks uninstalled. A .cmd wrapper naming this interpreter
    is what makes the same stub reachable there.
    """
    script = bin_dir / name
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    if os.name == "nt":
        (bin_dir / f"{name}.cmd").write_text(
            f'@echo off\r\n"{sys.executable}" "%~dp0{name}" %*\r\n', encoding="utf-8"
        )


def _install_stub(bin_dir: Path, name: str, source: str) -> None:
    (bin_dir / name).write_text(source, encoding="utf-8")
    _make_runnable(bin_dir, name)


def _write_agent_stubs(bin_dir: Path) -> Path:
    """Create fake ``codex`` / ``claude`` executables.

    ``codex`` is a full app-server stub -- the protocol Discuss actually
    speaks. ``claude`` only has to exist: the Claude client gates on
    ``shutil.which("claude")`` before handing off to the SDK, which the
    tests replace with ``tests/e2e/fake_claude_sdk``.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in ("claude",):
        # Python rather than /bin/sh: Windows has neither sh nor shebang
        # handling, and one language for every stub means one wrapper below.
        _install_stub(
            bin_dir,
            name,
            "#!/usr/bin/env python3\n"
            "import sys\n"
            f'print("{AGENT_MARKER} {name} argv:" + " ".join(sys.argv[1:]), flush=True)\n'
            "for line in sys.stdin:\n"
            '    print("STDIN:" + line.rstrip("\\n"), flush=True)\n',
        )

    codex = bin_dir / "codex"
    codex.write_text(
        """#!/usr/bin/env python3
import html, json, os, pathlib, sys, time

if len(sys.argv) >= 3 and sys.argv[1:3] == ['app-server', 'generate-json-schema']:
    out = pathlib.Path(sys.argv[sys.argv.index('--out') + 1]) / 'v2'
    out.mkdir(parents=True, exist_ok=True)
    schemas = {
        'ThreadStartParams.json': '{}',
        'ThreadReadParams.json': '{"includeTurns":true}',
        'TurnStartParams.json': '{"summary":true,"readableRoots":true,"outputSchema":true}',
        'TurnInterruptParams.json': '{}',
        'CommandExecutionRequestApproval.json': '{}',
        'CommandExecutionRequestApprovalResponse.json': '{"enum":["accept","acceptForSession","decline","cancel"]}',
        'FileChangeRequestApprovalResponse.json': '{"enum":["accept","acceptForSession","decline","cancel"]}',
        'PermissionsRequestApprovalResponse.json': '{"permissions":true}',
    }
    for name, body in schemas.items():
        (out / name).write_text(body, encoding='utf-8')
    raise SystemExit(0)

if len(sys.argv) < 2 or sys.argv[1] != 'app-server':
    print('PROSEVIEW_FAKE_AGENT codex argv:' + ' '.join(sys.argv[1:]), flush=True)
    for line in sys.stdin:
        print('STDIN:' + line.rstrip('\\n'), flush=True)
    raise SystemExit(0)

state_path = pathlib.Path.cwd() / '.proseview' / 'fake-codex-thread-state.json'
try:
    saved_state = json.loads(state_path.read_text(encoding='utf-8'))
except (FileNotFoundError, OSError, ValueError, TypeError):
    saved_state = {}
threads = saved_state.get('threads') if isinstance(saved_state.get('threads'), dict) else {}
next_thread = int(saved_state.get('next_thread') or 0)
next_turn = int(saved_state.get('next_turn') or 0)
pending = {}
unload_on_interrupt = set()
record = pathlib.Path(os.environ['HOME']) / 'fake-codex-received.jsonl'

def emit(value):
    print(json.dumps(value, separators=(',', ':')), flush=True)

def save_state():
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({
        'threads': threads, 'next_thread': next_thread, 'next_turn': next_turn,
    }), encoding='utf-8')

def wait_at_barrier(method):
    slug = ''.join(char if char.isalnum() else '-' for char in str(method)).strip('-')
    hold = pathlib.Path.cwd() / '.proseview' / ('hold-codex-' + slug)
    reached = pathlib.Path.cwd() / '.proseview' / ('codex-' + slug + '-reached')
    if not hold.exists():
        return
    reached.parent.mkdir(parents=True, exist_ok=True)
    reached.touch()
    deadline = time.monotonic() + 5
    while hold.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    try:
        reached.unlink()
    except FileNotFoundError:
        pass

for line in sys.stdin:
    message = json.loads(line)
    method = message.get('method')
    request_id = message.get('id')
    params = message.get('params') or {}
    wait_at_barrier(method)
    if method == 'initialize':
        emit({'id': request_id, 'result': {'userAgent': 'proseview-fake-codex/1', 'codexHome': '/isolated', 'platformFamily': 'unix', 'platformOs': 'test'}})
    elif method == 'initialized':
        continue
    elif method == 'account/read':
        emit({'id': request_id, 'result': {'account': {'type': 'apiKey'}, 'requiresOpenaiAuth': True}})
    elif method == 'thread/read':
        thread = threads.get(params.get('threadId'))
        if thread is None:
            emit({'id': request_id, 'error': {'code': -32004, 'message': 'thread not found'}})
        else:
            emit({'id': request_id, 'result': {'thread': thread}})
    elif method == 'thread/start':
        next_thread += 1
        thread_id = f'thread-{next_thread}'
        threads[thread_id] = {'id': thread_id, 'turns': []}
        save_state()
        emit({'id': request_id, 'result': {'thread': threads[thread_id]}})
    elif method == 'skills/list':
        cwd = params.get('cwds', [os.getcwd()])[0]
        emit({'id': request_id, 'result': {'data': [{'cwd': cwd, 'skills': [
            {'name': 'tighten-prose', 'path': str(pathlib.Path(cwd) / '.proseview/skills' / 'tighten-prose' / 'SKILL.md'), 'enabled': True, 'description': 'Remove filler from selected prose.', 'interface': {'displayName': 'Tighten Prose', 'shortDescription': 'Remove filler from selected prose.'}, 'dependencies': {}},
            {'name': 'snippet-continuity', 'path': str(pathlib.Path(cwd) / '.proseview/skills' / 'snippet-continuity' / 'SKILL.md'), 'enabled': True, 'description': 'Check story continuity.', 'interface': {'displayName': 'Continuity Check', 'shortDescription': 'Flag contradictions with the story bible.'}, 'dependencies': {}}
        ], 'errors': []}]}})
    elif method == 'model/list':
        emit({'id': request_id, 'result': {'data': [
            {'id': 'gpt-5.6-sol', 'displayName': 'GPT-5.6-Sol', 'description': 'Latest frontier agentic coding model.',
             'supportedReasoningEfforts': [{'reasoningEffort': e, 'description': e + ' reasoning'} for e in ['low', 'medium', 'high', 'xhigh', 'max']],
             'defaultReasoningEffort': 'medium', 'isDefault': True},
            {'id': 'gpt-5.6-luna', 'displayName': 'GPT-5.6-Luna', 'description': 'Fast and affordable agentic coding model.',
             'supportedReasoningEfforts': [{'reasoningEffort': e, 'description': e + ' reasoning'} for e in ['low', 'medium', 'high']],
             'defaultReasoningEffort': 'medium', 'isDefault': False},
        ]}})
    elif method == 'config/read':
        emit({'id': request_id, 'result': {'config': {'model': 'gpt-5.6-sol', 'model_reasoning_effort': 'xhigh'}}})
    elif method == 'turn/start':
        next_turn += 1
        turn_id = f'turn-{next_turn}'
        thread_id = params['threadId']
        prompt = params['input'][0]['text']
        if thread_id not in threads:
            emit({'id': request_id, 'error': {'code': -32004, 'message': 'thread not found: ' + thread_id}})
            continue
        if len(prompt) > 1048576:
            emit({'id': request_id, 'error': {'code': -32602, 'message': 'Input exceeds the maximum length of 1048576 characters.'}})
            continue
        record.parent.mkdir(parents=True, exist_ok=True)
        with record.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps({'threadId': thread_id, 'turnId': turn_id, 'params': params}) + '\\n')
        stored_prompt = prompt
        if 'SIMULATE_LEGACY_HISTORY' in prompt:
            stored_prompt = '\\n'.join(
                line for line in prompt.split('\\n')
                if not line.startswith('PROSVIEW_SELECTION_ACTION_V1')
            )
        turn = {'id': turn_id, 'status': 'inProgress', 'items': [{'type': 'userMessage', 'content': [{'type': 'text', 'text': stored_prompt}]}]}
        threads.setdefault(thread_id, {'id': thread_id, 'turns': []})['turns'].append(turn)
        emit({'id': request_id, 'result': {'turn': {'id': turn_id, 'status': 'inProgress'}}})
        if 'CRASH_PROCESS' in prompt:
            os._exit(7)
        emit({'method': 'turn/started', 'params': {'threadId': thread_id, 'turn': {'id': turn_id, 'status': 'inProgress'}}})
        emit({'method': 'item/reasoning/textDelta', 'params': {'threadId': thread_id, 'turnId': turn_id, 'delta': 'PRIVATE RAW REASONING'}})
        emit({'method': 'item/reasoning/summaryTextDelta', 'params': {'threadId': thread_id, 'turnId': turn_id, 'delta': 'Reviewing the attached document'}})
        emit({'method': 'turn/plan/updated', 'params': {'threadId': thread_id, 'turnId': turn_id, 'plan': [{'step': 'Read context', 'status': 'completed'}, {'step': 'Answer question', 'status': 'inProgress'}]}})
        emit({'method': 'item/started', 'params': {'threadId': thread_id, 'turnId': turn_id, 'item': {'id': 'tool-' + turn_id, 'type': 'commandExecution', 'command': 'printf inspect', 'cwd': os.getcwd(), 'status': 'inProgress'}}})
        if 'HOLD_UNLOAD_ON_STOP' in prompt:
            unload_on_interrupt.add(turn_id)
            continue
        if 'HOLD_FOR_STOP' in prompt:
            continue
        if 'REQUEST_APPROVAL' in prompt:
            approval_id = 9000 + next_turn
            pending[approval_id] = (thread_id, turn_id, turn)
            emit({'id': approval_id, 'method': 'item/commandExecution/requestApproval', 'params': {'threadId': thread_id, 'turnId': turn_id, 'itemId': 'tool-' + turn_id, 'command': 'printf approved', 'cwd': os.getcwd(), 'reason': 'Test approval', 'availableDecisions': ['accept', 'acceptForSession', 'decline', 'cancel']}})
            continue
        if 'REQUEST_FILE_CHANGE' in prompt:
            approval_id = 9000 + next_turn
            pending[approval_id] = (thread_id, turn_id, turn)
            emit({'method': 'item/started', 'params': {'threadId': thread_id, 'turnId': turn_id, 'item': {'id': 'file-' + turn_id, 'type': 'fileChange', 'changes': [{'path': 'manuscript/ch01/01-opening.md', 'kind': 'modified', 'diff': '@@ -18,1 +18,1 @@\\n-She had used the same four digits since spring.\\n+She had changed the four digits at the start of spring.'}], 'status': 'inProgress'}}})
            emit({'id': approval_id, 'method': 'item/fileChange/requestApproval', 'params': {'threadId': thread_id, 'turnId': turn_id, 'itemId': 'file-' + turn_id, 'reason': 'Test file change', 'availableDecisions': ['accept', 'decline', 'cancel']}})
            continue
        answer = "Fake answer for " + turn_id + ": Patel's note is **safe** [link](https://example.test) [unsafe](javascript:alert(1)) `&amp;` <script>hostile()</script>"
        if 'SHOW_FILE_LINKS' in prompt:
            current_scene = pathlib.Path.cwd() / 'manuscript' / 'ch01' / '01-opening.md'
            answer = (
                f'[current scene]({current_scene}:18) '
                '[another scene](manuscript/ch01/02-walk.md#L19) '
                '[repository file](scripts/check_continuity.py:2) '
                '[outside repository](/tmp/private-notes.md:4) '
                '[external](https://example.test/reference) '
                '[unsafe](javascript:alert(1))'
            )
        schema = params.get('outputSchema') or {}
        kind = ((((schema.get('properties') or {}).get('kind') or {}).get('enum') or [None])[0])
        selection = ''
        if 'BEGIN USER SELECTION\\n' in prompt and '\\nEND USER SELECTION' in prompt:
            selection = prompt.split('BEGIN USER SELECTION\\n', 1)[1].split('\\nEND USER SELECTION', 1)[0]
        if kind == 'alternatives':
            choices = [
                {'text': 'Rena pressed her thumb against the envelope seam.', 'rationale': 'Uses a direct physical action.'},
                {'text': 'Rena held the sealed envelope to the window.', 'rationale': 'Keeps the focus on the object.'},
                {'text': 'Rena traced the envelope seam with one thumb.', 'rationale': 'Uses a quieter physical beat.'}
            ]
            count = schema['properties']['alternatives']['maxItems']
            answer = json.dumps({'kind': 'alternatives', 'summary': 'A more direct version that preserves the scene beat.', 'alternatives': choices[:count]})
        elif kind == 'critique':
            evidence = selection[:120].strip() or 'selected passage'
            if "yesterday's receipts" in selection:
                evidence = '“' + evidence.replace("yesterday's", 'yesterday’s').replace(' ', '\\n', 1) + '”'
            elif 'dial turned with a dry clatter' in selection:
                evidence = 'a pressure gauge that was never selected'
            answer = json.dumps({'kind': 'critique', 'findings': [{'observation': 'The passage delays its strongest image.', 'evidence': evidence, 'why_it_matters': 'The opening beat lands less sharply.', 'next_step': 'Lead with the character’s concrete action.'}]})
        elif kind == 'continuity_report':
            answer = json.dumps({'kind': 'continuity_report', 'summary': 'One direct consequence needs review.', 'findings': [{
                'category': 'direct',
                'file': 'manuscript/ch01/01-opening.md',
                'line': 18,
                'quote': "She had used the same four digits since spring.",
                'explanation': 'This sentence preserves the old safe-code history.',
                'replacement': 'She had changed the four digits at the start of spring.'
            }]})
        emit({'method': 'item/agentMessage/delta', 'params': {'threadId': thread_id, 'turnId': turn_id, 'itemId': 'answer-' + turn_id, 'delta': answer[:24]}})
        emit({'method': 'item/completed', 'params': {'threadId': thread_id, 'turnId': turn_id, 'item': {'id': 'answer-' + turn_id, 'type': 'agentMessage', 'phase': 'final_answer', 'text': answer}}})
        stored_answer = html.escape(answer).replace('&#x27;', '&#39;') if kind in {'alternatives', 'critique', 'continuity_report'} else answer
        turn.update({'status': 'completed', 'items': turn['items'] + [{'type': 'agentMessage', 'phase': 'final_answer', 'text': stored_answer}]})
        save_state()
        emit({'method': 'turn/completed', 'params': {'threadId': thread_id, 'turn': {'id': turn_id, 'status': 'completed'}}})
        if 'FORGET_THREAD_AFTER_TURN' in prompt:
            threads.pop(thread_id, None)
            save_state()
    elif method == 'turn/interrupt':
        if params['turnId'] in unload_on_interrupt:
            unload_on_interrupt.discard(params['turnId'])
            threads.pop(params['threadId'], None)
            save_state()
            emit({'id': request_id, 'error': {'code': -32000, 'message': 'thread not loaded: ' + params['threadId']}})
            continue
        emit({'id': request_id, 'result': {}})
        emit({'method': 'turn/completed', 'params': {'threadId': params['threadId'], 'turn': {'id': params['turnId'], 'status': 'interrupted'}}})
    elif request_id in pending and ('result' in message or 'error' in message):
        thread_id, turn_id, turn = pending.pop(request_id)
        decision = (message.get('result') or {}).get('decision', 'decline')
        answer = 'Approval resolved: ' + decision
        emit({'method': 'item/completed', 'params': {'threadId': thread_id, 'turnId': turn_id, 'item': {'id': 'answer-' + turn_id, 'type': 'agentMessage', 'phase': 'final_answer', 'text': answer}}})
        turn.update({'status': 'completed', 'items': turn['items'] + [{'type': 'agentMessage', 'phase': 'final_answer', 'text': answer}]})
        save_state()
        emit({'method': 'turn/completed', 'params': {'threadId': thread_id, 'turn': {'id': turn_id, 'status': 'completed'}}})
""",
        encoding="utf-8",
    )
    _make_runnable(bin_dir, "codex")
    return bin_dir


# ── HTTP / SSE client ───────────────────────────────────────────────────────


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict[str, str]

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)


class SseStream:
    """Reader over a text/event-stream response.

    The server holds these connections open and emits heartbeats, so reads have
    to be pumped on a background thread to stay cancellable.
    """

    def __init__(self, resp: Any) -> None:
        self._resp = resp
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._event_queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        event_type = "message"
        event_id: int | None = None
        event_data: list[str] = []
        try:
            for raw in self._resp:
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line.startswith("data: "):
                    data = line[len("data: "):]
                    event_data.append(data)
                    self._queue.put(data)
                elif line.startswith("event: "):
                    event_type = line[len("event: "):]
                elif line.startswith("id: "):
                    try:
                        event_id = int(line[len("id: "):])
                    except ValueError:
                        event_id = None
                elif not line and event_data:
                    self._event_queue.put({"id": event_id, "type": event_type, "data": "\n".join(event_data)})
                    event_type = "message"
                    event_id = None
                    event_data = []
        except Exception:  # connection closed underneath us -- expected on teardown
            pass
        finally:
            self._queue.put(None)
            self._event_queue.put(None)

    def next(self, timeout: float = 5.0) -> str:
        try:
            item = self._queue.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError(f"no SSE frame within {timeout}s") from None
        if item is None:
            raise AssertionError("SSE stream closed")
        return item

    def wait_for(self, predicate: Callable[[str], bool], timeout: float = 10.0) -> str:
        """Return the first frame satisfying *predicate*, ignoring heartbeats."""
        deadline = time.monotonic() + timeout
        seen: list[str] = []
        while time.monotonic() < deadline:
            remaining = max(0.1, deadline - time.monotonic())
            try:
                frame = self.next(timeout=remaining)
            except AssertionError:
                break
            seen.append(frame)
            if predicate(frame):
                return frame
        raise AssertionError(f"no matching SSE frame within {timeout}s; saw {seen!r}")

    def next_event(self, timeout: float = 5.0) -> dict[str, Any]:
        try:
            event = self._event_queue.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError(f"no complete SSE event within {timeout}s") from None
        if event is None:
            raise AssertionError("SSE stream closed")
        return event

    def close(self) -> None:
        """Tear the stream down without waiting for the server.

        The pump thread is parked in a blocking ``readline`` and the server only
        writes again on its heartbeat -- 15s for ``/events``. Plain
        ``close()`` would inherit that latency on every test.
        Shutting the socket down first makes the pending read fail immediately.
        """
        try:
            self._resp.fp.raw._sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self._resp.close()
        except Exception:
            pass


class ProseviewServer:
    """Handle on a running ``python -m proseview`` subprocess."""

    def __init__(
        self,
        root: Path,
        port: int,
        proc: subprocess.Popen,
        env: dict[str, str],
        bin_dir: Path,
        home: Path,
    ):
        self.root = root
        self.port = port
        self.proc = proc
        self.env = env
        self.bin_dir = bin_dir
        self.home = home
        self.base_url = f"http://localhost:{port}"

    def restart(self) -> None:
        """Restart Prosview on the same origin while retaining external Codex history."""
        _stop_server(self)
        replacement = _start_server(self.root, self.bin_dir, self.home, port=self.port)
        self.proc = replacement.proc
        self.env = replacement.env

    @contextmanager
    def hold_codex_request(self, method: str) -> Iterator[Path]:
        """Pause one fake-Codex request type until the test leaves this block."""
        slug = "".join(char if char.isalnum() else "-" for char in method).strip("-")
        if not slug:
            raise ValueError("Codex request method must not be empty")
        runtime = self.root / ".proseview"
        runtime.mkdir(parents=True, exist_ok=True)
        hold = runtime / f"hold-codex-{slug}"
        reached = runtime / f"codex-{slug}-reached"
        reached.unlink(missing_ok=True)
        hold.write_text("hold\n", encoding="utf-8")
        try:
            yield reached
        finally:
            hold.unlink(missing_ok=True)
            reached.unlink(missing_ok=True)

    # -- paths -------------------------------------------------------------

    def scene_path(self, rel: str = SCENE_REL) -> Path:
        return self.root / "manuscript" / rel

    def url(self, path: str) -> str:
        return self.base_url + path

    # -- HTTP --------------------------------------------------------------

    def get(self, path: str, timeout: float = 30.0) -> Response:
        req = urllib.request.Request(self.url(path), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return Response(resp.status, resp.read(), dict(resp.headers))
        except urllib.error.HTTPError as exc:
            return Response(exc.code, exc.read(), dict(exc.headers or {}))

    def get_json(self, path: str, timeout: float = 30.0) -> Any:
        return self.get(path, timeout=timeout).json()

    @property
    def session_token(self) -> str:
        """The running server's mutation token, as a local client would read it."""
        runtime = self.root / ".proseview" / "server.json"
        try:
            return str(json.loads(runtime.read_text(encoding="utf-8")).get("session_token") or "")
        except (OSError, ValueError):
            return ""

    def post_json(self, path: str, payload: dict, timeout: float = 30.0, headers: dict[str, str] | None = None) -> Response:
        # Mutations are token-gated. Send it by default so tests exercise the
        # normal path; a test proving rejection passes its own headers.
        request_headers = {
            "Content-Type": "application/json",
            "X-Proseview-Session": self.session_token,
        }
        request_headers.update(headers or {})
        req = urllib.request.Request(
            self.url(path),
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers=request_headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return Response(resp.status, resp.read(), dict(resp.headers))
        except urllib.error.HTTPError as exc:
            return Response(exc.code, exc.read(), dict(exc.headers or {}))

    @contextmanager
    def sse(self, path: str = "/events", headers: dict[str, str] | None = None) -> Iterator[SseStream]:
        separator = "&" if "?" in path else "?"
        path += separator + urllib.parse.urlencode({"session": self.session_token})
        request = urllib.request.Request(self.url(path), headers=headers or {})
        resp = urllib.request.urlopen(request, timeout=30)
        stream = SseStream(resp)
        try:
            yield stream
        finally:
            stream.close()

    # -- CLI ---------------------------------------------------------------

    def cli(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        """Run the real ``proseview`` CLI against this server's repo.

        The CLI locates the server itself through ``.proseview/server.json``, so
        this exercises the same discovery path an agent would use.
        """
        proc = subprocess.run(
            [sys.executable, "-m", "proseview", *args],
            cwd=str(self.root),
            env=self.env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        if check and proc.returncode != 0:
            raise AssertionError(
                f"proseview {' '.join(args)} failed ({proc.returncode})\n"
                f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
            )
        return proc

    # -- scene helpers -----------------------------------------------------

    def scene_meta(self, rel: str = SCENE_REL) -> dict:
        return self.get_json("/data.json")["meta"][rel]

    def save_scene(self, content: str, rel: str = SCENE_REL, mtime: float | None = None) -> Response:
        meta = self.scene_meta(rel)
        return self.post_json("/save-scene", {
            "abs_path": meta["abs_path"],
            "content": content,
            "open_mtime": meta["mtime"] if mtime is None else mtime,
        })


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("localhost", 0))
        return int(sock.getsockname()[1])


def _server_env(bin_dir: Path, home: Path) -> dict[str, str]:
    """Environment for the server subprocess.

    ``PATH`` puts the agent stubs first, and ``HOME`` points at an empty
    directory so the developer's real profile cannot shadow them with
    whatever is actually installed.
    """
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["HOME"] = str(home)
    fake_claude_sdk = REPO_ROOT / "tests" / "e2e" / "fake_claude_sdk"
    env["PYTHONPATH"] = os.pathsep.join(
        [str(fake_claude_sdk), str(REPO_ROOT), env.get("PYTHONPATH", "")]
    )
    env.pop("PYTHONWARNINGS", None)
    return env


def _start_server(root: Path, bin_dir: Path, home: Path, *, port: int | None = None) -> ProseviewServer:
    env = _server_env(bin_dir, home)
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "proseview",
            "--root", str(root),
            "--port", str(port) if port is not None else "0",
            "--interval", str(WATCH_INTERVAL),
            "--no-open",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        # Ctrl-Break can only be delivered to a process that owns its group,
        # and it is the only interrupt Windows lets us send a child.
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )

    # Wait for the server to write its runtime file so we know what port it bound.
    runtime = root / ".proseview" / "server.json"
    deadline = time.monotonic() + BOOT_TIMEOUT
    port = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            output = proc.stdout.read() if proc.stdout else ""
            raise AssertionError(f"server exited during boot ({proc.returncode}):\n{output}")
        try:
            if runtime.exists():
                data = json.loads(runtime.read_text(encoding="utf-8"))
                if "port" in data:
                    port = int(data["port"])
                    break
        except (OSError, ValueError):
            pass
        time.sleep(0.05)

    if port is None:
        proc.kill()
        raise AssertionError(f"server did not write port within {BOOT_TIMEOUT}s")

    server = ProseviewServer(root, port, proc, env, bin_dir, home)

    while time.monotonic() < deadline:
        if proc.poll() is not None:
            output = proc.stdout.read() if proc.stdout else ""
            raise AssertionError(f"server exited during boot ({proc.returncode}):\n{output}")
        try:
            if server.get("/", timeout=2).status == 200:
                return server
        except Exception:
            time.sleep(0.1)
    proc.kill()
    raise AssertionError(f"server did not answer within {BOOT_TIMEOUT}s")


def _stop_server(server: ProseviewServer) -> None:
    """Stop the server the way a user does: Ctrl-C.

    ``serve()`` only unwinds on ``KeyboardInterrupt``; a bare ``SIGTERM`` skips
    the ``finally`` that removes ``.proseview/server.json``, leaving a runtime
    file pointing at a dead port. Sending SIGINT exercises the documented
    shutdown path and leaves the repo clean.
    """
    proc = server.proc
    if proc.poll() is None:
        # SIGINT cannot be sent to another process on Windows. Ctrl-Break is
        # the equivalent there, and serve() unwinds on both.
        interrupt = getattr(signal, "CTRL_BREAK_EVENT", signal.SIGINT)
        proc.send_signal(interrupt)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _browser_timeout(request: pytest.FixtureRequest) -> None:
    """Give CI runners longer before a wait is called a failure.

    pytest-playwright defaults to 30s, which is comfortable on a developer
    machine and marginal on a two-core hosted runner: the browser tier there
    fails one test per run, a different one each time, always a timeout and
    always passing in isolation. Locally the default stays put so a genuine
    hang still surfaces quickly.
    """
    if "page" not in request.fixturenames:
        return
    request.getfixturevalue("page").set_default_timeout(
        60_000 if os.environ.get("CI") else 30_000
    )


@pytest.fixture(scope="session")
def agent_bin(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _write_agent_stubs(tmp_path_factory.mktemp("agent-bin"))


@pytest.fixture(scope="session")
def fake_home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("fake-home")


@pytest.fixture(scope="session")
def shared_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One repo copy shared by read-only tests."""
    return _build_repo(tmp_path_factory.mktemp("shared-repo") / "novel")


@pytest.fixture(scope="session")
def shared_server(shared_repo: Path, agent_bin: Path, fake_home: Path) -> Iterator[ProseviewServer]:
    """Session-scoped server. Use only for tests that do not mutate the repo."""
    server = _start_server(shared_repo, agent_bin, fake_home)
    try:
        yield server
    finally:
        _stop_server(server)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Fresh repo copy, isolated per test."""
    return _build_repo(tmp_path / "novel")


@pytest.fixture
def bare_repo(tmp_path: Path) -> Path:
    """A manuscript with no frontmatter, no story bible, no config.

    The Obsidian case: someone points Proseview at a folder of prose. Every
    metadata-driven panel has nothing to work with, which is exactly the state
    a silently-empty chart is indistinguishable from.
    """
    root = tmp_path / "bare"
    root.mkdir()
    for name, text in [
        ("one.md", "She counted the boats twice, and then a third time.\n"),
        ("two.md", "The tide went out without her, and the quay went quiet.\n"),
        ("three.md", "He said nothing, which was his way of saying a great deal.\n"),
    ]:
        (root / name).write_text(text, encoding="utf-8")
    return root


@pytest.fixture
def bare_server(bare_repo: Path, agent_bin: Path, fake_home: Path) -> Iterator[ProseviewServer]:
    srv = _start_server(bare_repo, agent_bin, fake_home)
    try:
        yield srv
    finally:
        _stop_server(srv)


@pytest.fixture
def server(repo: Path, agent_bin: Path, fake_home: Path) -> Iterator[ProseviewServer]:
    """Function-scoped server for tests that write to the repo."""
    srv = _start_server(repo, agent_bin, fake_home)
    try:
        yield srv
    finally:
        _stop_server(srv)
        # serve() removes its runtime file on clean shutdown; a leftover file
        # means teardown did not run and later CLI calls could target a corpse.
        assert not (repo / ".proseview" / "server.json").exists(), \
            "server.json survived shutdown"
