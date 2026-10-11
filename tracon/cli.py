"""tracon command line."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from importlib.metadata import version
from urllib.parse import quote

from .judge import DEFAULT_MODEL, Judge
from .lane import assess_all
from .probe import Prober
from .session import Session, find_sessions
from .signals import Thresholds, fmt_duration


def parse_at(s: str) -> datetime:
    """Local wall-clock time ('2026-10-11 03:10', '03:10') or ISO with zone."""
    s = s.strip()
    if len(s) <= 5 and ":" in s:  # HH:MM today
        today = datetime.now().astimezone()
        hh, mm = map(int, s.split(":"))
        return today.replace(hour=hh, minute=mm, second=0, microsecond=0).astimezone(timezone.utc)
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tracon", description="Watch Claude Code subagents: who is working, stuck or off track.")
    p.add_argument("--version", action="version", version=f"%(prog)s {version('tracon')}")
    p.add_argument("command", nargs="?", choices=["tui", "web"], default="tui", help="tui (default) or web: serve the browser view only")
    p.add_argument("--project", "-p", help="project path (default: any project; newest session wins)")
    p.add_argument("--session", "-s", help="session id or prefix (default: the most recently active)")
    p.add_argument("--at", help="replay as of this local time, e.g. '2026-10-11 01:30' or '01:30'")
    p.add_argument("--list", action="store_true", help="list recent sessions that have subagents")
    p.add_argument("--once", action="store_true", help="print one snapshot and exit (no TUI)")
    # Off by default: they're sent through osascript, so macOS shows them as coming from Script Editor.
    p.add_argument("--notify", action="store_true", help="desktop notifications when an agent needs you (off by default)")
    p.add_argument("--no-notify", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--idle", type=float, default=5, help="minutes of silence before IDLE (default 5)")
    p.add_argument("--stall", type=float, default=20, help="minutes running without change before STALLED (default 20)")
    p.add_argument("--no-judge", action="store_true", help="turn off the LLM on-track check")
    p.add_argument("--judge-model", default=DEFAULT_MODEL, help=f"model for the on-track check (default {DEFAULT_MODEL})")
    p.add_argument("--judge-backend", choices=["auto", "api", "cli"], default="auto", help="api = Anthropic SDK with ANTHROPIC_API_KEY; cli = `claude -p` on your login (auto picks)")
    p.add_argument("--judge-budget", type=float, default=1.0, help="stop judging after this many dollars per run (default 1.00)")
    p.add_argument("--judge-every", type=float, default=3, help="minimum minutes between checks of one agent (default 3)")
    p.add_argument("--title-prefix", default="◎ ", help="prefix for the terminal title, which tracon sets to the watched session's name (default '◎ ')")
    p.add_argument("--no-title", action="store_true", help="leave the terminal title alone")
    p.add_argument("--demo", action="store_true", help="watch a made-up run (no Claude Code session needed)")
    p.add_argument("--port", type=int, default=8765, help="web view port (default 8765; the next free one is used)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    demo = None
    if args.demo:
        from .demo import Demo

        demo = Demo().start()
        refs = [demo.ref]
    else:
        # Inside a project with lanes, prefer its sessions; otherwise the newest anywhere.
        refs = find_sessions(args.project) if args.project else (find_sessions(os.getcwd()) or find_sessions())
    if args.list:
        for r in refs[:20]:
            when = datetime.fromtimestamp(r.mtime).strftime("%Y-%m-%d %H:%M")
            print(f"{r.session_id}  {when}  {r.n_agents:3} agents  {r.project_name}")
        return 0
    if args.session:
        refs = [r for r in refs if r.session_id.startswith(args.session)]
    if not refs:
        print("No Claude Code session with subagents found." + (" Try --list." if args.session else ""), file=sys.stderr)
        return 1
    th = Thresholds(idle_s=args.idle * 60, stall_s=args.stall * 60)
    at = parse_at(args.at) if args.at else None
    judge = None
    if demo and not args.no_judge:
        judge = demo.judge
    elif not args.no_judge:
        judge = Judge(model=args.judge_model, backend=args.judge_backend, budget_usd=args.judge_budget, min_interval_s=args.judge_every * 60)
    if args.once:
        return print_snapshot(Session(refs[0]), at, th, judge, demo.prober if demo else None)
    if args.command == "web":
        import webbrowser

        from .web import serve, standalone_probes

        url_at = f"?at={quote(at.isoformat())}" if at else ""
        # Standalone web view shows judgments a running TUI makes; it does not judge by itself.
        probes = demo.prober.all if demo else standalone_probes(refs[0])
        import threading

        threading.Timer(0.8, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}/{url_at}")).start()
        serve(refs[0], th, judge, port=args.port, probes=probes, block=True)
        return 0
    from .app import LaneApp

    app = LaneApp(refs, at=at, thresholds=th, notify=args.notify and not args.no_notify and demo is None, judge=judge, web_port=args.port,
                  title_prefix=None if args.no_title else args.title_prefix, prober=demo.prober if demo else None)
    try:
        app.run()
    finally:
        if not args.no_title:
            sys.stdout.write("\x1b[23;2t")  # restore the title saved at start
            sys.stdout.flush()
    return 0


def print_snapshot(session: Session, at: datetime | None, th: Thresholds, judge: Judge | None = None, prober=None) -> int:
    session.refresh()
    probes = {}
    if at is None:
        wts = [a.worktree for a in session.agents.values() if a.worktree]
        probes = (prober or Prober()).probe(wts)
    statuses, _ = assess_all(session, at, probes, th, judge)
    rows = sorted(statuses.values(), key=lambda s: s.sort_key())
    when = (at or datetime.now(timezone.utc)).astimezone().strftime("%Y-%m-%d %H:%M")
    print(f"lane: {session.lane() or '?'}   session {session.ref.session_id[:8]}   as of {when}\n")
    for s in rows:
        print(
            f"{s.icon} {s.state:9} {s.agent.description[:34]:34} {s.agent.model:6} "
            f"run {fmt_duration(s.run_s):>6}  quiet {fmt_duration(s.age_s):>6}  ctx {s.ctx // 1000:>3}k  "
            f"edits {len(s.edits):>2}  commits {s.commits:>2}"
        )
        print(f"    {s.detail[:150]}")
        if s.judgment:
            j = s.judgment
            print(f"    judge: {j.verdict} — {j.doing}" + (f" | {j.concern}" if j.concern else ""))
        for f in s.flags:
            print(f"    [{f.severity}] {f.code}: {f.detail[:140]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
