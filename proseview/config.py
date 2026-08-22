"""Configuration loader for ``.proseview.yaml``.

Holds the v1 config surface defined in the implementation plan: manuscript
path, target word count and daily cadence, lexical goal bands, chapter glob,
character and location overrides, and editor handoff settings. Every key has
a default so a repo without ``.proseview.yaml`` still produces a dashboard.

The loader implements a narrow subset of YAML sufficient for the v1 config
shape (scalars, inline arrays, block lists, one level of nested mapping).
Adopting PyYAML is an option tracked in the plan's Open Questions; for now
the stdlib-only parser keeps v1 single-file-ish.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any


DEFAULT_EDITOR_SCHEME = "vscode"
BUILTIN_EDITOR_SCHEMES = {"vscode", "cursor", "zed", "positron", "custom"}

# These defaults are conventions, not hard requirements. A novel repo that
# uses different folder names sets `characters_path`, `skills_path`, and
# `repo_tab.folders` in its `.proseview.yaml`; missing folders are simply
# skipped at render time.
DEFAULT_CHARACTERS_PATH = "story-bible/characters"
DEFAULT_SKILLS_PATH = ".proseview/skills"
DEFAULT_REPO_TAB_FOLDERS: tuple[str, ...] = (
    "plans", "continuity", "outline", "story-bible", "docs", "templates",
)
DEFAULT_REPO_TAB_PREVIEW_MAX_BYTES = 512 * 1024

# Typical MTLD ranges by genre.
#
# MTLD is length-independent by construction: McCarthy & Jarvis (2010), "MTLD,
# vocd-D, and HD-D: A validation study of sophisticated approaches to lexical
# diversity assessment" (Behavior Research Methods), established the 0.72
# factor threshold this project uses in ``lexical.MTLD_THRESHOLD``. That part
# has a citation.
#
# These bands do not. They are the consensus ranges reported across corpus
# stylistics work, not a measurement anyone made here, which is why the UI
# calls them "typical" rather than "benchmarked". They exist because the
# alternative -- one hardcoded pair for every book ever written -- badged most
# of Alice in Wonderland as too repetitive.
#
# The direction is the useful part and it is not controversial: dialogue-heavy
# contemporary prose repeats pronouns and auxiliaries, so it scores lower;
# world-building genres keep introducing distinct nouns, so they score higher.
GENRE_MTLD_BANDS: dict[str, tuple[float, float]] = {
    "childrens": (40.0, 60.0),
    "contemporary": (60.0, 85.0),
    "literary": (85.0, 110.0),
    "speculative": (90.0, 120.0),
}
GENRE_LABELS: dict[str, str] = {
    "childrens": "Children's / middle grade",
    "contemporary": "Commercial & contemporary",
    "literary": "Historical & literary",
    "speculative": "Fantasy & science fiction",
}
DEFAULT_GENRE = "contemporary"
DISCUSS_SELECTION_PRESET_MAX = 12
DISCUSS_SELECTION_PRESET_LENGTH_MAX = 32_768


@dataclass(frozen=True)
class EditorConfig:
    scheme: str = DEFAULT_EDITOR_SCHEME
    url_template: str | None = None


@dataclass(frozen=True)
class RepoTabConfig:
    folders: tuple[str, ...] = DEFAULT_REPO_TAB_FOLDERS
    preview_max_bytes: int = DEFAULT_REPO_TAB_PREVIEW_MAX_BYTES


@dataclass(frozen=True)
class DiscussConfig:
    """Stable, repository-level shortcuts for selection questions."""

    selection_presets: tuple[str, ...] = ()
    #: Which agent tab the dock opens on. Both tabs always exist and run
    #: independently; this only decides which one is in front to begin with.
    agent: str = "codex"


#: Agents Discuss can be pointed at.
DISCUSS_AGENTS: tuple[str, ...] = ("codex", "claude")


#: Accepted values for ``images``, loosest first.
IMAGE_MODES: tuple[str, ...] = ("all", "local", "off")


@dataclass(frozen=True)
class ImagesConfig:
    """Whether rendered Markdown may load images, and from where.

    ``all``   -- repo images and remote URLs both load.
    ``local`` -- only files inside this repository, served by this server. A
                 remote URL cannot report back who opened a document.
    ``off``   -- nothing loads; every image shows its alt text.

    ``remote_in_agent_output`` stays separate so it can be explicitly enabled
    without weakening the default local-first boundary. Discuss renders text an
    agent produced, so a remote image there is chosen by the model rather than
    by you: loading it tells that host your IP and that you opened the document.
    """

    mode: str = "all"
    remote_in_agent_output: bool = False


@dataclass(frozen=True)
class StoryConfig:
    """Which frontmatter keys carry the story-layer fields.

    Defaults are the names Proseview documents. A manuscript that already uses
    its own convention points these at its own keys instead of rewriting 48
    files.
    """

    thread_field: str = "thread"
    day_field: str = "day"


@dataclass(frozen=True)
class Config:
    manuscript_path: str = "manuscript/"
    characters_path: str = DEFAULT_CHARACTERS_PATH
    skills_path: str = DEFAULT_SKILLS_PATH
    target_words: int = 80000
    daily_target: int = 500
    genre: str = DEFAULT_GENRE
    mattr_band: tuple[float, float] = (0.74, 0.77)
    # Derived from ``genre`` unless the file sets it explicitly. The old default
    # was (105, 130), a pair of numbers with nothing behind them: it badged most
    # of Alice in Wonderland as too repetitive.
    mtld_band: tuple[float, float] = GENRE_MTLD_BANDS["contemporary"]
    chapter_pattern: str = "ch*"
    characters: tuple[str, ...] = ()
    locations: tuple[str, ...] = ()
    editor: EditorConfig = field(default_factory=EditorConfig)
    repo_tab: RepoTabConfig = field(default_factory=RepoTabConfig)
    discuss: DiscussConfig = field(default_factory=DiscussConfig)
    images: ImagesConfig = field(default_factory=ImagesConfig)
    story: StoryConfig = field(default_factory=StoryConfig)
    max_backups: int = 50

    @property
    def manuscript_subdir(self) -> str:
        return self.manuscript_path.rstrip("/")

    @property
    def characters_dir(self) -> str:
        return self.characters_path.rstrip("/")

    @property
    def skills_dir(self) -> str:
        return self.skills_path.rstrip("/")

    @classmethod
    def load(cls, repo_root: Path) -> "Config":
        """Load ``.proseview.yaml`` from ``repo_root``, falling back to defaults.

        Missing file: silently returns defaults. Unknown top-level keys: warned
        once but the rest of the config still loads. Invalid values for known
        keys raise ``ConfigError`` so the CLI fails loudly rather than silently
        rendering wrong numbers.
        """
        path = repo_root / ".proseview.yaml"
        if not path.exists():
            return cls()
        raw = _parse_yaml(path.read_text(encoding="utf-8-sig"))
        return cls._from_raw(raw, source=path)

    @classmethod
    def _from_raw(cls, raw: dict[str, Any], *, source: Path | None = None) -> "Config":
        defaults = cls()
        known = set(_config_field_names())
        for key in raw:
            if key not in known:
                import warnings
                origin = f" ({source})" if source else ""
                warnings.warn(f"proseview: unknown config key {key!r}{origin}; ignoring",
                              stacklevel=2)

        manuscript_path = _coerce_str(raw.get("manuscript_path", defaults.manuscript_path),
                                      "manuscript_path")
        if not manuscript_path.endswith("/"):
            manuscript_path = manuscript_path + "/"

        genre = _coerce_genre(raw.get("genre", defaults.genre))

        return cls(
            manuscript_path=manuscript_path,
            characters_path=_coerce_str(raw.get("characters_path", defaults.characters_path),
                                        "characters_path"),
            skills_path=_coerce_str(raw.get("skills_path", defaults.skills_path),
                                    "skills_path"),
            target_words=_coerce_int(raw.get("target_words", defaults.target_words),
                                     "target_words"),
            daily_target=_coerce_int(raw.get("daily_target", defaults.daily_target),
                                     "daily_target"),
            genre=genre,
            mattr_band=_coerce_band(raw.get("mattr_band", defaults.mattr_band),
                                    "mattr_band"),
            # An explicit mtld_band still wins: someone who has measured their
            # own corpus should not be overruled by a genre label.
            mtld_band=_coerce_band(raw.get("mtld_band", GENRE_MTLD_BANDS[genre]),
                                   "mtld_band"),
            chapter_pattern=_coerce_str(raw.get("chapter_pattern", defaults.chapter_pattern),
                                        "chapter_pattern"),
            characters=_coerce_str_tuple(raw.get("characters", ()), "characters"),
            locations=_coerce_str_tuple(raw.get("locations", ()), "locations"),
            editor=_coerce_editor(raw.get("editor")),
            repo_tab=_coerce_repo_tab(raw.get("repo_tab")),
            discuss=_coerce_discuss(raw.get("discuss")),
            images=_coerce_images(raw.get("images")),
            story=_coerce_story(raw.get("story")),
            max_backups=_coerce_int(raw.get("max_backups", defaults.max_backups), "max_backups"),
        )

    def with_overrides(self, **kwargs: Any) -> "Config":
        """Return a new ``Config`` with the given fields replaced. Convenience
        for tests and the upcoming ``--config`` CLI overrides.
        """
        return replace(self, **kwargs)

    def save(self, repo_root: Path) -> None:
        """Write the configuration back to ``.proseview.yaml``.
        
        Uses ruamel.yaml to preserve existing comments or formatting.
        """
        import ruamel.yaml
        path = repo_root / ".proseview.yaml"
        yaml = ruamel.yaml.YAML()
        yaml.preserve_quotes = True
        yaml.indent(mapping=2, sequence=4, offset=2)
        
        if path.exists():
            try:
                data = yaml.load(path)
            except Exception:
                data = {}
        else:
            data = {}
            
        if data is None:
            data = {}

        defaults = Config()
        
        if self.target_words != defaults.target_words or "target_words" in data:
            data["target_words"] = self.target_words
        if self.daily_target != defaults.daily_target or "daily_target" in data:
            data["daily_target"] = self.daily_target
        if self.max_backups != defaults.max_backups or "max_backups" in data:
            data["max_backups"] = self.max_backups
        if self.genre != defaults.genre or "genre" in data:
            data["genre"] = self.genre
        if self.mattr_band != defaults.mattr_band or "mattr_band" in data:
            data["mattr_band"] = list(self.mattr_band)
        
        default_mtld = GENRE_MTLD_BANDS.get(self.genre, defaults.mtld_band)
        if self.mtld_band != default_mtld or "mtld_band" in data:
            data["mtld_band"] = list(self.mtld_band)
        if self.discuss != defaults.discuss or "discuss" in data:
            discuss = data.get("discuss")
            if not isinstance(discuss, dict):
                discuss = {}
                data["discuss"] = discuss
            discuss["selection_presets"] = list(self.discuss.selection_presets)

        with path.open("w", encoding="utf-8") as f:
            yaml.dump(data, f)


class ConfigError(ValueError):
    """Raised when ``.proseview.yaml`` contains an invalid value for a known key."""


def _config_field_names() -> tuple[str, ...]:
    return (
        "manuscript_path", "characters_path", "skills_path",
        "target_words", "daily_target",
        "genre", "mattr_band", "mtld_band", "chapter_pattern",
        "characters", "locations", "editor", "repo_tab", "story", "images",
        "discuss", "max_backups",
    )


def _coerce_genre(v: Any) -> str:
    """Genre is a label the writer sets, never something Proseview infers.

    A guess here would be worse than the old fixed band: it would look
    authoritative while being wrong. Alice in Wonderland has a median MTLD of
    77.7, which lands in the contemporary range rather than the children's one
    the shelf label would predict.
    """
    if v is None:
        return DEFAULT_GENRE
    if not isinstance(v, str):
        raise ConfigError(f"genre: expected one of {sorted(GENRE_MTLD_BANDS)}, got {v!r}")
    key = v.strip().lower().replace("'", "").replace("_", "").replace(" ", "")
    aliases = {"children": "childrens", "middlegrade": "childrens", "mg": "childrens",
               "commercial": "contemporary", "historical": "literary",
               "fantasy": "speculative", "scifi": "speculative",
               "sciencefiction": "speculative"}
    key = aliases.get(key, key)
    if key not in GENRE_MTLD_BANDS:
        raise ConfigError(f"genre: expected one of {sorted(GENRE_MTLD_BANDS)}, got {v!r}")
    return key


def _coerce_str(v: Any, key: str) -> str:
    if isinstance(v, str):
        return v
    raise ConfigError(f"{key!r} must be a string, got {type(v).__name__}: {v!r}")


def _coerce_int(v: Any, key: str) -> int:
    if isinstance(v, bool):
        raise ConfigError(f"{key!r} must be an integer, got bool")
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    raise ConfigError(f"{key!r} must be an integer, got {type(v).__name__}: {v!r}")


def _coerce_float(v: Any, key: str) -> float:
    if isinstance(v, bool):
        raise ConfigError(f"{key!r} entries must be numbers, got bool")
    if isinstance(v, (int, float)):
        return float(v)
    raise ConfigError(f"{key!r} entries must be numbers, got {type(v).__name__}: {v!r}")


def _coerce_band(v: Any, key: str) -> tuple[float, float]:
    if isinstance(v, tuple) and len(v) == 2:
        return (_coerce_float(v[0], key), _coerce_float(v[1], key))
    if isinstance(v, list):
        if len(v) != 2:
            raise ConfigError(f"{key!r} must be a 2-element list, got {len(v)} elements")
        return (_coerce_float(v[0], key), _coerce_float(v[1], key))
    raise ConfigError(f"{key!r} must be a 2-element list, got {type(v).__name__}: {v!r}")


def _coerce_str_tuple(v: Any, key: str) -> tuple[str, ...]:
    if isinstance(v, tuple):
        return tuple(_coerce_str(x, key) for x in v)
    if isinstance(v, list):
        return tuple(_coerce_str(x, key) for x in v)
    if v is None:
        return ()
    raise ConfigError(f"{key!r} must be a list of strings, got {type(v).__name__}: {v!r}")


def _coerce_editor(v: Any) -> EditorConfig:
    defaults = EditorConfig()
    if v is None:
        return defaults
    if not isinstance(v, dict):
        raise ConfigError(f"'editor' must be a mapping, got {type(v).__name__}: {v!r}")
    scheme = v.get("scheme", defaults.scheme)
    if not isinstance(scheme, str):
        raise ConfigError(f"'editor.scheme' must be a string, got {type(scheme).__name__}")
    if scheme not in BUILTIN_EDITOR_SCHEMES:
        allowed = ", ".join(sorted(BUILTIN_EDITOR_SCHEMES))
        raise ConfigError(f"'editor.scheme' must be one of {{{allowed}}}; got {scheme!r}")
    url_template = v.get("url_template", defaults.url_template)
    if url_template is not None and not isinstance(url_template, str):
        raise ConfigError("'editor.url_template' must be a string or null")
    if scheme == "custom" and not url_template:
        raise ConfigError("'editor.scheme: custom' requires 'editor.url_template'")
    if scheme == "custom" and url_template:
        candidate_prefix = url_template.lstrip().split(":", 1)[0]
        if any(ord(char) < 0x20 or ord(char) == 0x7f for char in candidate_prefix):
            raise ConfigError(
                "'editor.url_template' must not contain controls in its URL scheme"
            )
        candidate_scheme = candidate_prefix.lower()
        if candidate_scheme in {"javascript", "data", "vbscript"}:
            raise ConfigError(
                "'editor.url_template' must not use a browser-executable URL scheme"
            )
    return EditorConfig(scheme=scheme, url_template=url_template)


def _coerce_repo_tab(v: Any) -> RepoTabConfig:
    defaults = RepoTabConfig()
    if v is None:
        return defaults
    if not isinstance(v, dict):
        raise ConfigError(f"'repo_tab' must be a mapping, got {type(v).__name__}: {v!r}")
    folders_raw = v.get("folders", defaults.folders)
    folders = _coerce_str_tuple(folders_raw, "repo_tab.folders") if folders_raw else defaults.folders
    preview_raw = v.get("preview_max_bytes", defaults.preview_max_bytes)
    preview_max_bytes = _coerce_int(preview_raw, "repo_tab.preview_max_bytes")
    if preview_max_bytes <= 0:
        raise ConfigError("'repo_tab.preview_max_bytes' must be a positive integer")
    return RepoTabConfig(folders=folders, preview_max_bytes=preview_max_bytes)


def _coerce_discuss(v: Any) -> DiscussConfig:
    if v is None:
        return DiscussConfig()
    if not isinstance(v, dict):
        raise ConfigError(f"'discuss' must be a mapping, got {type(v).__name__}: {v!r}")
    raw_presets = v.get("selection_presets", ())
    if not isinstance(raw_presets, (list, tuple)):
        raise ConfigError("'discuss.selection_presets' must be a list of strings")

    presets: list[str] = []
    for raw in raw_presets:
        if not isinstance(raw, str) or not raw.strip():
            raise ConfigError("'discuss.selection_presets' entries must be non-empty strings")
        value = raw.strip()
        if len(value) > DISCUSS_SELECTION_PRESET_LENGTH_MAX:
            raise ConfigError(
                "'discuss.selection_presets' entries must be at most "
                f"{DISCUSS_SELECTION_PRESET_LENGTH_MAX} characters"
            )
        if value not in presets:
            presets.append(value)
    if len(presets) > DISCUSS_SELECTION_PRESET_MAX:
        raise ConfigError(
            f"'discuss.selection_presets' supports at most {DISCUSS_SELECTION_PRESET_MAX} entries"
        )
    agent = v.get("agent", DiscussConfig().agent)
    if not isinstance(agent, str) or agent.strip().lower() not in DISCUSS_AGENTS:
        raise ConfigError(
            "'discuss.agent' must be one of " + ", ".join(DISCUSS_AGENTS) + f", got {agent!r}"
        )
    return DiscussConfig(selection_presets=tuple(presets), agent=agent.strip().lower())


def _coerce_images(v: Any) -> ImagesConfig:
    defaults = ImagesConfig()
    if v is None:
        return defaults
    # `images: off` is the shorthand people will reach for first.
    if isinstance(v, str):
        return ImagesConfig(mode=_validated_image_mode(v))
    if isinstance(v, bool):
        return ImagesConfig(mode="all" if v else "off")
    if not isinstance(v, dict):
        raise ConfigError(f"'images' must be a mapping, string, or boolean, got {v!r}")
    mode = _validated_image_mode(v.get("mode", defaults.mode))
    remote_raw = v.get("remote_in_agent_output", defaults.remote_in_agent_output)
    if not isinstance(remote_raw, bool):
        raise ConfigError("'images.remote_in_agent_output' must be true or false")
    return ImagesConfig(mode=mode, remote_in_agent_output=remote_raw)


def _validated_image_mode(v: Any) -> str:
    if not isinstance(v, str) or v.strip().lower() not in IMAGE_MODES:
        raise ConfigError(f"'images.mode' must be one of {', '.join(IMAGE_MODES)}; got {v!r}")
    return v.strip().lower()


def _parse_yaml(text: str) -> dict[str, Any]:
    """Parse ``.proseview.yaml`` text using PyYAML's safe loader.

    Returns an empty dict for empty input. Raises :class:`ConfigError`
    if the file isn't a top-level mapping or if PyYAML rejects the
    syntax.
    """
    import yaml

    if not text.strip():
        return {}
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError(
            f"top-level YAML must be a mapping, got {type(loaded).__name__}"
        )
    return loaded


def _coerce_story(v: Any) -> StoryConfig:
    defaults = StoryConfig()
    if v is None:
        return defaults
    if not isinstance(v, dict):
        raise ConfigError(f"'story' must be a mapping, got {type(v).__name__}: {v!r}")
    thread_field = v.get("thread_field")
    day_field = v.get("day_field")
    return StoryConfig(
        thread_field=_coerce_str(thread_field, "story.thread_field") if thread_field else defaults.thread_field,
        day_field=_coerce_str(day_field, "story.day_field") if day_field else defaults.day_field,
    )
