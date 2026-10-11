"""Find Claude Code sessions with subagents and keep their transcripts current."""

from __future__ import annotations

import glob
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime

from . import ghostty
from .inbox import SESSIONS_DIR, Live, live_sessions
from .transcript import Event, Transcript, iter_until, read_title

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
        """The project folder's encoded name, less the encoded home directory. Lossy (both '/' and
        '-' encode as '-'), so views prefer the lead's cwd when the transcript has one."""
        name = os.path.basename(self.project_dir)
        home = encode_project(os.path.expanduser("~")) + "-"
        return name[len(home):] if name.startswith(home) else name.lstrip("-")


def find_sessions(project: str | None = None, root: str = PROJECTS_ROOT) -> list[SessionRef]:
    """Sessions that spawned subagents, most recently active first."""
    pattern = os.path.join(root, encode_project(project) if project else "*", "*", "subagents")
    refs = [_ref(os.path.dirname(sub)) for sub in glob.glob(pattern)]
    return sorted((r for r in refs if r.n_agents), key=lambda r: r.mtime, reverse=True)


def find_sessions_near(path: str, root: str = PROJECTS_ROOT) -> list[SessionRef]:
    """Sessions of the project at `path`, or of the nearest folder above it that has any."""
    path = os.path.abspath(os.path.expanduser(path))
    while True:
        refs = find_sessions(path, root)
        parent = os.path.dirname(path)
        if refs or parent == path or parent == "/":
            return refs
        path = parent


def lead_path(session_id: str, cwd: str = "", root: str = PROJECTS_ROOT) -> str | None:
    """The lead transcript of `session_id`; `cwd` (where it was started) saves a search."""
    if cwd:
        path = os.path.join(root, encode_project(cwd), session_id + ".jsonl")
        if os.path.exists(path):
            return path
    return next(iter(glob.glob(os.path.join(root, "*", session_id + ".jsonl"))), None)


def session_ref(session_id: str, cwd: str = "", root: str = PROJECTS_ROOT) -> SessionRef | None:
    """A session by id, whether or not it has spawned subagents yet."""
    path = lead_path(session_id, cwd, root)
    return _ref(path[: -len(".jsonl")]) if path else None


def _ref(session_dir: str) -> SessionRef:
    files = glob.glob(os.path.join(session_dir, "subagents", "*.jsonl"))
    mtimes = [os.path.getmtime(f) for f in files + [session_dir + ".jsonl"] if os.path.exists(f)]
    return SessionRef(os.path.dirname(session_dir), os.path.basename(session_dir), max(mtimes, default=0.0), len(files))


@dataclass
class Chooser:
    """Picks the session to watch when none was named: the Claude Code session in a split next to
    tracon (Ghostty only), else the newest with subagents in this project or the nearest parent
    folder that has one, else the newest anywhere. `refs()` is asked again while tracon follows."""

    cwd: str = ""
    project: str | None = None  # --project: only this project, and no split matching
    terminal: str | None = None  # tracon's own Ghostty terminal, from ghostty.find_self()
    ignore_prefix: str = ""  # titles of other tracon panes
    root: str = PROJECTS_ROOT
    sessions_root: str = SESSIONS_DIR
    neighbour: str | None = None  # session id found in the split at the last refs()
    _titles: dict[str, tuple[int, str]] = field(default_factory=dict)  # path -> (size, title)

    def refs(self) -> list[SessionRef]:
        if self.project:
            refs = find_sessions(self.project, self.root)
        else:
            refs = find_sessions_near(self.cwd, self.root) if self.cwd else []
            refs = refs or find_sessions(root=self.root)
        nb = self._neighbour()
        self.neighbour = nb.session_id if nb else None
        if nb:
            refs = [nb] + [r for r in refs if r.session_id != nb.session_id]
        return refs

    def _neighbour(self) -> SessionRef | None:
        if not self.terminal or self.project:
            return None
        panes = ghostty.panes()
        if not panes:
            return None
        live = live_sessions(self.sessions_root)
        sid = ghostty.pick_neighbour(self.terminal, panes, live, self._title, self.ignore_prefix)
        lv = next((lv for lv in live if lv.session_id == sid), None)
        return session_ref(lv.session_id, lv.cwd, self.root) if lv else None

    def _title(self, lv: Live) -> str:
        path = lead_path(lv.session_id, lv.cwd, self.root)
        try:
            size = os.path.getsize(path or "")
        except OSError:
            return ""
        cached = self._titles.get(path)
        if cached is None or cached[0] != size:
            cached = self._titles[path] = (size, read_title(path))
        return cached[1]


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

    def shorten(self, text: str) -> str:
        """`text` with this agent's own worktree path written as `<worktree>`."""
        wd = self.workdir.rstrip("/")
        return text.replace(wd, "<worktree>") if wd else text

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
