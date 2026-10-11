"""Turn an agent's transcript (plus optional live probe) into a state and warning flags."""

from __future__ import annotations

import fnmatch
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .probe import WorktreeProbe
from .session import Agent, Session
from .transcript import (
    EDIT_TOOLS,
    HANDBACK_TOOLS,
    INSTANT_TOOLS,
    Event,
    is_base_merge,
    is_commit,
    iter_until,
)


# Shell commands that finish at once; still pending means a permission prompt.
QUICK_BASH_RE = re.compile(
    r"^\s*(?:cd [^;&|]+(?:&&|;)\s*)?(?:git\s+(?:status|log|diff|branch|show|rev-parse|rev-list|worktree list|stash list)"
    r"|ls|cat|head|tail|wc|pwd|echo|grep|rg|find|stat|readlink|which|date)\b"
)


DENIAL_REASON_RE = re.compile(r"Reason: \[([^\]]+)\]")


@dataclass
class Thresholds:
    blocked_s: float = 45  # an instant tool still pending after this is waiting on approval
    idle_s: float = 5 * 60  # no transcript growth and nothing running
    stall_s: float = 20 * 60  # something runs, but no commit or file change
    loop_n: int = 3  # same call this many times with no edit in between
    loop_window: int = 25  # ...within the last N tool calls
    error_streak: int = 3
    uncommitted_s: float = 45 * 60
    ctx_warn: int = 300_000
    ctx_bad: int = 450_000


# Ordered most to least urgent; the table sorts by this.
STATES = {
    "BLOCKED": ("bad", "⛔"),
    "IDLE": ("bad", "⏸"),
    "STALLED": ("bad", "🐌"),
    "ORPHANED": ("bad", "👻"),
    "PENDING": ("warn", "…"),
    "WAITING": ("warn", "⏳"),
    "RUNNING": ("ok", "▶"),
    "WORKING": ("ok", "●"),
    "STARTING": ("ok", "○"),
    "REPORTED": ("done", "✓"),
    "STOPPED": ("done", "■"),
}


@dataclass
class Flag:
    code: str
    severity: str  # bad | warn | info
    detail: str


@dataclass
class Status:
    agent: Agent
    state: str
    detail: str
    last: Event | None
    age_s: float  # since the transcript last moved
    run_s: float  # since the agent started
    ctx: int
    edits: list[str] = field(default_factory=list)
    off_brief: list[str] = field(default_factory=list)
    commits: int = 0
    lead_msgs: int = 0
    flags: list[Flag] = field(default_factory=list)
    probe: WorktreeProbe | None = None
    judgment: object | None = None  # judge.Judgment, attached by lane.assess_all

    @property
    def severity(self) -> str:
        return STATES[self.state][0]

    @property
    def icon(self) -> str:
        return STATES[self.state][1]

    @property
    def finished(self) -> bool:
        return self.severity == "done"

    def sort_key(self) -> tuple:
        flag_rank = 0 if any(f.severity == "bad" for f in self.flags) else 1 if self.flags else 2
        age = self.age_s if self.finished else -self.age_s  # done: newest first; else stuck longest first
        return (list(STATES).index(self.state), flag_rank, age)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def assess(
    agent: Agent,
    session: Session,
    now: datetime | None = None,
    probe: WorktreeProbe | None = None,
    th: Thresholds | None = None,
) -> Status:
    """`now` None means live; a datetime replays the agent as it stood then (probe ignored)."""
    th = th or Thresholds()
    replay = now is not None
    at = now or now_utc()
    if replay:
        probe = None
    events = list(iter_until(agent.transcript.events, now))
    ctx_points = [c for t, c in agent.transcript.ctx if t <= at]
    ctx = ctx_points[-1] if ctx_points else 0
    last = events[-1] if events else None
    age = (at - last.ts).total_seconds() if last else 0.0
    started = events[0].ts if events else at
    st = Status(agent, "STARTING", "", last, age, (at - started).total_seconds(), ctx, probe=probe)
    st.lead_msgs = len(session.lead_messages(agent.id, now))
    if not events:
        return st

    _work_summary(st, events, agent)
    st.state, st.detail = _state(agent, session, events, at, now, probe, th)
    st.flags = _flags(st, agent, session, events, at, probe, th)
    return st


# -- state ------------------------------------------------------------------


def _state(agent, session, events, at, now, probe, th) -> tuple[str, str]:
    last = events[-1]
    age = (at - last.ts).total_seconds()

    stopped = session.stopped_at(agent.id, now)
    if stopped and last.ts <= stopped + _seconds(5):
        return "STOPPED", "stopped by the lead"

    # The lead's task notification is authoritative, unless the agent was resumed after it.
    handback = _last_handback(events)
    note = session.task_note(agent.id, now)
    if note and last.ts <= note.ts + _seconds(5):
        status = note.extra.get("status", "")
        final_text = _final_text(events)
        if status == "completed":
            parked = [
                e for e in events
                if e.kind == "tool_use" and e.background and (handback is None or e.ts > handback.ts)
            ]
            if parked:
                # It stopped while work it launched was still going: parked mid-task, not done.
                return "WAITING", f"stopped mid-task while its background work runs (lead told 'completed'): {final_text}"
            return "REPORTED", final_text or note.extra.get("summary", "") or "completed"
        return "STOPPED", f"{status}: {final_text or note.extra.get('summary', '')}"

    if handback is not None and not any(
        e.kind == "tool_use" and e.ts > handback.ts and e.tool not in HANDBACK_TOOLS for e in events
    ):
        return "REPORTED", handback.summary

    live = probe.live if probe else None
    orphans = [p for p in probe.procs if p.orphan] if probe else []

    # Agents without a hand-back tool (Explore, general-purpose) finish by ending their turn
    # with a final answer. If they left work running they are waiting on it, not done.
    if last.kind == "text" and last.final and not _pending(events):
        if live:
            return "WAITING", f"ended its turn; background work still running: {probe.leaf_command()[:80]}"
        if live is None and any(e.kind == "tool_use" and e.background for e in events):
            # No process data (replay, or no worktree) and it launched background work: it may
            # be parked until that work notifies it. The lead's task note settles it either way.
            return "WAITING", "ended its turn; may be waiting on background work it launched"
        return "REPORTED", last.summary

    pending = _pending(events)
    if pending:
        p_age = (at - pending.ts).total_seconds()
        what = f"{pending.tool}: {pending.summary}"
        if pending.tool in INSTANT_TOOLS and p_age > th.blocked_s:
            return "BLOCKED", f"awaiting approval {_dur(p_age)} — {what}"
        if pending.tool == "Bash":
            if live is False and p_age > th.blocked_s:
                return "BLOCKED", f"nothing running for {_dur(p_age)} — awaiting approval? {what}"
            if live is None and p_age > th.blocked_s and QUICK_BASH_RE.match(pending.key.removeprefix("Bash:")):
                return "BLOCKED", f"instant command open {_dur(p_age)} — awaiting approval? {what}"
            if live:
                quiet = time.time() - probe.last_change if probe.last_change else 0
                if p_age > th.stall_s and quiet > th.stall_s:
                    return "STALLED", f"running {_dur(p_age)}, no file change for {_dur(quiet)} — {probe.leaf_command()[:80]}"
                return "RUNNING", f"{_dur(p_age)} — {what}"
            if p_age > th.idle_s:  # no probe (replay): can't tell a long test from a prompt
                return "PENDING", f"tool open {_dur(p_age)} — {what}"
        return "RUNNING", f"{_dur(p_age)} — {what}"

    if age > th.idle_s:
        if orphans:
            return "ORPHANED", f"silent {_dur(age)}; orphaned process still running: {orphans[-1].command[:80]}"
        if live:
            return "WAITING", f"silent {_dur(age)}, waiting on background work: {probe.leaf_command()[:80]}"
        if handback is None and last.kind == "notification" and last.is_error:
            return "STOPPED", last.summary
        return "IDLE", f"silent {_dur(age)} with nothing running"
    last_call = next((e for e in reversed(events) if e.kind == "tool_use"), None)
    if last_call:
        return "WORKING", f"{last_call.tool}: {last_call.summary}"
    return "WORKING", last.summary


def _pending(events: list[Event]) -> Event | None:
    done = {e.tool_id for e in events if e.kind == "tool_result"}
    open_calls = [e for e in events if e.kind == "tool_use" and e.tool_id and e.tool_id not in done]
    return open_calls[0] if open_calls else None


def _final_text(events: list[Event]) -> str:
    for e in reversed(events):
        if e.kind == "tool_use":
            return ""
        if e.kind == "text" and e.final:
            return e.summary
    return ""


def _last_handback(events: list[Event]) -> Event | None:
    for e in reversed(events):
        if e.kind == "tool_use" and e.tool in HANDBACK_TOOLS:
            return e
    return None


# -- work done --------------------------------------------------------------


def _work_summary(st: Status, events: list[Event], agent: Agent) -> None:
    root = agent.workdir.rstrip("/")
    mentioned = brief_paths(agent.transcript.brief)
    seen = []
    for e in events:
        if e.kind == "tool_use" and e.tool in EDIT_TOOLS and e.path and e.path not in seen:
            seen.append(e.path)
        if e.kind == "tool_use" and is_commit(e):
            st.commits += 1
    st.edits = [_rel(p, root) for p in seen]
    st.off_brief = [
        r for p, r in zip(seen, st.edits) if p.startswith(root + "/") and mentioned and not on_brief(r, mentioned)
    ]


PATH_TOKEN_RE = re.compile(r"[\w.*-]+(?:/[\w.*-]+)+/?|[\w*-]+\.[A-Za-z]{1,6}\b")


def brief_paths(brief: str) -> set[str]:
    """File and directory names the brief mentions, normalised for matching."""
    out = set()
    for tok in PATH_TOKEN_RE.findall(brief):
        tok = tok.removeprefix("res://").removeprefix("./").rstrip(".,:;)")
        tok = re.sub(r":\d+.*$", "", tok)
        if tok and not tok.startswith(("http", "www.")):
            out.add(tok)
    return out


def on_brief(rel: str, mentioned: set[str]) -> bool:
    base = os.path.basename(rel)
    for m in mentioned:
        if rel == m or rel.endswith("/" + m) or base == m:
            return True
        if rel.startswith(m.rstrip("/") + "/"):
            return True
        if "*" in m and (fnmatch.fnmatch(rel, m) or fnmatch.fnmatch(base, m)):
            return True
    return False


# -- flags ------------------------------------------------------------------


def _flags(st, agent, session, events, at, probe, th) -> list[Flag]:
    flags: list[Flag] = []
    tool_uses = [e for e in events if e.kind == "tool_use"]
    root = agent.workdir.rstrip("/")

    loop = _loop(tool_uses[-th.loop_window :], th.loop_n)
    if loop and not st.finished:
        flags.append(Flag("LOOP", "bad", f"{loop[1]}× with no edit between: {loop[0][:120]}"))

    streak = 0
    for e in reversed([e for e in events if e.kind == "tool_result"]):
        if not e.is_error:
            break
        streak += 1
    if streak >= th.error_streak and not st.finished:
        flags.append(Flag("ERRORS", "warn", f"last {streak} tool calls failed"))

    if agent.worktree:
        project_root = _project_root(agent.worktree)
        outside = [
            e.path for e in tool_uses
            if e.tool in EDIT_TOOLS and e.path.startswith(project_root + "/") and not e.path.startswith(root + "/")
        ]
        if outside:
            flags.append(Flag("OUTSIDE", "bad", f"edited the main checkout, not its worktree: {', '.join(sorted(set(outside))[:3])}"))

    if st.off_brief:
        flags.append(Flag("OFF-BRIEF", "info", f"{len(st.off_brief)} file(s) the brief never names: {', '.join(st.off_brief[:4])}"))

    # Only the live probe: a transcript's gitBranch field does not follow the agent's worktree.
    branch = probe.branch if probe else ""
    if agent.own_branch and branch and branch != agent.own_branch and not st.finished:
        flags.append(Flag("BRANCH", "warn", f"on {branch}, expected {agent.own_branch}"))

    if agent.worktree:
        # A worktree branches from where the lead's session started. Once the lead has merged
        # something, an agent that edits before merging the base works on stale code.
        first_edit = next((e for e in tool_uses if e.tool in EDIT_TOOLS), None)
        if first_edit:
            lead_merge = session.lead_merge_before(first_edit.ts)
            merged = next((e for e in tool_uses if is_base_merge(e)), None)
            if lead_merge and (merged is None or merged.ts > first_edit.ts):
                flags.append(Flag("STALE-BASE", "warn", "edited before merging master, which had moved since its worktree was made"))

    last_commit = max((e.ts for e in tool_uses if is_commit(e)), default=None)
    first_unc = next((e for e in tool_uses if e.tool in EDIT_TOOLS and (last_commit is None or e.ts > last_commit)), None)
    if first_unc and not st.finished:
        unc = (at - first_unc.ts).total_seconds()
        if unc > th.uncommitted_s:
            flags.append(Flag("UNCOMMITTED", "warn", f"edits uncommitted for {_dur(unc)}"))

    if st.ctx >= th.ctx_bad:
        flags.append(Flag("CTX", "bad", f"context at {st.ctx // 1000}k tokens"))
    elif st.ctx >= th.ctx_warn:
        flags.append(Flag("CTX", "warn", f"context at {st.ctx // 1000}k tokens"))

    bg = [e for e in tool_uses if e.background]
    if bg:
        against = "foreground" in agent.transcript.brief.lower()
        flags.append(Flag(
            "BG",
            "warn" if against else "info",
            f"{len(bg)} background/detached launch(es)" + (" — its brief says run in the foreground" if against else ""),
        ))

    denied = [e for e in events if e.kind == "tool_result" and e.rejected]
    if denied:
        reasons = sorted({m for e in denied for m in DENIAL_REASON_RE.findall(e.summary)})
        why = f" ({', '.join(reasons)})" if reasons else ""
        recent = (at - denied[-1].ts).total_seconds() < 30 * 60
        flags.append(Flag("DENIED", "warn" if recent else "info", f"{len(denied)} tool call(s) denied{why}"))

    if probe and any(p.orphan for p in probe.procs):
        o = next(p for p in probe.procs if p.orphan)
        sev = "warn" if st.state in ("RUNNING", "WORKING", "WAITING") else "bad"
        flags.append(Flag("ORPHAN", sev, f"detached pid {o.pid} running {o.etime}: {o.command[:90]}"))

    if st.finished:  # history, not something to act on — except leftovers in the worktree
        for f in flags:
            if f.code not in ("OUTSIDE", "ORPHAN"):
                f.severity = "info"
    return flags


def _loop(tool_uses: list[Event], n: int) -> tuple[str, int] | None:
    counts: dict[str, int] = {}
    worst = None
    for e in tool_uses:
        if e.tool in EDIT_TOOLS:  # an edit breaks the loop: only a run since the last edit counts
            counts.clear()
            worst = None
            continue
        if not e.key:
            continue
        counts[e.key] = counts.get(e.key, 0) + 1
        if counts[e.key] >= n and (worst is None or counts[e.key] > worst[1]):
            worst = (e.summary, counts[e.key])
    return worst


# -- helpers ----------------------------------------------------------------


def _project_root(worktree: str) -> str:
    marker = "/.claude/worktrees/"
    return worktree.split(marker)[0] if marker in worktree else os.path.dirname(worktree)


def _rel(path: str, root: str) -> str:
    return path[len(root) + 1 :] if root and path.startswith(root + "/") else path


def _seconds(n: float):
    from datetime import timedelta

    return timedelta(seconds=n)


def _dur(s: float) -> str:
    s = int(max(s, 0))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


fmt_duration = _dur
