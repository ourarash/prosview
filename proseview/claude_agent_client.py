"""Claude Agent SDK transport for Prosview's Discuss feature.

This is the sibling of :mod:`proseview.codex_app_server`. It presents the same
synchronous surface that :class:`~proseview.discuss.DiscussManager` already
drives — ``start``, ``request``, ``respond``, ``close`` — so the manager needs
no branching on which agent is in use, and it emits its own notification shape
that :func:`sanitize_claude_message` translates into Prosview's event
vocabulary.

Three things differ structurally from the Codex transport, and each is the
reason for a piece of machinery below:

* **One session per conversation.** ``codex app-server`` multiplexes many
  threads over one process; a ``ClaudeSDKClient`` is a single session. This
  class therefore owns a pool keyed by thread id, with bounded size and
  least-recently-used eviction.
* **Async underneath, threads above.** The SDK is asyncio; ``DiscussManager``
  is threads and queues. One event loop runs on a private thread and every
  public method bridges to it, while inbound notifications are handed to a
  dispatcher thread so manager callbacks can safely re-enter this class.
* **Approvals are hooks, not callbacks.** ``can_use_tool`` is not a reliable
  gate: an ``allowed_tools``/``tools`` entry that permits a whole tool
  auto-approves it before the callback runs, and allow rules in the user's
  settings files shadow it invisibly. A ``PreToolUse`` hook is consulted
  either way, so the writer's approval decision hangs there. See
  ``plans/claude-agent-sdk-spike.md``.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

# Approval requests reuse Codex's method names on purpose: they name the same
# domain event, and DiscussManager's approval UI already understands them.
APPROVAL_METHODS = {
    "command": "item/commandExecution/requestApproval",
    "fileChange": "item/fileChange/requestApproval",
}
APPROVAL_DECISIONS = ["accept", "acceptForSession", "decline", "cancel"]

# Tools Prosview allows without asking. Everything else reaches the writer.
READ_ONLY_TOOLS = ["Read", "Glob", "Grep"]

#: Tools that change the manuscript. They are offered only on a turn the writer
#: asked to change something, and every one of them still stops at the
#: PreToolUse gate below, so Claude asks before it writes.
WRITE_TOOLS = ["Write", "Edit", "NotebookEdit"]

#: SDK-internal plumbing, not writer-facing actions. ``StructuredOutput`` is how
#: the CLI delivers a JSON result when an output schema is set, so gating it
#: would stall every selection action behind an approval nobody can explain.
#: These are never shown as activity and never reach the approval UI.
INTERNAL_TOOLS = frozenset({"StructuredOutput"})

MAX_SESSIONS = 8
APPROVAL_TIMEOUT = 600.0
DEFAULT_REQUEST_TIMEOUT = 15.0
TURN_START_TIMEOUT = 60.0

#: Reasoning effort the SDK accepts, weakest first. Unlike Codex, every model
#: takes the same ladder, so the picker's effort row never changes shape.
EFFORT_LADDER = ("low", "medium", "high", "xhigh", "max")

#: Claude Code has no ``model/list`` to ask, so the roster is the set of
#: aliases its CLI resolves. Aliases rather than dated ids on purpose: an alias
#: keeps meaning the current model, which is what a writer picking "Opus"
#: means, and a pinned dated id would quietly rot.
MODEL_CATALOG = (
    {
        "id": "opus",
        "displayName": "Opus 5",
        "description": "Most capable. Long continuity sweeps and structural reads.",
        "defaultReasoningEffort": "high",
    },
    {
        "id": "sonnet",
        "displayName": "Sonnet 5",
        "description": "Balanced speed and depth. A good standing choice for line work.",
        "defaultReasoningEffort": "medium",
    },
    {
        "id": "fable",
        "displayName": "Fable 5",
        "description": "Tuned for prose and voice.",
        "defaultReasoningEffort": "medium",
    },
    {
        "id": "haiku",
        "displayName": "Haiku 4.5",
        "description": "Fastest and cheapest. Quick lookups and small rewrites.",
        "defaultReasoningEffort": "low",
    },
)

EFFORT_DESCRIPTIONS = {
    "low": "Fast answers with little deliberation",
    "medium": "Everyday balance of speed and depth",
    "high": "Deeper reasoning for tangled questions",
    "xhigh": "Extended reasoning for hard problems",
    "max": "Maximum reasoning depth",
}


class ClaudeError(RuntimeError):
    pass


class ClaudeUnavailableError(ClaudeError):
    pass


class ClaudeAuthError(ClaudeError):
    pass


class ClaudeProtocolError(ClaudeError):
    pass


class ClaudeRequestError(ClaudeError):
    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def _bounded(value: Any, limit: int = 16_384) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "\n… output truncated by Prosview …"


def sanitize_claude_message(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Translate one Claude transport notification into Prosview events.

    The sibling of :func:`proseview.discuss.sanitize_agent_message`, emitting
    exactly the same vocabulary:

        progress.delta, response.delta, response.completed, plan.updated,
        turn.started, turn.completed, activity.updated, warning, error

    Raw thinking is dropped rather than forwarded, matching how the Codex
    translator refuses ``item/reasoning/textDelta``. Prosview shows progress,
    never the model's unedited reasoning.
    """
    method = str(message.get("method") or "")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    common = {"thread_id": params.get("threadId"), "turn_id": params.get("turnId")}

    if method == "assistant/thinkingDelta":
        return []
    if method == "assistant/progress":
        return [{"type": "progress.delta", **common, "text": _bounded(params.get("text"))}]
    if method == "assistant/textDelta":
        return [{
            "type": "response.delta",
            **common,
            "item_id": params.get("itemId"),
            "text": _bounded(params.get("delta")),
        }]
    if method == "assistant/message":
        return [{
            "type": "response.completed",
            **common,
            "item_id": params.get("itemId"),
            "phase": params.get("phase") or "final_answer",
            "text": _bounded(params.get("text")),
        }]
    if method in {"turn/started", "turn/completed"}:
        return [{
            "type": "turn.started" if method.endswith("started") else "turn.completed",
            "thread_id": params.get("threadId"),
            "turn_id": params.get("turnId"),
            "status": params.get("status"),
            "error": _bounded(params.get("error")),
        }]
    if method in {"tool/started", "tool/completed"}:
        tool = str(params.get("tool") or "")
        activity: dict[str, Any] = {
            "id": params.get("itemId"),
            "status": params.get("status") or ("inProgress" if method.endswith("started") else "completed"),
        }
        if not tool:
            # A completion knows the outcome, not the tool. Guessing a kind from
            # an empty name would relabel a finished command as a dynamic tool
            # call and lose the command with it.
            activity["output"] = _bounded(params.get("output"))
            return [{"type": "activity.updated", **common, "activity": activity}]
        activity["kind"] = _activity_kind(tool)
        if activity["kind"] == "commandExecution":
            activity.update(
                command=_bounded(params.get("command"), 4000),
                cwd=_bounded(params.get("cwd"), 2000),
                output=_bounded(params.get("output")),
            )
        elif activity["kind"] == "fileChange":
            activity["changes"] = [
                {"path": _bounded(row.get("path"), 2000), "kind": row.get("kind")}
                for row in (params.get("changes") or [])
                if isinstance(row, dict)
            ]
        elif activity["kind"] == "webSearch":
            activity["query"] = _bounded(params.get("query"), 4000)
        else:
            activity.update(tool=_bounded(tool, 1000), server=_bounded(params.get("server"), 1000))
        return [{"type": "activity.updated", **common, "activity": activity}]
    if method == "warning":
        return [{"type": "warning", **common, "message": _bounded(params.get("message"))}]
    if method == "error":
        return [{"type": "error", **common, "message": _bounded(params.get("message"))}]
    return []


def _activity_kind(tool: str) -> str:
    if tool == "Bash":
        return "commandExecution"
    if tool in {"Write", "Edit", "NotebookEdit"}:
        return "fileChange"
    if tool in {"WebSearch", "WebFetch"}:
        return "webSearch"
    if tool.startswith("mcp__"):
        return "mcpToolCall"
    return "dynamicToolCall"


class _Session:
    """One Claude session, standing in for one Codex thread."""

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        self.client: Any = None
        self.session_id: str = ""
        self.turn_id: str = ""
        self.drain: asyncio.Task[None] | None = None
        self.last_used = time.monotonic()
        self.busy = False
        # Captured at thread/start; the developer instructions live here rather
        # than on each turn, matching where the manager sends them.
        self.thread_params: dict[str, Any] = {}
        #: True once a turn has run, so later turns resume the session rather
        #: than trying to name one that already exists.
        self.started = False
        #: What this session's live client was actually connected with. The
        #: model can be changed on a running session; the effort cannot, so a
        #: change to it has to be noticed here and reconnected.
        self.model = ""
        self.effort = ""
        #: Set per turn. When an output schema is in play the JSON — not the
        #: model's prose — is the final answer.
        self.expects_structured = False
        #: Tool-use ids belonging to internal plumbing, so their results are
        #: suppressed too. A result block carries no tool name of its own.
        self.internal_tool_ids: set[str] = set()


class ClaudeAgentClient:
    """Drive Claude Code through ``claude-agent-sdk`` behind Codex's interface."""

    #: This transport's own translator into Prosview's event vocabulary.
    translate = staticmethod(sanitize_claude_message)

    def __init__(
        self,
        *,
        cwd: Path | str,
        on_message: Callable[[dict[str, Any]], None] | None = None,
        on_failure: Callable[[BaseException], None] | None = None,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        max_sessions: int = MAX_SESSIONS,
        options_factory: Callable[..., Any] | None = None,
        client_factory: Callable[[Any], Any] | None = None,
        session_reader: Callable[[str, str], Any] | None = None,
    ) -> None:
        self.cwd = str(Path(cwd).resolve())
        self.on_message = on_message or (lambda _message: None)
        self.on_failure = on_failure or (lambda _error: None)
        self.request_timeout = request_timeout
        self.max_sessions = max_sessions
        self.user_agent = ""
        self.capabilities: dict[str, Any] = {}
        # Injection seams so tests can drive the transport without a live CLI.
        self._options_factory = options_factory
        self._client_factory = client_factory
        self._session_reader = session_reader

        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._dispatch_thread: threading.Thread | None = None
        self._outbound: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._sessions: dict[str, _Session] = {}
        self._approvals: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._session_grants: dict[str, set[str]] = {}
        self._lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._fatal: BaseException | None = None
        self._closed = False

    # --- lifecycle ---------------------------------------------------------

    @property
    def alive(self) -> bool:
        return self._loop is not None and not self._closed and self._fatal is None

    def start(self) -> None:
        with self._start_lock:
            if self.alive:
                return
            if self._closed:
                raise ClaudeUnavailableError("Claude client is closed")
            self._fatal = None
            ready = threading.Event()

            def run_loop() -> None:
                loop = asyncio.new_event_loop()
                self._loop = loop
                asyncio.set_event_loop(loop)
                ready.set()
                try:
                    loop.run_forever()
                finally:
                    loop.close()

            self._loop_thread = threading.Thread(target=run_loop, name="proseview-claude-loop", daemon=True)
            self._loop_thread.start()
            if not ready.wait(timeout=10):
                raise ClaudeUnavailableError("Claude client event loop failed to start")

            self._dispatch_thread = threading.Thread(
                target=self._dispatch_loop, name="proseview-claude-dispatch", daemon=True
            )
            self._dispatch_thread.start()

    def _dispatch_loop(self) -> None:
        """Deliver notifications on a thread of their own.

        Manager callbacks re-enter this class (an approval decision calls
        ``respond``), so they must never run on the event loop thread.
        """
        while True:
            message = self._outbound.get()
            if message is None:
                return
            try:
                self.on_message(message)
            except Exception:
                # A domain consumer must not be able to kill the transport.
                continue

    def _emit(self, method: str, params: dict[str, Any]) -> None:
        self._outbound.put({"method": method, "params": params})

    def _submit(self, coro: Any, timeout: float) -> Any:
        loop = self._loop
        if loop is None or self._closed:
            if self._fatal is not None:
                raise self._fatal
            raise ClaudeUnavailableError("Claude client is not running")
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout=timeout)
        except TimeoutError as exc:
            future.cancel()
            raise ClaudeProtocolError("Claude request timed out") from exc

    def _fail(self, error: BaseException) -> None:
        first = self._fatal is None
        self._fatal = error
        if first:
            try:
                self.on_failure(error)
            except Exception:
                pass

    # --- capability gating -------------------------------------------------

    def inspect_capabilities(self) -> dict[str, Any]:
        """Refuse an SDK or CLI that cannot host Discuss.

        Claude Code has no schema-generation equivalent to Codex's, so this
        checks that the SDK exposes the surfaces Discuss depends on. Failing at
        startup is the point: the alternative is failing mid-question with the
        writer's request already in flight.
        """
        try:
            import claude_agent_sdk as sdk
        except ImportError as exc:
            raise ClaudeUnavailableError(
                "claude-agent-sdk is not installed; install it to use Claude for Discuss"
            ) from exc
        if not shutil.which("claude"):
            raise ClaudeUnavailableError("Claude Code CLI is not installed or is not on PATH")

        required = {
            "session pooling": ("ClaudeSDKClient",),
            "structured output": ("ClaudeAgentOptions",),
            "approval hooks": ("HookMatcher",),
            "history read-back": ("get_session_messages", "list_sessions"),
        }
        missing = [
            label for label, names in required.items()
            if not all(hasattr(sdk, name) for name in names)
        ]
        if missing:
            raise ClaudeProtocolError(
                "claude-agent-sdk is unsupported (missing " + ", ".join(missing) + ")"
            )
        if "output_format" not in getattr(sdk.ClaudeAgentOptions, "__annotations__", {}):
            raise ClaudeProtocolError(
                "claude-agent-sdk is unsupported (missing structured output support)"
            )

        self.user_agent = f"claude-agent-sdk/{getattr(sdk, '__version__', 'unknown')}"
        self.capabilities = {
            "schema_generation": False,
            # Thinking is summarised into progress rather than forwarded raw.
            "reasoning_summary": True,
            "restricted_read_access": True,
            "stable_discuss_protocol": True,
            "approval_decisions": {
                "command": list(APPROVAL_DECISIONS),
                "network": list(APPROVAL_DECISIONS),
                "fileChange": list(APPROVAL_DECISIONS),
                # Codex's whole-root permission grant has no Claude analogue.
                "permissions": [],
            },
        }
        return dict(self.capabilities)

    def probe_capabilities(self) -> dict[str, Any]:
        """Nothing further to probe: inspection is authoritative for this SDK."""
        self.capabilities.setdefault("stable_discuss_protocol", True)
        return dict(self.capabilities)

    # --- request dispatch --------------------------------------------------

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if not self.alive:
            if self._fatal is not None:
                raise self._fatal
            raise ClaudeUnavailableError("Claude client is not running")
        params = params or {}
        handlers = {
            "thread/start": self._handle_thread_start,
            "turn/start": self._handle_turn_start,
            "turn/interrupt": self._handle_turn_interrupt,
            "thread/read": self._handle_thread_read,
            "skills/list": self._handle_skills_list,
            "model/list": self._handle_model_list,
        }
        handler = handlers.get(method)
        if handler is None:
            raise ClaudeRequestError(f"method not found: {method}", code=-32601)
        budget = timeout if timeout is not None else self.request_timeout
        if method == "turn/start":
            budget = max(budget, TURN_START_TIMEOUT)
        return handler(params, budget)

    def _handle_thread_start(self, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        # The SDK lets us name the session, so the thread id Prosview persists
        # *is* the session id. Without that the id means nothing after a
        # restart and every reopened conversation looks lost.
        thread_id = str(uuid.uuid4())
        session = _Session(thread_id)
        session.thread_params = dict(params)
        with self._lock:
            self._sessions[thread_id] = session
            self._evict_idle_locked()
        return {"thread": {"id": thread_id}}

    def _handle_turn_start(self, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        thread_id = str(params.get("threadId") or "")
        with self._lock:
            session = self._sessions.get(thread_id)
        if session is None:
            raise ClaudeRequestError("thread not found", code=-32004)

        prompt_parts = [
            str(item.get("text") or "")
            for item in (params.get("input") or [])
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        prompt = "\n\n".join(part for part in prompt_parts if part)
        if not prompt:
            raise ClaudeRequestError("turn input is empty", code=-32602)

        turn_id = uuid.uuid4().hex
        self._submit(
            self._start_turn(session, turn_id, prompt, params),
            timeout=timeout,
        )
        return {"turn": {"id": turn_id}}

    def _handle_turn_interrupt(self, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        thread_id = str(params.get("threadId") or "")
        with self._lock:
            session = self._sessions.get(thread_id)
        if session is None:
            raise ClaudeRequestError("thread not found", code=-32004)
        self._submit(self._interrupt(session), timeout=timeout)
        return {}

    def _handle_thread_read(self, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        """Read a conversation, adopting one this process has never seen.

        Sessions live in the SDK's own store, so a thread started before a
        restart is still readable. Adopting it here is what lets the history
        pane reopen a conversation instead of reporting it lost.
        """
        thread_id = str(params.get("threadId") or "")
        if not thread_id:
            raise ClaudeRequestError("thread not found", code=-32004)
        result = self._submit(self._read_session(thread_id), timeout=timeout)
        if not result.get("turns"):
            raise ClaudeRequestError("thread not found", code=-32004)
        with self._lock:
            session = self._sessions.get(thread_id)
            if session is None:
                session = _Session(thread_id)
                session.thread_params = dict(params)
                self._sessions[thread_id] = session
                self._evict_idle_locked()
            # It exists in the store, so the next turn resumes rather than
            # trying to claim an id that is already taken.
            session.started = True
        return result

    def _handle_model_list(self, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        """Publish the roster in the shape Codex's ``model/list`` uses.

        Answering the default here as well as the catalog is the difference
        between the two transports: Codex keeps its resolved configuration
        behind a second call, while this one already knows what an unpinned
        turn will run as.
        """
        defaults = self.user_model_defaults()
        rows = [
            {
                **row,
                "supportedReasoningEfforts": [
                    {"reasoningEffort": effort, "description": EFFORT_DESCRIPTIONS[effort]}
                    for effort in EFFORT_LADDER
                ],
                "isDefault": row["id"] == (defaults.get("model") or ""),
            }
            for row in MODEL_CATALOG
        ]
        return {"data": rows, "default": defaults}

    def user_model_defaults(self) -> dict[str, str]:
        """Read the writer's configured model and effort, and nothing else.

        Prosview starts Claude sessions with ``setting_sources`` unset, so this
        file is deliberately not loaded: it can carry hooks and permission
        allow-rules that would shadow the approval gate. Two scalar keys carry
        none of that, and ignoring them would mean a writer who configured Opus
        silently gets whatever the SDK defaults to instead.
        """
        path = Path(
            os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")
        ) / "settings.json"
        try:
            shown = "~/" + str(path.relative_to(Path.home()))
        except ValueError:
            shown = str(path)
        source = f"Claude Code settings ({shown})"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {"model": "", "effort": "", "source": source}
        if not isinstance(data, dict):
            return {"model": "", "effort": "", "source": source}
        model = str(data.get("model") or "").strip()
        if len(model) > 120 or not re.fullmatch(r"[A-Za-z0-9._:@/+-]*", model):
            model = ""
        effort = str(data.get("effortLevel") or "").strip().lower()
        if effort not in EFFORT_LADDER:
            effort = ""
        return {"model": model, "effort": effort, "source": source}

    def _handle_skills_list(self, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        # Skills selection lands with the writer-facing milestone; advertising
        # an empty list keeps the picker honest rather than showing stale data.
        return {"skills": []}

    # --- session pool ------------------------------------------------------

    def _evict_idle_locked(self) -> None:
        if len(self._sessions) <= self.max_sessions:
            return
        idle = sorted(
            (s for s in self._sessions.values() if not s.busy),
            key=lambda s: s.last_used,
        )
        while len(self._sessions) > self.max_sessions and idle:
            victim = idle.pop(0)
            self._sessions.pop(victim.thread_id, None)
            if victim.client is not None and self._loop is not None:
                asyncio.run_coroutine_threadsafe(self._disconnect(victim), self._loop)

    async def _disconnect(self, session: _Session) -> None:
        client = session.client
        session.client = None
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception:
            pass

    def turn_model(self, params: dict[str, Any]) -> dict[str, str]:
        """Resolve what one turn should run as.

        A turn that names nothing falls back to the writer's own configuration
        rather than to the SDK's built-in default, so "Default" in the picker
        means what they configured.
        """
        defaults = self.user_model_defaults()
        model = str(params.get("model") or "").strip() or defaults["model"]
        effort = str(params.get("effort") or "").strip().lower() or defaults["effort"]
        return {"model": model, "effort": effort if effort in EFFORT_LADDER else ""}

    def _supports_effort(self) -> bool:
        """Whether the installed SDK takes an effort level.

        An older SDK rejects the keyword outright, and losing the effort
        setting is a far better outcome than failing every turn.
        """
        if self._options_factory is not None:
            return True
        try:
            from claude_agent_sdk import ClaudeAgentOptions
        except ImportError:  # pragma: no cover - inspection already refused this
            return False
        return "effort" in getattr(ClaudeAgentOptions, "__annotations__", {})

    def _build_options(self, params: dict[str, Any], session: _Session) -> Any:
        async def pre_tool_use(input_data: dict[str, Any], tool_use_id: Any, context: Any):
            return await self._gate_tool(session, input_data)

        options: dict[str, Any] = {
            "cwd": self.cwd,
            # Load-bearing: without this the writer's own CLAUDE.md, hooks, and
            # settings enter a session Prosview believes it has bounded — and
            # settings allow-rules can shadow the approval gate entirely.
            "setting_sources": None,
            "strict_mcp_config": True,
            "mcp_servers": {},
            "permission_mode": "default",
            "tools": list(READ_ONLY_TOOLS) + (
                list(WRITE_TOOLS) if params.get("mayWrite") else []
            ),
            "include_partial_messages": True,
        }
        selection = self.turn_model(params)
        if selection["model"]:
            options["model"] = selection["model"]
        if selection["effort"] and self._supports_effort():
            options["effort"] = selection["effort"]
        instructions = params.get("developerInstructions") or session.thread_params.get(
            "developerInstructions"
        )
        if instructions:
            options["system_prompt"] = str(instructions)
        schema = params.get("outputSchema")
        if schema:
            options["output_format"] = {"type": "json_schema", "schema": schema}
        if session.started:
            options["resume"] = session.thread_id
        else:
            options["session_id"] = session.thread_id
        if self._options_factory is not None:
            return self._options_factory(hooks={"PreToolUse": [pre_tool_use]}, **options)

        from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

        options["hooks"] = {"PreToolUse": [HookMatcher(matcher=None, hooks=[pre_tool_use])]}
        return ClaudeAgentOptions(**options)

    async def _ensure_session_client(self, session: _Session, params: dict[str, Any]) -> Any:
        if session.client is not None:
            return session.client
        options = self._build_options(params, session)
        if self._client_factory is not None:
            client = self._client_factory(options)
        else:
            from claude_agent_sdk import ClaudeSDKClient

            client = ClaudeSDKClient(options=options)
        await client.connect()
        session.client = client
        return client

    # --- turn execution ----------------------------------------------------

    async def _apply_model_change(self, session: _Session, params: dict[str, Any]) -> None:
        """Bring a live session in line with this turn's model and effort.

        The two settings are not equally cheap. ``set_model`` changes a running
        session in place, so switching model keeps the conversation's context.
        Effort is fixed when the session connects, so a change to it has to
        reconnect -- which resumes the same session id, and so is invisible
        apart from the reconnect itself.
        """
        selection = self.turn_model(params)
        if session.client is None:
            session.model = selection["model"]
            session.effort = selection["effort"]
            return
        if selection["effort"] != session.effort:
            await self._disconnect(session)
            session.model = selection["model"]
            session.effort = selection["effort"]
            return
        if selection["model"] != session.model:
            try:
                await session.client.set_model(selection["model"] or None)
            except Exception as exc:  # noqa: BLE001
                # An SDK without live switching must not cost the writer their
                # conversation; reconnecting applies the choice the slow way.
                self._emit("error", {
                    "threadId": session.thread_id,
                    "message": _bounded(f"Reconnecting to change model: {exc}", 500),
                })
                await self._disconnect(session)
            session.model = selection["model"]

    async def _start_turn(
        self, session: _Session, turn_id: str, prompt: str, params: dict[str, Any]
    ) -> None:
        await self._apply_model_change(session, params)
        client = await self._ensure_session_client(session, params)
        session.turn_id = turn_id
        session.expects_structured = bool(params.get("outputSchema"))
        session.internal_tool_ids = set()
        session.started = True
        session.busy = True
        session.last_used = time.monotonic()
        await client.query(prompt)
        self._emit("turn/started", {"threadId": session.thread_id, "turnId": turn_id})
        session.drain = asyncio.ensure_future(self._drain(session, turn_id, client))

    async def _drain(self, session: _Session, turn_id: str, client: Any) -> None:
        """Pump one turn's messages into the dispatcher queue."""
        thread_id = session.thread_id
        common = {"threadId": thread_id, "turnId": turn_id}
        status = "completed"
        error_text = ""
        try:
            async for message in client.receive_response():
                for method, params in self._translate_sdk_message(
                    message,
                    common,
                    structured=session.expects_structured,
                    internal_ids=session.internal_tool_ids,
                ):
                    self._emit(method, params)
                session_id = getattr(message, "session_id", None)
                if session_id:
                    session.session_id = str(session_id)
                if type(message).__name__ == "ResultMessage":
                    if session.expects_structured:
                        payload = self._structured_payload(message)
                        if payload:
                            self._emit("assistant/message", {**common, "text": payload})
                    status, error_text = self._turn_outcome(message)
        except Exception as exc:  # noqa: BLE001
            status = "failed"
            error_text = str(exc)
            self._emit("error", {**common, "message": _bounded(str(exc), 4000)})
        finally:
            session.busy = False
            session.turn_id = ""
            session.last_used = time.monotonic()
            self._emit("turn/completed", {**common, "status": status, "error": error_text})

    @staticmethod
    def _turn_outcome(message: Any) -> tuple[str, str]:
        """Classify a terminating ResultMessage.

        An interrupted turn arrives as ``error_during_execution`` with
        ``terminal_reason='aborted_streaming'``. That is a writer-initiated
        stop, not a failure, and must not surface as an error in the UI.
        """
        terminal = str(getattr(message, "terminal_reason", "") or "")
        subtype = str(getattr(message, "subtype", "") or "")
        if terminal == "aborted_streaming":
            return "interrupted", ""
        if getattr(message, "is_error", False) or subtype.startswith("error"):
            errors = getattr(message, "errors", None) or []
            detail = "; ".join(str(item) for item in errors) if errors else subtype
            return "failed", _bounded(detail, 4000)
        return "completed", ""

    @staticmethod
    def _structured_payload(message: Any) -> str:
        """Extract the JSON a schema-bound turn was asked to produce."""
        structured = getattr(message, "structured_output", None)
        if structured is not None:
            if isinstance(structured, str):
                return structured
            try:
                return json.dumps(structured, ensure_ascii=False)
            except (TypeError, ValueError):
                return ""
        return str(getattr(message, "result", "") or "")

    def _translate_sdk_message(
        self,
        message: Any,
        common: dict[str, Any],
        *,
        structured: bool = False,
        internal_ids: set[str] | None = None,
    ) -> list[tuple[str, dict[str, Any]]]:
        """Map one SDK message onto this transport's notification shape."""
        name = type(message).__name__
        internal_ids = internal_ids if internal_ids is not None else set()
        out: list[tuple[str, dict[str, Any]]] = []

        if name == "AssistantMessage":
            for block in getattr(message, "content", []) or []:
                block_name = type(block).__name__
                if block_name == "ThinkingBlock":
                    # Never forward raw reasoning; the translator drops it too.
                    out.append(("assistant/thinkingDelta", dict(common)))
                    # ...but silence is its own failure. A fixed heartbeat says
                    # the model is working without saying what it is thinking.
                    out.append(("assistant/progress", {**common, "text": "Thinking\n"}))
                elif block_name == "TextBlock":
                    if structured:
                        # Prose alongside a schema-bound turn is commentary. The
                        # answer arrives as structured output; treating this as
                        # the final message would fail the manager's validator.
                        continue
                    out.append((
                        "assistant/message",
                        {**common, "itemId": getattr(message, "uuid", None), "text": getattr(block, "text", "")},
                    ))
                elif block_name == "ToolUseBlock":
                    tool = getattr(block, "name", "")
                    if tool in INTERNAL_TOOLS:
                        internal_ids.add(str(getattr(block, "id", "")))
                        continue
                    tool_input = getattr(block, "input", {}) or {}
                    out.append((
                        "tool/started",
                        {
                            **common,
                            "itemId": getattr(block, "id", None),
                            "tool": tool,
                            "command": tool_input.get("command"),
                            "cwd": tool_input.get("cwd") or self.cwd,
                            "query": tool_input.get("query"),
                            "changes": [{"path": tool_input.get("file_path"), "kind": "modify"}]
                            if tool_input.get("file_path")
                            else [],
                        },
                    ))
        elif name == "UserMessage":
            for block in getattr(message, "content", []) or []:
                if type(block).__name__ == "ToolResultBlock":
                    if str(getattr(block, "tool_use_id", "")) in internal_ids:
                        continue
                    out.append((
                        "tool/completed",
                        {
                            **common,
                            "itemId": getattr(block, "tool_use_id", None),
                            "tool": "",
                            "output": _bounded(getattr(block, "content", "")),
                            "status": "failed" if getattr(block, "is_error", False) else "completed",
                        },
                    ))
        elif name == "StreamEvent" and not structured:
            delta = self._stream_text(message)
            if delta:
                out.append(("assistant/textDelta", {**common, "delta": delta}))
        return out

    @staticmethod
    def _stream_text(message: Any) -> str:
        event = getattr(message, "event", None) or {}
        if not isinstance(event, dict) or event.get("type") != "content_block_delta":
            return ""
        delta = event.get("delta") or {}
        if isinstance(delta, dict) and delta.get("type") == "text_delta":
            return str(delta.get("text") or "")
        return ""

    async def _interrupt(self, session: _Session) -> None:
        client = session.client
        if client is None:
            return
        await client.interrupt()

    async def _read_session(self, session_id: str) -> dict[str, Any]:
        if self._session_reader is not None:
            messages = self._session_reader(session_id, self.cwd)
        else:
            import claude_agent_sdk as sdk

            try:
                messages = sdk.get_session_messages(session_id, directory=self.cwd)
            except Exception:
                # An id the store has never heard of is a missing thread, not a
                # broken transport; the caller turns this into "not found".
                messages = []
        if asyncio.iscoroutine(messages):
            messages = await messages
        # Shaped the way DiscussManager already parses history: a turn is a
        # user message plus the answers that followed it. The manager's restore
        # path is deliberately strict, so meeting its structure here is what
        # makes a Claude conversation reopenable at all.
        turns: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for row in messages or []:
            payload = getattr(row, "message", None) or {}
            role = payload.get("role") if isinstance(payload, dict) else getattr(row, "type", "")
            text = self._session_message_text(payload)
            if not text:
                continue
            if role == "user":
                current = {
                    "id": str(getattr(row, "uuid", "") or f"turn-{len(turns) + 1}"),
                    "items": [{
                        "type": "userMessage",
                        "content": [{"type": "text", "text": _bounded(text, 65536)}],
                    }],
                }
                turns.append(current)
            elif role == "assistant" and current is not None:
                current["items"].append({
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": _bounded(text, 65536),
                })
        return {"thread": {"id": session_id, "turns": turns}, "turns": turns}

    @staticmethod
    def _session_message_text(payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        content = payload.get("content")
        if isinstance(content, str):
            return content
        parts: list[str] = []
        for block in content or []:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "".join(parts)

    # --- approvals ---------------------------------------------------------

    async def _gate_tool(self, session: _Session, input_data: dict[str, Any]) -> dict[str, Any]:
        """Ask the writer before a tool runs, and block this turn until they answer."""
        tool = str(input_data.get("tool_name") or "")
        tool_input = input_data.get("tool_input") or {}
        if tool in READ_ONLY_TOOLS or tool in INTERNAL_TOOLS:
            return self._hook_decision("allow")

        granted = self._session_grants.get(session.thread_id) or set()
        if tool in granted:
            return self._hook_decision("allow")

        kind = "fileChange" if _activity_kind(tool) == "fileChange" else "command"
        request_id = uuid.uuid4().hex
        loop = self._loop
        if loop is None:
            return self._hook_decision("deny", "Prosview is not connected.")
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._approvals[request_id] = future

        params: dict[str, Any] = {
            "threadId": session.thread_id,
            "turnId": session.turn_id,
            "itemId": input_data.get("tool_use_id"),
            "reason": f"Claude wants to use {tool}, which is outside Prosview's read-only scope.",
            "cwd": str(input_data.get("cwd") or self.cwd),
            "availableDecisions": list(APPROVAL_DECISIONS),
        }
        if kind == "command":
            params["command"] = _bounded(tool_input.get("command") or tool, 4000)
        else:
            params["changes"] = [{"path": tool_input.get("file_path"), "kind": "modify"}]
            params["command"] = _bounded(f"{tool} {tool_input.get('file_path') or ''}".strip(), 4000)

        self._outbound.put({"id": request_id, "method": APPROVAL_METHODS[kind], "params": params})
        try:
            result = await asyncio.wait_for(future, timeout=APPROVAL_TIMEOUT)
        except (TimeoutError, asyncio.CancelledError):
            return self._hook_decision("deny", "Prosview did not receive an approval in time.")
        finally:
            self._approvals.pop(request_id, None)

        decision = str(result.get("decision") or "decline")
        if decision == "acceptForSession":
            self._session_grants.setdefault(session.thread_id, set()).add(tool)
            return self._hook_decision("allow")
        if decision == "accept":
            return self._hook_decision("allow")
        return self._hook_decision("deny", "The writer declined this action.")

    @staticmethod
    def _hook_decision(decision: str, reason: str = "") -> dict[str, Any]:
        output: dict[str, Any] = {"hookEventName": "PreToolUse", "permissionDecision": decision}
        if reason:
            output["permissionDecisionReason"] = reason
        return {"hookSpecificOutput": output}

    def respond(self, request_id: int | str, result: dict[str, Any]) -> None:
        key = str(request_id)
        future = self._approvals.get(key)
        loop = self._loop
        if future is None or loop is None:
            raise ClaudeRequestError("approval is no longer pending")
        loop.call_soon_threadsafe(lambda: None if future.done() else future.set_result(result))

    def respond_error(self, request_id: int | str, message: str, code: int = -32601) -> None:
        key = str(request_id)
        future = self._approvals.get(key)
        loop = self._loop
        if future is None or loop is None:
            return
        loop.call_soon_threadsafe(
            lambda: None if future.done() else future.set_result({"decision": "decline"})
        )

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        # The SDK has no unsolicited-notification channel; nothing to send.
        return None

    # --- shutdown ----------------------------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        loop = self._loop
        if loop is not None:
            # Release anyone still waiting on a writer decision. Abandoning the
            # future instead would leave the gate coroutine — and the turn it is
            # holding — pending forever.
            for request_id, future in list(self._approvals.items()):
                self._approvals.pop(request_id, None)
                loop.call_soon_threadsafe(
                    lambda f=future: None if f.done() else f.set_result({"decision": "decline"})
                )
            with self._lock:
                sessions = list(self._sessions.values())
                self._sessions.clear()
            for session in sessions:
                try:
                    asyncio.run_coroutine_threadsafe(self._disconnect(session), loop).result(timeout=5)
                except Exception:
                    pass
            loop.call_soon_threadsafe(loop.stop)
        self._outbound.put(None)
        self._loop = None
        self._fail(ClaudeUnavailableError("Claude client stopped"))
