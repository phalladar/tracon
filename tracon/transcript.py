"""Incremental parsing of Claude Code JSONL transcripts into compact events.

Transcripts are not a public API. Everything that depends on their shape lives in
this module so a format change is fixed in one place.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
# Tools that return in well under a second; a pending one is waiting on a person.
INSTANT_TOOLS = EDIT_TOOLS | {"Read", "Grep", "Glob", "LS", "TodoWrite", "ToolSearch"}
HANDBACK_TOOLS = {"SubagentHandback"}
COORDINATION_TOOLS = {"SendMessage", "TaskStop", "Agent", "Task"}
COMMAND_RE = re.compile(r"<command-name>/?([^<]+)</command-name>")
COMMAND_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.S)
# The lead is told when a background task (agent or shell) stops. Status is one of
# completed / failed / killed / stopped. The same task may notify again after a resume.
TASK_NOTE_RE = re.compile(r"<task-id>([^<]+)</task-id>.*?<status>([^<]+)</status>(?:.*?<summary>([^<]*)</summary>)?", re.S)
TASK_NOTE_TOOL_RE = re.compile(r"<tool-use-id>([^<]+)</tool-use-id>")

COORDINATOR_PREFIX = "The coordinator sent a message"
SYSTEM_NOTIFICATION_PREFIX = "[SYSTEM NOTIFICATION"
REJECTED_MARKERS = (
    "doesn't want to proceed",
    "tool use was rejected",
    "Request interrupted by user",
    "Permission for this action was denied",
)
# A guard (Claude Code's worktree isolation, its sleep block) refused the call and said what to
# do instead. Not a permission the user could grant.
REFUSED_MARKERS = (
    "Refusing to run",
    "<tool_use_error>Blocked:",
)
ERROR_TEXT_MAX = 1500  # failed results keep this much text; `summary` is only a feed line
SAID_TEXT_MAX = 6000  # the agent's own words keep this much, line breaks intact, for the detail feed

MERGE_BASE_RE = re.compile(r"\bgit\s+(merge|rebase|pull|reset)\b[^\n;&|]*\b(master|main)\b")
COMMIT_RE = re.compile(r"\bgit\s+commit\b")
# nohup, setsid, disown, or a lone trailing `&` (not `&&`): work that outlives the tool call.
DETACH_RE = re.compile(r"\bnohup\b|\bsetsid\b|\bdisown\b|(?<![&>|])&\s*(?:$|;|\n|\))")


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@dataclass(slots=True)
class Event:
    ts: datetime
    kind: str  # tool_use | tool_result | text | coordinator | notification | brief
    tool: str = ""
    summary: str = ""
    key: str = ""  # identity of a tool call, for loop detection
    tool_id: str = ""
    is_error: bool = False
    rejected: bool = False
    refused: bool = False  # refused by a guard (REFUSED_MARKERS), not denied a permission
    text: str = ""  # a failed result's text, one line, up to ERROR_TEXT_MAX; an agent's text in full
    path: str = ""  # file edited, for edit tools
    background: bool = False
    detached: bool = False  # backgrounded by the shell (nohup, &): never reports back to the agent
    extra: dict | None = None  # raw input for coordination tools; parsed fields for task notes
    final: bool = False  # text that ended the agent's turn (stop_reason end_turn)


@dataclass
class Transcript:
    """One JSONL file, read incrementally from where the last read stopped."""

    path: str
    events: list[Event] = field(default_factory=list)
    brief: str = ""
    branches: list[tuple[datetime, str]] = field(default_factory=list)
    ctx: list[tuple[datetime, int]] = field(default_factory=list)  # context tokens per turn
    output_tokens: int = 0
    cwd: str = ""
    commands: list[tuple[datetime, str, str]] = field(default_factory=list)  # slash commands
    model: str = ""  # last model seen on an assistant message
    title: str = ""  # Claude Code's generated session title (lead transcripts)
    name: str = ""  # the session's name from /rename, if any
    _offset: int = 0
    _partial: bytes = b""
    _seen_msg_ids: set = field(default_factory=set)
    _inherited: bool = False  # inside a fork's copy of the lead's spawning message

    def refresh(self) -> bool:
        """Read any appended lines. Returns True when something new arrived."""
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return False
        if size < self._offset:  # truncated or replaced: start over
            self.__init__(self.path)  # type: ignore[misc]
        if size == self._offset:
            return False
        with open(self.path, "rb") as f:
            f.seek(self._offset)
            chunk = f.read()
        self._offset += len(chunk)
        data = self._partial + chunk
        lines = data.split(b"\n")
        self._partial = lines.pop()  # incomplete last line, if any
        for line in lines:
            if line.strip():
                try:
                    self._ingest(json.loads(line))
                except (ValueError, KeyError, TypeError):
                    continue
        return True

    # -- record handling -------------------------------------------------

    def _ingest(self, rec: dict) -> None:
        rtype = rec.get("type")
        ts_raw = rec.get("timestamp")
        if rtype == "ai-title" and rec.get("aiTitle"):
            self.title = rec["aiTitle"]
            return
        if rtype == "agent-name" and rec.get("agentName"):
            self.name = rec["agentName"]  # set by /rename; wins over the generated title
            return
        if rtype == "fork-context-ref":
            # A fork's transcript opens with the lead's message that spawned it. That Agent
            # call (and its context usage) is the lead's, not the fork's.
            self._inherited = True
            return
        if rtype == "attachment":
            # Background task completions can arrive as a queued command instead of user text.
            att = rec.get("attachment") or {}
            prompt = att.get("prompt")
            if att.get("type") == "queued_command" and isinstance(prompt, str) and ts_raw:
                if prompt.strip().startswith("<task-notification>"):
                    self._user_text(parse_ts(ts_raw), prompt)
            return
        if rtype not in ("user", "assistant") or not ts_raw:
            return
        ts = parse_ts(ts_raw)
        if rec.get("cwd") and not self.cwd:
            self.cwd = rec["cwd"]
        branch = rec.get("gitBranch")
        if branch and (not self.branches or self.branches[-1][1] != branch):
            self.branches.append((ts, branch))
        msg = rec.get("message") or {}
        content = msg.get("content")
        if rtype == "assistant":
            if self._inherited:
                return
            self._assistant(ts, msg, content)
        else:
            self._inherited = False
            self._user(ts, content)

    def _assistant(self, ts: datetime, msg: dict, content) -> None:
        usage = msg.get("usage") or {}
        mid = msg.get("id")
        if msg.get("model") and not msg["model"].startswith("<"):
            self.model = msg["model"]
        final = msg.get("stop_reason") == "end_turn"
        if usage and mid not in self._seen_msg_ids:
            # One API response is split over several records that repeat its usage.
            self._seen_msg_ids.add(mid)
            self.output_tokens += usage.get("output_tokens", 0)
            total = (
                usage.get("input_tokens", 0)
                + usage.get("cache_read_input_tokens", 0)
                + usage.get("cache_creation_input_tokens", 0)
            )
            if total:
                self.ctx.append((ts, total))
        if not isinstance(content, list):
            return
        for b in content:
            bt = b.get("type")
            if bt == "tool_use":
                self.events.append(_tool_use_event(ts, b, self.cwd))
            elif bt == "text" and b.get("text", "").strip():
                said = b["text"].strip()
                if len(said) > SAID_TEXT_MAX:
                    said = said[: SAID_TEXT_MAX - 1] + "…"
                self.events.append(Event(ts, "text", summary=_one_line(said), text=said, final=final))

    def _user(self, ts: datetime, content) -> None:
        if isinstance(content, str):
            self._user_text(ts, content)
            return
        if not isinstance(content, list):
            return
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_result":
                text = _result_text(b.get("content"))
                rejected = any(m in text for m in REJECTED_MARKERS)
                refused = not rejected and any(m in text for m in REFUSED_MARKERS)
                is_error = bool(b.get("is_error")) or rejected or refused
                self.events.append(
                    Event(
                        ts,
                        "tool_result",
                        tool_id=b.get("tool_use_id", ""),
                        is_error=is_error,
                        rejected=rejected,
                        refused=refused,
                        summary=_one_line(text),
                        text=_one_line(text, ERROR_TEXT_MAX) if is_error else "",
                    )
                )
            elif b.get("type") == "text":
                self._user_text(ts, b.get("text", ""))

    def _user_text(self, ts: datetime, text: str) -> None:
        t = text.strip()
        if not t:
            return
        m = COMMAND_RE.search(t)
        if m and t.startswith("<command-"):
            a = COMMAND_ARGS_RE.search(t)
            self.commands.append((ts, m.group(1).strip(), a.group(1).strip() if a else ""))
            return
        if t.startswith("<task-notification>"):
            n = TASK_NOTE_RE.search(t)
            if n:
                note = {"task_id": n.group(1).strip(), "status": n.group(2).strip(), "summary": (n.group(3) or "").strip()}
                if tu := TASK_NOTE_TOOL_RE.search(t):
                    note["tool_use_id"] = tu.group(1).strip()
                self.events.append(Event(ts, "task_note", summary=f"{note['status']}: {note['summary']}", extra=note))
            return
        if not self.brief:
            self.brief = t
            self.events.append(Event(ts, "brief", summary=_one_line(t)))
        elif t.startswith(COORDINATOR_PREFIX):
            self.events.append(Event(ts, "coordinator", summary=_one_line(t.split(":", 1)[-1])))
        elif t.startswith(SYSTEM_NOTIFICATION_PREFIX):
            self.events.append(Event(ts, "notification", summary=_one_line(t)))
        elif t.startswith("[Request interrupted"):
            self.events.append(Event(ts, "notification", summary=t, is_error=True))
        # <system-reminder> and similar harness text is not shown.


def _tool_use_event(ts: datetime, b: dict, cwd: str) -> Event:
    name = b.get("name", "?")
    inp = b.get("input") or {}
    path = ""
    if name in EDIT_TOOLS:
        path = inp.get("file_path") or inp.get("notebook_path") or ""
        summary = _rel(path, cwd)
        key = ""
    elif name == "Bash":
        cmd = inp.get("command", "")
        summary = inp.get("description") or _one_line(cmd)
        summary = f"{summary}  $ {_one_line(cmd, 80)}" if inp.get("description") else summary
        key = "Bash:" + cmd.strip()
    elif name == "Read":
        path = inp.get("file_path", "")
        p = _rel(path, cwd)
        rng = f":{inp['offset']}" if inp.get("offset") else ""
        summary = p + rng
        key = f"Read:{p}{rng}:{inp.get('limit', '')}"
    elif name in ("Grep", "Glob"):
        summary = f"{inp.get('pattern', '')}  {_rel(inp.get('path', ''), cwd)}".strip()
        key = f"{name}:{json.dumps(inp, sort_keys=True)}"
    elif name in HANDBACK_TOOLS:
        summary = _one_line(inp.get("message", ""), 200)
        key = ""
    elif name in ("Agent", "Task"):
        summary = inp.get("description") or _one_line(inp.get("prompt", ""), 120)
        key = f"{name}:{json.dumps(inp, sort_keys=True)}"
    elif name == "SendMessage":
        summary = f"→ {inp.get('to', '?')}: {_one_line(str(inp.get('message', '')), 140)}"
        key = f"{name}:{json.dumps(inp, sort_keys=True)}"
    elif name == "TaskStop":
        summary = f"stop {inp.get('task_id', '?')}"
        key = f"{name}:{json.dumps(inp, sort_keys=True)}"
    else:
        summary = _one_line(json.dumps(inp), 120)
        key = f"{name}:{json.dumps(inp, sort_keys=True)}"
    detached = name == "Bash" and not inp.get("run_in_background") and bool(DETACH_RE.search(inp.get("command", "")))
    return Event(
        ts,
        "tool_use",
        tool=name,
        summary=summary,
        key=key,
        tool_id=b.get("id", ""),
        path=path,
        background=bool(inp.get("run_in_background")) or detached,
        detached=detached,
        extra=inp if name in COORDINATION_TOOLS else None,
    )


def _result_text(c) -> str:
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(x.get("text", "") for x in c if isinstance(x, dict))
    return ""


def _one_line(s: str, n: int = 140) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _rel(path: str, cwd: str) -> str:
    if cwd and path.startswith(cwd.rstrip("/") + "/"):
        return path[len(cwd.rstrip("/")) + 1 :]
    return path


def is_commit(ev: Event) -> bool:
    return ev.tool == "Bash" and bool(COMMIT_RE.search(ev.key))


def is_base_merge(ev: Event) -> bool:
    return ev.tool == "Bash" and bool(MERGE_BASE_RE.search(ev.key))


def iter_until(events: list[Event], now: datetime | None) -> Iterator[Event]:
    for e in events:
        if now is not None and e.ts > now:
            return
        yield e


TITLE_TAIL = 256 * 1024
TITLE_RECORD_RE = re.compile(rb'^.*"type":\s*"(?:ai-title|agent-name)".*$', re.M)


def read_title(path: str) -> str:
    """The session's name as Claude Code shows it in the terminal tab (the /rename name, else the
    generated title), without parsing the whole transcript. Claude Code re-appends its title records
    as a group, so they are read from the end, in growing chunks until some turn up."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            span = TITLE_TAIL
            while True:
                start = max(0, size - span)
                f.seek(start)
                data = f.read(size - start)
                if start:
                    data = data[data.find(b"\n") + 1 :]  # drop the cut-off first line
                name = title = ""
                for line in TITLE_RECORD_RE.findall(data):
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if rec.get("type") == "agent-name" and rec.get("agentName"):
                        name = rec["agentName"]
                    elif rec.get("type") == "ai-title" and rec.get("aiTitle"):
                        title = rec["aiTitle"]
                if name or title or not start:
                    return name or title
                span *= 4
    except OSError:
        return ""
