"""Local web view: a zoomable lane timeline, file heatmap and agent drawer in the browser.

Binds to 127.0.0.1 only. Endpoints:
  GET /                    the page (tracon/static/index.html)
  GET /api/state?at=ISO    lane snapshot: agents, activity segments, lead marks, file matrix
  GET /api/agent/<id>?at=  one agent's activity feed
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Callable
from urllib.parse import parse_qs, urlparse

from .judge import Judge
from .lane import FileMatrix, assess_all, card_label
from .probe import Prober, WorktreeProbe
from .session import Agent, Session, SessionRef
from .signals import Status, Thresholds
from .transcript import EDIT_TOOLS, HANDBACK_TOOLS, iter_until, parse_ts

MERGE_GAP_S = 60  # consecutive calls of one kind closer than this draw as one segment


def ms(ts: datetime) -> int:
    return int(ts.timestamp() * 1000)


def kind_of(tool: str) -> str:
    if tool in EDIT_TOOLS:
        return "edit"
    if tool == "Bash":
        return "shell"
    if tool in ("Read", "Grep", "Glob", "LS"):
        return "read"
    return "other"


class LaneState:
    """Owns its own Session (thread-safe behind a lock) so the TUI's objects are never shared."""

    def __init__(
        self,
        ref: SessionRef,
        th: Thresholds,
        judge: Judge | None,
        probes: Callable[[], dict[str, WorktreeProbe]] | None,
    ) -> None:
        self.session = Session(ref)
        self.th = th
        self.judge = judge
        self.probes = probes or (lambda: {})
        self.lock = threading.Lock()
        self._refreshed = 0.0

    def _refresh(self) -> None:
        if time.time() - self._refreshed > 1:
            self.session.refresh()
            self._refreshed = time.time()

    def snapshot(self, at: datetime | None) -> dict:
        with self.lock:
            self._refresh()
            if self.judge and self.judge.cache_path:
                self.judge.reload()
            statuses, matrix = assess_all(self.session, at, None if at else self.probes(), self.th, self.judge)
            s = self.session
            span = s.span()
            now = at or datetime.now(timezone.utc)
            return {
                "lane": s.lane(),
                "title": s.title(),
                "project": (s.lead.cwd.rstrip("/").split("/")[-1] if s.lead.cwd else s.ref.project_name),
                "session": s.ref.session_id,
                "live": at is None,
                "now": ms(now),
                "span": [ms(span[0]), ms(max(span[1], now) if at is None else span[1])] if span else None,
                "idle_s": self.th.idle_s,
                "judge": self._judge_info(),
                "lead": self._lead(at),
                "agents": [self._agent(st, at, now) for st in sorted(statuses.values(), key=lambda x: x.agent.started() or now)],
                "files": self._files(matrix, statuses),
            }

    def feed(self, agent_id: str, at: datetime | None) -> list[dict]:
        with self.lock:
            self._refresh()
            agent = self.session.agents.get(agent_id)
            if not agent:
                return []
            out = []
            for e in iter_until(agent.transcript.events, at):
                if e.kind == "tool_result" and not e.is_error:
                    continue
                out.append({
                    "t": ms(e.ts),
                    "kind": e.kind,
                    "tool": "REPORT" if e.tool in HANDBACK_TOOLS else e.tool,
                    "cat": kind_of(e.tool) if e.kind == "tool_use" else e.kind,
                    "text": e.summary,
                    "err": e.is_error,
                    "bg": e.background,
                })
            for e in self.session.lead_messages(agent_id, at):
                x = e.extra or {}
                out.append({"t": ms(e.ts), "kind": "lead", "tool": "", "cat": "lead", "text": f"{x.get('summary', '')}: {x.get('message', '')[:400]}", "err": False, "bg": False})
            out.sort(key=lambda r: r["t"])
            return out[-500:]

    # -- pieces -------------------------------------------------------------

    def _judge_info(self) -> dict | None:
        j = self.judge
        if not j:
            return None
        return {"backend": j.backend, "model": j.model, "spent": round(j.spent, 4), "budget": j.budget_usd}

    def _lead(self, at: datetime | None) -> list[dict]:
        marks = []
        for e in iter_until(self.session.lead.events, at):
            x = e.extra or {}
            if e.tool == "Bash" and "git merge" in e.key and "worktree-agent-" in e.key:
                marks.append({"t": ms(e.ts), "k": "merge", "label": _merge_label(e.key)})
            elif e.tool == "SendMessage":
                marks.append({"t": ms(e.ts), "k": "msg", "label": f"to {x.get('to', '?')[:8]}: {x.get('summary', '')}"})
            elif e.tool in ("Agent", "Task"):
                marks.append({"t": ms(e.ts), "k": "spawn", "label": x.get("description", "")})
            elif e.tool == "TaskStop":
                marks.append({"t": ms(e.ts), "k": "stop", "label": f"stop {x.get('task_id', '')[:8]}"})
        return marks

    def _agent(self, st: Status, at: datetime | None, now: datetime) -> dict:
        a: Agent = st.agent
        events = list(iter_until(a.transcript.events, at))
        results = {e.tool_id: e.ts for e in events if e.kind == "tool_result"}
        segs: list[dict] = []
        marks: list[dict] = []
        for e in events:
            if e.kind == "tool_use":
                if e.tool in HANDBACK_TOOLS:
                    marks.append({"t": ms(e.ts), "k": "report", "label": e.summary})
                    continue
                k = kind_of(e.tool)
                t0 = e.ts
                t1 = results.get(e.tool_id, now if not st.finished else e.ts)
                last = segs[-1] if segs else None
                if last and last["k"] == k and ms(t0) - last["t1"] < MERGE_GAP_S * 1000:
                    last["t1"] = max(last["t1"], ms(t1))
                    last["n"] += 1
                    if len(last["labels"]) < 4:
                        last["labels"].append(f"{e.tool}: {e.summary}")
                else:
                    segs.append({"t0": ms(t0), "t1": ms(t1), "k": k, "n": 1, "labels": [f"{e.tool}: {e.summary}"]})
            elif e.kind == "tool_result" and e.is_error:
                marks.append({"t": ms(e.ts), "k": "error", "label": e.summary})
        for e in self.session.lead_messages(a.id, at):
            marks.append({"t": ms(e.ts), "k": "lead", "label": (e.extra or {}).get("summary", "")})
        j = st.judgment
        return {
            "id": a.id,
            "label": card_label(a),
            "title": a.description,
            "model": a.model,
            "state": st.state,
            "severity": st.severity,
            "icon": st.icon,
            "detail": st.detail,
            "quiet_s": round(st.age_s),
            "run_s": round(st.run_s),
            "ctx": st.ctx,
            "edits": st.edits,
            "off_brief": st.off_brief,
            "commits": st.commits,
            "lead_msgs": st.lead_msgs,
            "worktree": a.worktree,
            "flags": [{"code": f.code, "severity": f.severity, "detail": f.detail} for f in st.flags],
            "judge": {"verdict": j.verdict, "doing": j.doing, "concern": j.concern, "as_of": ms(j.as_of_dt)} if j else None,
            "start": ms(events[0].ts) if events else None,
            "end": ms(events[-1].ts) if events else None,
            "segments": segs,
            "marks": marks,
            "procs": [{"pid": p.pid, "etime": p.etime, "cmd": p.command[:200], "orphan": p.orphan} for p in (st.probe.procs if st.probe else [])],
        }

    def _files(self, m: FileMatrix, statuses: dict[str, Status]) -> dict:
        agent_ids = [a.id for a in m.agents]
        idx = {aid: i for i, aid in enumerate(agent_ids)}
        colliding = {(f, aid) for aid, hits in m.collisions.items() for f, _ in hits}
        files = [f for f in m.files if m.editors(f)]
        cells = []
        for fi, f in enumerate(files):
            for aid in agent_ids:
                c = m.cells.get((f, aid))
                if c:
                    cells.append([fi, idx[aid], c.edits, c.reads, (f, aid) in colliding])
        return {"files": files, "agents": agent_ids, "cells": cells}


def _merge_label(cmd: str) -> str:
    import re

    m = re.search(r'-m\s+["\']([^"\']+)', cmd)
    return m.group(1) if m else "merge"


class Handler(BaseHTTPRequestHandler):
    state: LaneState  # set on the subclass made in serve()

    def log_message(self, *args) -> None:  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        q = parse_qs(url.query)
        at = None
        if q.get("at") and q["at"][0]:
            try:
                at = parse_ts(q["at"][0]) if "T" in q["at"][0] else datetime.fromtimestamp(int(q["at"][0]) / 1000, timezone.utc)
            except ValueError:
                return self._send(400, b"bad at", "text/plain")
        try:
            if url.path in ("/", "/index.html"):
                body = resources.files("tracon").joinpath("static/index.html").read_bytes()
                return self._send(200, body, "text/html; charset=utf-8")
            if url.path == "/api/state":
                return self._send(200, json.dumps(self.state.snapshot(at)).encode(), "application/json")
            if url.path.startswith("/api/agent/"):
                aid = url.path.rsplit("/", 1)[-1]
                return self._send(200, json.dumps(self.state.feed(aid, at)).encode(), "application/json")
        except Exception as exc:  # surface errors to the page instead of dropping the connection
            return self._send(500, json.dumps({"error": str(exc)}).encode(), "application/json")
        self._send(404, b"not found", "text/plain")


def serve(
    ref: SessionRef,
    th: Thresholds,
    judge: Judge | None,
    port: int = 8765,
    probes: Callable[[], dict[str, WorktreeProbe]] | None = None,
    block: bool = False,
) -> str:
    """Start the server (in a daemon thread unless `block`) and return its URL."""
    state = LaneState(ref, th, judge, probes)
    handler = type("LaneHandler", (Handler,), {"state": state})
    server = None
    for p in range(port, port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", p), handler)
            break
        except OSError:
            continue
    if server is None:
        raise RuntimeError(f"no free port in {port}-{port + 19}")
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    if block:
        print(f"tracon web: {url}  (ctrl-c to stop)")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    else:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    return url


def standalone_probes(session_ref: SessionRef) -> Callable[[], dict[str, WorktreeProbe]]:
    """For `tracon web` without the TUI: probe worktrees every 5 s in the background."""
    session = Session(session_ref)
    prober = Prober()
    latest: dict[str, WorktreeProbe] = {}

    def loop() -> None:
        while True:
            try:
                session.refresh()
                wts = [a.worktree for a in session.agents.values() if a.worktree]
                latest.update(prober.probe(wts))
            except Exception:
                pass
            time.sleep(5)

    threading.Thread(target=loop, daemon=True).start()
    return lambda: dict(latest)
