"""Everything about a lane at one moment: per-agent status plus what needs several agents to see
(file collisions) and the judge's read. The TUI, `--once` and the web view all use this."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .judge import Judge
from .probe import WorktreeProbe
from .session import Agent, Session
from .signals import Flag, Status, Thresholds, assess
from .transcript import EDIT_TOOLS, iter_until

JUDGE_FLAGS = {"drifting": ("DRIFT", "warn"), "stuck": ("STUCK", "bad")}


@dataclass
class FileCell:
    edits: int = 0
    reads: int = 0
    first_edit: datetime | None = None
    last_edit: datetime | None = None


@dataclass
class FileMatrix:
    """Which agent touched which file, keyed by path relative to its worktree (or the project)."""

    agents: list[Agent]
    files: list[str]
    cells: dict[tuple[str, str], FileCell] = field(default_factory=dict)  # (file, agent_id)
    collisions: dict[str, list[tuple[str, list[str]]]] = field(default_factory=dict)  # agent -> [(file, others)]

    def editors(self, path: str) -> list[str]:
        return [a.id for a in self.agents if (c := self.cells.get((path, a.id))) and c.edits]


def rel_to_agent(path: str, agent: Agent) -> str | None:
    """Path inside the agent's worktree (or the main checkout), else None (scratch files etc.)."""
    for root in filter(None, (agent.worktree, _project_root(agent))):
        root = root.rstrip("/") + "/"
        if path.startswith(root):
            return path[len(root):]
    return None


def _project_root(agent: Agent) -> str:
    wt = agent.worktree or agent.transcript.cwd
    return wt.split("/.claude/worktrees/")[0] if "/.claude/worktrees/" in wt else wt


def file_matrix(session: Session, now: datetime | None, statuses: dict[str, Status] | None = None) -> FileMatrix:
    agents = sorted(session.visible_agents(now), key=lambda a: a.started() or datetime.max)
    cells: dict[tuple[str, str], FileCell] = {}
    life: dict[str, tuple[datetime, datetime]] = {}
    for a in agents:
        events = list(iter_until(a.transcript.events, now))
        if not events:
            continue
        life[a.id] = (events[0].ts, events[-1].ts)
        for e in events:
            if e.kind != "tool_use" or e.tool not in EDIT_TOOLS | {"Read"}:
                continue
            rel = rel_to_agent(e.path, a) if e.path else None
            if not rel:
                continue
            c = cells.setdefault((rel, a.id), FileCell())
            if e.tool in EDIT_TOOLS:
                c.edits += 1
                c.first_edit = c.first_edit or e.ts
                c.last_edit = e.ts
            else:
                c.reads += 1
    files = sorted(
        {f for f, _ in cells},
        key=lambda f: (-sum(1 for a in agents if (c := cells.get((f, a.id))) and c.edits), f),
    )
    m = FileMatrix(agents, files, cells)
    # A collision: two agents edited the same file while both were alive (overlapping lifetimes),
    # so their branches change it independently and the second merge may conflict.
    for f in files:
        eds = m.editors(f)
        for aid in eds:
            others = [o for o in eds if o != aid and _overlap(life.get(aid), life.get(o))]
            if others:
                m.collisions.setdefault(aid, []).append((f, others))
    return m


def _overlap(a, b) -> bool:
    return bool(a and b and a[0] <= b[1] and b[0] <= a[1])


def assess_all(
    session: Session,
    now: datetime | None = None,
    probes: dict[str, WorktreeProbe] | None = None,
    th: Thresholds | None = None,
    judge: Judge | None = None,
) -> tuple[dict[str, Status], FileMatrix]:
    probes = probes or {}
    statuses = {a.id: assess(a, session, now, probes.get(a.worktree), th) for a in session.visible_agents(now)}
    matrix = file_matrix(session, now)
    short = {a.id: card_label(a) for a in matrix.agents}
    for aid, hits in matrix.collisions.items():
        st = statuses.get(aid)
        if not st:
            continue
        live_other = any(not statuses[o].finished for _, others in hits for o in others if o in statuses)
        sev = "info" if st.finished else ("warn" if live_other else "info")
        desc = "; ".join(f"{f} (also {', '.join(short.get(o, o) for o in others)})" for f, others in hits[:3])
        st.flags.append(Flag("COLLIDE", sev, f"{len(hits)} file(s) edited concurrently by another agent: {desc}"))
    if judge:
        for aid, st in statuses.items():
            j = judge.latest(aid, now)
            st.judgment = j
            st.dismissed = bool(j and judge.is_dismissed(aid, j))
            if j and j.verdict in JUDGE_FLAGS and not st.finished and not st.dismissed:
                code, sev = JUDGE_FLAGS[j.verdict]
                st.flags.append(Flag(code, sev, j.concern or j.doing))
    return statuses, matrix


def card_label(agent: Agent) -> str:
    """'#749' when the description starts with a card number, else its first word."""
    d = agent.description.strip()
    head = d.split()[0] if d else agent.id[:6]
    return head if head.startswith("#") else head[:8]
