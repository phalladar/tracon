"""Textual UI: agent table, live preview, per-agent detail, lane timeline, file heatmap."""

from __future__ import annotations

import os
import re
import subprocess
import webbrowser
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, RichLog, Static, TextArea

from . import inbox
from .advice import compose
from .inbox import Inbox
from .judge import Judge, Judgment
from .lane import FileMatrix, assess_all, card_label
from .probe import Prober, WorktreeProbe
from .session import Agent, Session, SessionRef, find_sessions
from .signals import Status, Thresholds, _pending, fmt_duration
from .transcript import EDIT_TOOLS, HANDBACK_TOOLS, Event, iter_until

# Status colours (good / warning / serious / critical) are kept apart from the activity colours,
# which are the first three categorical slots, so a state never reads as an activity.
GOOD, WARNING, SERIOUS, CRITICAL, MUTED = "#0ca30c", "#fab219", "#ec835a", "#d03b3b", "#898781"
SEV_STYLE = {"bad": f"bold {CRITICAL}", "warn": WARNING, "ok": GOOD, "done": "grey50", "info": "grey58"}
TOOL_STYLE = {"edit": "#3987e5", "bash": "#d95926", "read": "#199e70", "other": MUTED}
VERDICT_CELL = {
    "on_track": ("✓ on track", GOOD),
    "drifting": ("↯ drifting", WARNING),
    "stuck": ("⟳ stuck", CRITICAL),
    "unclear": ("? unclear", MUTED),
}
NOTIFY_STATES = {"BLOCKED", "IDLE", "STALLED", "ORPHANED"}
NOTIFY_FLAGS = {"DRIFT", "STUCK", "COLLIDE"}  # notify on these even though they are warnings
CTX_SCALE = 500_000
LEAD_ID = "__lead__"
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
FEED_LINES = 600


def tool_kind(tool: str) -> str:
    if tool in EDIT_TOOLS:
        return "edit"
    if tool == "Bash":
        return "bash"
    if tool in ("Read", "Grep", "Glob", "LS"):
        return "read"
    return "other"


def local(ts: datetime, fmt: str = "%H:%M:%S") -> str:
    return ts.astimezone().strftime(fmt)


def short_path(path: str) -> str:
    if "/scratchpad/" in path:
        return "scratchpad/" + path.split("/scratchpad/", 1)[1]
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home + "/") else path


def ctx_bar(ctx: int, th: Thresholds) -> Text:
    cells = 6
    filled = min(cells, round(ctx / CTX_SCALE * cells))
    style = "red" if ctx >= th.ctx_bad else "yellow" if ctx >= th.ctx_warn else "green"
    t = Text("▕", style="grey35")
    t.append("█" * filled, style=style)
    t.append("░" * (cells - filled), style="grey30")
    t.append("▏", style="grey35")
    t.append(f"{ctx // 1000:>4}k", style=style if ctx >= th.ctx_warn else "")
    return t


def flags_text(st: Status) -> Text:
    """Table cell: what needs attention. CONTEXT has its own column; history stays in detail."""
    t = Text()
    for f in st.flags:
        if f.code == "CTX" or (st.finished and f.severity == "info"):
            continue
        if t:
            t.append(" ")
        t.append(f.code, style=SEV_STYLE[f.severity])
    return t


def verdict_text(st: Status, judge: Judge | None) -> Text:
    if judge and st.agent.id in judge.in_flight:
        return Text("… judging", style=MUTED)
    j: Judgment | None = st.judgment  # type: ignore[assignment]
    if not j:
        return Text("")
    label, color = VERDICT_CELL.get(j.verdict, ("?", MUTED))
    return Text(label, style="grey50" if st.finished else color)


def event_line(e: Event) -> Text | None:
    t = Text(local(e.ts) + " ", style="grey50")
    if e.kind == "tool_use":
        if e.tool in HANDBACK_TOOLS:
            t.append("✓ REPORT ", style="bold green")
            t.append(e.summary)
            return t
        t.append(f"{e.tool:<6} ", style=TOOL_STYLE[tool_kind(e.tool)])
        t.append(e.summary)
        if e.background:
            t.append("  [background]", style="yellow")
        return t
    if e.kind == "tool_result":
        if not e.is_error:
            return None
        t.append("  ✗ ", style="red")
        t.append(("denied: " if e.rejected else "") + e.summary[:160], style="red")
        return t
    if e.kind == "text":
        t.append("  » " + e.summary, style="italic grey70")
        return t
    if e.kind == "coordinator":
        t.append("◆ lead: ", style="bold magenta")
        t.append(e.summary, style="magenta")
        return t
    if e.kind == "brief":
        t.append("brief: ", style="bold")
        t.append(e.summary, style="grey70")
        return t
    t.append(e.summary, style="red" if e.is_error else "grey50")
    return t


def lead_line(e: Event) -> Text:
    t = Text(local(e.ts) + " ", style="grey50")
    t.append("◆ lead → ", style="bold magenta")
    t.append((e.extra or {}).get("summary") or (e.extra or {}).get("message", "")[:140], style="magenta")
    return t


def feed(agent: Agent, session: Session, at: datetime | None) -> list[Text]:
    """The agent's own events, with the lead's messages to it merged in by time."""
    items = [(e.ts, event_line(e)) for e in iter_until(agent.transcript.events, at)]
    items += [(e.ts, lead_line(e)) for e in session.lead_messages(agent.id, at)]
    return [line for _, line in sorted(items, key=lambda x: x[0]) if line is not None]


def notify(title: str, body: str) -> None:
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    subprocess.Popen(
        ["osascript", "-e", f'display notification "{esc(body[:200])}" with title "{esc(title)}" sound name "Ping"'],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# -- screens ----------------------------------------------------------------


class DetailScreen(Screen):
    BINDINGS = [
        Binding("escape,q", "app.pop_screen", "Back"),
        Binding("b", "toggle_brief", "Brief"),
        Binding("o", "app.open_worktree", "Open in Ghostty"),
        Binding("c", "app.copy_path", "Copy path"),
        Binding("m", "app.message_lead", "Message lead"),
    ]

    def __init__(self, agent_id: str) -> None:
        super().__init__()
        self.agent_id = agent_id
        self.show_brief = False
        self._fed = 0

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="detail-top"):
            yield Static(id="detail-head")
        with VerticalScroll(id="detail-feed"):
            yield Static(id="detail-feed-text")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_view()
        self.call_after_refresh(self.query_one("#detail-feed", VerticalScroll).scroll_end, animate=False)

    def action_toggle_brief(self) -> None:
        self.show_brief = not self.show_brief
        self.refresh_view()

    def refresh_view(self) -> None:
        app: LaneApp = self.app  # type: ignore[assignment]
        agent = app.session.agents.get(self.agent_id)
        st = app.statuses.get(self.agent_id)
        if agent is None or st is None:
            return
        self.query_one("#detail-head", Static).update(self._head(agent, st, app))
        # A Static reflows on resize, unlike RichLog lines, which keep the width they were written at.
        scroller = self.query_one("#detail-feed", VerticalScroll)
        at_end = scroller.scroll_y >= scroller.max_scroll_y - 1
        lines = feed(agent, app.session, app.at)[-FEED_LINES:]
        self.query_one("#detail-feed-text", Static).update(Text("\n").join(lines))
        if at_end and len(lines) != self._fed:
            self.call_after_refresh(scroller.scroll_end, animate=False)
        self._fed = len(lines)

    def _head(self, agent: Agent, st: Status, app: "LaneApp") -> Text:
        t = Text()
        t.append(f"{st.icon} {agent.description}", style="bold")
        t.append(f"   {agent.model}   {agent.id}\n", style="grey58")
        t.append(f"{st.state} ", style=SEV_STYLE[st.severity])
        t.append(st.detail + "\n")
        t.append(
            f"running {fmt_duration(st.run_s)} · quiet {fmt_duration(st.age_s)} · {st.commits} commits · "
            f"{len(st.edits)} files edited · {st.lead_msgs} lead messages · ctx ",
            style="grey70",
        )
        t.append_text(ctx_bar(st.ctx, app.th))
        t.append("\n")
        if agent.worktree:
            t.append(f"worktree {short_path(agent.worktree)}\n", style="grey58")
        p = st.probe
        if p:
            if not p.exists:
                t.append("worktree removed\n", style="grey50")
            else:
                ahead = f"{p.ahead} ahead" if p.ahead is not None else ""
                t.append(f"git {p.branch or '?'} @ {p.head} · {ahead} · {p.dirty} uncommitted paths\n", style="grey70")
                for pr in p.procs:
                    t.append("  ▶ " if not pr.orphan else "  👻 ", style="yellow")
                    t.append(f"{pr.pid} {pr.etime}  {pr.command[:150]}\n", style="grey70")
        j = st.judgment
        if j:
            label, color = VERDICT_CELL.get(j.verdict, ("?", MUTED))
            t.append("judge ", style="grey58")
            t.append(label, style=f"bold {color}")
            t.append(f"  {j.doing}", style="")
            t.append(f"   as of {local(j.as_of_dt, '%H:%M')}, {j.n_calls} calls\n", style="grey50")
            if j.concern:
                t.append(f"      {j.concern}\n", style=color)
            changes = _verdict_changes(app.judge, agent.id, app.at) if app.judge else []
            if len(changes) > 1:
                t.append("      history: ", style="grey50")
                t.append("  ".join(f"{local(x.as_of_dt, '%H:%M')} {x.verdict}" for x in changes[-8:]) + "\n", style="grey50")
        for f in st.flags:
            t.append(f"  {f.code:<12}", style=SEV_STYLE[f.severity])
            t.append(f.detail + "\n")
        if st.edits:
            t.append("files: ", style="grey58")
            for i, path in enumerate(st.edits):
                off = path in st.off_brief
                t.append(("  " if i else "") + short_path(path) + ("⚑" if off else ""), style="yellow" if off else "green")
            t.append("\n")
        if self.show_brief and agent.transcript.brief:
            t.append("\n── brief ──\n", style="bold")
            t.append(agent.transcript.brief + "\n", style="grey70")
        return t


def _verdict_changes(judge: Judge, agent_id: str, at: datetime | None) -> list[Judgment]:
    out: list[Judgment] = []
    for j in judge.history.get(agent_id, []):
        if j.error or (at is not None and j.as_of_dt > at):
            continue
        if not out or out[-1].verdict != j.verdict:
            out.append(j)
    return out


class FilesScreen(Screen):
    """Heatmap: files down, agents across. Shows where agents' changes will meet at merge."""

    BINDINGS = [Binding("escape,q,x", "app.pop_screen", "Back"), Binding("a", "toggle_all", "Shared/all")]

    def __init__(self) -> None:
        super().__init__()
        self.show_all = False

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(id="files")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_view()

    def action_toggle_all(self) -> None:
        self.show_all = not self.show_all
        self.refresh_view()

    def refresh_view(self) -> None:
        app: LaneApp = self.app  # type: ignore[assignment]
        self.query_one("#files", Static).update(render_matrix(app.matrix, app.statuses, self.show_all, self.size.width))


# Sequential blue, dark-surface steps: more edits, brighter.
EDIT_RAMP = [(1, "#184f95"), (2, "#256abf"), (4, "#3987e5"), (10, "#86b6ef")]


def render_matrix(m: FileMatrix, statuses: dict[str, Status], show_all: bool, width: int) -> Text:
    agents = [a for a in m.agents if any(m.cells.get((f, a.id)) for f in m.files)]
    files = [f for f in m.files if len(m.editors(f)) >= (1 if show_all else 2)]
    t = Text()
    t.append("Files edited by more than one agent" if not show_all else "Every file an agent edited", style="bold")
    t.append(f"   {len(files)} shown · press a for {'shared only' if show_all else 'all'}\n\n", style="grey58")
    if not files:
        t.append("No file was edited by two agents.\n", style="grey58")
        return t
    col = 6
    path_w = max(20, min(60, width - col * len(agents) - 4))
    labels = [card_label(a)[: col - 1] for a in agents]
    t.append(" " * path_w)
    for a, lab in zip(agents, labels):
        st = statuses.get(a.id)
        t.append(f"{lab:<{col}}", style="grey50" if st and st.finished else "bold")
    t.append("\n")
    colliding = {(f, aid) for aid, hits in m.collisions.items() for f, _ in hits}
    for f in files:
        shown = f if len(f) <= path_w - 1 else "…" + f[-(path_w - 2):]
        t.append(f"{shown:<{path_w}}", style="")
        for a in agents:
            c = m.cells.get((f, a.id))
            if not c:
                t.append(" " * col)
            elif c.edits:
                color = next(clr for n, clr in reversed(EDIT_RAMP) if c.edits >= n)
                t.append("████", style=color)
                t.append("!" if (f, a.id) in colliding else " ", style=f"bold {CRITICAL}")
                t.append(" ")
            else:
                t.append("··" + " " * (col - 2), style=MUTED)
        t.append("\n")
    t.append("\nedits ", style="grey58")
    for n, clr in EDIT_RAMP:
        t.append("██", style=clr)
        t.append(f" {n}+  ", style="grey58")
    t.append("·· read only   ", style="grey58")
    t.append("!", style=f"bold {CRITICAL}")
    t.append(" edited while another agent was editing it too (merge-conflict risk)\n", style="grey58")
    return t


class TimelineScreen(Screen):
    """One row per agent across the lane's wall-clock span, coloured by what it was doing."""

    BINDINGS = [Binding("escape,q,t", "app.pop_screen", "Back")]

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(id="timeline")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_view()

    def on_resize(self) -> None:
        self.refresh_view()

    def refresh_view(self) -> None:
        app: LaneApp = self.app  # type: ignore[assignment]
        self.query_one("#timeline", Static).update(self.render_timeline(app))

    def render_timeline(self, app: "LaneApp") -> Text:
        session, at = app.session, app.at
        span = session.span()
        if not span:
            return Text("no activity yet")
        start = span[0]
        end = at or max(span[1], datetime.now(timezone.utc))
        label_w = 30
        width = max(20, self.size.width - label_w - 4)
        bucket = (end - start) / width
        t = Text()
        # Axis: a time label every ~12 columns.
        axis = [" "] * width
        step = max(1, width // 8)
        for i in range(0, width - 5, step):
            for j, ch in enumerate(local(start + bucket * i, "%H:%M")):
                axis[i + j] = ch
        t.append(" " * label_w + "".join(axis) + "\n", style="grey58")
        # Lead row: merges and messages.
        lead = [" "] * width
        for e in iter_until(session.lead.events, at):
            i = min(width - 1, int((e.ts - start) / bucket))
            if e.tool == "Bash" and "git merge" in e.key and "worktree-agent-" in e.key:
                lead[i] = "▼"
            elif e.tool == "SendMessage" and lead[i] == " ":
                lead[i] = "◆"
            elif e.tool in ("Agent", "Task") and lead[i] == " ":
                lead[i] = "+"
        t.append(f"{'lead (▼ merge ◆ msg + spawn)':<{label_w}}", style="bold magenta")
        t.append("".join(lead) + "\n", style="magenta")
        agents = sorted(session.visible_agents(at), key=lambda a: a.started() or start)
        for agent in agents:
            st = app.statuses.get(agent.id)
            label = f"{(st.icon if st else ' ')} {agent.description}"[: label_w - 1]
            t.append(f"{label:<{label_w}}", style=SEV_STYLE[st.severity] if st else "")
            t.append_text(self._row(agent, start, bucket, width, at, app.th))
            t.append("\n")
        t.append(
            "\n█ edit  ▓ shell  ▒ read/search  ░ other  · silent past the idle threshold  ✓ reported  ■ stopped\n",
            style="grey58",
        )
        return t

    def _row(self, agent: Agent, start, bucket, width, at, th: Thresholds) -> Text:
        cells: list[tuple[str, str]] = [(" ", "")] * width
        rank = {"edit": 4, "bash": 3, "read": 2, "other": 1}
        best = [0] * width
        events = list(iter_until(agent.transcript.events, at))
        if not events:
            return Text(" " * width)
        for e in events:
            i = min(width - 1, max(0, int((e.ts - start) / bucket)))
            if e.kind == "tool_use":
                if e.tool in HANDBACK_TOOLS:
                    cells[i], best[i] = ("✓", f"bold {GOOD}"), 9
                    continue
                k = tool_kind(e.tool)
                if rank[k] > best[i]:
                    best[i] = rank[k]
                    cells[i] = ({"edit": "█", "bash": "▓", "read": "▒", "other": "░"}[k], TOOL_STYLE[k])
            elif e.kind in ("text", "coordinator") and best[i] == 0:
                best[i], cells[i] = 1, ("░", MUTED)
        # Silent stretches past the idle threshold inside the agent's life.
        idle = timedelta(seconds=th.idle_s)
        for a, b in zip(events, events[1:]):
            if b.ts - a.ts > idle:
                i0 = int((a.ts + idle - start) / bucket)
                i1 = int((b.ts - start) / bucket)
                for i in range(max(0, i0), min(width, i1)):
                    if best[i] == 0:
                        cells[i] = ("·", SERIOUS)
        t = Text()
        for ch, style in cells:
            t.append(ch, style=style)
        return t


# -- main app ---------------------------------------------------------------


class MessageScreen(ModalScreen):
    """Preview and edit a note to the lead about one agent, then send it to the lead's inbox."""

    BINDINGS = [Binding("ctrl+s", "send", "Send"), Binding("escape", "app.pop_screen", "Cancel")]
    CSS = """
    MessageScreen { align: center middle; }
    #msg-box { width: 90%; max-width: 120; height: 80%; border: round $primary; background: $surface; padding: 0 1; }
    #msg-to { height: auto; }
    #msg-text { height: 1fr; }
    """

    def __init__(self, text: str, inbox: Inbox | None) -> None:
        super().__init__()
        self.text, self.inbox = text, inbox

    def compose(self) -> ComposeResult:
        with Vertical(id="msg-box"):
            yield Static(self._to(), id="msg-to")
            yield TextArea(self.text, id="msg-text", soft_wrap=True)
        yield Footer()

    def _to(self) -> Text:
        t = Text()
        if self.inbox:
            t.append("to the lead ", style="bold")
            t.append(f"{self.inbox.name or 'session'} · pid {self.inbox.pid} · {self.inbox.status or '?'}", style="cyan")
            t.append("  ctrl+s sends · esc cancels", style="grey58")
        else:
            t.append("no live inbox for this session", style=f"bold {WARNING}")
            t.append("  ctrl+s copies to the clipboard · esc cancels", style="grey58")
        return t

    def action_send(self) -> None:
        text = self.query_one(TextArea).text.strip()
        app: LaneApp = self.app  # type: ignore[assignment]
        self.dismiss()
        if not text:
            return
        if self.inbox:
            app.send_message(self.inbox.socket, text)
        else:
            subprocess.run(["pbcopy"], input=text, text=True)
            app.notify("copied the message; paste it into the lead's session", timeout=4)


class LaneApp(App):
    TITLE = "tracon"
    CSS = """
    #header { height: auto; padding: 0 1; background: $boost; }
    #agents { height: 1fr; }
    #preview { height: 12; border-top: solid $primary-darken-2; padding: 0 1; }
    #detail-top { height: auto; max-height: 50%; padding: 0 1; border-bottom: solid $primary-darken-2; }
    #detail-feed { height: 1fr; padding: 0 1; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("enter", "detail", "Detail", show=True),
        Binding("t", "timeline", "Timeline"),
        Binding("x", "files", "Files"),
        Binding("w", "web", "Web"),
        Binding("j", "judge_now", "Judge"),
        Binding("m", "message_lead", "Message lead"),
        Binding("h", "toggle_done", "Hide done"),
        Binding("o", "open_worktree", "Ghostty"),
        Binding("c", "copy_path", "Copy path"),
        Binding("s", "next_session", "Session"),
        Binding("left_square_bracket", "step(-5)", "-5m"),
        Binding("right_square_bracket", "step(5)", "+5m"),
        Binding("left_curly_bracket", "step(-30)", "-30m", show=False),
        Binding("right_curly_bracket", "step(30)", "+30m", show=False),
        Binding("space", "play", "Play"),
        Binding("l", "live", "Live"),
    ]

    def __init__(
        self,
        refs: list[SessionRef],
        at: datetime | None,
        thresholds: Thresholds,
        notify: bool,
        judge: Judge | None = None,
        web_port: int = 8765,
        title_prefix: str | None = "◎ ",
        prober: Prober | None = None,
    ) -> None:
        super().__init__()
        self.title_prefix = title_prefix
        self._term_title: str | None = None
        self.judge = judge
        self.matrix = FileMatrix([], [])
        self.web_port = web_port
        self.web_url: str | None = None
        self.refs = refs
        self.session = Session(refs[0])
        self.at = at
        self.th = thresholds
        self.notify_enabled = notify
        self.hide_done = False
        self.playing = False
        self.statuses: dict[str, Status] = {}
        self.probes: dict[str, WorktreeProbe] = {}
        self.prober = prober or Prober()
        self._order: list[str] = []
        self._seen: dict[str, tuple[str, frozenset]] | None = None

    def compose(self) -> ComposeResult:
        yield Static(id="header")
        yield DataTable(id="agents", cursor_type="row", zebra_stripes=False)
        yield RichLog(id="preview", wrap=False, markup=False, max_lines=200)
        yield Footer()

    def on_mount(self) -> None:
        if self.title_prefix is not None:
            self._terminal_write("\x1b[22;2t")  # save the terminal's title; cli restores it on exit
        table = self.query_one(DataTable)
        for label, key in [
            ("", "icon"), ("STATE", "state"), ("CARD", "card"), ("MODEL", "model"), ("JUDGE", "judge"), ("NOW", "now"),
            ("QUIET", "quiet"), ("RUN", "run"), ("CONTEXT", "ctx"), ("EDITS", "edits"), ("CMT", "cmt"),
            ("◆", "msgs"), ("FLAGS", "flags"),
        ]:
            table.add_column(label, key=key)
        self.tick()
        self.set_interval(2, self.tick)
        self.set_interval(5, self.probe_tick)
        self.set_interval(0.5, self.play_tick)
        self.set_interval(10, self.judge_tick)
        self.probe_tick()

    # -- data ---------------------------------------------------------------

    def tick(self) -> None:
        self.session.refresh()
        self.statuses, self.matrix = assess_all(self.session, self.at, self.probes, self.th, self.judge)
        self._maybe_notify()
        self.render_all()

    def probe_tick(self) -> None:
        if self.at is None:
            self.run_probe([a.worktree for a in self.session.agents.values() if a.worktree and self._wants_probe(a)])

    def _wants_probe(self, agent: Agent) -> bool:
        st = self.statuses.get(agent.id)
        return st is None or not st.finished or agent.worktree not in self.probes or bool(self.probes[agent.worktree].procs)

    @work(thread=True, exclusive=True, group="probe")
    def run_probe(self, worktrees: list[str]) -> None:
        result = self.prober.probe(worktrees)
        self.call_from_thread(self._set_probes, result)

    def _set_probes(self, result: dict[str, WorktreeProbe]) -> None:
        self.probes.update(result)
        self.tick()

    def judge_tick(self, force_id: str | None = None) -> None:
        """Ask the judge about active agents that have done enough since their last check."""
        if self.at is not None or not self.judge or not self.judge.enabled:
            return
        for aid, st in sorted(self.statuses.items(), key=lambda kv: kv[1].sort_key()):
            if len(self.judge.in_flight) >= 2:
                break
            agent = st.agent
            if aid == force_id or self.judge.due(agent, st.finished):
                self.run_judge(agent, f"{st.state}: {st.detail}")

    @work(thread=True, group="judge")
    def run_judge(self, agent: Agent, state_line: str) -> None:
        self.judge.run(agent, self.session, state_line)  # type: ignore[union-attr]
        self.call_from_thread(self.tick)

    def _maybe_notify(self) -> None:
        current = {
            aid: (st.state, frozenset(f.code for f in st.flags if f.severity == "bad" or f.code in NOTIFY_FLAGS))
            for aid, st in self.statuses.items()
        }
        previous, self._seen = self._seen, current
        if previous is None or self.at is not None or not self.notify_enabled:
            return
        for aid, (state, bad) in current.items():
            st = self.statuses[aid]
            old_state, old_bad = previous.get(aid, ("", frozenset()))
            if state != old_state and state in NOTIFY_STATES:
                notify(f"{st.agent.description} — {state}", st.detail)
            elif state == "REPORTED" and old_state not in ("REPORTED", ""):
                notify(f"{st.agent.description} reported", st.detail)
            elif bad - old_bad:
                f = next(f for f in st.flags if f.code in bad - old_bad)
                notify(f"{st.agent.description} — {f.code}", f.detail)

    # -- rendering ----------------------------------------------------------

    def render_all(self) -> None:
        self.render_header()
        self.render_table()
        self.render_preview()
        screen = self.screen
        if isinstance(screen, (DetailScreen, TimelineScreen, FilesScreen)):
            screen.refresh_view()

    def render_header(self) -> None:
        s = self.session
        t = Text()
        title = s.title()
        t.append(title, style="bold cyan")
        if s.lane() and s.lane() != title:
            t.append(f"  lane {s.lane()}", style="cyan")
        self._set_terminal_title(title)
        project = os.path.basename(s.lead.cwd) if s.lead.cwd else s.ref.project_name
        t.append(f"   {project} · session {s.ref.session_id[:8]}", style="grey58")
        lead_ev = list(iter_until(s.lead.events, self.at))
        now = self.at or datetime.now(timezone.utc)
        if lead_ev:
            age = (now - lead_ev[-1].ts).total_seconds()
            ctx = [c for ts, c in s.lead.ctx if ts <= now]
            t.append(f"   lead: last move {fmt_duration(age)} ago", style="magenta")
            if ctx:
                t.append(f" · ctx {ctx[-1] // 1000}k", style="red" if ctx[-1] >= self.th.ctx_bad else "magenta")
        counts: dict[str, int] = {}
        for st in self.statuses.values():
            counts[st.severity] = counts.get(st.severity, 0) + 1
        t.append("\n")
        for sev, label in (("bad", "need you"), ("warn", "check"), ("ok", "working"), ("done", "done")):
            if counts.get(sev):
                t.append(f" {counts[sev]} {label} ", style=f"reverse {SEV_STYLE[sev]}" if sev != "done" else "grey50")
                t.append(" ")
        if self.judge:
            if not self.judge.enabled:
                t.append("   judge off (no claude CLI or API key)", style="grey50")
            else:
                over = " · budget reached, paused" if self.judge.over_budget else ""
                t.append(
                    f"   judge {self.judge.model.replace('claude-', '')} via {self.judge.backend} · "
                    f"${self.judge.spent:.3f} of ${self.judge.budget_usd:.2f}{over}",
                    style=WARNING if over else "grey58",
                )
        if self.at is not None:
            t.append(f"   ⏪ REPLAY {local(self.at, '%a %H:%M')}", style="bold reverse yellow")
            t.append("  [ ] step  space play  l live" + ("  ▶ playing" if self.playing else ""), style="yellow")
        else:
            t.append(f"   live {datetime.now().strftime('%H:%M:%S')}", style="green")
        self.query_one("#header", Static).update(t)

    def _terminal_write(self, seq: str) -> None:
        driver = getattr(self, "_driver", None)
        if driver is not None:
            try:
                driver.write(seq)
                driver.flush()
            except Exception:
                pass

    def _set_terminal_title(self, title: str) -> None:
        """Name this split after the session it watches, the way Claude Code names its own tab."""
        if self.title_prefix is None:  # --no-title
            return
        want = CONTROL_CHARS.sub("", f"{self.title_prefix}{title}")[:120]
        if want != self._term_title:
            self._term_title = want
            self._terminal_write(f"\x1b]2;{want}\x07")

    def lead_row(self) -> tuple[str, str, str]:
        """(state, severity, now) for the orchestrating session itself."""
        events = list(iter_until(self.session.lead.events, self.at))
        if not events:
            return "STARTING", "ok", ""
        now = self.at or datetime.now(timezone.utc)
        pending = _pending(events)
        last_call = next((e for e in reversed(events) if e.kind == "tool_use"), None)
        doing = f"{last_call.tool}: {last_call.summary}" if last_call else events[-1].summary
        if pending:
            return "RUNNING", "ok", f"{pending.tool}: {pending.summary}"
        if (now - events[-1].ts).total_seconds() < 90:
            return "WORKING", "ok", doing
        active = sum(1 for st in self.statuses.values() if not st.finished)
        if active:
            return "WAITING", "done", f"waiting on {active} agent{'s' if active > 1 else ''}"
        return "WAITING", "done", "waiting on you"

    def render_table(self) -> None:
        table = self.query_one(DataTable)
        selected = self.selected_id()
        rows = sorted(self.statuses.values(), key=lambda s: s.sort_key())
        if self.hide_done:
            rows = [r for r in rows if not r.finished]
        table.clear()
        self._order = []
        lstate, lsev, lnow = self.lead_row()
        lead_ctx = [c for t, c in self.session.lead.ctx if self.at is None or t <= self.at]
        lead_events = list(iter_until(self.session.lead.events, self.at))
        lead_age = ((self.at or datetime.now(timezone.utc)) - lead_events[-1].ts).total_seconds() if lead_events else 0
        table.add_row(
            Text("◆", style="bold magenta"),
            Text(lstate, style=SEV_STYLE[lsev]),
            Text("lead · " + self.session.title()[:27], style="bold magenta"),
            Text(self.session.lead.model.replace("claude-", "").split("-")[0] if self.session.lead.model else "", style="grey58"),
            Text(""),
            Text(lnow[:70]),
            Text(fmt_duration(lead_age), style="grey58"),
            Text(""),
            ctx_bar(lead_ctx[-1], self.th) if lead_ctx else Text(""),
            Text(""), Text(""), Text(""), Text(""),
            key=LEAD_ID,
        )
        self._order.append(LEAD_ID)
        for st in rows:
            sev = SEV_STYLE[st.severity]
            off = f" ({len(st.off_brief)}⚑)" if st.off_brief else ""
            table.add_row(
                Text(st.icon),
                Text(st.state, style=sev),
                Text(st.agent.description[:34], style="grey50" if st.finished else "bold"),
                Text(st.agent.model, style="grey58"),
                verdict_text(st, self.judge),
                Text(self._now_text(st)[:70], style="grey50" if st.finished else ""),
                Text(fmt_duration(st.age_s), style="red" if st.age_s > self.th.idle_s and not st.finished else "grey58"),
                Text(fmt_duration(st.run_s), style="grey58"),
                ctx_bar(st.ctx, self.th),
                Text(f"{len(st.edits)}{off}"),
                Text(str(st.commits)),
                Text(str(st.lead_msgs or ""), style="magenta"),
                flags_text(st),
                key=st.agent.id,
            )
            self._order.append(st.agent.id)
        if selected in self._order:
            table.move_cursor(row=self._order.index(selected), animate=False)

    @staticmethod
    def _now_text(st: Status) -> str:
        """Plain-language 'doing' from the judge while things are normal; the mechanical detail
        when something is wrong or the judgment is behind the transcript."""
        j = st.judgment
        if j and st.severity == "ok" and st.last and (st.last.ts - j.as_of_dt).total_seconds() < 600:
            return j.doing
        return st.detail

    def render_preview(self) -> None:
        log = self.query_one("#preview", RichLog)
        log.clear()
        aid = self.selected_id()
        if aid == LEAD_ID:
            lines = [event_line(e) for e in iter_until(self.session.lead.events, self.at)]
            for line in [x for x in lines if x is not None][-10:]:
                log.write(line)
            return
        agent = self.session.agents.get(aid) if aid else None
        if not agent:
            return
        for line in feed(agent, self.session, self.at)[-10:]:
            log.write(line)

    def selected_id(self) -> str | None:
        table = self.query_one(DataTable)
        if not self._order or table.cursor_row is None or table.cursor_row >= len(self._order):
            return None
        return self._order[table.cursor_row]

    def on_data_table_row_highlighted(self, _event) -> None:
        self.render_preview()

    def on_data_table_row_selected(self, _event) -> None:
        self.action_detail()

    # -- actions ------------------------------------------------------------

    def action_detail(self) -> None:
        aid = self.selected_id()
        if aid and aid != LEAD_ID:
            self.push_screen(DetailScreen(aid))

    def action_timeline(self) -> None:
        self.push_screen(TimelineScreen())

    def action_files(self) -> None:
        self.push_screen(FilesScreen())

    def action_judge_now(self) -> None:
        agent = self._current_agent()
        if not self.judge or not self.judge.enabled:
            self.notify("judge is off", timeout=3)
        elif self.at is not None:
            self.notify("judging works on live agents only", timeout=3)
        elif agent:
            self.run_judge(agent, f"{self.statuses[agent.id].state}: {self.statuses[agent.id].detail}")
            self.notify(f"judging {agent.description}", timeout=2)

    def action_message_lead(self) -> None:
        agent = self._current_agent()
        if self.at is not None:
            self.notify("messaging works live only; press l", timeout=3)
        elif agent and agent.id in self.statuses:
            box = inbox.find_inbox(self.session.ref.session_id)
            self.push_screen(MessageScreen(compose(self.statuses[agent.id]), box))
        else:
            self.notify("select an agent to message the lead about", timeout=3)

    @work(thread=True, group="message")
    def send_message(self, sock: str, text: str) -> None:
        try:
            inbox.send(sock, text)
            self.call_from_thread(self.notify, "sent to the lead", timeout=3)
        except OSError as e:
            self.call_from_thread(self.notify, f"send failed: {e}", severity="error", timeout=6)

    def action_web(self) -> None:
        if self.web_url is None:
            from .web import serve

            self.web_url = serve(self.session.ref, self.th, self.judge, port=self.web_port, probes=lambda: dict(self.probes))
        webbrowser.open(self.web_url + (f"?at={quote(self.at.isoformat())}" if self.at else ""))
        self.notify(f"web view at {self.web_url}", timeout=4)

    def action_toggle_done(self) -> None:
        self.hide_done = not self.hide_done
        self.render_table()

    def _current_agent(self) -> Agent | None:
        if isinstance(self.screen, DetailScreen):
            return self.session.agents.get(self.screen.agent_id)
        aid = self.selected_id()
        return self.session.agents.get(aid) if aid else None

    def action_open_worktree(self) -> None:
        agent = self._current_agent()
        if agent and agent.workdir:
            subprocess.Popen(["open", "-a", "Ghostty", agent.workdir], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def action_copy_path(self) -> None:
        agent = self._current_agent()
        if agent and agent.workdir:
            subprocess.run(["pbcopy"], input=agent.workdir, text=True)
            self.notify(f"copied {agent.workdir}", timeout=2)

    def action_next_session(self) -> None:
        self.refs = find_sessions() or self.refs
        ids = [r.session_id for r in self.refs]
        cur = ids.index(self.session.ref.session_id) if self.session.ref.session_id in ids else -1
        self.session = Session(self.refs[(cur + 1) % len(self.refs)])
        self.probes.clear()
        self._seen = None
        self.tick()
        self.probe_tick()
        self.notify(f"session {self.session.ref.session_id[:8]} · {self.session.lane() or '?'}", timeout=3)

    def action_step(self, minutes: int) -> None:
        span = self.session.span()
        if not span:
            return
        base = self.at or datetime.now(timezone.utc)
        new = base + timedelta(minutes=minutes)
        if new >= span[1] and self.at is not None and minutes > 0:
            new = span[1]
        self.at = max(span[0], new)
        self.tick()

    def action_play(self) -> None:
        if self.at is None:
            span = self.session.span()
            self.at = span[0] if span else None
        self.playing = not self.playing
        self.tick()

    def play_tick(self) -> None:
        if not self.playing or self.at is None:
            return
        span = self.session.span()
        self.at += timedelta(minutes=1)
        if span and self.at >= span[1]:
            self.at, self.playing = span[1], False
        self.tick()

    def action_live(self) -> None:
        self.at, self.playing = None, False
        self._seen = None  # don't notify for everything that changed while rewound
        self.tick()
        self.probe_tick()
