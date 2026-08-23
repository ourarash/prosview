"""Repo file tree for the dashboard's file browser and preview surface.

Walks the top-level folders listed in ``cfg.repo_tab.folders`` (plans,
continuity, outline, story-bible, docs, templates by default) and returns a
nested tree that the client renders. Manuscript content is excluded on
purpose: the Scene tab already covers it.

File bodies are embedded inline for files at or below
``cfg.repo_tab.preview_max_bytes`` so the dashboard can preview without a
server round-trip. Oversized files and non-text files carry metadata only;
the client renders a warning instead of loading their contents into the DOM.

The returned structure is JSON-safe so the generator can embed it directly
with ``json.dumps``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any
import unicodedata
import uuid

from .config import Config

TEXT_SUFFIXES: frozenset[str] = frozenset({
    ".md", ".markdown", ".txt", ".yaml", ".yml",
    ".json", ".toml", ".cfg", ".ini", ".rst",
})
CONTEXT_FILE_MAX_BYTES = 512 * 1024
CONTEXT_SKIP_DIRS: frozenset[str] = frozenset({
    ".git", ".proseview", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "node_modules", "__pycache__",
    # Skills are instructions to an agent, not story material. Prosview writes
    # its own defaults in here, and a manuscript view that counted them as
    # scenes would be reporting its own furniture back as prose.
    ".proseview/skills",
})


def _read_utf8_text(path: Path, max_bytes: int) -> str | None:
    """Read a bounded UTF-8 text file, rejecting binary-looking content."""
    try:
        if path.stat().st_size > max_bytes:
            return None
        payload = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in payload:
        return None
    try:
        return payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def is_context_text_file(path: Path, max_file_bytes: int = CONTEXT_FILE_MAX_BYTES) -> bool:
    """Return whether *path* is attachable UTF-8 repository context.

    Discuss is intentionally not limited to the dashboard preview suffixes:
    source files, templates, prompts, and extensionless text files are useful
    agent context too. Binary, malformed, and oversized files stay outside the
    browser inventory and are rejected again at the API boundary.
    """
    return path.is_file() and _read_utf8_text(path, max_file_bytes) is not None


def _iso_mtime(path: Path) -> str:
    ts = path.stat().st_mtime
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .astimezone()
        .isoformat(timespec="seconds")
    )


def read_repo_text(path: Path) -> str:
    """Read a file that came from the user's repository.

    ``utf-8-sig``, not ``utf-8``: a byte-order mark is invisible but it sits
    *before* the ``---`` that opens frontmatter, so the block fails to match and
    every field is silently lost -- the title falls back to the filename and the
    metadata leaks into the prose, inflating word counts and polluting search.
    Files exported from Word or written by Windows editors carry one routinely.

    Identical to ``utf-8`` when no BOM is present.
    """
    return path.read_text(encoding="utf-8-sig")


def _is_hidden(name: str) -> bool:
    return name.startswith(".")


def resolve_visible_repository_path(root: Path, value: str) -> Path:
    """Resolve a path shared by repository-facing browser capabilities."""
    resolved_root = root.resolve()
    raw = str(value or "").strip().replace("\\", "/")
    relative = Path(raw)
    if (
        not raw
        or relative.is_absolute()
        or ".." in relative.parts
        or any((part.startswith(".") and part != ".proseview.yaml") or part in CONTEXT_SKIP_DIRS for part in relative.parts)
    ):
        raise ValueError("path must be a safe visible repository-relative path")

    candidate = resolved_root
    has_symlink = False
    for part in relative.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            has_symlink = True
    resolved = candidate.resolve()
    if not resolved.is_relative_to(resolved_root):
        raise ValueError("path resolves outside the repository")
    if has_symlink:
        raise ValueError("symlinks are not a safe visible repository path")
    return resolved


_WINDOWS_RESERVED_NAME_RE = re.compile(
    r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", re.IGNORECASE
)
_PORTABLE_NAME_FORBIDDEN = frozenset('<>:"/\\|?*')


def _portable_entry_name(value: str) -> str:
    """Return one portable, visible file-browser name.

    Proseview supports Windows as well as POSIX. Rejecting the strictest common
    set here means a project created on macOS can still be cloned and edited on
    Windows without names that cannot be checked out or renamed there.
    """
    name = unicodedata.normalize("NFC", str(value or "").strip())
    if (
        not name
        or name in {".", ".."}
        or name.startswith(".")
        or name.endswith((".", " "))
        or any(char in _PORTABLE_NAME_FORBIDDEN or ord(char) < 32 for char in name)
        or _WINDOWS_RESERVED_NAME_RE.fullmatch(name)
        or len(name.encode("utf-8")) > 240
    ):
        raise ValueError("name must be a single visible name that works on every supported platform")
    return name


def _managed_repository_roots(root: Path, cfg: Config) -> tuple[Path, ...]:
    """Existing top-level folders represented by the file-browser sidebar."""
    resolved_root = root.resolve()
    configured = (cfg.manuscript_subdir, *cfg.repo_tab.folders)
    managed: list[Path] = []
    for value in configured:
        relative = str(value or "").strip("/").strip()
        if not relative:
            continue
        try:
            candidate = resolve_visible_repository_path(resolved_root, relative)
        except ValueError:
            continue
        if candidate.is_dir() and candidate not in managed:
            managed.append(candidate)
    return tuple(managed)


def _resolve_managed_repository_path(root: Path, cfg: Config, value: str) -> tuple[Path, tuple[Path, ...]]:
    resolved_root = root.resolve()
    candidate = resolve_visible_repository_path(resolved_root, value)
    managed = _managed_repository_roots(resolved_root, cfg)
    if not any(candidate == folder or candidate.is_relative_to(folder) for folder in managed):
        raise PermissionError("path is outside a managed file-browser folder")
    return candidate, managed


def _entry_name_for_kind(name: str, kind: str, source: Path | None = None) -> str:
    if kind not in {"file", "folder"}:
        raise ValueError("kind must be file or folder")
    portable = _portable_entry_name(name)
    if kind == "file" and (source is None or source.suffix.lower() == ".md"):
        if portable.lower().endswith(".md"):
            portable = portable[:-3]
            portable = _portable_entry_name(portable)
        portable += ".md"
    return portable


def create_repository_entry(
    root: Path,
    cfg: Config,
    parent_path: str,
    name: str,
    kind: str,
) -> str:
    """Create one empty Markdown file or one folder in the visible sidebar.

    Parent directories are never created implicitly and existing paths are
    never overwritten. The returned path is repository-relative POSIX text.
    """
    resolved_root = root.resolve()
    parent, _managed = _resolve_managed_repository_path(resolved_root, cfg, parent_path)
    if not parent.is_dir():
        raise FileNotFoundError("parent folder does not exist")
    entry_name = _entry_name_for_kind(name, kind)
    # Build from the already validated, resolved parent. Reusing the raw input
    # here would interpret Windows-style separators differently on POSIX.
    relative = (parent.relative_to(resolved_root) / entry_name).as_posix()
    target = resolve_visible_repository_path(resolved_root, relative)
    if target.parent != parent:
        raise ValueError("target must stay inside the selected folder")
    if target.exists():
        raise FileExistsError(f"{relative} already exists")
    if kind == "folder":
        target.mkdir()
    else:
        with target.open("xb"):
            pass
    return target.relative_to(resolved_root).as_posix()


def rename_repository_entry(
    root: Path,
    cfg: Config,
    path: str,
    new_name: str,
) -> str:
    """Rename a visible file or nested folder without changing its parent."""
    resolved_root = root.resolve()
    source, managed = _resolve_managed_repository_path(resolved_root, cfg, path)
    if not source.exists():
        raise FileNotFoundError(f"{path} does not exist")
    if source in managed:
        raise PermissionError("top-level managed folders cannot be renamed")
    if not (source.is_file() or source.is_dir()):
        raise ValueError("path must name a regular file or folder")
    kind = "file" if source.is_file() else "folder"
    entry_name = _entry_name_for_kind(new_name, kind, source)
    destination_rel = (source.relative_to(resolved_root).parent / entry_name).as_posix()
    destination = resolve_visible_repository_path(resolved_root, destination_rel)
    if destination == source:
        return destination_rel
    if destination.exists():
        raise FileExistsError(f"{destination_rel} already exists")
    source.rename(destination)
    return destination.relative_to(resolved_root).as_posix()


def trash_repository_entry(root: Path, cfg: Config, path: str) -> dict[str, Any]:
    """Move a visible file or nested folder into Proseview's local trash.

    Moving instead of recursively unlinking keeps deletion recoverable without
    adding a restore workflow to the sidebar. The trash lives on the same
    filesystem under ``.proseview/``, so the move is a single rename.
    """
    resolved_root = root.resolve()
    source, managed = _resolve_managed_repository_path(resolved_root, cfg, path)
    if not source.exists():
        raise FileNotFoundError(f"{path} does not exist")
    if source in managed:
        raise PermissionError("top-level managed folders cannot be deleted")
    if not (source.is_file() or source.is_dir()):
        raise ValueError("path must name a regular file or folder")

    kind = "file" if source.is_file() else "folder"
    entry_count = 1 if kind == "file" else sum(1 for _entry in source.rglob("*"))
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    internal = resolved_root / ".proseview"
    trash = internal / "trash"
    if internal.is_symlink() or trash.is_symlink():
        raise PermissionError("Proseview Trash must not be a symlink")
    trash.mkdir(parents=True, exist_ok=True)
    if not trash.resolve().is_relative_to(resolved_root):
        raise PermissionError("Proseview Trash resolves outside the repository")
    bucket = trash / f"{stamp}-{uuid.uuid4().hex[:8]}"
    destination = bucket / source.relative_to(resolved_root)
    destination.parent.mkdir(parents=True, exist_ok=False)
    source.rename(destination)
    return {
        "path": source.relative_to(resolved_root).as_posix(),
        "kind": kind,
        "entry_count": entry_count,
        "trash_path": destination.relative_to(resolved_root).as_posix(),
    }


def scene_relative_path(rel: str, manuscript_subdir: str) -> str | None:
    """Return the scene-index key for *rel*, or ``None`` when it is not a scene.

    Mirrors ``scenes.iter_scene_paths``, which is what actually populates the
    client's scene index: only ``*.md`` files exactly one directory below the
    manuscript root are scenes, and READMEs are skipped. Deeper manuscript
    notes (``manuscript/ch05/review/foo.md``) are ordinary repository files —
    flagging them as scenes routes the client to a scene it cannot find.
    """
    prefix = manuscript_subdir.rstrip("/") + "/"
    if not rel.startswith(prefix):
        return None
    scene_rel = rel[len(prefix):]
    parts = Path(scene_rel).parts
    if len(parts) != 2 or not parts[1].endswith(".md") or parts[1].lower() == "readme.md":
        return None
    return scene_rel


def _file_node(path: Path, root: Path, preview_max: int) -> dict[str, Any]:
    rel = path.relative_to(root).as_posix()
    size = path.stat().st_size
    too_large = size > preview_max
    body = _read_utf8_text(path, preview_max)
    is_text = body is not None or (too_large and path.suffix.lower() in TEXT_SUFFIXES)
    return {
        "name": path.name,
        "path": rel,
        "abs_path": str(path.resolve()),
        "is_file": True,
        "modified_at": _iso_mtime(path),
        "size": size,
        "is_text": is_text,
        "too_large": too_large,
        "body": body,
    }


def _dir_node(path: Path, root: Path, preview_max: int, excluded: set[str]) -> dict[str, Any] | None:
    rel = path.relative_to(root).as_posix()
    if rel in excluded:
        return None
    children: list[dict[str, Any]] = []
    try:
        entries = sorted(
            path.iterdir(),
            key=lambda p: (p.is_file(), p.name.lower()),
        )
    except OSError:
        entries = []
    for child in entries:
        if _is_hidden(child.name):
            continue
        if child.is_dir():
            sub = _dir_node(child, root, preview_max, excluded)
            if sub is not None:
                children.append(sub)
        elif child.is_file():
            children.append(_file_node(child, root, preview_max))
    return {
        "name": path.name,
        "path": rel,
        "is_file": False,
        "modified_at": _iso_mtime(path),
        "children": children,
    }


def _file_node_scene(path: Path, root: Path, manuscript_subdir: str) -> dict[str, Any]:
    """Lightweight node for a manuscript Markdown file (body omitted).

    Files the scene index does not carry (READMEs, notes nested below a
    chapter dir) stay in the sidebar but are marked as ordinary files, so the
    click handler previews them instead of opening an absent scene.
    """
    rel = path.relative_to(root).as_posix()
    scene_path = scene_relative_path(rel, manuscript_subdir)
    return {
        "name": path.name,
        "path": rel,
        "abs_path": str(path.resolve()),
        "is_file": True,
        "is_scene": scene_path is not None,
        "scene_path": scene_path,
        "modified_at": _iso_mtime(path),
        "size": path.stat().st_size,
        "is_text": True,
        "too_large": False,
        "body": None,
    }


def _dir_node_manuscript(path: Path, root: Path, manuscript_subdir: str) -> dict[str, Any] | None:
    """Walk the manuscript directory and mark .md files as scene nodes."""
    children: list[dict[str, Any]] = []
    try:
        entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except OSError:
        entries = []
    for child in entries:
        if _is_hidden(child.name):
            continue
        if child.is_dir():
            sub = _dir_node_manuscript(child, root, manuscript_subdir)
            if sub is not None:
                children.append(sub)
        elif child.is_file() and child.suffix.lower() in {".md", ".markdown"}:
            children.append(_file_node_scene(child, root, manuscript_subdir))
    return {
        "name": path.name,
        "path": path.relative_to(root).as_posix(),
        "is_file": False,
        "modified_at": _iso_mtime(path),
        "children": children,
    }


def _file_node_meta(path: Path, root: Path) -> dict[str, Any]:
    """Minimal sidebar node for a non-manuscript file. No body embedded.

    The JS sidebar click handler uses ``repoFileByPath`` (built from
    ``repoTree``) for the actual body, so there is no need to duplicate it
    here.
    """
    return {
        "name": path.name,
        "path": path.relative_to(root).as_posix(),
        "is_file": True,
        "is_scene": False,
    }


def _dir_node_meta(path: Path, root: Path, excluded: set[str]) -> dict[str, Any] | None:
    """Walk a directory for the sidebar without embedding file bodies."""
    rel = path.relative_to(root).as_posix()
    if rel in excluded:
        return None
    children: list[dict[str, Any]] = []
    try:
        entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except OSError:
        entries = []
    for child in entries:
        if _is_hidden(child.name):
            continue
        if child.is_dir():
            sub = _dir_node_meta(child, root, excluded)
            if sub is not None:
                children.append(sub)
        elif child.is_file():
            children.append(_file_node_meta(child, root))
    return {
        "name": path.name,
        "path": rel,
        "is_file": False,
        "modified_at": _iso_mtime(path),
        "children": children,
    }


def build_sidebar_tree(root: Path, cfg: Config) -> list[dict[str, Any]]:
    """Return tree nodes for the persistent sidebar.

    Manuscript directory is listed first with ``is_scene`` markers so the
    sidebar JS can open the scene modal on click. Non-manuscript repo folders
    follow as metadata-only nodes; their file bodies live in ``repoTree`` /
    ``repoFileByPath`` and are looked up there at click time.
    """
    nodes: list[dict[str, Any]] = []

    ms = root / cfg.manuscript_subdir
    if ms.exists() and ms.is_dir():
        ms_node = _dir_node_manuscript(ms, root, cfg.manuscript_subdir)
        if ms_node is not None:
            nodes.append(ms_node)

    excluded = {cfg.manuscript_subdir}
    for name in cfg.repo_tab.folders:
        trimmed = name.strip("/").strip()
        if not trimmed or trimmed in excluded:
            continue
        candidate = root / trimmed
        if not candidate.exists() or not candidate.is_dir():
            continue
        node = _dir_node_meta(candidate, root, excluded)
        if node is not None:
            nodes.append(node)

    yaml_path = root / ".proseview.yaml"
    if yaml_path.exists() and yaml_path.is_file():
        nodes.append({
            "name": ".proseview.yaml",
            "path": ".proseview.yaml",
            "is_file": True,
            "is_scene": False,
        })

    return nodes


def recent_changes(
    root: Path,
    cfg: Config,
    since: str = "7 days ago",
) -> tuple[list[dict[str, Any]], bool]:
    """Return files changed in the last ``since`` period from git log.

    Returns ``(entries, git_available)``.  When git is unavailable or ``root``
    is not the worktree top-level the list is empty and the flag is ``False``.

    Each entry carries:
      path          relative path from repo root (forward slashes)
      abs_path      resolved absolute path string
      is_scene      True when the file is in the client's scene index
      scene_path    path relative to manuscript_subdir for scenes, else None
      modified_at   ISO date string of the most-recent touching commit
    """
    from .history import is_git_repo  # avoid circular at module level
    import subprocess as _sp

    if not is_git_repo(root):
        return [], False

    content_dirs: list[str] = [cfg.manuscript_path, *list(cfg.repo_tab.folders)]

    try:
        result = _sp.run(
            [
                "git", "log",
                "--since", since,
                "-z",
                "--name-only",
                "--diff-filter=AM",
                "--pretty=format:__PV_DATE__ %ai%x00",
                "--first-parent",
                "--",
                *content_dirs,
            ],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
    except (FileNotFoundError, _sp.TimeoutExpired):
        return [], False

    if result.returncode != 0:
        return [], False

    # NUL delimiters preserve filenames exactly. Git's default newline format
    # C-quotes names containing quotes, tabs, or newlines; treating that quoted
    # representation as a real path both breaks navigation and makes correct
    # destination-specific escaping impossible.
    #
    # Sentinel records supply the date; all other non-empty records are file
    # paths. Deduplicate by path, keeping the first (most-recent) occurrence.
    entries: dict[str, dict[str, Any]] = {}
    current_date = ""
    for raw in result.stdout.split("\0"):
        line = raw.lstrip("\n")
        if not line:
            continue
        if line.startswith("__PV_DATE__ "):
            current_date = line[len("__PV_DATE__ "):]
        elif line not in entries:
            scene_rel = scene_relative_path(line, cfg.manuscript_subdir)
            entries[line] = {
                "path": line,
                "abs_path": str((root / line).resolve()),
                "is_scene": scene_rel is not None,
                "scene_path": scene_rel,
                "modified_at": current_date,
            }

    return list(entries.values()), True


def build_tree(root: Path, cfg: Config) -> list[dict[str, Any]]:
    """Return the top-level tree nodes for the dashboard file browser.

    Only configured folders that exist on disk as directories are included.
    The manuscript directory is excluded even if it appears in
    ``cfg.repo_tab.folders``: the Scene tab is the authoritative surface
    for that content.
    """
    preview_max = cfg.repo_tab.preview_max_bytes
    excluded = {cfg.manuscript_subdir}
    nodes: list[dict[str, Any]] = []
    for name in cfg.repo_tab.folders:
        trimmed = name.strip("/").strip()
        if not trimmed or trimmed in excluded:
            continue
        candidate = root / trimmed
        if not candidate.exists() or not candidate.is_dir():
            continue
        node = _dir_node(candidate, root, preview_max, excluded)
        if node is not None:
            nodes.append(node)
    return nodes


def _repository_file_node(
    path: Path,
    root: Path,
    cfg: Config,
    context_max_bytes: int,
) -> dict[str, Any] | None:
    """Return capability metadata for one contained repository file."""
    try:
        resolved = path.resolve()
        if path.is_symlink() or not resolved.is_relative_to(root) or not resolved.is_file():
            return None
        size = resolved.stat().st_size
    except OSError:
        return None
    inspection_limit = max(cfg.repo_tab.preview_max_bytes, context_max_bytes)
    text = _read_utf8_text(resolved, inspection_limit)
    rel = path.relative_to(root).as_posix()
    scene_rel = scene_relative_path(rel, cfg.manuscript_subdir)
    return {
        "name": path.name,
        "path": rel,
        "is_file": True,
        "is_scene": scene_rel is not None,
        "scene_path": scene_rel,
        "is_text": text is not None or (
            size > inspection_limit and path.suffix.lower() in TEXT_SUFFIXES
        ),
        "previewable": size <= cfg.repo_tab.preview_max_bytes and text is not None,
        "attachable": size <= context_max_bytes and text is not None,
        "too_large": size > cfg.repo_tab.preview_max_bytes,
        "size": size,
    }


def _repository_dir_node(
    path: Path,
    root: Path,
    cfg: Config,
    context_max_bytes: int,
) -> dict[str, Any] | None:
    """Walk one repository directory for the canonical metadata index."""
    if path.name.startswith(".") or path.name in CONTEXT_SKIP_DIRS or path.is_symlink():
        return None
    try:
        entries = sorted(path.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
    except OSError:
        return None
    children: list[dict[str, Any]] = []
    for child in entries:
        if child.name.startswith(".") or child.name in CONTEXT_SKIP_DIRS:
            continue
        if child.is_dir():
            node = _repository_dir_node(child, root, cfg, context_max_bytes)
        elif child.is_file():
            node = _repository_file_node(child, root, cfg, context_max_bytes)
        else:
            node = None
        if node is not None:
            children.append(node)
    if not children:
        return None
    return {
        "name": path.name,
        "path": path.relative_to(root).as_posix(),
        "is_file": False,
        "attachable": any(bool(child.get("attachable")) for child in children),
        "children": children,
    }


def build_repository_tree(
    root: Path,
    cfg: Config | None = None,
    *,
    context_max_bytes: int = CONTEXT_FILE_MAX_BYTES,
) -> list[dict[str, Any]]:
    """Return the canonical metadata-only repository inventory.

    Consumers select files by explicit capability flags instead of maintaining
    separate universes for navigation and agent context. Hidden/internal paths
    and symlinks remain outside the inventory; no file bodies or absolute paths
    are serialized into the browser.
    """
    resolved_root = root.resolve()
    cfg = cfg or Config.load(resolved_root)
    try:
        entries = sorted(resolved_root.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
    except OSError:
        return []
    nodes: list[dict[str, Any]] = []
    for entry in entries:
        if (entry.name.startswith(".") and entry.name != ".proseview.yaml") or entry.name in CONTEXT_SKIP_DIRS:
            continue
        if entry.is_dir():
            node = _repository_dir_node(entry, resolved_root, cfg, context_max_bytes)
        elif entry.is_file():
            node = _repository_file_node(entry, resolved_root, cfg, context_max_bytes)
        else:
            node = None
        if node is not None:
            nodes.append(node)
    return nodes


def build_context_tree(
    root: Path,
    *,
    max_file_bytes: int = CONTEXT_FILE_MAX_BYTES,
) -> list[dict[str, Any]]:
    """Compatibility projection containing only attachable context paths."""
    repository = build_repository_tree(root, context_max_bytes=max_file_bytes)

    def project(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        projected: list[dict[str, Any]] = []
        for node in nodes:
            if node.get("is_file"):
                if node.get("attachable"):
                    projected.append(dict(node))
                continue
            children = project(list(node.get("children") or []))
            if children:
                copy = dict(node)
                copy["children"] = children
                projected.append(copy)
        return projected

    return project(repository)
