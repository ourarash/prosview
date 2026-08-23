# Proseview

> A local dashboard for Markdown-first novel repositories. ✍️

Proseview reads a folder of Markdown scenes and reports lexical health,
pacing, character presence, and revision history. The same pages can be read,
edited, and annotated in place.

Core analysis stays on your machine: no account, subscription, database, or
telemetry. AI features are opt-in and run through agent CLIs you installed and
logged into yourself — Codex or Claude in the side dock. When you use one,
Proseview sends the active document by default, plus any selection or
attachments, to that agent under its own login and data handling.

Proseview reads plain `.md` files in whatever layout they already have, so it
sits alongside Obsidian, Vim, or any other Markdown editor.

[![CI](https://github.com/ourarash/proseview/actions/workflows/ci.yml/badge.svg)](https://github.com/ourarash/proseview/actions/workflows/ci.yml)
![status](https://img.shields.io/badge/status-alpha-orange)
![python](https://img.shields.io/badge/python-3.11%2B-blue)
![platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey)
![license](https://img.shields.io/badge/license-MIT-green)

![The Proseview dashboard with the file sidebar open on chapter one's scenes: total words against the goal, scene count, reading time, and a Goals panel with writing streak and per-chapter average](https://raw.githubusercontent.com/ourarash/proseview/main/docs/images/dashboard.png)

## 📸 Screenshots

### Editorial passes

The Analysis panel lists nine prose passes, each with the number of matches in
this scene. Switching a row on marks the page you are reading: repetition,
filter verbs, sensory language, passive voice.

![The Analysis panel open beside a scene, with four highlight passes switched on from their rows and the matching words marked in the prose](https://raw.githubusercontent.com/ourarash/proseview/main/docs/images/demo-highlights.gif)

### Editing

`Edit` turns the page you were reading into the page you type into — same
typography, same highlights. `Mod-S` saves to the file, and the stats update.

![Typing a new closing line into a scene and saving it with Mod-S, with the word count updating](https://raw.githubusercontent.com/ourarash/proseview/main/docs/images/demo-writing.gif)

### Agent tabs

Codex and Claude each get a tab and a separate project conversation. Opening
another file does not replace either conversation or silently send that file
to the agent. Attach the current file when you want it included.

![The Codex and Claude tabs answering the same question about a scene in separate conversations, with each answer staying in its own tab](https://raw.githubusercontent.com/ourarash/proseview/main/docs/images/demo-agents.gif)

### Search

`Mod-K` from anywhere. File paths, scene metadata, TODOs, notes, and prose,
grouped by kind.

![The search palette open over a scene, showing file, scene, and prose matches grouped by kind](https://raw.githubusercontent.com/ourarash/proseview/main/docs/images/demo-search.gif)

### Timeline

Chapter proportion and words per scene, your storylines as lanes, and reading
order against the order events actually happen — so a scene read far from where
it happens is visible instead of inferred. Hover any scene for its card.

![The Timeline tab showing chapter proportion, words per scene, three storyline lanes, and reading order against story order, with a scene card on hover](https://raw.githubusercontent.com/ourarash/proseview/main/docs/images/demo-timeline.gif)

### Character presence

![The Analysis tab's character presence timeline, tracking how often each character is mentioned across all twelve chapters](https://raw.githubusercontent.com/ourarash/proseview/main/docs/images/analytics.png)

> Clips use the bundled demo manuscript: *Alice's Adventures in Wonderland*
> (public domain), split into 39 scenes across 12 chapters with story fields
> filled in. See [fixtures/demo-book](fixtures/demo-book/README.md).

## ✨ Features

- 📊 **Dashboard.** Word count, chapter pacing, lexical health, sentence
  rhythm, character presence, setting stickiness, and a sortable scene
  table. [What the numbers mean →](docs/analytics.md)
- 📖 **Reading view and WYSIWYG editor.** The same typographic page, with
  `Edit` toggled on. `Mod-S` saves; a conflict guard checks the file mtime
  so a change made in your own editor is never silently overwritten.
- 🎨 **Editorial highlights.** Nine prose passes over any scene, switched
  on from the Analysis panel with a match count each: repetition, passive
  voice, filter verbs, crutch words, hyperbole, lyrical reach, sensory
  density, comedy beats, first-person rate.
- 🗒️ **Inline TODOs and Notes.** Select a passage, drop a `TODO` or a
  tagged `NOTE`, and it lands in the file as a Markdown comment. They stay
  in the file, so they survive in git and turn up in grep.
- 🕰️ **Timeline.** The shape of the book, storylines as lanes, and reading
  order against the order events happen.
- 🤖 **Two agents, side by side.** Codex and Claude each get a dock tab and
  their own project conversation. Both run at once; switching tabs or opening
  another file never interrupts either one. [How the AI features work →](docs/ai.md)
- 🔎 **Repository search.** `Mod-K` from anywhere — paths, metadata, TODOs,
  notes, and prose, grouped by kind.
- 📁 **File browser.** Create empty Markdown files and folders, rename them
  inline, or move them to Proseview Trash from the row menu or right-click.
- 🕹️ **File history.** Versions of a scene, restored through a diff you
  can read first.
- 📦 **EPUB export.** `proseview export` compiles your scenes in the order
  the dashboard counts them. [Export options →](docs/export.md)
- 🔁 **Live reload, deep links, themes.** Save in your editor and the page
  follows. Every scene has a URL. Six themes, seven fonts.
- 🧪 **Tested.** A unit suite plus a browser tier that drives the real UI.
  One behaviour suite runs against both agents, so a feature cannot
  silently work on one tab and not the other.

### 🤖 Optional AI

Entirely optional, and it never runs on its own. Proseview has no model of its
own and no API key of yours — it drives the agent CLIs already installed on
your machine, under your login: Codex and Claude, one Discuss tab each.

Agent sessions are read-only. Anything beyond reading — a shell command, a file
write — stops at an approval you have to grant, and raw model reasoning is
never forwarded to the browser.

Continuity and canon questions come back with citations, and suggested edits
arrive as proposals you accept or reject before anything is written.

Everything else works without an agent installed. [Details →](docs/ai.md)

## 🚀 Quick start

**Requirements:** Python 3.11+ on macOS, Linux, or Windows.

```bash
pipx install proseview
proseview --root /path/to/your/novel
```

A browser tab opens at `http://localhost:7842`. Press Ctrl-C to stop.

Point it at any folder of Markdown. If there is no `manuscript/` directory the
whole folder is the manuscript, so an Obsidian vault or a flat pile of chapter
files works with no configuration.

```text
my-novel/
├── manuscript/
│   ├── ch01/
│   │   ├── 01-opening.md
│   │   └── 02-meeting.md
│   └── ch02/
│       └── 01-aftermath.md
└── .proseview.yaml          # optional
```

Frontmatter is optional. Proseview reads what is there and falls back on what
is not. [Layout and the full frontmatter contract →](docs/manuscript.md)

## ⚙️ Configuration

Proseview runs without a config file. A `.proseview.yaml` at the repo root can
change where things live, set a word-count goal, pick which agent tab opens
first, or point the Timeline at your own frontmatter keys. `proseview init`
writes a starter file.

```yaml
manuscript_path: manuscript/
target_words: 80000
discuss:
  agent: codex
```

[Every key, with defaults →](docs/configuration.md)

## 📚 Documentation

- [Manuscript layout and frontmatter](docs/manuscript.md)
- [Configuration reference](docs/configuration.md)
- [Working with AI](docs/ai.md)
- [The analytics](docs/analytics.md)
- [EPUB export](docs/export.md)

## 🛣️ Status

Proseview is alpha. Everything described above works today. Still unfinished:

- 🚧 Skills on the Claude tab. Codex discovers them today; Claude's picker
  is still empty.
- 🚧 Frontmatter editor (status, where, todos) inside the scene viewer
  so you don't need to drop into your text editor for routine fields.

See [plans/roadmap.md](plans/roadmap.md) for the full punch list.

## 🧪 Development

```bash
pip install -e ".[dev]"
pytest
```

That runs 642 unit tests plus an HTTP end-to-end tier that boots a real
`proseview` subprocess and drives every endpoint — saves and the conflict
guard, TODOs and notes, the AI proposal bridge through the actual CLI, and
live reload over SSE — asserting on bytes written to disk.
Discuss integration tests use a deterministic fake app-server and isolated
home/state directories; they never contact Codex, the network, or your profile.
~15 seconds, no extra dependencies.

A browser tier drives the real UI in Chromium (editor round-trip fidelity,
the selection menu, highlight passes, deep links, Discuss
streaming/approvals/dock behavior, and applying an AI proposal end to end). It's opt-in:

```bash
pip install -e ".[e2e]"
python -m playwright install chromium
pytest -m e2e_browser
```

Both tiers work on a throwaway copy of `fixtures/demo-repo`. See
[CONTRIBUTING.md](CONTRIBUTING.md) for details.

## 📜 License

MIT. See [LICENSE](LICENSE).
