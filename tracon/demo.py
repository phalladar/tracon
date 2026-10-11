"""`tracon --demo`: a synthetic run to look at without a Claude Code session.

Writes a lead and seven subagent transcripts, in Claude Code's on-disk layout, to a temporary
directory (removed at exit), with times relative to now. A background thread keeps the busy
agents busy so the demo stays live. Probes and the judge are canned: no `lsof`, `git` or model
calls, and nothing is written outside the temporary directory.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

from .judge import Judge, Judgment
from .probe import Proc, WorktreeProbe
from .session import SessionRef, encode_project

PROJECT = os.path.expanduser("~/code/acme-shop")  # display only; never read or written
SESSION_ID = "de300000-7e4b-4c19-9b8c-2a6f1d0e9c41"

# id, description, model, minutes before now it started, brief
AGENTS = [
    ("a6e1f20c", "#11 Orders table migration", "claude-sonnet-5-5", 95,
     "Card #11: add the `orders` table. Files: `migrations/0042_orders.py`, `shop/models/order.py`, `tests/test_orders.py`. Run `git merge main` first. Run tests in the foreground. Commit and hand back."),
    ("a1c7d93e", "#12 Payment webhooks", "claude-opus-5-5", 90,
     "Card #12: verify and process payment webhooks. Files: `payments/webhooks.py`, `payments/signing.py`, `tests/test_webhooks.py`. Run `git merge main` first. Run tests in the foreground."),
    ("a2b84f61", "#13 Cart totals refactor", "claude-opus-5-5", 85,
     "Card #13: move cart total calculation into `shop/pricing.py`. Files: `shop/cart.py`, `shop/pricing.py`, `tests/test_pricing.py`. Leave the admin pages alone. Run `git merge main` first."),
    ("a3d05e7a", "#14 Discount codes", "claude-sonnet-5-5", 60,
     "Card #14: discount codes at checkout. Files: `shop/discounts.py`, `shop/cart.py`, `tests/test_discounts.py`. Run `git merge main` first. Run tests in the foreground."),
    ("a4f6c218", "#15 Order confirmation emails", "claude-sonnet-5-5", 55,
     "Card #15: send an order confirmation email. Files: `emails/order_confirmed.py`, `emails/templates/order_confirmed.html`. Run `git merge main` first."),
    ("a5e93b04", "#16 Inventory sync job", "claude-sonnet-5-5", 50,
     "Card #16: nightly inventory sync job. Files: `jobs/inventory_sync.py`, `tests/test_inventory_sync.py`. Run `git merge main` first. Run tests in the foreground."),
    ("a7a2d6c5", "#17 Search index rebuild", "claude-haiku-5-5", 25,
     "Card #17: rebuild the product search index on deploy. Files: `search/index.py`, `deploy/hooks.py`. Run `git merge main` first."),
]
SYNC = "uv run python -m jobs.inventory_sync --dry-run"
SYNC_ERR = "ConnectionRefusedError: [Errno 61] Connection refused (redis://localhost:6379)"


def worktree(aid: str) -> str:
    return os.path.join(PROJECT, ".claude", "worktrees", f"agent-{aid}")


class Writer:
    """Appends one transcript's records, timestamped relative to a fixed `now`."""

    def __init__(self, path: str, cwd: str, model: str, ctx: int = 20_000, ctx_step: int = 4_000) -> None:
        self.path, self.cwd, self.model = path, cwd, model
        self.ctx, self.ctx_step = ctx, ctx_step
        self.n = 0
        self.pending: str | None = None
        self.lock = threading.Lock()

    def _put(self, *recs: dict) -> None:
        with self.lock, open(self.path, "a") as f:
            f.writelines(json.dumps(r) + "\n" for r in recs)

    @staticmethod
    def ts(at: datetime) -> str:
        return at.isoformat().replace("+00:00", "Z")

    def raw(self, rec: dict) -> None:
        self._put(rec)

    def user(self, at: datetime, text: str) -> None:
        self._put({"type": "user", "timestamp": self.ts(at), "cwd": self.cwd, "message": {"role": "user", "content": text}})

    def call(self, at: datetime, name: str, inp: dict, result: str | None = "ok", dt: float = 3, error: bool = False) -> None:
        self.n += 1
        tid = f"toolu_{self.n:04d}"
        self.ctx += self.ctx_step
        recs = [{"type": "assistant", "timestamp": self.ts(at), "cwd": self.cwd, "message": {
            "id": f"msg_{tid}", "model": self.model,
            "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}],
            "usage": {"input_tokens": 50, "cache_read_input_tokens": self.ctx, "output_tokens": 300}}}]
        if result is None:
            self.pending = tid
        else:
            recs.append(self._result(at + timedelta(seconds=dt), tid, result, error))
        self._put(*recs)

    def finish(self, at: datetime, result: str = "ok") -> None:
        if self.pending:
            self._put(self._result(at, self.pending, result, False))
            self.pending = None

    def _result(self, at: datetime, tid: str, content: str, error: bool) -> dict:
        return {"type": "user", "timestamp": self.ts(at), "cwd": self.cwd, "message": {"content": [
            {"type": "tool_result", "tool_use_id": tid, "content": content, "is_error": error}]}}

    def read(self, at: datetime, rel: str) -> None:
        self.call(at, "Read", {"file_path": f"{self.cwd}/{rel}"}, "…")

    def edit(self, at: datetime, rel: str) -> None:
        self.call(at, "Edit", {"file_path": f"{self.cwd}/{rel}", "old_string": "a", "new_string": "b"})

    def bash(self, at: datetime, cmd: str, desc: str, result: str | None = "ok", error: bool = False) -> None:
        self.call(at, "Bash", {"command": cmd, "description": desc}, result, 20, error)

    def text(self, at: datetime, text: str, final: bool = False) -> None:
        self.n += 1
        self._put({"type": "assistant", "timestamp": self.ts(at), "cwd": self.cwd, "message": {
            "id": f"msg_text{self.n}", "model": self.model, "stop_reason": "end_turn" if final else None,
            "content": [{"type": "text", "text": text}]}})


def steady(w: Writer, now: datetime, start: float, end: float, files: list[str], tests: str, step: float = 1.6) -> None:
    """A read / edit / search / test rhythm from `start` to `end` minutes before now, committing now and then."""
    m, i = start, 0
    while m > end:
        at = now - timedelta(minutes=m)
        f = files[i % len(files)]
        k = i % 5
        if k == 0:
            w.read(at, f)
        elif k in (1, 2):
            w.edit(at, f)
        elif k == 3:
            w.call(at, "Grep", {"pattern": "def " + os.path.basename(f).split(".")[0]}, "3 matches")
        else:
            w.bash(at, tests, "Run tests", "12 passed")
        if i % 10 == 9:
            w.bash(at + timedelta(minutes=step / 2), "git commit -am 'wip'", "Commit")
        m -= step
        i += 1


class DemoProber:
    """Canned worktree probes: branch and git state per agent, and #14's test run as a live process."""

    def __init__(self) -> None:
        state = {  # commits ahead, dirty paths
            "a6e1f20c": (4, 0), "a1c7d93e": (5, 2), "a2b84f61": (5, 5), "a3d05e7a": (3, 2),
            "a4f6c218": (2, 1), "a5e93b04": (2, 1), "a7a2d6c5": (0, 3),
        }
        self._probes = {
            worktree(aid): WorktreeProbe(branch=f"worktree-agent-{aid}", ahead=ahead, dirty=dirty, head=aid[1:8])
            for aid, (ahead, dirty) in state.items()
        }
        self._probes[worktree("a3d05e7a")].procs = [Proc(48213, "00:42", "uv run pytest tests/ -x -q")]

    def probe(self, worktrees: list[str], git: bool = True) -> dict[str, WorktreeProbe]:
        return {wt: p for wt, p in self.all().items() if wt in worktrees}

    def all(self) -> dict[str, WorktreeProbe]:
        now = time.time()
        for p in self._probes.values():
            p.probed_at = p.last_change = now
        return dict(self._probes)


class DemoJudge(Judge):
    """Replays seeded verdicts. Never calls a model and never writes a cache."""

    def __init__(self, cache_dir: str) -> None:
        super().__init__(backend="demo", cache_dir=cache_dir)
        self.history = {}
        self.spent = 0.0

    def due(self, agent, finished: bool) -> bool:
        return False

    def run(self, agent, session, state_line: str = "", at=None) -> Judgment:
        return self.latest(agent.id) or Judgment("unclear", "", "", Writer.ts(datetime.now(timezone.utc)), 0, time.time())

    def reload(self) -> None:
        pass

    def _save(self) -> None:
        pass

    def seed(self, aid: str, verdict: str, doing: str, concern: str, as_of: datetime, n_calls: int) -> None:
        self.history.setdefault(aid, []).append(Judgment(verdict, doing, concern, Writer.ts(as_of), n_calls, as_of.timestamp(), 0.0012))
        self.spent += 0.0012


class Demo:
    """Build the run under a temporary root and keep it moving."""

    def __init__(self, root: str | None = None) -> None:
        self.root = root or tempfile.mkdtemp(prefix="tracon-demo-")
        if root is None:
            atexit.register(shutil.rmtree, self.root, True)
        self.project_dir = os.path.join(self.root, encode_project(PROJECT))
        sub = os.path.join(self.project_dir, SESSION_ID, "subagents")
        os.makedirs(sub)
        self.now = now = datetime.now(timezone.utc)
        self.prober = DemoProber()
        self.judge = DemoJudge(os.path.join(self.root, "cache"))
        self.w: dict[str, Writer] = {}
        for aid, desc, model, start, brief in AGENTS:
            with open(os.path.join(sub, f"agent-{aid}.meta.json"), "w") as f:
                json.dump({"description": desc, "model": model, "worktreePath": worktree(aid), "worktreeBranch": f"worktree-agent-{aid}"}, f)
            w = self.w[aid] = Writer(os.path.join(sub, f"agent-{aid}.jsonl"), worktree(aid), model)
            w.user(now - timedelta(minutes=start), brief)
            if aid != "a7a2d6c5":  # #17 skips it: STALE-BASE
                w.bash(now - timedelta(minutes=start - 0.3), "git merge main", "Merge base branch")
        self._history(now)
        self._lead(now)
        self._verdicts(now)
        self.ref = SessionRef(self.project_dir, SESSION_ID, time.time(), len(AGENTS))
        self._stop = threading.Event()

    def _history(self, now: datetime) -> None:
        def ago(m: float) -> datetime:
            return now - timedelta(minutes=m)

        w = self.w["a6e1f20c"]  # finished, reported, merged
        steady(w, now, 94.5, 40, ["migrations/0042_orders.py", "shop/models/order.py", "tests/test_orders.py"], "uv run pytest tests/test_orders.py -q")
        w.bash(ago(38), "git add -A && git commit -m 'Add orders table'", "Commit")
        w.text(ago(31), "Done: orders table, model, 9 tests. Committed.", final=True)

        w = self.w["a1c7d93e"]  # steady and on track
        steady(w, now, 89, 0.6, ["payments/webhooks.py", "payments/signing.py", "tests/test_webhooks.py"], "uv run pytest tests/test_webhooks.py -q", step=1.9)
        w.edit(ago(0.2), "payments/webhooks.py")

        w = self.w["a2b84f61"]  # drifts into the admin pages; big context; collides with #14
        w.ctx, w.ctx_step = 60_000, 6_000
        steady(w, now, 84, 30, ["shop/cart.py", "shop/pricing.py", "tests/test_pricing.py"], "uv run pytest tests/test_pricing.py -q", step=1.5)
        steady(w, now, 29, 1, ["shop/admin/dashboard.py", "shop/admin/styles.css", "shop/cart.py"], "uv run pytest -q", step=1.4)

        w = self.w["a3d05e7a"]  # running the suite now
        steady(w, now, 59, 1, ["shop/discounts.py", "shop/cart.py", "tests/test_discounts.py"], "uv run pytest tests/test_discounts.py -q", step=1.8)
        w.bash(ago(0.15), "uv run pytest tests/ -x -q", "Run full test suite", result=None)

        w = self.w["a4f6c218"]  # waiting on a permission prompt
        steady(w, now, 54, 6, ["emails/order_confirmed.py", "emails/templates/order_confirmed.html"], "uv run pytest tests/test_emails.py -q", step=2.2)
        w.call(ago(3.2), "Edit", {"file_path": f"{w.cwd}/emails/templates/order_confirmed.html", "old_string": "a", "new_string": "b"}, result=None)

        w = self.w["a5e93b04"]  # retrying the same failing command
        steady(w, now, 49, 20, ["jobs/inventory_sync.py", "tests/test_inventory_sync.py"], "uv run pytest tests/test_inventory_sync.py -q", step=2.0)
        for m in (12, 9, 7.5, 4.4, 3.1, 1.4):
            w.bash(ago(m), SYNC, "Dry-run the sync job", SYNC_ERR, error=True)

        w = self.w["a7a2d6c5"]  # edited without merging main, a denied call, then went quiet
        w.read(ago(24.5), "search/index.py")
        w.edit(ago(23), "search/index.py")
        w.edit(ago(21.5), "deploy/hooks.py")
        w.bash(ago(19), "rm -rf .search-cache", "Clear search cache", "Permission for this action was denied. Reason: Modify Shared Resources", error=True)
        w.read(ago(17), "deploy/hooks.py")
        w.edit(ago(15), "search/index.py")
        w.edit(ago(13), "shop/models/order.py")
        w.text(ago(8.5), "Should the index rebuild run before or after migrations? Waiting for guidance.")

    def _lead(self, now: datetime) -> None:
        lead = Writer(os.path.join(self.project_dir, f"{SESSION_ID}.jsonl"), PROJECT, "claude-opus-5-5", 40_000, 2_000)
        lead.raw({"type": "agent-name", "agentName": "checkout-v2 sprint"})
        lead.user(now - timedelta(minutes=97), "<command-name>/expedite</command-name><command-args>checkout-v2</command-args>")
        for aid, desc, _, start, brief in AGENTS:
            lead.call(now - timedelta(minutes=start + 0.2), "Agent", {"description": desc, "prompt": brief, "subagent_type": "general-purpose", "isolation": "worktree", "run_in_background": True}, f"Started {aid}")
        lead.user(now - timedelta(minutes=30), "<task-notification><task-id>a6e1f20c</task-id><status>completed</status><summary>Agent \"#11 Orders table migration\" completed</summary></task-notification>")
        lead.bash(now - timedelta(minutes=28), "git merge --no-ff worktree-agent-a6e1f20c", "Merge #11", "Merge made by the 'ort' strategy.")
        lead.call(now - timedelta(minutes=6), "SendMessage", {"to": "a5e93b04", "message": "Redis isn't running in worktrees. Mock the client in tests instead of calling the real job."}, "sent")
        lead.text(now - timedelta(minutes=2), "Waiting on #12–#17.")

    def _verdicts(self, now: datetime) -> None:
        def j(aid, verdict, doing, concern, as_of):
            self.judge.seed(aid, verdict, doing, concern, as_of, self.w[aid].n)

        j("a6e1f20c", "on_track", "Committing the orders migration and tests", "", now - timedelta(minutes=38))
        j("a1c7d93e", "on_track", "HMAC signature check in payments/webhooks.py", "", now - timedelta(minutes=1))
        j("a2b84f61", "on_track", "Moving subtotal logic into shop/pricing.py", "", now - timedelta(minutes=40))
        j("a2b84f61", "drifting", "Restyling the admin dashboard", "Brief says to leave the admin pages alone; last 12 edits are admin dashboard styling.", now - timedelta(minutes=1.5))
        j("a3d05e7a", "on_track", "Full test run after wiring discounts into cart", "", now - timedelta(minutes=1))
        j("a4f6c218", "on_track", "Editing the confirmation email template", "", now - timedelta(minutes=6))
        j("a5e93b04", "stuck", "Retrying the sync dry-run against local Redis", "Ran the same dry-run 6 times; each fails with connection refused and nothing changed between tries.", now - timedelta(minutes=1.5))

    # -- keep it live ------------------------------------------------------------

    def start(self, every: float = 6.0) -> "Demo":
        threading.Thread(target=self._run, args=(every,), daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _run(self, every: float) -> None:
        i = 0
        while not self._stop.wait(every):
            self.step(i)
            i += 1

    def step(self, i: int) -> None:
        """One beat: the working agents make a call; #14 finishes and restarts its test run."""
        at = datetime.now(timezone.utc)
        files12 = ["payments/webhooks.py", "payments/signing.py", "tests/test_webhooks.py"]
        files13 = ["shop/admin/dashboard.py", "shop/admin/styles.css", "shop/cart.py"]
        if i % 2 == 0:
            w = self.w["a1c7d93e"]
            (w.read if i % 6 == 0 else w.edit)(at, files12[(i // 2) % 3])
        else:
            w = self.w["a2b84f61"]
            (w.read if i % 5 == 1 else w.edit)(at, files13[(i // 2) % 3])
        w = self.w["a3d05e7a"]
        if i % 8 == 5:
            w.finish(at, "41 passed in 38.2s")
        elif i % 8 == 6:
            w.edit(at, "shop/discounts.py")
        elif i % 8 == 7:
            w.bash(at, "uv run pytest tests/ -x -q", "Run full test suite", result=None)
        if i % 10 == 9:
            self.w["a5e93b04"].bash(at, SYNC, "Dry-run the sync job", SYNC_ERR, error=True)
