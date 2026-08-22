"""Regression tests for the post-refactor surface.

Covers the items landed in the spinoff hardening pass (5/6/7/8/9):

- The dashboard renders one ProseMirror surface; the static ``marked.parse``
  fallback is gone (item 3 of the plan, retested here).
- Scene/file viewers are routed inline views, driven by ``data-view``,
  not overlay modals (item 2 retest).
- The Pensive/Action/Balanced emoji label is replaced with Tone
  (Talky/Mixed/Internal) — item 5.
- The dead ``.hl-adverb`` CSS is gone — item 6.
- The front-end is split across ``templates/assets/js/`` and concatenated
  by the generator — item 8.
- ``build_scene_data`` is the single source for ``/data.json``; the
  HTML-scraping ``_extract_script_vars`` helper is removed — item 7.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from proseview.config import Config  # noqa: E402
from proseview.generator import _load_app_js, build_dashboard, build_scene_data  # noqa: E402
from proseview.scenes import collect_scene_stats  # noqa: E402

FIXTURE = REPO_ROOT / "fixtures" / "demo-repo"
TEMPLATES = REPO_ROOT / "proseview" / "templates"
JS_DIR = TEMPLATES / "assets" / "js"
APP_CSS = TEMPLATES / "assets" / "app.css"


# ── Item 8: JS bundle layout ─────────────────────────────────────────────────


def test_js_directory_replaces_monolithic_app_js():
    """The 3,300-line ``app.js`` was split into topical files; the old
    monolith should no longer ship.
    """
    assert not (TEMPLATES / "assets" / "app.js").exists(), \
        "Legacy monolithic app.js should be removed"
    assert JS_DIR.is_dir()
    files = sorted(JS_DIR.glob("*.js"))
    assert len(files) >= 3, "Expected at least three topical JS files"
    # Sortable filenames so concatenation order is stable across platforms.
    for f in files:
        assert re.match(r"\d{2}-[a-z0-9-]+\.js$", f.name), \
            f"Unexpected JS file name: {f.name!r}"


def test_load_app_js_concatenates_in_lexical_order():
    """``_load_app_js`` is the single point that defines bundling order."""
    bundle = _load_app_js()
    assert bundle, "Bundle should not be empty"
    # First file's first non-blank declaration must appear at the start.
    first_file = sorted(JS_DIR.glob("*.js"))[0]
    first_text = first_file.read_text(encoding="utf-8")
    first_decl = next(
        (line.strip() for line in first_text.splitlines() if line.strip()),
        "",
    )
    if first_decl:
        # The bundle starts with the first file's content, possibly with a
        # leading newline from the join separator.
        head = bundle.lstrip("\n").splitlines()[0].strip()
        assert head == first_decl


def test_dashboard_inlines_concatenated_bundle():
    """Markers from multiple split files must end up in the rendered HTML
    so a single ``<script>`` tag carries the whole front-end.
    """
    html = build_dashboard(FIXTURE, Config.load(FIXTURE))
    # From 00-state.js
    assert "const VALID_TABS = ['overview', 'analysis', 'timeline', 'todos', 'notes', 'settings'];" in html
    # From 19-analysis.js
    assert "function buildAnalysisTab()" in html
    # From 30-router-modal.js
    assert "function openSceneModal(p)" in html
    # From 70-terminal.js
    assert "function spawnTerminal" in html or "function _initSessionXterm" in html
    # From 80-sidebar-init.js
    assert "function previewRepoFile(path, options)" in html
    assert "body.innerHTML = marked.parse(node.body)" not in html
    assert "renderSafeMarkdown(body, node.body, {basePath: node.path})" in html


# ── Item 7: build_scene_data is the data source ──────────────────────────────


def test_extract_script_vars_helper_is_gone():
    """The HTML-scraping fallback is removed; the data flow is direct."""
    from proseview import server  # noqa: WPS433
    assert not hasattr(server, "_extract_script_vars"), \
        "Server should no longer ship HTML-scraping fallback for /data.json"


def test_build_scene_data_matches_template_embed():
    """The dict returned by ``build_scene_data`` must be the same payload
    the template embeds for first paint, so client refresh is consistent.
    """
    cfg = Config.load(FIXTURE)
    scenes = collect_scene_stats(FIXTURE, cfg)
    data = build_scene_data(scenes, FIXTURE, cfg)
    html = build_dashboard(FIXTURE, cfg)

    for path in data["contents"]:
        # Each scene's display path must appear in the embedded JSON.
        assert path in html, f"path {path!r} missing from rendered HTML"
    # Every meta entry must carry the fields the client refresh and
    # scene-stats grid rely on.
    for path, m in data["meta"].items():
        for required in ("words", "energy", "dlg_pct", "abs_path", "mtime"):
            assert required in m, f"{path}: missing meta key {required!r}"


# ── Item 5: tone label replaces Pensive / Action / Balanced ──────────────────


def test_tone_label_replaces_pensive_emoji():
    bundle = _load_app_js()
    assert "Pensive" not in bundle, "Old Pensive label should be gone"
    assert "Action ⚡" not in bundle
    assert "Balanced ⚖️" not in bundle
    # Pass metadata documents what each pass measures.
    assert "PASS_EXAMPLES" in bundle
    assert "PASS_NOTES" in bundle
    assert "PASS_INLINE_TIPS" in bundle


# ── Item 6: dead CSS removed ─────────────────────────────────────────────────


def test_hl_adverb_dead_css_removed():
    css = APP_CSS.read_text(encoding="utf-8")
    assert "hl-adverb" not in css, ".hl-adverb has no matching highlight pass; CSS should be gone"


def test_flavor_column_is_actually_populated():
    """The plan flagged Flavor as possibly broken; verify it isn't.
    Each scene with prose should have a non-empty flavor_words tuple.
    """
    cfg = Config.load(FIXTURE)
    scenes = collect_scene_stats(FIXTURE, cfg)
    populated = [s for s in scenes if s.flavor_words]
    assert populated, "Expected at least one scene with flavor_words"


# ── Item 2 (retest): scene viewer is a routed view, not a modal ──────────────


def test_scene_view_uses_data_view_routing():
    """The scene viewer is shown by setting ``data-view='scene'`` on the
    document element, not by toggling ``display`` on the modal node.
    """
    bundle = _load_app_js()
    css = APP_CSS.read_text(encoding="utf-8")

    assert 'document.documentElement.dataset.view = \'scene\'' in bundle
    # Closing the scene clears the view.
    assert "delete document.documentElement.dataset.view" in bundle
    # No more direct display:block toggles on #sceneModal.
    assert "document.getElementById('sceneModal').style.display" not in bundle
    # CSS gates visibility on the data attribute and hides dashboard chrome.
    assert "[data-view=\"scene\"] .modal { display: block; }" in css
    assert "[data-view=\"scene\"] .tab-panel" in css


# ── Item 9 supplemental: bundle is syntactically valid (when Node is present) ─


def test_bundle_passes_node_syntax_check_when_available():
    node = shutil.which("node")
    if node is None:
        return  # skip silently in CI environments without node
    bundle = _load_app_js()
    proc = subprocess.run(
        [node, "--check", "-"],
        input=bundle,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        msg = proc.stderr.strip().splitlines()[:10]
        raise AssertionError("Bundle failed Node syntax check:\n" + "\n".join(msg))


# ── Item 4 (config decoupling): characters_path / skills_path ────────────────


def test_config_keys_for_characters_and_skills_paths(tmp_path: Path):
    """A ``.proseview.yaml`` that names different folders should override
    the defaults so the tool works on any novel-repo layout.
    """
    yaml = tmp_path / ".proseview.yaml"
    yaml.write_text(
        "characters_path: people/who\n"
        "skills_path: prompts\n",
        encoding="utf-8",
    )
    cfg = Config.load(tmp_path)
    assert cfg.characters_path == "people/who"
    assert cfg.characters_dir == "people/who"
    assert cfg.skills_path == "prompts"
    assert cfg.skills_dir == "prompts"


def test_config_defaults_match_legacy_layout():
    """Out of the box, paths default to the layout the original book repo
    used so nothing breaks for existing users.
    """
    cfg = Config()
    assert cfg.characters_path == "story-bible/characters"
    assert cfg.skills_path == ".proseview/skills"


def test_vendor_directory_ships_pinned_assets():
    """The dashboard loads chart.js / marked / xterm from /vendor/ so
    it works offline and can't break on a jsDelivr major bump. The
    files must be present in the package.
    """
    vendor = REPO_ROOT / "proseview" / "templates" / "vendor"
    assert vendor.is_dir()
    expected = {
        "chart.js", "chartjs-plugin-annotation.js", "marked.js",
        "xterm.css", "xterm.js", "xterm-addon-fit.js",
    }
    present = {p.name for p in vendor.iterdir() if p.is_file()}
    missing = expected - present
    assert not missing, f"vendor directory missing files: {missing}"


def test_template_loads_vendored_assets_from_local_paths():
    """No more jsDelivr URLs in the rendered HTML for the non-ESM
    deps; they all come from /vendor/.
    """
    html = build_dashboard(FIXTURE, Config.load(FIXTURE))
    # All vendored assets are loaded relative to /vendor/.
    for filename in ("chart.js", "marked.js", "xterm.css", "xterm.js",
                     "xterm-addon-fit.js", "chartjs-plugin-annotation.js"):
        assert "/vendor/" + filename in html, f"vendor path missing: {filename}"
    assert "cdn.jsdelivr.net" not in html, \
        "jsDelivr URLs should be vendored locally"


PM_PACKAGES = [
    "prosemirror-model", "prosemirror-markdown", "prosemirror-state",
    "prosemirror-view", "prosemirror-history", "prosemirror-keymap",
    "prosemirror-commands",
]


def test_prosemirror_is_served_from_this_origin():
    """The editor must not fetch code from a CDN at page load.

    It used to import seven packages from esm.sh, which cost real load time,
    made the app unusable offline, and disclosed manuscript-opening activity to
    a third party. They are vendored under ``vendor/pm/`` now.
    """
    template = (REPO_ROOT / "proseview" / "templates" / "index.html.j2").read_text(encoding="utf-8")
    imports = re.findall(r"^\s*import\s.*?from\s+'([^']+)'", template, re.M)
    assert imports, "expected the ProseMirror import block"
    for spec in imports:
        assert spec.startswith("/vendor/"), f"{spec} is not served from this origin"

    for pkg in PM_PACKAGES:
        assert f"'/vendor/pm/{pkg}.js'" in template, f"{pkg} is not imported from vendor/pm"


def test_vendored_prosemirror_graph_is_complete_and_local():
    """Every vendored module must resolve inside the vendor directory.

    A missed rewrite would still load -- from the CDN -- and only show up as a
    slow page, so this checks the graph rather than trusting the download.
    """
    pm_dir = REPO_ROOT / "proseview" / "templates" / "vendor" / "pm"
    files = sorted(pm_dir.glob("*.js"))
    assert len(files) > 20, f"expected the full ESM graph, found {len(files)} files"

    names = {f.name for f in files}
    for path in files:
        source = path.read_text(encoding="utf-8")
        # The `/* esm.sh - pkg@version */` provenance banner is fine and worth
        # keeping; anything the browser would actually fetch is not.
        assert "sourceMappingURL" not in source, \
            f"{path.name} points at an un-vendored source map"
        for spec in re.findall(r"""(?:from|import)\s*['"]([^'"]+)['"]""", source):
            if spec.startswith("data:"):
                continue
            assert spec.startswith("./"), f"{path.name} has a non-local import: {spec}"
            assert spec[2:] in names, f"{path.name} imports missing module {spec}"


def test_prosemirror_model_is_deduplicated():
    """One copy only.

    Two copies would mean two ``Schema`` classes, and ``instanceof`` checks
    across the boundary would fail in ways that surface as confusing parser
    errors rather than an obvious import problem.
    """
    pm_dir = REPO_ROOT / "proseview" / "templates" / "vendor" / "pm"
    implementations = [
        p for p in pm_dir.glob("*prosemirror-model*.js")
        if len(p.read_text(encoding="utf-8")) > 5000
    ]
    assert len(implementations) == 1, \
        f"expected exactly one prosemirror-model implementation, got {[p.name for p in implementations]}"


# ── Repo-wide search palette (plan item 20) ──────────────────────────────────


def test_search_module_present_in_bundle():
    """The search palette JS is its own topical file, sorted to load
    after state/prose-indicators but before the router so it can
    reference globals (contents, meta, paths, repositoryFileByPath) and
    define functions the router needs (focusSearch).
    """
    files = sorted(p.name for p in JS_DIR.glob("*.js"))
    search_files = [n for n in files if "search" in n]
    assert search_files, f"no search module in bundle: {files}"
    # Search file must sort before the router so its functions are
    # hoisted into scope when the router code runs.
    router_files = [n for n in files if "router" in n]
    if router_files:
        assert search_files[0] < router_files[0], \
            f"search module must sort before router: {files}"


def test_template_contains_search_input_and_panel():
    template = (REPO_ROOT / "proseview" / "templates" / "index.html.j2").read_text(encoding="utf-8")
    assert 'id="searchBox"' in template
    assert 'id="searchResults"' in template
    assert 'id="searchPalette"' in template
    # Dashboard search is inline and wide. The same menu is moved into the
    # dialog only from routed scene/file views.
    mount_start = template.find('id="dashboardSearchMount"')
    font_start = template.find('id="fontMenu"', mount_start)
    assert mount_start >= 0 and font_start > mount_start
    dashboard_search_html = template[mount_start:font_start]
    assert 'id="searchBox"' in dashboard_search_html
    assert 'aria-label="Close search"' in dashboard_search_html
    assert 'aria-label="Close scene and return to dashboard"' in template


def test_search_cmd_k_shortcut_in_bundle():
    """Cmd-K / Ctrl-K focuses the search input from anywhere on the page."""
    bundle = _load_app_js()
    # The keydown handler tests both modifier flags so it works on
    # macOS (Cmd-K) and Linux/Windows (Ctrl-K).
    assert "e.metaKey" in bundle and "e.ctrlKey" in bundle
    assert "e.key === 'k'" in bundle or "e.key === 'K'" in bundle
    assert "function focusSearch(" in bundle


def test_search_navigates_to_correct_routes():
    """File hits go through previewRepoFile; scene title / frontmatter
    hits go through openSceneModal; TODO/NOTE hits jump to the Tasks
    panel row (where Edit / Delete buttons live); PROSE hits scroll
    the matching paragraph into the editor surface.
    """
    bundle = _load_app_js()
    activate = re.search(
        r"function _activateSearchResult\([^)]*\)\s*\{(.+?)\n        \}",
        bundle, re.DOTALL,
    )
    assert activate, "_activateSearchResult function not found"
    body = activate.group(1)
    assert "previewRepoFile(" in body
    assert "openSceneModal(" in body
    assert "_scrollToPara(" in body
    # TODO / NOTE hits route to the Tasks panel row helper.
    assert "_jumpToTaskRow('todo'" in body
    assert "_jumpToTaskRow('note'" in body
    # The helper itself walks the live Tasks panel by data attribute.
    assert "function _findTaskRowInScene(" in bundle
    assert "data-todo-text" in bundle or "dataset.todoText" in bundle


def test_search_respects_min_query_length_and_cap():
    """A blank or one-character query returns no results; total hits
    are capped so the dropdown can never explode.
    """
    bundle = _load_app_js()
    assert "SEARCH_MIN_LEN" in bundle
    assert "SEARCH_RESULT_CAP" in bundle


def test_data_json_contract_round_trips_through_json():
    """Whatever ``build_scene_data`` returns must be JSON-serializable as-is,
    since the server hands it straight to ``json.dumps``.
    """
    cfg = Config.load(FIXTURE)
    scenes = collect_scene_stats(FIXTURE, cfg)
    data = build_scene_data(scenes, FIXTURE, cfg)
    # If this raises, the dict has non-JSON values that the server cannot send.
    json.dumps(data)


# ── Scroll restoration: refresh must not animate from top ────────────────────


def test_write_scroll_top_uses_instant_behavior():
    """``writeScrollTop`` is called from route restoration. CSS sets
    ``scroll-behavior: smooth`` on .modal-content for in-page jumps; the
    restoration path must override it with ``behavior: 'instant'`` so a
    page refresh does not visibly animate from the top to the saved
    position.
    """
    bundle = _load_app_js()
    body = re.search(
        r"function writeScrollTop\([^)]*\)\s*\{(.+?)\n        \}",
        bundle, re.DOTALL,
    )
    assert body, "writeScrollTop function not found"
    fn = body.group(1)
    assert "behavior: 'instant'" in fn, \
        "writeScrollTop must request instant scrolling to bypass smooth CSS"


# ── Annotation rendering: HTML comments become marker nodes ──────────────────


def test_template_enables_html_in_markdown_tokenizer():
    """``<!-- TODO/NOTE -->`` blocks reach the annotation parser rule only
    if markdown-it's html option is on. The default tokenizer is loaded
    with html:false; the bootstrap script must flip it.
    """
    template = (REPO_ROOT / "proseview" / "templates" / "index.html.j2").read_text(encoding="utf-8")
    assert "defaultMarkdownParser.tokenizer.set({ html: true })" in template, \
        "Template must enable html on the markdown-it tokenizer so html_block tokens are emitted"


def test_task_jump_button_resolves_against_prosemirror_dom():
    """The Tasks panel inside a scene shows a downward arrow next to each
    TODO/note that scrolls the prose to the matching paragraph. Before
    the spinoff, scenes were rendered with ``<div class="prose-para"
    data-para-idx="N">`` wrappers and the click handler used those.
    Once ProseMirror became the only renderer those wrappers were
    gone, so the arrows silently no-op'd. This test locks in a query
    that walks the live ProseMirror DOM.
    """
    bundle = _load_app_js()
    # The new resolver function exists.
    assert "function _findParaTarget(paraIdx)" in bundle
    # It walks the live ProseMirror tree, not the legacy wrapper class.
    assert "host.querySelector('.ProseMirror')" in bundle
    assert ".prose-para[data-para-idx=" not in bundle, \
        "Stale .prose-para query (the wrapper is no longer rendered)"
    # Headings are filtered so the index lines up with paragraph_blocks().
    assert "/^H[1-6]$/i.test(el.tagName)" in bundle


def test_scene_and_file_views_hide_the_dashboard_chrome():
    """The banner and toolbar must collapse when a routed view takes over.

    These selectors once shared a rule with ``.deep-link-help``; removing that
    footer took the ``display: none`` declaration with it and silently left the
    chrome on screen in scene view. The rule is worth pinning.
    """
    css = APP_CSS.read_text(encoding="utf-8")
    block = css.split('[data-view="scene"] .top-banner,')[1].split("}")[0]
    assert "display: none" in block
    for selector in ('[data-view="scene"] .app-toolbar',
                     '[data-view="file"] .top-banner',
                     '[data-view="file"] .app-toolbar'):
        assert selector in block, f"{selector} no longer shares the hide rule"


def test_the_deep_link_help_footer_is_gone():
    """It documented URL syntax on every dashboard tab; the README covers it."""
    template = (TEMPLATES / "index.html.j2").read_text(encoding="utf-8")
    assert "deep-link-help" not in template
    assert "Deep-link URLs" not in template


def test_empty_character_charts_explain_themselves():
    """A chart with no rows must say why, not show a bare axis.

    "No characters configured" and "the name matching is broken" looked
    identical on screen, which is how the ``stem.capitalize()`` bug survived:
    an empty co-occurrence chart is what a frontmatter-free manuscript looks
    like too.
    """
    bundle = _load_app_js()
    assert "function noteIfEmpty(" in bundle
    for chart in ("presenceChart", "coOccurChart", "locationChart"):
        assert f"noteIfEmpty('{chart}'" in bundle, f"{chart} has no empty state"
    assert "No characters yet" in bundle
    assert "No settings yet" in bundle
