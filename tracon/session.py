"""Find Claude Code sessions with subagents and keep their transcripts current."""

from __future__ import annotations

import glob
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime

from .transcript import Event, Transcript, iter_until

PROJECTS_ROOT = os.path.expanduser("~/.claude/projects")


def encode_project(path: str) -> str:
    """Claude Code names a project's folder after its path with every non-alphanumeric as '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(os.path.expanduser(path)))


@dataclass
class SessionRef:
    project_dir: str
    session_id: str
    mtime: float
    n_agents: int

    @property
    def project_name(self) -> str:
        return os.path.basename(self.project_dir)


def find_sessions(project: str | None = None, root: str = PROJECTS_ROOT) -> list[SessionRef]:
    """Sessions that spawned subagents, most recently active first."""
    pattern = os.path.join(root, encode_project(project) if project else "*", "*", "subagents")
    refs = []
    for sub in glob.glob(pattern):
        files = glob.glob(os.path.join(sub, "*.jsonl"))
        if not files:
            continue
        session_dir = os.path.dirname(sub)
        lead = session_dir + ".jsonl"
        mtimes = [os.path.getmtime(f) for f in files]
        if os.path.exists(lead):
            mtimes.append(os.path.getmtime(lead))
        refs.append(SessionRef(os.path.dirname(session_dir), os.path.basename(session_dir), max(mtimes), len(files)))
    return sorted(refs, key=lambda r: r.mtime, reverse=True)


@dataclass
class Agent:
    id: str
    meta: dict
    transcript: Transcript

    @property
    def description(self) -> str:
        return self.meta.get("description") or self.id

    @property
    def model(self) -> str:
        """'opus', 'sonnet', ... from the spawn metadata, else from the transcript itself."""
        m = self.meta.get("model") or self.transcript.model
        for family in ("opus", "sonnet", "haiku", "fable", "mythos"):
            if family in m:
                return family
        return m.removeprefix("claude-") or "?"

    @property
    def worktree(self) -> str:
        return self.meta.get("worktreePath") or ""

    @property
    def own_branch(self) -> str:
        return self.meta.get("worktreeBranch") or ""

    @property
    def workdir(self) -> str:
        return self.worktree or self.transcript.cwd

    def started(self) -> datetime | None:
        ev = self.transcript.events
        return ev[0].ts if ev else None

    def exists_at(self, now: datetime | None) -> bool:
        s = self.started()
        return now is None or (s is not None and s <= now)


@dataclass
class Session:
    ref: SessionRef
    lead: Transcript = field(init=False)
    agents: dict[str, Agent] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.lead = Transcript(os.path.join(self.ref.project_dir, self.ref.session_id + ".jsonl"))

    @property
    def subagent_dir(self) -> str:
        return os.path.join(self.ref.project_dir, self.ref.session_id, "subagents")

    def refresh(self) -> bool:
        changed = self.lead.refresh()
        for path in glob.glob(os.path.join(self.subagent_dir, "agent-*.jsonl")):
            aid = os.path.basename(path)[len("agent-") : -len(".jsonl")]
            agent = self.agents.get(aid)
            if agent is None:
                agent = Agent(aid, _read_meta(path), Transcript(path))
                self.agents[aid] = agent
                changed = True
            elif not agent.meta:
                agent.meta = _read_meta(path)
            changed = agent.transcript.refresh() or changed
        return changed

    # -- what the lead did ------------------------------------------------

    def visible_agents(self, now: datetime | None = None) -> list[Agent]:
        return [a for a in self.agents.values() if a.exists_at(now)]

    def lane(self) -> str:
        """A name for the run: the arguments of the lead's latest slash command (e.g. `/expedite M4`),
        else Claude Code's generated session title."""
        return next((args for _, _, args in reversed(self.lead.commands) if args), "") or self.lead.title

    def title(self) -> str:
        """What Claude Code calls this session in the terminal tab: the /rename name, else the
        generated title, else the lane, else the project folder."""
        return self.lead.name or self.lead.title or self.lane() or os.path.basename(self.lead.cwd or self.ref.project_name)

    def task_note(self, agent_id: str, now: datetime | None = None) -> Event | None:
        """The lead's latest notification that this agent stopped (completed/failed/killed/stopped)."""
        hit = None
        for e in iter_until(self.lead.events, now):
            if e.kind == "task_note" and e.extra and e.extra.get("task_id") == agent_id:
                hit = e
        return hit

    def lead_messages(self, agent_id: str, now: datetime | None = None) -> list[Event]:
        return [
            e
            for e in iter_until(self.lead.events, now)
            if e.tool == "SendMessage" and e.extra and e.extra.get("to") == agent_id
        ]

    def stopped_at(self, agent_id: str, now: datetime | None = None) -> datetime | None:
        for e in iter_until(self.lead.events, now):
            if e.tool == "TaskStop" and e.extra and e.extra.get("task_id") == agent_id:
                return e.ts
        return None

    def lead_merge_before(self, ts: datetime) -> Event | None:
        """The lead's last `git merge` of an agent branch before `ts`."""
        hit = None
        for e in self.lead.events:
            if e.ts >= ts:
                break
            if e.tool == "Bash" and re.search(r"git merge\b[^\n]*worktree-agent-", e.key):
                hit = e
        return hit

    def span(self) -> tuple[datetime, datetime] | None:
        ts = [a.transcript.events[0].ts for a in self.agents.values() if a.transcript.events]
        te = [a.transcript.events[-1].ts for a in self.agents.values() if a.transcript.events]
        if self.lead.events:
            ts.append(self.lead.events[0].ts)
            te.append(self.lead.events[-1].ts)
        return (min(ts), max(te)) if ts else None


def _read_meta(jsonl_path: str) -> dict:
    try:
        with open(jsonl_path[: -len(".jsonl")] + ".meta.json") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}
