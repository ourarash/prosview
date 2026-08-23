# Security

## What Proseview does on your machine

Proseview is a local tool, not a sandbox. When you run it, it:

- **serves HTTP on `localhost`** (default port 7842) for as long as it runs;
- **reads and writes files in the repository you point it at** — saving a
  scene, adding a TODO, or accepting an AI edit rewrites your Markdown in place;
- **starts a local agent process** on demand if you use Discuss — a
  `codex app-server`, or Claude through `claude-agent-sdk` — running as your
  user, from the repository root, on your existing login, model, and history.

There is no sandbox, no container, and no privilege separation. Anything the
agent can do, you can do — because it is running as you.

Proseview sends nothing to a remote server on its own. Network traffic happens
only when *you* invoke an AI agent, and then it goes to that agent's provider
under that agent's own credentials.

## How local requests are authenticated

The listening socket is bound to `localhost`, but that alone is not a security
boundary — any web page in your browser can send requests to `localhost`, and a
DNS-rebinding page can do it with its own origin attached.

So every state-changing request must additionally:

1. address the server as loopback (`Host`, and `Origin` when present), and
2. carry the `X-Proseview-Session` header matching a token generated fresh on
   each run.

The browser gets that token embedded in the page it loaded from Proseview. The
CLI (`proseview propose` and friends) reads it from `.proseview/server.json`,
which is written with `0600` permissions. A hostile web page can do neither: it
cannot read the page it did not load, and it cannot read your filesystem.
Requiring a custom header also forces a CORS preflight that Proseview never
answers, so cross-origin mutations are rejected by the browser before they
arrive.

Read-only endpoints are not token-gated — a browser navigating to the dashboard
cannot set a custom header — but they are still restricted to loopback `Host`.

Absolute paths supplied by the page are checked for containment inside the
served repository, and symlinked paths are refused.

## Running it safely

- **Do not expose the port.** Do not put Proseview behind a tunnel, reverse
  proxy, or `--port` binding reachable from another machine. It assumes every
  caller is you.
- **Point it at repositories you trust.** Scene text and repository files are
  rendered in your browser, and are handed to AI agents as context.
- **Treat an approval as a real one.** Agents launched from Proseview run with
  your permissions, and anything you approve can modify your files.
- **On a shared machine, remember other local users can reach `localhost`.**
  The session token is what stops them; it lives in `.proseview/server.json`, so
  keep that file's `0600` permissions.

## Supported versions

Proseview is alpha and pre-1.0. Only the latest commit on `main` receives
security fixes.

## Reporting a vulnerability

Please report privately rather than opening a public issue:

- Use [GitHub's private vulnerability reporting](https://github.com/ourarash/proseview/security/advisories/new), or
- email the maintainer listed in `pyproject.toml`.

Include what you did, what happened, and what you expected. A proof-of-concept
request or a short script helps a great deal.

Expect an acknowledgement within a week. Because this is a single-maintainer
alpha project, please allow reasonable time for a fix before public disclosure.
