"""Smoke tests for M0/M1: the new generator renders the demo fixture into
HTML containing the section markers a reader would expect, the same scene
data flows through to the embedded JSON, and ``.proseview.yaml`` overrides
drive paths, goal bands, and editor handoff.

Byte-for-byte parity with the legacy ``scripts/manuscript_stats.py`` is
explicitly out of scope (the M0 acceptance criteria allow whitespace and
ordering differences), so these tests focus on structure and content.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from proseview.config import Config, DiscussConfig  # noqa: E402
from proseview.generator import build_analysis_payload, build_dashboard  # noqa: E402
from proseview.scenes import collect_scene_stats, split_frontmatter  # noqa: E402

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "demo-repo"


def test_dashboard_contains_structural_markers():
    html = build_dashboard(FIXTURE, Config.load(FIXTURE))

    for marker in [
        "<!DOCTYPE html>",
        '<html lang="en" data-theme="light">',
        'class="top-banner"',
        'id="themeToggle"',
        "Book-Wide Lexical Health",
        "Character Presence Timeline",
        "Character Co-Occurrence",
        "Scene Deep Dive",
        "Lexical Health Map",
        "Project Configuration",
        'id="sceneModal"',
        'id="utilityTabScene"',
        'id="utilityTabAnalysis"',
        'id="utilityTabCodex"',
        'id="utilityTabClaude"',
        'id="presenceChart"',
        'id="locationChart"',
        'id="coOccurChart"',
        'id="lexicalScatterChart"',
    ]:
        assert marker in html, f"missing marker: {marker!r}"


def test_dashboard_emits_theme_bootstrap_and_token_blocks():
    html = build_dashboard(FIXTURE, Config.load(FIXTURE))

    for marker in [
        "const storedKey = 'proseview-theme'",
        "const THEME_STORAGE_KEY = 'proseview-theme'",
        "localStorage.getItem(storedKey)",
        "window.matchMedia('(prefers-color-scheme: dark)')",
        "document.documentElement.dataset.theme = theme;",
        "document.documentElement.dataset.theme = name;",
        ":root {",
        '[data-theme="dark"] {',
        "--bg-app:",
        "--surface-card:",
        "--chart-grid:",
        "--hl-repeat-bg:",
    ]:
        assert marker in html, f"missing theme marker: {marker!r}"


def test_dashboard_inlines_template_assets_and_data_bootstrap():
    html = build_dashboard(FIXTURE, Config.load(FIXTURE))

    for marker in [
        'id="proseview-app-css"',
        'id="proseview-bootstrap"',
        'id="proseview-data"',
        'id="proseview-app"',
        'id="modalFontSize"',
        "let contents = JSON.parse(",
        "const repoTree = JSON.parse(",
        "const presenceChartData = JSON.parse(",
        "const VALID_TABS = ['overview', 'analysis', 'timeline', 'todos', 'notes', 'settings'];",
        "const MODAL_FONT_SIZE_STORAGE_KEY = 'proseview-modal-font-size';",
        "const VIEW_SCROLL_STORAGE_PREFIX = 'proseview-scroll:';",
        "if ('scrollRestoration' in history) history.scrollRestoration = 'manual';",
        "routeHydrating = true;",
        "sessionStorage.setItem(VIEW_SCROLL_STORAGE_PREFIX + key",
        "function previewRepoFile(path, options)",
        "name = VALID_TABS.includes(name) ? name : 'overview';",
        ".top-banner {",
    ]:
        assert marker in html, f"missing asset marker: {marker!r}"
    assert 'content: "\\u25B8"' not in html
    assert ".repo-tree .sidebar-disclosure-icon {" in html
    assert 'class="sidebar-chrome-icon"' in html
    assert "\\U0001f9e0" not in html


def test_dashboard_routes_chart_theming_through_runtime_helpers():
    html = build_dashboard(FIXTURE, Config.load(FIXTURE))

    for marker in [
        "const chartRefs = {}",
        "function getThemePalette()",
        "function applyThemeToChart(chart)",
        "function updateChartsForTheme()",
        "chartRefs.presenceChart = makeChart(",
        "chartRefs.locationChart = makeChart(",
        "chartRefs.coOccurChart = makeChart(",
        "chartRefs.lexicalScatterChart = makeChart(",
        "updateChartsForTheme();",
    ]:
        assert marker in html, f"missing chart theme hook: {marker!r}"


def test_dashboard_app_script_is_valid_javascript(tmp_path: Path):
    node = shutil.which("node")
    if node is None:
        return

    html = build_dashboard(FIXTURE, Config.load(FIXTURE))
    script = _extract_script_block(html, "proseview-app")
    script_path = tmp_path / "app.js"
    script_path.write_text(script, encoding="utf-8")

    result = subprocess.run(
        [node, "--check", str(script_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_dashboard_embeds_every_fixture_scene():
    html = build_dashboard(FIXTURE, Config.load(FIXTURE))
    scenes = collect_scene_stats(FIXTURE)
    assert scenes, "fixture should contain scenes"

    paths_json = _extract_js_literal(html, "paths")
    embedded = json.loads(paths_json)
    # The embedded keys are posix on every platform, so compare like for
    # like rather than against the OS-native separator.
    expected = [s.path.as_posix().removeprefix("manuscript/") for s in scenes]
    assert sorted(embedded) == sorted(expected)


def test_dashboard_embeds_every_character_bio():
    html = build_dashboard(FIXTURE, Config.load(FIXTURE))
    bios_json = _extract_js_literal(html, "bios")
    bios = json.loads(bios_json)
    assert {"rena", "lowe", "patel"} <= set(bios), \
        f"expected character bios to be embedded, got keys={sorted(bios)}"


def test_dashboard_reports_correct_word_and_scene_counts():
    html = build_dashboard(FIXTURE, Config.load(FIXTURE))
    scenes = collect_scene_stats(FIXTURE)
    total_words = sum(s.words for s in scenes)
    # Banner formats with thousands separator: "{total:,} / {target:,}". The
    # target is a button that jumps to Settings, so the two halves are no
    # longer contiguous text and are asserted separately.
    assert f"{total_words:,} / " in html
    assert ">80,000</button>" in html
    assert f'<span class="value">{len(scenes)}</span>' in html


def test_split_frontmatter_folds_multiline_scalar_values():
    fm, _ = split_frontmatter(
        """---
title: Example
goal: Turn the new collaboration into real progress without discovering that
  the practical version of the project is conceptually weaker than he has been
  telling himself
conflict: Maya's precision and Patel's speed expose that the blockwise
  formulation may be optimizing the wrong object, threatening the paper's
  architecture before it has even properly formed
outcome: Patel identifies a missing residual-interaction term in the scalable
  objective, the workday ends destabilized, and Nima leaves with a technical
  problem he has to solve alone
---

Body.
"""
    )

    assert fm["goal"] == (
        "Turn the new collaboration into real progress without discovering that "
        "the practical version of the project is conceptually weaker than he has been "
        "telling himself"
    )
    assert fm["conflict"] == (
        "Maya's precision and Patel's speed expose that the blockwise "
        "formulation may be optimizing the wrong object, threatening the paper's "
        "architecture before it has even properly formed"
    )
    assert fm["outcome"] == (
        "Patel identifies a missing residual-interaction term in the scalable "
        "objective, the workday ends destabilized, and Nima leaves with a technical "
        "problem he has to solve alone"
    )


def test_dashboard_meta_keeps_full_multiline_frontmatter_values(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "manuscript" / "ch01").mkdir(parents=True)
    (repo / "plans").mkdir()
    (repo / "story-bible" / "characters").mkdir(parents=True)
    (repo / "manuscript" / "ch01" / "01-scene.md").write_text(
        """---
chapter: 1
scene: 1
title: "Test Scene"
where: Lab
goal: Turn the new collaboration into real progress without discovering that
  the practical version of the project is conceptually weaker than he has been
  telling himself
conflict: Maya's precision and Patel's speed expose that the blockwise
  formulation may be optimizing the wrong object, threatening the paper's
  architecture before it has even properly formed
outcome: Patel identifies a missing residual-interaction term in the scalable
  objective, the workday ends destabilized, and Nima leaves with a technical
  problem he has to solve alone
status: draft
---

# Test Scene

Draft body.
""",
        encoding="utf-8",
    )

    html = build_dashboard(repo, Config.load(repo))
    meta = json.loads(_extract_js_literal(html, "meta"))
    scene = meta["ch01/01-scene.md"]["fm"]

    assert scene["goal"].endswith("telling himself")
    assert "conceptually weaker than he has been" in scene["goal"]
    assert scene["conflict"].endswith("properly formed")
    assert "optimizing the wrong object" in scene["conflict"]
    assert scene["outcome"].endswith("solve alone")
    assert "residual-interaction term" in scene["outcome"]


def test_renaming_manuscript_dir_via_config_produces_same_dashboard(tmp_path: Path):
    """Acceptance: renaming ``manuscript/`` to ``book/`` in the fixture and
    pointing ``manuscript_path`` at it should produce a dashboard with the
    same scene set and counts, just under the new directory.
    """
    renamed = tmp_path / "repo"
    shutil.copytree(FIXTURE, renamed)
    (renamed / "book").mkdir()
    for entry in (renamed / "manuscript").iterdir():
        shutil.move(str(entry), str(renamed / "book" / entry.name))
    (renamed / "manuscript").rmdir()
    (renamed / ".proseview.yaml").write_text("manuscript_path: book/\n", encoding="utf-8")

    baseline_html = build_dashboard(FIXTURE, Config.load(FIXTURE))
    renamed_html = build_dashboard(renamed, Config.load(renamed))

    baseline_scenes = collect_scene_stats(FIXTURE)
    renamed_scenes = collect_scene_stats(renamed, "book")
    assert len(baseline_scenes) == len(renamed_scenes)

    for name in ("paths", "contents"):
        baseline_val = json.loads(_extract_js_literal(baseline_html, name))
        renamed_val = json.loads(_extract_js_literal(renamed_html, name))
        assert sorted(baseline_val) == sorted(renamed_val) if isinstance(baseline_val, list) \
            else sorted(baseline_val.keys()) == sorted(renamed_val.keys())


def test_character_list_falls_back_to_story_bible_scan():
    """Acceptance: without ``characters:`` in the config, the presence timeline
    still populates by scanning ``story-bible/characters/*.md``.
    """
    html = build_dashboard(FIXTURE, Config())
    assert Config().characters == ()
    bios = json.loads(_extract_js_literal(html, "bios"))
    assert bios, "scanning fallback should populate bios from story-bible"
    assert "Rena" in html and "Lowe" in html and "Patel" in html


def test_characters_config_overrides_story_bible_scan():
    """When ``characters:`` is provided, it is used verbatim. Unlisted names
    from ``story-bible/characters/`` still show bios (the map is built from
    the directory), but only listed names appear in the presence timeline.
    """
    cfg = Config().with_overrides(characters=("Rena",))
    html = build_dashboard(FIXTURE, cfg)
    presence = json.loads(_extract_js_literal(html, "presenceChartData"))
    labels = [dataset["label"] for dataset in presence["datasets"]]
    assert labels == ["Rena"], f"expected only Rena in presence chart, got {labels}"


def test_editor_scheme_is_honored_in_editor_button():
    cfg = Config().with_overrides(editor=Config().editor.__class__(scheme="zed"))
    html = build_dashboard(FIXTURE, cfg)
    assert "zed://file/" in html
    assert "vscode://file/" not in html
    assert "Open in Zed" in html


def test_mattr_band_override_moves_scatter_annotation():
    """The scatter bands moved to the on-demand Analysis payload.

    The chart is no longer built at first paint, so the override has to be
    asserted where the data now lives rather than in the embedded HTML.
    """
    payload = build_analysis_payload(FIXTURE, Config().with_overrides(mattr_band=(0.60, 0.95)))
    assert payload["scatterChart"]["bands"]["mattr"] == [0.60, 0.95]


def test_overview_build_skips_the_lexical_passes():
    """The Overview tab must not pay for analysis it does not display.

    ``build_dashboard`` scans with ``analyze=False``, so the per-scene lexical
    and style fields come back zeroed; the real numbers are only computed when
    the Analysis tab asks for them.
    """
    html = build_dashboard(FIXTURE, Config.load(FIXTURE))
    assert "lexicalScatterChartData" not in html, "scatter data should no longer be inlined"
    assert "rhythmChartData" not in html, "rhythm data should no longer be inlined"
    # The cheap, frontmatter-driven charts are still served at first paint.
    assert "const presenceChartData = JSON.parse(" in html
    assert "const coOccurChartData = JSON.parse(" in html


def test_analysis_payload_carries_every_panel_the_tab_renders():
    payload = build_analysis_payload(FIXTURE, Config.load(FIXTURE))

    assert set(payload) == {"tableBody", "scatterChart", "lexical"}
    assert payload["scatterChart"]["datasets"][0]["data"], "scatter chart needs points"
    for key in ("mattrText", "mtldText", "mattrMarkerLeft", "mtldMarkerLeft",
                "mattrMedianText", "mtldMedianText", "mtldBand", "genreLabel"):
        assert payload["lexical"][key], f"missing lexical field {key!r}"
    # The Analysis table keeps the four analysis columns and their filter hooks.
    assert "data-dlg=" in payload["tableBody"] and "data-rep=" in payload["tableBody"]


def test_dashboard_has_no_book_specific_strings_from_host_repo():
    """OSS protection: the demo fixture's dashboard must not leak phrases from
    this book (character names, scene titles, etc.). Character names from the
    fixture are expected; names from the host book are not.

    "Claude", "Codex", and "Gemini" appear unconditionally as agent-menu
    labels in the dashboard product itself, so they are not treated as host
    leakage. (They will become configurable as part of the standalone
    proseview repo's decoupling work.)
    """
    html = build_dashboard(FIXTURE, Config.load(FIXTURE))
    forbidden = ["AGENTS.md", "Anthropic"]
    for phrase in forbidden:
        assert phrase.lower() not in html.lower(), \
            f"dashboard leaked host-repo phrase: {phrase!r}"


def test_goals_card_absent_on_non_git_fixture():
    """FIXTURE lives inside the book's git repo, but the ``is_git_repo``
    check requires the worktree top-level to equal ``root`` — so history is
    empty and the Goals card is hidden. A dashboard without a Goals card
    should still render cleanly.
    """
    html = build_dashboard(FIXTURE, Config.load(FIXTURE))
    assert "card-title\">Goals<" not in html
    assert "Rolling 7-day velocity" not in html
    assert "Added Today" not in html


def test_goals_card_renders_when_goals_are_supplied():
    """Calling the generator with an explicit ``goals`` kwarg should render
    the banner stat and Goals card. This stays fixture-agnostic: we feed in
    a synthetic Goals object and look for its numbers in the HTML.
    """
    from proseview.generator import render_html_report
    from proseview.goals import Goals
    from proseview.scenes import collect_scene_stats, filter_scenes, sort_scenes

    cfg = Config.load(FIXTURE)
    scenes = collect_scene_stats(FIXTURE, cfg.manuscript_subdir)
    display = sort_scenes(filter_scenes(scenes, "all", cfg), "path", False)
    fake = Goals(
        rolling_velocity=423.0, streak_days=9, per_chapter_target=20_000,
        per_chapter_current=14_500, words_added_today=777,
        scenes_touched_today=3, head_total_words=58_000, head_chapter_count=4,
    )
    html = render_html_report(scenes, display, FIXTURE, None, cfg, goals=fake)
    assert "Rolling 7-day velocity" in html
    assert "Writing streak" in html
    assert ">423<" in html
    assert ">9<" in html
    assert "+777" in html
    assert "Added Today" in html


def test_dashboard_renders_repo_browser_with_fixture_tree():
    html = build_dashboard(FIXTURE, Config.load(FIXTURE))
    for marker in [
        'id="repoSidebar"',
        'id="sidebarTree"',
        'id="file-preview-panel"',
        'id="filePreviewBody"',
        "function renderSidebarTree",
        "function previewRepoFile",
        "const repoTree =",
        "const repoPreviewMax =",
        ]:
        assert marker in html, f"missing marker: {marker!r}"


def test_dashboard_embeds_discuss_selection_presets_as_safe_json():
    cfg = Config().with_overrides(
        discuss=DiscussConfig(selection_presets=('Check "grammar" </script><carefully>.',))
    )

    html = build_dashboard(FIXTURE, cfg)

    assert 'const discussSelectionPresets = JSON.parse(' in html
    assert json.loads(_extract_js_literal(html, "discussSelectionPresets")) == [
        'Check "grammar" </script><carefully>.'
    ]
    assignment = html[html.index("const discussSelectionPresets"):]
    assert "<\\\\/script>" in assignment.split(";", 1)[0]


def test_dashboard_scene_meta_embeds_related_docs_and_modal_sidebar(tmp_path: Path):
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE, repo)
    (repo / "plans" / "opening-ledger-notes.md").write_text(
        "# Opening Ledger Notes\n\nOpening Ledger needs a stronger opening beat.\n",
        encoding="utf-8",
    )

    html = build_dashboard(repo, Config.load(repo))
    meta_json = _extract_js_literal(html, "meta")
    meta = json.loads(meta_json)
    opening = meta["ch01/01-opening.md"]

    assert "related_docs" in opening
    assert opening["related_docs"][0]["path"] == "plans/opening-ledger-notes.md"
    assert opening["related_docs"][0]["score"] == 3
    assert 'class="scene-card-related"' in html
    assert 'class="related-doc-link"' in html
    assert "function openRelatedDoc(path)" in html
    assert "No related planning or continuity docs matched this scene." in html


def test_dashboard_embeds_non_manuscript_folders_in_tree():
    html = build_dashboard(FIXTURE, Config.load(FIXTURE))
    tree_json = _extract_js_literal(html, "repoTree")
    tree = json.loads(tree_json)
    top_names = {n["name"] for n in tree}
    assert "plans" in top_names
    assert "story-bible" in top_names
    assert "manuscript" not in top_names


def test_dashboard_oversized_file_flagged_as_too_large(tmp_path: Path):
    from proseview.config import RepoTabConfig
    renamed = tmp_path / "repo"
    shutil.copytree(FIXTURE, renamed)
    big_path = renamed / "plans" / "big.md"
    big_path.write_text("z" * 4096, encoding="utf-8")

    cfg = Config.load(renamed).with_overrides(
        repo_tab=RepoTabConfig(preview_max_bytes=1024),
    )
    html = build_dashboard(renamed, cfg)
    tree = json.loads(_extract_js_literal(html, "repoTree"))

    def find(nodes, target):
        for n in nodes:
            if n.get("path") == target:
                return n
            hit = find(n.get("children") or [], target)
            if hit is not None:
                return hit
        return None

    node = find(tree, "plans/big.md")
    assert node is not None
    assert node["too_large"] is True
    assert node["body"] is None
    assert "const repoPreviewMax = 1024" in html


def _extract_js_literal(html: str, name: str) -> str:
    """Pull out a top-level JS assignment of the form ``<name> = <json>`` from
    the embedded script block.

    Supports both direct JSON literals and the generator's
    ``JSON.parse('...')`` wrapper used for large payloads.
    """
    parse_match = re.search(rf"\b{name}\s*=\s*JSON\.parse\('", html)
    if parse_match:
        start = parse_match.end()
        end = start
        escaped = False
        while end < len(html):
            ch = html[end]
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == "'":
                break
            end += 1
        assert end < len(html), f"unterminated JSON.parse literal for {name!r}"
        return ast.literal_eval("'" + html[start:end] + "'")

    match = re.search(rf"\b{name}\s*=\s*(?=[\[\{{\"])", html)
    assert match, f"could not locate JS literal for {name!r}"
    start = match.end()
    opener = html[start]
    if opener == '"':
        end = start + 1
        while end < len(html):
            if html[end] == '\\':
                end += 2
                continue
            if html[end] == '"':
                return html[start:end + 1]
            end += 1
        raise AssertionError(f"unterminated string literal for {name!r}")
    closer = {"[": "]", "{": "}"}[opener]
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(html)):
        ch = html[i]
        if in_str:
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return html[start:i + 1]
    raise AssertionError(f"could not balance delimiters for {name!r}")


def _extract_script_block(html: str, script_id: str) -> str:
    start_marker = f'<script id="{script_id}">'
    start = html.index(start_marker) + len(start_marker)
    end = html.index("</script>", start)
    return html[start:end]


def test_character_names_come_from_frontmatter_not_the_filename(tmp_path: Path):
    """Multi-word characters were invisible to presence and co-occurrence.

    ``white-rabbit.md`` became ``White-rabbit`` via ``stem.capitalize()``, which
    matches nothing in prose that says "White Rabbit" — so those charts came
    back empty without explaining why.
    """
    from proseview.generator import character_name_from_file

    chars = tmp_path / "characters"
    chars.mkdir()
    declared = chars / "white-rabbit.md"
    declared.write_text("---\nname: White Rabbit\n---\n\n# White Rabbit\n", encoding="utf-8")
    bare = chars / "mock-turtle.md"
    bare.write_text("# Mock Turtle\n", encoding="utf-8")

    assert character_name_from_file(declared) == "White Rabbit"
    # No frontmatter: de-hyphenate rather than leaving "Mock-turtle".
    assert character_name_from_file(bare) == "Mock Turtle"


def test_co_occurrence_finds_pairs_for_multi_word_characters(tmp_path: Path):
    from proseview.generator import build_dashboard

    scene = tmp_path / "manuscript" / "ch01"
    scene.mkdir(parents=True)
    (scene / "01.md").write_text(
        "---\ntitle: Tea\n---\n\n# Tea\n\n"
        "The White Rabbit spoke to the Mock Turtle, and the Mock Turtle wept.\n",
        encoding="utf-8",
    )
    chars = tmp_path / "story-bible" / "characters"
    chars.mkdir(parents=True)
    (chars / "white-rabbit.md").write_text("---\nname: White Rabbit\n---\n", encoding="utf-8")
    (chars / "mock-turtle.md").write_text("---\nname: Mock Turtle\n---\n", encoding="utf-8")

    html = build_dashboard(tmp_path, Config.load(tmp_path))

    assert "White Rabbit+Mock Turtle" in html or "Mock Turtle+White Rabbit" in html
