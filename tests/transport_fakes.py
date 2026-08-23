"""Transport doubles for the two agents Discuss can drive.

``DiscussManager`` is meant to be indifferent to which agent answered: each
transport translates its own wire protocol into one event vocabulary, and
nothing above that seam branches on the agent. These fakes are how that claim
is tested. They accept the same requests and drive the same manager
behaviour, but they speak different protocols on the way in --
``CodexFakeClient`` emits app-server notifications, ``ClaudeFakeClient`` emits
the Claude transport's own shape and carries its translator.

A behaviour that only holds for one of them is a bug in a translator.
"""

from __future__ import annotations

import json
import threading
import time

from proseview.claude_agent_client import ClaudeRequestError, sanitize_claude_message
from proseview.codex_app_server import CodexRequestError


class CodexFakeClient:
    def __init__(self, callback, agent: str = "codex"):
        self.agent = agent
        self.callback = callback
        self.alive = True
        self.next_thread = 0
        self.next_turn = 0
        self.prompts: list[str] = []
        self.turn_params: list[dict] = []
        self.responses: list[tuple[object, dict]] = []
        self.interrupts: list[dict] = []
        self.active = 0
        self.max_active = 0
        self.turn_start_attempts = 0
        self.finish_delay = 0.04
        self.hold_next_turn = False
        self.interrupt_error: BaseException | None = None
        self.complete_turn_before_interrupt_error = False
        self.reject_turn_starts = False
        self.invalid_continuity_result = False
        self.continuity_file = "manuscript/one.md"
        self.continuity_line = 3
        self.continuity_quote = "Mira learned winter in Boston."
        self.capabilities = {"reasoning_summary": True}
        self.config = {"model": "gpt-5.6-sol", "model_reasoning_effort": "xhigh"}
        self.threads: dict[str, dict] = {}
        self._lock = threading.Lock()

    def inspect_capabilities(self):
        return {"stable_discuss_protocol": True}

    def probe_capabilities(self):
        return {"stable_discuss_protocol": True}

    def start(self):
        return None

    def request(self, method, params, *, timeout=None):
        if method == "skills/list":
            return {"data": [{"cwd": params["cwds"][0], "skills": [{
                "name": "scene-review", "path": "/.proseview/skills/scene-review/SKILL.md", "enabled": True,
                "description": "Review a scene", "interface": {"displayName": "Scene Review", "shortDescription": "Review selected prose"},
            }]}]}
        if method == "thread/read":
            thread = self.threads.get(params["threadId"])
            if thread is None:
                raise CodexRequestError(f"thread not found: {params['threadId']}", code=-32004)
            return {"thread": thread}
        if method == "thread/start":
            self.next_thread += 1
            thread = {"id": f"thread-{self.next_thread}", "turns": []}
            self.threads[thread["id"]] = thread
            return {"thread": thread}
        if method == "turn/start":
            self.turn_start_attempts += 1
            if self.reject_turn_starts:
                raise CodexRequestError(f"thread not found: {params['threadId']}", code=-32004)
            if params["threadId"] not in self.threads:
                raise CodexRequestError(f"thread not found: {params['threadId']}", code=-32004)
            self.next_turn += 1
            turn_id = f"turn-{self.next_turn}"
            thread_id = params["threadId"]
            self.prompts.append(params["input"][0]["text"])
            self.turn_params.append(dict(params))
            with self._lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)

            if self.hold_next_turn:
                self.hold_next_turn = False
                return {"turn": {"id": turn_id, "status": "inProgress"}}

            def finish():
                self.callback({
                    "method": "item/reasoning/textDelta",
                    "params": {"threadId": thread_id, "turnId": turn_id, "delta": "RAW SECRET"},
                })
                self.callback({
                    "method": "item/reasoning/summaryTextDelta",
                    "params": {"threadId": thread_id, "turnId": turn_id, "delta": "Reading context"},
                })
                answer = f"Answer {turn_id}"
                schema = params.get("outputSchema") or {}
                kind = (((schema.get("properties") or {}).get("kind") or {}).get("enum") or [None])[0]
                if kind == "alternatives":
                    count = schema["properties"]["alternatives"]["maxItems"]
                    choices = [
                        {"text": "Revised document.", "rationale": "Removes repetition."},
                        {"text": "A revised document.", "rationale": "Changes the rhythm."},
                        {"text": "Document, revised.", "rationale": "Leads with the subject."},
                    ]
                    answer = json.dumps({"kind": "alternatives", "summary": "A tighter beat.", "alternatives": choices[:count]})
                elif kind == "critique":
                    answer = json.dumps({"kind": "critique", "findings": [{"observation": "The opening is abstract.", "evidence": "First document.", "why_it_matters": "The image is hard to picture.", "next_step": "Use one concrete detail."}]})
                elif kind == "continuity_report":
                    answer = json.dumps({
                        "kind": "continuity_report",
                        "summary": "One direct contradiction needs review.",
                        "findings": [{
                            "category": "direct",
                            "file": self.continuity_file,
                            "line": self.continuity_line,
                            "quote": self.continuity_quote,
                            "explanation": "This conflicts with the requested Chicago childhood.",
                            "replacement": "Mira learned winter in Chicago.",
                        }],
                    })
                    if self.invalid_continuity_result:
                        answer = json.dumps({
                            "kind": "continuity_report",
                            "summary": "Unsupported citation.",
                            "findings": [{
                                "category": "direct",
                                "file": "manuscript/one.md",
                                "line": 3,
                                "quote": "This quote is not in the scanned file.",
                                "explanation": "Invented evidence.",
                                "replacement": "Replacement.",
                            }],
                        })
                self.callback({
                    "method": "item/completed",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "item": {"id": f"answer-{turn_id}", "type": "agentMessage", "phase": "final_answer", "text": answer},
                    },
                })
                self.callback({
                    "method": "turn/completed",
                    "params": {"threadId": thread_id, "turn": {"id": turn_id, "status": "completed"}},
                })
                with self._lock:
                    self.active -= 1

            threading.Timer(self.finish_delay, finish).start()
            return {"turn": {"id": turn_id, "status": "inProgress"}}
        if method == "model/list":
            # Codex publishes the catalog and the resolved configuration
            # separately, and its effort ladder differs per model.
            return {"data": [
                {
                    "id": "gpt-5.6-sol",
                    "displayName": "GPT-5.6-Sol",
                    "description": "Latest frontier agentic coding model.",
                    "supportedReasoningEfforts": [
                        {"reasoningEffort": effort, "description": f"{effort} reasoning"}
                        for effort in ("low", "medium", "high", "xhigh", "max", "ultra")
                    ],
                    "defaultReasoningEffort": "medium",
                    "isDefault": True,
                },
                {
                    "id": "gpt-5.6-luna",
                    "displayName": "GPT-5.6-Luna",
                    "description": "Fast and affordable agentic coding model.",
                    "supportedReasoningEfforts": [
                        {"reasoningEffort": effort, "description": f"{effort} reasoning"}
                        for effort in ("low", "medium", "high")
                    ],
                    "defaultReasoningEffort": "medium",
                    "isDefault": False,
                },
                {
                    "id": "gpt-5.4",
                    "displayName": "GPT-5.4",
                    "description": "Strong model for everyday coding.",
                    "supportedReasoningEfforts": [{"reasoningEffort": "high", "description": "high reasoning"}],
                    "defaultReasoningEffort": "high",
                    "isDefault": False,
                    "upgrade": "gpt-5.6-terra",
                },
            ]}
        if method == "config/read":
            return {"config": dict(self.config)}
        if method == "turn/interrupt":
            self.interrupts.append(dict(params))
            if self.complete_turn_before_interrupt_error:
                self.callback({
                    "method": "turn/completed",
                    "params": {
                        "threadId": params["threadId"],
                        "turn": {"id": params["turnId"], "status": "interrupted"},
                    },
                })
                deadline = time.monotonic() + 1.0
                while self.next_turn < 2 and time.monotonic() < deadline:
                    time.sleep(0.001)
            if self.interrupt_error is not None:
                raise self.interrupt_error
            return {}
        raise AssertionError(method)

    def respond(self, request_id, result):
        self.responses.append((request_id, result))

    def respond_error(self, request_id, message):
        self.responses.append((request_id, {"error": message}))

    def close(self):
        self.alive = False


class ClaudeFakeClient:
    """Drives the same manager behaviour over the Claude transport's shape.

    Deliberately a separate implementation rather than a subclass of the Codex
    double: the point is that the two protocols really are different on the
    wire and still land on the same events. Sharing the emitting code would
    test nothing.
    """

    # The manager takes each transport's translator from the client itself.
    translate = staticmethod(sanitize_claude_message)

    def __init__(self, callback, agent: str = "claude"):
        self.agent = agent
        self.callback = callback
        self.alive = True
        self.next_thread = 0
        self.next_turn = 0
        self.prompts: list[str] = []
        self.turn_params: list[dict] = []
        self.responses: list[tuple[object, dict]] = []
        self.interrupts: list[dict] = []
        self.approvals: list[dict] = []
        self.finish_delay = 0.04
        self.hold_next_turn = False
        self.reject_turn_starts = False
        self.interrupt_error: BaseException | None = None
        self.capabilities = {
            "reasoning_summary": True,
            "approval_decisions": {
                "command": ["accept", "acceptForSession", "decline", "cancel"],
                "fileChange": ["accept", "acceptForSession", "decline", "cancel"],
            },
        }
        self.model_default = {"model": "opus", "effort": "high", "source": "Claude Code settings"}
        self.threads: dict[str, dict] = {}
        self._lock = threading.Lock()

    def inspect_capabilities(self):
        return {"stable_discuss_protocol": True}

    def probe_capabilities(self):
        return {"stable_discuss_protocol": True}

    def start(self):
        return None

    def _structured_answer(self, params, turn_id):
        schema = params.get("outputSchema") or {}
        kind = (((schema.get("properties") or {}).get("kind") or {}).get("enum") or [None])[0]
        if kind == "alternatives":
            count = schema["properties"]["alternatives"]["maxItems"]
            choices = [
                {"text": "Revised document.", "rationale": "Removes repetition."},
                {"text": "A revised document.", "rationale": "Changes the rhythm."},
                {"text": "Document, revised.", "rationale": "Leads with the subject."},
            ]
            return json.dumps({
                "kind": "alternatives",
                "summary": "A tighter beat.",
                "alternatives": choices[:count],
            })
        if kind == "critique":
            return json.dumps({"kind": "critique", "findings": [{
                "observation": "The opening is abstract.",
                "evidence": "First document.",
                "why_it_matters": "The image is hard to picture.",
                "next_step": "Use one concrete detail.",
            }]})
        return f"Answer {turn_id}"

    def request(self, method, params, *, timeout=None):
        if method == "skills/list":
            return {"skills": []}
        if method == "model/list":
            # The Claude transport already knows its own resolved default, so
            # it answers the catalog and the default in one response.
            return {
                "data": [
                    {
                        "id": "opus",
                        "displayName": "Opus 5",
                        "description": "Most capable.",
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": effort, "description": f"{effort} reasoning"}
                            for effort in ("low", "medium", "high", "xhigh", "max")
                        ],
                        "defaultReasoningEffort": "high",
                        "isDefault": True,
                    },
                    {
                        "id": "haiku",
                        "displayName": "Haiku 4.5",
                        "description": "Fastest and cheapest.",
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": effort, "description": f"{effort} reasoning"}
                            for effort in ("low", "medium", "high", "xhigh", "max")
                        ],
                        "defaultReasoningEffort": "low",
                        "isDefault": False,
                    },
                ],
                "default": dict(self.model_default),
            }
        if method == "thread/read":
            thread = self.threads.get(params["threadId"])
            if thread is None:
                raise ClaudeRequestError(f"thread not found: {params['threadId']}", code=-32004)
            return {"thread": thread, "turns": thread.get("turns") or []}
        if method == "thread/start":
            self.next_thread += 1
            thread = {"id": f"claude-thread-{self.next_thread}", "turns": []}
            self.threads[thread["id"]] = thread
            return {"thread": thread}
        if method == "turn/start":
            if self.reject_turn_starts or params["threadId"] not in self.threads:
                raise ClaudeRequestError(f"thread not found: {params['threadId']}", code=-32004)
            self.next_turn += 1
            turn_id = f"claude-turn-{self.next_turn}"
            thread_id = params["threadId"]
            self.prompts.append(params["input"][0]["text"])
            self.turn_params.append(dict(params))
            if self.hold_next_turn:
                self.hold_next_turn = False
                return {"turn": {"id": turn_id}}

            def finish():
                common = {"threadId": thread_id, "turnId": turn_id}
                # Raw thinking must never reach the browser; this translator
                # drops it, exactly as the Codex one drops reasoning deltas.
                self.callback({"method": "assistant/thinkingDelta",
                               "params": {**common, "text": "RAW SECRET"}})
                self.callback({"method": "assistant/progress",
                               "params": {**common, "text": "Reading context"}})
                self.callback({"method": "assistant/message", "params": {
                    **common,
                    "itemId": f"answer-{turn_id}",
                    "phase": "final_answer",
                    "text": self._structured_answer(params, turn_id),
                }})
                self.callback({"method": "turn/completed",
                               "params": {**common, "status": "completed", "error": ""}})

            threading.Timer(self.finish_delay, finish).start()
            return {"turn": {"id": turn_id}}
        if method == "turn/interrupt":
            self.interrupts.append(dict(params))
            if self.interrupt_error is not None:
                raise self.interrupt_error
            return {}
        raise AssertionError(method)

    def ask_approval(self, thread_id, turn_id, request_id="approval-1"):
        """Raise a tool-permission request the way the PreToolUse gate does."""
        message = {
            "id": request_id,
            "method": "item/commandExecution/requestApproval",
            "params": {
                "threadId": thread_id,
                "turnId": turn_id,
                "itemId": "tool-1",
                "command": "rm -rf /",
                "cwd": ".",
                "reason": "Claude wants to use Bash.",
                "availableDecisions": ["accept", "acceptForSession", "decline", "cancel"],
            },
        }
        self.approvals.append(message)
        self.callback(message)
        return request_id

    def respond(self, request_id, result):
        self.responses.append((request_id, result))

    def respond_error(self, request_id, message, code=-32601):
        self.responses.append((request_id, {"error": message}))

    def close(self):
        self.alive = False


def fake_factory(callback, agent):
    """Give the manager the double matching the agent it asked for."""
    return ClaudeFakeClient(callback) if agent == "claude" else CodexFakeClient(callback)
