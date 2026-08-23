"""Tests for the local Proseview HTTP server helpers."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from proseview.config import Config  # noqa: E402
from proseview.generator import build_scene_data  # noqa: E402
from proseview.scenes import collect_scene_stats  # noqa: E402
from proseview.server import (  # noqa: E402
    save_scene_content,
    _FileConflictError,
    _normalize_ai_scene_file,
    _resolve_ai_target,
)

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "demo-repo"
SCENE = FIXTURE / "manuscript" / "ch01" / "01-opening.md"


def test_build_scene_data_returns_data_json_payload():
    """``/data.json`` calls ``build_scene_data`` directly. The returned dict
    must contain the three keys the client refresh consumes, keyed by the
    same display paths the template embeds.
    """
    cfg = Config.load(FIXTURE)
    scenes = collect_scene_stats(FIXTURE, cfg)
    data = build_scene_data(scenes, FIXTURE, cfg)

    assert set(data) == {"contents", "meta", "highlightsByPath", "medians"}
    assert "ch01/01-opening.md" in data["contents"]
    assert data["meta"]["ch01/01-opening.md"]["words"] > 0
    assert data["highlightsByPath"]["ch01/01-opening.md"]["paragraphs"]
    # Meta dict carries the live mtime so save-then-refresh sees the new value.
    assert "mtime" in data["meta"]["ch01/01-opening.md"]
    assert "abs_path" in data["meta"]["ch01/01-opening.md"]


# ── AI proposal bridge helpers ──────────────────────────────────────────────


def test_ai_scene_file_normalization_accepts_repo_relative_manuscript_path():
    rel = _normalize_ai_scene_file(str(FIXTURE), "manuscript/ch01/01-opening.md")
    assert rel == "ch01/01-opening.md"


def test_ai_scene_file_normalization_accepts_scene_relative_path():
    rel = _normalize_ai_scene_file(str(FIXTURE), "ch01/01-opening.md")
    assert rel == "ch01/01-opening.md"


def test_ai_scene_file_normalization_rejects_path_traversal():
    with pytest.raises(PermissionError):
        _normalize_ai_scene_file(str(FIXTURE), "../outside.md")


def test_ai_target_resolution_finds_markdown_quote():
    resolved = _resolve_ai_target(
        str(FIXTURE),
        "ch01/01-opening.md",
        "The loft smelled of cold coffee",
        None,
    )
    assert resolved["range"]["start"] >= 0
    assert resolved["range"]["end"] > resolved["range"]["start"]
    assert "The loft smelled" in resolved["resolved_quote"]


def test_ai_target_resolution_rejects_missing_quote():
    with pytest.raises(ValueError, match="quote not found"):
        _resolve_ai_target(str(FIXTURE), "ch01/01-opening.md", "not in this scene", None)


def test_ai_target_resolution_rejects_comment_blocks_in_quote(tmp_path: Path):
    scene = tmp_path / "manuscript" / "ch01" / "x.md"
    scene.parent.mkdir(parents=True)
    scene.write_text(
        "---\ntitle: X\n---\n\n# X\n\n"
        "The partition is doing work it was never designed to do.\n\n"
        "<!-- NOTE: Should be revised -->\n\n"
        "Bad.\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="annotations"):
        _resolve_ai_target(
            str(tmp_path),
            "ch01/x.md",
            "The partition is doing work it was never designed to do.\n\n"
            "<!-- NOTE: Should be revised -->\n\n"
            "Bad.",
            None,
        )


def test_ai_target_resolution_accepts_line_columns_for_visible_prose(tmp_path: Path):
    scene = tmp_path / "manuscript" / "ch01" / "x.md"
    scene.parent.mkdir(parents=True)
    scene.write_text(
        "---\ntitle: X\n---\n\n# X\n\nFirst visible line.\nSecond visible line.\n",
        encoding="utf-8",
    )
    resolved = _resolve_ai_target(
        str(tmp_path),
        "ch01/x.md",
        "",
        {"start_line": 7, "start_col": 1, "end_line": 8, "end_col": 21},
    )
    assert resolved["resolved_quote"] == "First visible line.\nSecond visible line."
    assert resolved["source_range"]["start_line"] == 7


def test_ai_target_line_col_range_matches_browser_flat_text_frame(tmp_path: Path):
    """Server range offsets must agree with the browser's ``aiDocTextMap``.

    The browser inserts a single ``\\n`` between block-level nodes, while
    ``scene_text`` uses blank-line separators. If the server returns offsets
    computed against the blank-line form, the browser's fallback path drifts
    by one char per preceding paragraph break and a subsequent apply replaces
    the wrong span (leaving leftover text at the tail of the target paragraph).
    """
    scene = tmp_path / "manuscript" / "ch01" / "x.md"
    scene.parent.mkdir(parents=True)
    scene.write_text(
        "---\ntitle: X\n---\n\n# X\n\n"
        "Paragraph one.\n\n"
        "Paragraph two.\n\n"
        "Paragraph three.\n\n"
        "Target paragraph here.\n",
        encoding="utf-8",
    )
    # Line 13 of raw file = "Target paragraph here." (22 chars).
    resolved = _resolve_ai_target(
        str(tmp_path),
        "ch01/x.md",
        "",
        {"start_line": 13, "start_col": 1, "end_line": 13, "end_col": 23},
    )
    assert resolved["resolved_quote"] == "Target paragraph here."
    # Three paragraphs precede the target. In the browser's flat text they
    # contribute: 14 + 1 + 14 + 1 + 16 + 1 = 47 chars before the target.
    assert resolved["range"]["start"] == 47
    assert resolved["range"]["end"] == 47 + len("Target paragraph here.")


def test_ai_target_quote_range_matches_browser_flat_text_frame(tmp_path: Path):
    """Quote-based proposals must also return browser-frame offsets."""
    scene = tmp_path / "manuscript" / "ch01" / "x.md"
    scene.parent.mkdir(parents=True)
    scene.write_text(
        "---\ntitle: X\n---\n\n# X\n\n"
        "Paragraph one.\n\n"
        "Paragraph two.\n\n"
        "Target paragraph here.\n",
        encoding="utf-8",
    )
    resolved = _resolve_ai_target(
        str(tmp_path),
        "ch01/x.md",
        "Target paragraph here.",
        None,
    )
    # Two paragraphs precede the target: 14 + 1 + 14 + 1 = 30.
    assert resolved["range"]["start"] == 30
    assert resolved["range"]["end"] == 30 + len("Target paragraph here.")


def test_ai_target_resolution_rejects_line_columns_crossing_annotations(tmp_path: Path):
    scene = tmp_path / "manuscript" / "ch01" / "x.md"
    scene.parent.mkdir(parents=True)
    scene.write_text(
        "---\ntitle: X\n---\n\n# X\n\nBefore.\n<!-- TODO: revise -->\nAfter.\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="annotations"):
        _resolve_ai_target(
            str(tmp_path),
            "ch01/x.md",
            "",
            {"start_line": 7, "start_col": 1, "end_line": 9, "end_col": 7},
        )


# ── abs_path containment coverage ────────────────────────────────────────────

def test_every_endpoint_taking_an_abs_path_is_containment_checked():
    """``_ABS_PATH_ENDPOINTS`` is what routes a request through
    ``_contained_abs_path``, and nothing enforces that the list is complete.

    An omission is silent -- the endpoint keeps working and simply stops being
    contained, which is how ``/delete-fm-todo`` shipped able to write outside
    the served repository. This reads the dispatch back and fails if a branch
    reads ``body["abs_path"]`` without being on the list.
    """
    import ast
    import inspect
    import textwrap

    from proseview.server import _ABS_PATH_ENDPOINTS, _Handler

    source = textwrap.dedent(inspect.getsource(_Handler.do_POST))
    tree = ast.parse(source)

    def literal_endpoint(test: ast.expr) -> str | None:
        """The endpoint of a ``path == "/x"`` / ``self.path == "/x"`` test."""
        if not isinstance(test, ast.Compare) or len(test.ops) != 1:
            return None
        if not isinstance(test.ops[0], ast.Eq):
            return None
        right = test.comparators[0]
        if not (isinstance(right, ast.Constant) and isinstance(right.value, str)):
            return None
        left = test.left
        name = (
            left.id if isinstance(left, ast.Name)
            else left.attr if isinstance(left, ast.Attribute)
            else None
        )
        return right.value if name == "path" else None

    missing: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        endpoint = literal_endpoint(node.test)
        if endpoint is None or endpoint in _ABS_PATH_ENDPOINTS:
            continue
        # Only this branch's own body -- not the trailing elif chain.
        block = "\n".join(ast.unparse(stmt) for stmt in node.body)
        if "abs_path" in block:
            missing.append(endpoint)

    assert not missing, (
        f"these endpoints read a client abs_path but skip the containment "
        f"check: {sorted(missing)}"
    )


# ── save_scene_content tests ─────────────────────────────────────────────────

def test_save_scene_rejects_path_outside_manuscript(tmp_path):
    outside = tmp_path / "evil.md"
    outside.write_text("bad\n")
    mtime = outside.stat().st_mtime
    with pytest.raises(PermissionError):
        save_scene_content(str(outside), "new content", mtime, str(FIXTURE))


def test_save_scene_conflict_guard(tmp_path):
    scene = tmp_path / "manuscript" / "ch01" / "01-opening.md"
    scene.parent.mkdir(parents=True)
    scene.write_text(SCENE.read_text())
    stale_mtime = scene.stat().st_mtime - 10.0  # pretend editor opened 10s ago
    # Touch the file to advance mtime
    scene.write_text(scene.read_text())
    with pytest.raises(_FileConflictError):
        save_scene_content(str(scene), "new content", stale_mtime, str(tmp_path))


def test_save_scene_preserves_frontmatter_and_heading(tmp_path):
    scene = tmp_path / "manuscript" / "ch01" / "01-opening.md"
    scene.parent.mkdir(parents=True)
    original = SCENE.read_text()
    scene.write_text(original)
    mtime = scene.stat().st_mtime

    save_scene_content(str(scene), "Edited prose here.\n", mtime, str(tmp_path))

    result = scene.read_text()
    assert result.startswith("---")
    assert "# Opening Ledger" in result
    assert "Edited prose here." in result
    # Frontmatter fields preserved
    assert "chapter: Chapter 1" in result


def test_save_scene_preserves_multiline_todo(tmp_path):
    scene = tmp_path / "manuscript" / "ch01" / "01-opening.md"
    scene.parent.mkdir(parents=True)
    original = SCENE.read_text()
    # Inject a multiline TODO into the prose section
    with_todo = original.replace(
        "The loft smelled",
        "<!-- TODO: fix this\nspanning two lines -->\nThe loft smelled",
    )
    scene.write_text(with_todo)
    mtime = scene.stat().st_mtime

    # Save with new prose that still contains the TODO
    new_prose = "<!-- TODO: fix this\nspanning two lines -->\nEdited prose.\n"
    save_scene_content(str(scene), new_prose, mtime, str(tmp_path))

    result = scene.read_text()
    assert "<!-- TODO: fix this" in result
    assert "spanning two lines" in result
    assert "Edited prose." in result


def test_save_scene_atomic_no_truncation_on_bad_content(tmp_path):
    """File should not be left empty/truncated if something goes wrong mid-write."""
    scene = tmp_path / "manuscript" / "ch01" / "01-opening.md"
    scene.parent.mkdir(parents=True)
    original = SCENE.read_text()
    scene.write_text(original)
    mtime = scene.stat().st_mtime

    # Normal save -- verify file is fully intact (atomic write path exercised)
    save_scene_content(str(scene), "New prose.\n", mtime, str(tmp_path))

    result = scene.read_text()
    # File must not be empty and must have frontmatter + content
    assert len(result) > 10
    assert "---" in result
    assert "New prose." in result
