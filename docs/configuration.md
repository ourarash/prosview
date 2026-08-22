# Configuring Proseview

[← back to the README](../README.md)

`proseview` works with zero config against any folder of Markdown. Drop a
`.proseview.yaml` at the repo root if you want to customize:

```yaml
# Whether rendered Markdown may load images: all | local | off.
# `local` serves only files inside this repo; `off` shows alt text instead.
# `remote_in_agent_output: false` stops remote images inside AI replies
# from loading, since there the URL is the model's choice, not yours.
images:
  mode: all
  remote_in_agent_output: true

# Where the manuscript lives. Default: manuscript/, falling back to the
# repo root when that folder does not exist. Use ./ to force the root.
manuscript_path: manuscript/

# Where character bios live. Default: story-bible/characters
characters_path: story-bible/characters

# Where AI skill prompts live. Default: .proseview/skills
skills_path: .proseview/skills

# Word-count goal for the finished book.
target_words: 80000

# Daily word goal (drives the "days to finish" estimate).
daily_target: 500

# Healthy band for local lexical variety (MATTR).
mattr_band: [0.74, 0.77]

# Healthy band for whole-scene lexical variety (MTLD).
mtld_band: [105, 130]

# Editor URL handler. One of: vscode, cursor, zed, positron, custom.
editor:
  scheme: vscode

# Folders shown in the file tree alongside the manuscript.
repo_tab:
  folders: [plans, continuity, outline, story-bible, docs, templates]

# Stable shortcuts shown when asking Codex about selected prose. Proseview
# displays at most three presets inline; additional presets are under More.
discuss:
  # Which agent tab the dock opens on: codex | claude. Both tabs always
  # exist and run independently; this only picks which one is in front.
  agent: codex
  selection_presets:
    - Is the grammar correct?
    - Make this more direct.
    - Check the point of view.

# Which frontmatter keys the Timeline reads. Defaults shown; point them at
# your own convention instead of renaming fields across the manuscript.
story:
  thread_field: thread
  day_field: day
```

Every key has a sensible default. Missing optional folders are skipped, but the
configured manuscript directory is required.
