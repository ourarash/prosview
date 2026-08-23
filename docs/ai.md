# Working with AI

Every AI feature is opt-in and none of it runs on its own. Proseview has
no model of its own and no API key of yours: it drives the agent CLIs
already installed on your machine, under your login.

[← back to the README](../README.md)

Four places where AI shows up, all opt-in:

1. **Selection menu.** Highlight any text in a scene. The pill that
   appears includes `Add TODO`, `Add Note`, and (if the corresponding
   tools are installed locally) `Run in Codex` and `Skills`. Skills are
   reusable prompts you keep in `.proseview/skills/<name>/SKILL.md`; they show up
   automatically in the menu. Skills are discovered by Codex today — the
   Claude tab's picker is still empty.
2. **Agent menu.** From the scene header, launch a conversation with
   Codex, Claude, or Gemini scoped to that file. The conversation runs
   in the in-browser terminal so you can keep reading the prose
   underneath while the agent works.
3. **Discuss, on either agent.** The side dock has a **Codex** tab and a
   **Claude** tab. Each has a separate project conversation, with its own queue,
   history, and approvals — ask one, switch agents or open another file, and
   both keep working. `discuss.agent` decides which tab opens first; both tabs
   are always there, and one that cannot start explains why rather than
   disappearing.

   Ordinary questions start without any file contents attached. Choose
   **Attach current** to include the file on screen, or press `@` to attach
   other files and folders. An attached current-file chip stays on that file
   when you navigate, until you remove it; navigation alone never changes what
   is sent. Selecting prose explicitly attaches that selection to its source
   scene, and selection tasks and their follow-ups stay anchored there. Tool
   and file actions wait on approvals you can see.

   A status strip between the conversation and the composer says what the
   agent is doing: starting, working (with the current step and a running
   clock), waiting on an approval, or finished, with how long the answer took.
   **Details** opens the turn's trail — the files it read, the searches it ran,
   the approvals you granted — in the order they happened. A turn that has
   produced nothing for a minute says so rather than spinning. The **Stop**
   button lives in that strip.

   The same state reaches you when you are not looking at the dock: the agent's
   tab carries a pulsing dot while it works and a steady amber one when it
   needs a decision, and the browser tab title says which agent is working or
   waiting whenever the dock is closed or the window is in the background.

   Every button is a shortcut to a skill. Proseview copies its defaults into
   `.proseview/skills/` the first time it runs in a repository, and from then on the file
   is yours: edit `.proseview/skills/quick_critique/SKILL.md` and Quick critique says what
   you wrote. Delete one and it stays deleted. That file also owns the sentence
   the button shows about itself — the `description:` line in its frontmatter,
   which is what the cards below explain themselves with; reopen the dock and
   the card reads back what you wrote. The wording that ships lives in the same
   format under `proseview/skills/` in the Proseview repository, so there is one
   place to read and one place to change.

   Reading passes — critiques from the selection pill, and the scene passes
   below — are ordinary questions with ordinary answers. Nothing they return is
   written to your manuscript, so none of them produces a card, a structured
   result or anything that can go stale: the agent replies in the conversation
   and the passage it read stays attached, so a follow-up needs no reselecting.
   Rewrites are different, because their answer is prose that gets applied to
   your draft; those keep their card and their exact target.

   Opening the dock on a scene offers passes over that scene, one click and no
   typing. **Quick critique** returns evidence-linked findings, each quoting
   the line it came from and suggesting a fix. **Style and consistency** works
   differently: Proseview's own analysis finds the passive constructions, filter verbs,
   repeated words and point-of-view slips first, and the agent only decides
   which of them weaken the scene and which are the narrator's voice. It can
   neither miss what the analysis found nor report anything the analysis did
   not, and on prose with nothing mechanical to flag it says so without
   starting a turn at all.

   Discuss also has evidence-first continuity actions. **Trace a canon
   change** scans the configured manuscript and repository-tab folders,
   separates direct contradictions from ambiguous and likely intentional
   references, and cites the exact file, line, and passage for every finding.
   Nothing is edited during the scan. You can preserve an intentional
   exception, send one scene finding at a time through the existing proposal
   review, then rescan to verify the result. **Check this scene's continuity**
   runs the same guarded workflow with the active document as its focus.

   Repository continuity scans are bounded to 200 supported text files, 4 MB
   of context, and 50 displayed findings. The impact report shows the actual
   files and bytes scanned, and warns when the finding limit is reached. These
   impact reports and their decisions are session-only in the current MVP.

   Under the hood each tab starts its agent on demand — a local
   `codex app-server`, or Claude through `claude-agent-sdk` — and uses the
   login, model, and history you already have. Neither sees the other's
   conversation, and a failure on one tab leaves the other running.

   Proseview stores one bounded project history per agent in your state
   directory, including which documents each thread discussed, and discards raw
   reasoning: only progress summaries reach the browser, never unedited model
   thinking. `History` lets you reopen, rename, export, or remove a previous
   project conversation. `New conversation` starts a blank discussion while
   keeping the previous one available there.

   The Claude tab needs `claude-agent-sdk` installed alongside the Claude Code
   CLI:

   ```bash
   pip install claude-agent-sdk
   ```

   Its session runs with a fixed read-only tool allowlist and without loading
   your personal Claude settings, so nothing outside Proseview's own scope can
   widen what the agent may do. Anything beyond reading — a shell command, a
   file write — stops at an approval you have to grant.

   When selected prose is attached, the composer shows up to three **Presets**
   from `discuss.selection_presets` and your browser-local favorites. Favorites
   come first, duplicates are removed, and additional presets are available
   under **More**. Recent instructions are kept out of the inline preset row;
   open **More** to reuse or star one as a personal preset.
4. **TODOs as Markdown.** Every TODO and Note is a plain
   `<!-- TODO: ... -->` or `<!-- NOTE[tag]: ... -->` comment in the
   scene file. Your AI assistant can see them through the file, your
   repo can track them through git, and you can grep them.
