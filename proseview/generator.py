"""HTML dashboard generator.

Public entry: :func:`build_dashboard`. Takes a repo root and a
:class:`~proseview.config.Config`, returns the single-file HTML string that
``.proseview/stats.html`` is written from.

The dashboard is emitted as one self-contained HTML file, but the source is
split into a Jinja template plus inlined CSS and JavaScript assets under
``proseview/templates/``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import html
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import GENRE_LABELS, Config
from .editor import build_url as _editor_url, label as _editor_label
from .goals import Goals, compute_goals
from .highlights import PASS_NAMES, compute_scene_highlights
from .history import HistoryRow, load_history, working_copy_delta
from .lexical import QUOTE_RE, FILTER_VERBS, calculate_lexical_stats
from .related import find_related
from .repo import build_repository_tree, build_sidebar_tree, build_tree, read_repo_text, recent_changes
from .story import story_payload
from .scenes import (
    BaselineStats,
    SceneStats,
    collect_scene_stats,
    filter_scenes,
    goal_position,
    revision_signal,
    scene_is_outlier,
    scene_mattr_median,
    scene_mtld_median,
    sort_scenes,
)

TEMPLATE_DIR = Path(__file__).with_name("templates")


@lru_cache(maxsize=1)
def _template_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(enabled_extensions=("html", "xml", "j2")),
        keep_trailing_newline=True,
    )


@lru_cache(maxsize=None)
def _load_asset(relative_path: str) -> str:
    return (TEMPLATE_DIR / relative_path).read_text(encoding="utf-8")


def _load_app_css() -> str:
    """Concatenate ``assets/app.css`` with every theme in ``assets/themes``.

    Themes live in their own files so one can be added or edited without
    touching the main stylesheet — the same reason the front-end JS is split.
    Order is app.css first, then themes lexically, so a theme's ``[data-theme]``
    block always wins over the defaults it overrides.
    """
    parts = [_load_asset("assets/app.css")]
    themes_dir = TEMPLATE_DIR / "assets" / "themes"
    if themes_dir.is_dir():
        for path in sorted(themes_dir.glob("*.css")):
            parts.append(_load_asset(f"assets/themes/{path.name}"))
    return "\n".join(parts)


def _load_app_js() -> str:
    """Concatenate ``templates/assets/js/*.js`` in lexical order.

    The front-end is split into topical files for readability but emitted
    as one ``<script>`` block so the runtime is identical to the legacy
    single-file form (no module boundaries, all top-level functions
    hoisted into the same scope). Each file is fetched via ``_load_asset``
    so ``_load_asset.cache_clear()`` invalidates them all at once.
    """
    js_dir = TEMPLATE_DIR / "assets" / "js"
    if not js_dir.is_dir():
        return _load_asset("assets/app.js")
    rel_paths = sorted(p.relative_to(TEMPLATE_DIR).as_posix() for p in js_dir.glob("*.js"))
    return "\n".join(_load_asset(rel) for rel in rel_paths)


def _render_template(name: str, **context: object) -> str:
    return _template_env().get_template(name).render(**context)


def _render_goals_sections(goals: Goals | None, cfg: Config) -> tuple[str, str]:
    """Return (banner_fragment, card_fragment). Both are empty strings when
    ``goals`` is ``None`` (non-git repo or no history), which hides the Goals
    panel entirely while keeping the surrounding layout intact.
    """
    if goals is None:
        return "", ""

    banner = (
        '<div class="banner-stat" style="margin-left:30px;">'
        '<span class="label">Added Today</span>'
        f'<span class="value">+{goals.words_added_today:,}</span>'
        "</div>"
    )

    chapter_target = goals.per_chapter_target or 1
    chapter_pct = min(100, goals.per_chapter_current / chapter_target * 100) if chapter_target else 0
    card = f"""<div class="card">
        <div class="card-header"><span class="card-title">Goals</span></div>
        <div class="goals-grid">
            <div class="goal-metric">
                <span class="goal-value">{goals.rolling_velocity:.0f}</span>
                <span class="goal-label">Rolling 7-day velocity</span>
                <span class="goal-sub">target: {cfg.daily_target:,} words/day</span>
            </div>
            <div class="goal-metric">
                <span class="goal-value">{goals.streak_days}</span>
                <span class="goal-label">Writing streak</span>
                <span class="goal-sub">consecutive days</span>
            </div>
            <div class="goal-metric">
                <span class="goal-value">{goals.scenes_touched_today}</span>
                <span class="goal-label">Scenes touched today</span>
                <span class="goal-sub">uncommitted + today's commits</span>
            </div>
            <div class="goal-metric">
                <span class="goal-value">{goals.per_chapter_current:,}</span>
                <span class="goal-label">Per-chapter average</span>
                <span class="goal-sub">target: {goals.per_chapter_target:,}</span>
                <div class="range-bar" style="margin-top:6px;">
                    <div class="target-zone" style="width:{chapter_pct:.1f}%;"></div>
                </div>
            </div>
        </div>
    </div>"""
    return banner, card


def character_name_from_file(path: Path) -> str:
    """The character's name, preferring what the file declares.

    A character file usually carries ``name: White Rabbit`` in frontmatter.
    Deriving the name from the filename instead gave ``White-rabbit``, which
    matches nothing in the prose -- so multi-word characters were silently
    absent from presence and co-occurrence, and the charts came back empty
    without saying why.
    """
    try:
        head = read_repo_text(path)[:400]
    except OSError:
        head = ""
    match = re.search(r"^name:\s*(.+?)\s*$", head, re.M)
    if match:
        declared = match.group(1).strip().strip("\"'")
        if declared:
            return declared
    return path.stem.replace("-", " ").replace("_", " ").title()


def _js_json(data: object) -> str:
    """Return a JS expression ``JSON.parse('...')`` that evaluates to *data*.

    Embedding large data as JS object literals causes V8's recursive-descent
    parser to exhaust the JS call stack (RangeError). JSON.parse() is
    implemented in C++ and uses no JS stack depth, so this is the safe way to
    inline large JSON payloads in a <script> tag.

    Escaping applied to the inner string literal:
      1. ``</`` → ``<\\/`` so the HTML parser never sees ``</script>``
      2. ``\\`` → ``\\\\`` so the JS string parser doesn't consume the
         backslashes that JSON.parse needs
      3. ``'`` → ``\\'`` so the single-quoted JS string stays well-formed
    """
    s = json.dumps(data, ensure_ascii=True)
    s = s.replace("</", "<\\/")
    s = s.replace("\\", "\\\\")
    s = s.replace("'", "\\'")
    return f"JSON.parse('{s}')"


def _html_esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _render_recent_changes_card(
    entries: list[dict[str, object]],
    git_available: bool,
    cfg: Config,
) -> str:
    """Return the Recently Modified card wrapped in a full-width grid row."""
    editor_label = _editor_label(cfg)

    if not git_available:
        inner = "<p>Git is not available; recent changes cannot be determined.</p>"
    elif not entries:
        inner = "<p>No files changed in the last 7 days.</p>"
    else:
        rows: list[str] = []
        for entry in entries[:20]:
            path = str(entry["path"])
            abs_path = str(entry["abs_path"])
            editor_url = _editor_url(cfg, abs_path)
            open_btn = (
                f'<a class="editor-btn" href="{_html_esc(editor_url)}" target="_blank">'
                f"&#x2197; {_html_esc(editor_label)}</a>"
            )
            if entry.get("is_scene") and entry.get("scene_path"):
                data_attr = f'data-scene-path="{_html_esc(str(entry["scene_path"]))}"'
                onclick = "openSceneModal(this.dataset.scenePath)"
            else:
                data_attr = f'data-repo-path="{_html_esc(path)}"'
                onclick = "previewRepoFile(this.dataset.repoPath)"
            path_link = (
                f'<button type="button" class="recent-file-link" '
                f'{data_attr} onclick="{onclick}" title="Open {_html_esc(path)}">'
                f"{_html_esc(path)}</button>"
            )
            rows.append(
                f'<div style="display:flex; align-items:center; justify-content:space-between;'
                f' padding:10px 0; border-bottom:1px solid var(--border);">'
                f"{path_link}"
                f'<span style="flex-shrink:0; margin-left:12px;">{open_btn}</span>'
                f"</div>"
            )
        inner = "".join(rows)

    card = (
        '<div class="card">'
        '<div class="card-header">'
        '<span class="card-title">Recently Modified</span>'
        '<span style="font-size:11px; color:var(--text-muted); margin-left:8px;">last 7 days</span>'
        "</div>"
        f"{inner}"
        "</div>"
    )
    return f'<div class="dashboard-grid" style="grid-template-columns: 1fr;">{card}</div>'


def build_dashboard(
    root: Path,
    cfg: Config | None = None,
    *,
    history_rows: list[HistoryRow] | None = None,
    session_token: str = "",
) -> str:
    """Produce the full HTML dashboard for the repo at ``root``."""
    cfg = cfg or Config.load(root)
    # MATTR/MTLD are read only by the Analysis tab, which fetches them on
    # demand, so a dashboard rebuild no longer pays for that pass.
    scenes = collect_scene_stats(root, cfg.manuscript_subdir, lexical=False)
    display_scenes = sort_scenes(filter_scenes(scenes, "all", cfg), "path", False)
    history = load_history(root, cfg) if history_rows is None else history_rows
    delta = working_copy_delta(root, cfg, history, scenes)
    goals = compute_goals(history, cfg, delta) if history or delta.words_added_today else None
    tree_nodes = build_tree(root, cfg)
    sidebar_nodes = build_sidebar_tree(root, cfg)
    repository_nodes = build_repository_tree(root, cfg)
    recent_entries, recent_git = recent_changes(root, cfg)
    return render_html_report(
        scenes,
        display_scenes,
        root,
        None,
        cfg,
        goals=goals,
        tree_nodes=tree_nodes,
        sidebar_nodes=sidebar_nodes,
        repository_nodes=repository_nodes,
        recent_entries=recent_entries,
        recent_git=recent_git,
        session_token=session_token,
    )


def build_scene_data(
    scenes: list[SceneStats],
    root: Path,
    cfg: Config,
    tree_nodes: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Build the JSON payload consumed by ``/data.json`` and embedded in the
    initial HTML.

    Returns a dict with three keys:

    - ``contents``: ``{display_path: prose_text}`` for the embedded ProseMirror
      editor and the highlight overlays.
    - ``meta``: ``{display_path: scene_meta_dict}`` for the scene header,
      stats grid, todos, notes, and related docs.
    - ``highlightsByPath``: ``{display_path: {paragraphs, highlights}}`` for
      the nine highlight passes.

    The data shape is identical to what the template embeds for first paint,
    so a client can hot-refresh the open scene by fetching ``/data.json``
    without a full page reload.
    """
    if tree_nodes is None:
        tree_nodes = build_tree(root, cfg)

    manuscript_prefix = cfg.manuscript_subdir + "/"

    def clean_path(path: Path) -> str:
        # as_posix, not str: these become keys in the embedded JSON and the
        # fragments of every scene URL. On Windows str() yields backslashes,
        # so the browser looks up "ch01/one.md" against "ch01\\one.md" and
        # finds nothing.
        text = path.as_posix()
        return text[len(manuscript_prefix):] if text.startswith(manuscript_prefix) else text

    def _compute_char_mentions(scene: SceneStats) -> dict[str, dict[str, int]]:
        chars = scene.frontmatter.get("characters") if isinstance(scene.frontmatter, dict) else None
        if not chars:
            return {}
        if not isinstance(chars, (list, tuple)):
            chars = [chars]

        dlg_spans = [(m.start(), m.end()) for m in QUOTE_RE.finditer(scene.text)]
        mentions: dict[str, dict[str, int]] = {}

        for c in chars:
            if not isinstance(c, str) or not c.strip():
                continue
            c_name = c.strip()
            c_pattern = re.compile(rf"\b{re.escape(c_name)}\b", re.IGNORECASE)
            
            dlg_count = 0
            prose_count = 0
            
            for m in c_pattern.finditer(scene.text):
                start = m.start()
                is_dlg = any(ds <= start < de for ds, de in dlg_spans)
                if is_dlg:
                    dlg_count += 1
                else:
                    prose_count += 1
            
            mentions[c_name] = {"dialogue": dlg_count, "prose": prose_count}
            
        return mentions

    contents = {clean_path(s.path): s.text for s in scenes}
    highlights = {
        clean_path(s.path): compute_scene_highlights(s.text, repeat_terms=s.repetition_examples)
        for s in scenes
    }
    meta = {
        clean_path(scene.path): {
            "title": scene.title,
            "repeats": scene.repetition_examples,
            "avg_sent": scene.avg_sentence_words,
            "sent_stdev": scene.sent_len_stdev,
            "dlg_pct": scene.dialogue_pct,
            "first_person": scene.first_person_per_1k,
            "italics": scene.italics_per_1k,
            "questions": scene.questions_per_1k,
            "para_words": scene.avg_paragraph_words,
            "signals": revision_signal(scene, cfg).split(". "),
            "read_min": scene.reading_minutes,
            "words": scene.words,
            "passive": scene.passive_per_1k,
            "crutch": scene.crutch_per_1k,
            "sensory": scene.sensory_density,
            "energy": scene.energy_score,
            "top_dlg": scene.top_dialogue_words,
            "abs_path": str((root / scene.path).resolve()),
            "fm": scene.frontmatter,
            "filters": (
                sum(len(re.findall(rf"\b{verb}\b", scene.text, re.IGNORECASE)) for verb in FILTER_VERBS)
                * 1000 / scene.words
                if scene.words else 0
            ),
            "char_mentions": _compute_char_mentions(scene),
            "related_docs": find_related(scene, cfg, tree_nodes),
            "mattr": scene.mattr,
            "mtld": scene.mtld,
            "todos": scene.todos,
            "notes": scene.notes,
            "txt_line_offset": scene.txt_line_offset,
            "mtime": (root / scene.path).stat().st_mtime,
            # A stable token for the exact Markdown source that produced the
            # browser's ProseMirror document. Selection offsets belong to that
            # rendered document, not to a server-side Markdown approximation.
            "revision": hashlib.sha256(scene.text.encode("utf-8")).hexdigest(),
        }
        for scene in scenes
    }
    medians = {
        "words": statistics.median([s.words for s in scenes]) if scenes else 0,
        "avg_sent": statistics.median([s.avg_sentence_words for s in scenes]) if scenes else 0.0,
        "sent_stdev": statistics.median([s.sent_len_stdev for s in scenes]) if scenes else 0.0,
        "dlg_pct": statistics.median([s.dialogue_pct for s in scenes]) if scenes else 0.0,
        "sensory": statistics.median([s.sensory_density for s in scenes]) if scenes else 0.0,
        "first_person": statistics.median([s.first_person_per_1k for s in scenes]) if scenes else 0.0,
        "passive": statistics.median([s.passive_per_1k for s in scenes]) if scenes else 0.0,
        "crutch": statistics.median([s.crutch_per_1k for s in scenes]) if scenes else 0.0,
    }

    return {"contents": contents, "meta": meta, "highlightsByPath": highlights, "medians": medians}

def _scene_table_body(
    display_scenes: list[SceneStats],
    root: Path,
    cfg: Config,
    editor_label: str,
    *,
    analysis: bool,
) -> str:
    """Render the Scene Deep Dive rows.

    With ``analysis=False`` the four analysis-derived columns (Variety, Top
    Repeat, Dlg%, Sent) and their filter data attributes are omitted, so the
    table can be built from a scan that never ran the lexical passes. The
    Analysis tab asks for the full form.
    """
    manuscript_prefix = cfg.manuscript_subdir + "/"

    def clean_path(path: Path) -> str:
        # as_posix, not str: these become keys in the embedded JSON and the
        # fragments of every scene URL. On Windows str() yields backslashes,
        # so the browser looks up "ch01/one.md" against "ch01\\one.md" and
        # finds nothing.
        text = path.as_posix()
        return text[len(manuscript_prefix):] if text.startswith(manuscript_prefix) else text

    span = 8 if analysis else 4
    body = ""
    last_chapter = None
    for scene in display_scenes:
        if scene.chapter != last_chapter:
            chapter_total = sum(
                current.words for current in display_scenes if current.chapter == scene.chapter
            )
            body += (
                f'<tr class="ch-row chapter-row">'
                f'<td colspan="2">{html.escape(scene.chapter)}</td><td>{chapter_total:,}</td>'
                f'<td colspan="{span - 3}"></td></tr>'
            )
            last_chapter = scene.chapter

        display_path = clean_path(scene.path)
        display_path_html = html.escape(display_path)
        display_path_attr = html.escape(display_path, quote=True)
        editor_url = _editor_url(cfg, str((root / scene.path).resolve()))
        todo_count = len(scene.todos)
        note_count = len(scene.notes)
        status_slug = re.sub(r'[^a-z0-9]+', '-', scene.status.lower()) if scene.status else 'unknown'
        status_badge = f'<span class="badge status-badge status-{status_slug}">{html.escape(scene.status)}</span> '
        todo_badge = (
            f'<span class="badge todo-badge" title="{todo_count} TODO(s)">{todo_count} todo</span> '
            if todo_count else ''
        )
        note_badge = (
            f'<span class="badge note-badge" title="{note_count} note(s)">{note_count} note</span> '
            if note_count else ''
        )
        
        alert_badge = ""
        if analysis and scene_is_outlier(scene, cfg):
            msg = html.escape(revision_signal(scene, cfg), quote=True)
            alert_badge = f'<span class="badge badge-danger" title="{msg}" style="cursor:help;">Review</span> '

        style_attrs = (
            f'data-dlg="{scene.dialogue_pct}" data-sent="{scene.avg_sentence_words}" '
            f'data-rep="{scene.repetition_score}" '
            if analysis else ""
        )
        body += (
            f'<tr class="scene-row" {style_attrs}'
            f'data-todos="{todo_count}" data-notes="{note_count}" data-status="{status_slug}">'
            f'<td><button type="button" class="scene-table-link" data-scene-path="{display_path_attr}" '
            f'onclick="openSceneModal(this.dataset.scenePath)">{display_path_html}</button>'
            f'{alert_badge}{status_badge}{todo_badge}{note_badge}'
            f'<a class="editor-btn" href="{html.escape(editor_url, quote=True)}" title="Open in {html.escape(editor_label, quote=True)}" '
            f'onclick="event.stopPropagation()">↗ {html.escape(editor_label)}</a></td>'
            f'<td><span style="font-size:11px; opacity:0.7;">{html.escape(scene.chapter)}</span></td>'
            f'<td style="font-weight:600;">{scene.words:,}</td>'
        )
        if analysis:
            m_pos = goal_position(scene.mattr, cfg.mattr_band)
            l_pos = goal_position(scene.mtld, cfg.mtld_band)
            body += (
                f'<td><span class="badge badge-{m_pos}">{scene.mattr:.3f}</span> '
                f'<span class="badge badge-{l_pos}">{scene.mtld:.1f}</span></td>'
            )
        body += (
            f'<td><span style="font-size:10px; color:var(--text-muted); font-style:italic;">'
            f'{html.escape(", ".join(scene.flavor_words))}</span></td>'
        )
        if analysis:
            body += (
                f'<td><span class="badge {"badge-danger" if scene.repetition_score > 25 else ""}">'
                f'{html.escape(scene.repetition_examples[0] if scene.repetition_examples else "")}</span></td>'
                f'<td><span class="badge {"badge-danger" if scene.dialogue_pct > 45 else ("badge-low" if scene.dialogue_pct < 8 else "")}">'
                f"{scene.dialogue_pct:.1f}%</span></td>"
                f'<td><span class="badge {"badge-high" if scene.avg_sentence_words > 19 else ("badge-low" if scene.avg_sentence_words < 8 else "")}">'
                f"{scene.avg_sentence_words:.1f}</span></td>"
            )
        body += "</tr>"
    return body


def build_analysis_payload(root: Path, cfg: Config | None = None) -> dict[str, object]:
    """Everything the Analysis tab needs, computed on demand.

    This is the expensive half of the old dashboard: the lexical and style
    passes over every scene, which cost roughly 60% of a full build. Nothing on
    the Overview tab reads any of it, so it is no longer paid on every rebuild.
    """
    cfg = cfg or Config.load(root)
    scenes = collect_scene_stats(root, cfg.manuscript_subdir, lexical=True)
    display_scenes = sort_scenes(filter_scenes(scenes, "all", cfg), "path", False)

    manuscript_prefix = cfg.manuscript_subdir + "/"

    def clean_path(path: Path) -> str:
        # as_posix, not str: these become keys in the embedded JSON and the
        # fragments of every scene URL. On Windows str() yields backslashes,
        # so the browser looks up "ch01/one.md" against "ch01\\one.md" and
        # finds nothing.
        text = path.as_posix()
        return text[len(manuscript_prefix):] if text.startswith(manuscript_prefix) else text

    chapters = sorted({scene.chapter for scene in scenes})



    lexical = calculate_lexical_stats("\n\n".join(scene.text for scene in scenes))

    def get_pct(value: float, minimum: float, maximum: float) -> float:
        return max(0, min(100, (value - minimum) / (maximum - minimum) * 100))

    m_start = get_pct(cfg.mattr_band[0], 0.65, 0.85)
    l_start = get_pct(cfg.mtld_band[0], 50, 180)

    return {
        "tableBody": _scene_table_body(
            display_scenes, root, cfg, _editor_label(cfg), analysis=True
        ),
        # Same shapes the template used to inject, so the existing Chart.js
        # setup in 70-dashboard.js builds them without changes.
        "scatterChart": {
            "datasets": [
                {
                    "data": [
                        {"x": scene.mattr, "y": scene.mtld, "label": clean_path(scene.path)}
                        for scene in scenes
                    ]
                }
            ],
            "bands": {"mattr": list(cfg.mattr_band), "mtld": list(cfg.mtld_band)},
        },
        "lexical": {
            "mattrText": f"{lexical.mattr * 100:.1f}%",
            "mattrZoneLeft": f"{m_start:.1f}",
            "mattrZoneWidth": f"{get_pct(cfg.mattr_band[1], 0.65, 0.85) - m_start:.1f}",
            "mattrMarkerLeft": f"{get_pct(lexical.mattr, 0.65, 0.85):.1f}",
            "mtldText": f"{int(lexical.mtld)} words",
            "mtldZoneLeft": f"{l_start:.1f}",
            "mtldZoneWidth": f"{get_pct(cfg.mtld_band[1], 50, 180) - l_start:.1f}",
            "mtldMarkerLeft": f"{get_pct(lexical.mtld, 50, 180):.1f}",
            # The manuscript's own median, which the code has always computed
            # and always discarded. It is the more useful of the two references
            # for revision: it says which scenes are unusual *for this book*,
            # which is a question the genre band cannot answer.
            "mtldMedian": scene_mtld_median(scenes),
            "mattrMedian": scene_mattr_median(scenes),
            "mtldMedianText": f"{int(scene_mtld_median(scenes))} words",
            "mattrMedianText": f"{scene_mattr_median(scenes) * 100:.1f}%",
            "mtldBand": [cfg.mtld_band[0], cfg.mtld_band[1]],
            "genreLabel": GENRE_LABELS.get(cfg.genre, cfg.genre),
        },
    }


def render_html_report(
    scenes: list[SceneStats],
    display_scenes: list[SceneStats],
    root: Path,
    baseline: BaselineStats | None,
    cfg: Config | None = None,
    *,
    goals: Goals | None = None,
    tree_nodes: list[dict[str, object]] | None = None,
    sidebar_nodes: list[dict[str, object]] | None = None,
    repository_nodes: list[dict[str, object]] | None = None,
    recent_entries: list[dict[str, object]] | None = None,
    recent_git: bool | None = None,
    session_token: str = "",
) -> str:
    del baseline

    cfg = cfg or Config.load(root)
    if tree_nodes is None:
        tree_nodes = build_tree(root, cfg)
    if sidebar_nodes is None:
        sidebar_nodes = build_sidebar_tree(root, cfg)
    if repository_nodes is None:
        repository_nodes = build_repository_tree(root, cfg)
    if recent_entries is None:
        recent_entries, recent_git = recent_changes(root, cfg)
    elif recent_git is None:
        recent_git = True
    recent_card = _render_recent_changes_card(recent_entries, bool(recent_git), cfg)

    target_words = cfg.target_words
    daily_target = cfg.daily_target
    total_words = sum(s.words for s in scenes)
    percent_target = (total_words / target_words * 100) if target_words else 0.0
    days_to_finish = math.ceil(max(0, target_words - total_words) / daily_target) if daily_target else 0

    manuscript_prefix = cfg.manuscript_subdir + "/"

    def clean_path(path: Path) -> str:
        # as_posix, not str: these become keys in the embedded JSON and the
        # fragments of every scene URL. On Windows str() yields backslashes,
        # so the browser looks up "ch01/one.md" against "ch01\\one.md" and
        # finds nothing.
        text = path.as_posix()
        return text[len(manuscript_prefix):] if text.startswith(manuscript_prefix) else text

    scene_data = build_scene_data(scenes, root, cfg, tree_nodes)
    scene_content_map = scene_data["contents"]
    highlights_map = scene_data["highlightsByPath"]
    scene_meta_map = scene_data["meta"]
    medians_map = scene_data["medians"]
    ordered_paths = [clean_path(s.path) for s in display_scenes]

    char_dir = root / cfg.characters_dir
    char_bio_map: dict[str, str] = {}
    if char_dir.exists():
        for path in char_dir.glob("*.md"):
            char_bio_map[path.stem.lower()] = read_repo_text(path)

    chapters = sorted({s.chapter for s in scenes})
    if cfg.characters:
        char_names = [c.strip() for c in cfg.characters if c.strip()]
    elif char_dir.exists():
        char_names = [character_name_from_file(p) for p in sorted(char_dir.glob("*.md"))]
    else:
        char_names = []

    presence_data: list[dict[str, object]] = []
    for name in char_names:
        chapter_mentions = [
            len(
                re.findall(
                    rf"\b{re.escape(name)}\b",
                    "\n".join(s.text for s in scenes if s.chapter == chapter),
                    re.IGNORECASE,
                )
            )
            for chapter in chapters
        ]
        if sum(chapter_mentions) > 0:
            presence_data.append({"label": name, "data": chapter_mentions, "fill": False, "tension": 0.3})
    presence_data = sorted(presence_data, key=lambda row: sum(row["data"]), reverse=True)[:12]

    location_counts: Counter[str] = Counter()
    for scene in scenes:
        location_counts[scene.location] += scene.words
    top_locs = location_counts.most_common(8)

    co_occur: dict[tuple[str, str], int] = defaultdict(int)
    for scene in scenes:
        present = [
            name
            for name in char_names
            if re.search(rf"\b{re.escape(name)}\b", scene.text, re.IGNORECASE)
        ]
        for i, first in enumerate(present):
            for second in present[i + 1:]:
                co_occur[tuple(sorted((first, second)))] += 1
    top_pairs = sorted(co_occur.items(), key=lambda item: item[1], reverse=True)[:15]

    editor_label = _editor_label(cfg)
    goals_banner_html, goals_card_html = _render_goals_sections(goals, cfg)
    goals_card_wrapper = (
        f'<div class="dashboard-grid" style="grid-template-columns: 1fr;">{goals_card_html}</div>'
        if goals_card_html
        else ""
    )

    table_body = _scene_table_body(display_scenes, root, cfg, editor_label, analysis=False)

    context = {
        "app_css": _load_app_css(),
        "app_js": _load_app_js(),
        "total_words_text": f"{total_words:,}",
        "target_words_text": f"{target_words:,}",
        "percent_target_text": f"{percent_target:.1f}",
        "percent_target_width": f"{min(100, percent_target):.1f}",
        "scene_count": len(scenes),
        "days_to_finish": days_to_finish,
        "goals_banner_html": goals_banner_html,
        "goals_card_wrapper": goals_card_wrapper,
        "recent_changes_card": recent_card,
        "table_body": table_body,
        "editor_label": editor_label,
        "contents_json": _js_json(scene_content_map),
        "bios_json": _js_json(char_bio_map),
        "meta_json": _js_json(scene_meta_map),
        "medians_json": _js_json(medians_map),
        "paths_json": _js_json(ordered_paths),
        "highlights_json": _js_json(highlights_map),
        "editor_scheme_json": _js_json(cfg.editor.scheme),
        "editor_url_template_json": _js_json(cfg.editor.url_template),
        "editor_label_json": _js_json(editor_label),
        "repo_tree_json": _js_json(tree_nodes),
        "sidebar_tree_json": _js_json(sidebar_nodes),
        "repository_tree_json": _js_json(repository_nodes),
        "repo_preview_max": cfg.repo_tab.preview_max_bytes,
        "images_config_json": _js_json(
            {"mode": cfg.images.mode, "remoteInAgentOutput": cfg.images.remote_in_agent_output}
        ),
        "discuss_presets_json": _js_json(list(cfg.discuss.selection_presets)),
        # Which agent tab the dock opens on. Both tabs always exist; live
        # availability is fetched from /api/discuss/agents once the page runs.
        "discuss_default_agent_json": _js_json(cfg.discuss.agent),
        "lexical_bands_json": _js_json({"mattr": list(cfg.mattr_band), "mtld": list(cfg.mtld_band)}),
        "pass_order_json": _js_json(list(PASS_NAMES)),
        "presence_chart_json": _js_json({"labels": chapters, "datasets": presence_data}),
        "location_chart_json": _js_json(
            {"labels": [name for name, _ in top_locs], "datasets": [{"data": [words for _, words in top_locs]}]}
        ),
        "cooccur_chart_json": _js_json(
            {
                "labels": [f"{pair[0]}+{pair[1]}" for pair, _ in top_pairs],
                "datasets": [{"label": "Shared Scenes", "data": [count for _, count in top_pairs]}],
            }
        ),
        "skills_json": _js_json(_load_skills(root, cfg.skills_dir)),
        "story_json": _js_json(story_payload(scenes, cfg)),
        "session_token_json": json.dumps(session_token),
        "config_json": _js_json(dataclasses.asdict(cfg)),
        "cfg": cfg,
        "config_exists": (root / ".proseview.yaml").exists(),
        "repo_root_json": _js_json(str(root.resolve())),
        "yaml_editor_url": _editor_url(cfg, str((root / ".proseview.yaml").resolve())),
    }
    return _render_template("index.html.j2", **context)


def _load_skills(root: Path, skills_subdir: str = ".proseview/skills") -> list[dict]:
    skills_dir = root / skills_subdir
    if not skills_dir.exists():
        return []

    def _parse_yaml_block(text: str) -> dict:
        """Parse a simple nested YAML block (one level of indentation only)."""
        result: dict = {}
        section: str | None = None
        for line in text.splitlines():
            top = re.match(r'^(\w[\w-]*):\s*$', line)
            if top:
                section = top.group(1)
                result[section] = {}
                continue
            nested = re.match(r'^  (\w[\w_-]*):\s*"?(.*?)"?\s*$', line)
            if nested and section and isinstance(result.get(section), dict):
                result[section][nested.group(1)] = nested.group(2)
                continue
            flat = re.match(r'^(\w[\w-]*):\s*(.+)$', line)
            if flat:
                result[flat.group(1)] = flat.group(2).strip().strip('"')
        return result

    skills = []
    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        raw = read_repo_text(skill_md)
        fm_match = re.match(r'^---\n(.*?)\n---', raw, re.DOTALL)
        name = skill_dir.name
        if fm_match:
            fm = _parse_yaml_block(fm_match.group(1))
            name = fm.get("name", name)

        display_name = name
        short_description = ""
        default_prompt = ""
        openai_yaml = skill_dir / "agents" / "openai.yaml"
        if openai_yaml.exists():
            oy = _parse_yaml_block(read_repo_text(openai_yaml))
            iface = oy.get("interface", {})
            if isinstance(iface, dict):
                display_name = iface.get("display_name", display_name)
                short_description = iface.get("short_description", short_description)
                default_prompt = iface.get("default_prompt", default_prompt)

        skills.append({
            "name": name,
            "display_name": display_name,
            "short_description": short_description,
            "default_prompt": default_prompt,
            "type": "snippet" if name.startswith("snippet-") else "scene",
        })
    return skills
