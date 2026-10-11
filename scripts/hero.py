"""Render docs/screenshots/hero.gif: a Claude Code session and tracon side by side in one
terminal split, the way tracon is meant to be used.

    uv run --with pillow python scripts/hero.py [out.gif]

The right pane is the real tracon TUI watching the `--demo` run on a virtual clock: transcripts
are written up to the clock, so agents appear as the lead dispatches them and trouble builds up
as time runs. The left pane is a scripted Claude Code session drawn from the same lead
transcript. At the end you press `m` on the stuck agent and send the note; the left pane shows
it arriving and the lead's reply, and the agent gets going again. Sending is stubbed: nothing
leaves this process. Frames are Rich SVGs rendered with headless Chrome and stitched with Pillow.
"""

import asyncio
import glob
import io
import json
import os
import shutil
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from rich.console import Console
from rich.segment import Segment
from rich.style import Style
from rich.text import Text

import tracon.app as app_mod
import tracon.inbox as inbox_mod
import tracon.signals as signals_mod
from tracon.app import LaneApp
from tracon.demo import SESSION_ID, Demo
from tracon.judge import Judge, Judgment
from tracon.session import SessionRef
from tracon.signals import Thresholds

sys.path.insert(0, os.path.dirname(__file__))
from screenshots import chrome_png  # noqa: E402

OUT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "docs/screenshots/hero.gif")
LEFT, RIGHT, ROWS = 78, 200, 36  # columns per pane, rows
GIF_WIDTH = 2400

ORANGE, GREY, DIM, WHITE = "#d77757", "#8a8a8a", "#5f5f5f", "#e6e6e6"
SPINNER = "✢✳✶✻✽✻✶✳"


# -- a virtual clock for tracon ---------------------------------------------------

class Clock:
    v = datetime.now(timezone.utc)


class FakeDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return Clock.v.astimezone(tz) if tz else Clock.v.astimezone().replace(tzinfo=None)


app_mod.datetime = FakeDatetime
signals_mod.now_utc = lambda: Clock.v

SENT: list[str] = []
inbox_mod.find_inbox = lambda sid, root=None: inbox_mod.Inbox(48120, "/nonexistent/inbox.sock", "checkout-v2 sprint", "idle")
inbox_mod.send = lambda sock, text, timeout=3.0: SENT.append(text)


# -- the run, written out as the clock moves ---------------------------------------

class Stage:
    """Copies the demo's transcripts into a live directory, line by line, up to the clock."""

    def __init__(self, demo: Demo, live_root: str) -> None:
        self.demo = demo
        self.src = demo.project_dir
        self.dst = os.path.join(live_root, os.path.basename(self.src))
        os.makedirs(os.path.join(self.dst, SESSION_ID, "subagents"))
        self.files: dict[str, list[tuple[datetime | None, str]]] = {}
        for path in glob.glob(os.path.join(self.src, "**", "*.jsonl"), recursive=True):
            lines = []
            for line in open(path):
                rec = json.loads(line)
                if _is_old_nudge(rec):
                    continue
                ts = rec.get("timestamp")
                lines.append((datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None, line))
            self.files[os.path.relpath(path, self.src)] = lines
        self.done = {rel: 0 for rel in self.files}
        self.ref = SessionRef(self.dst, SESSION_ID, 0, 7)

    def add(self, rel: str, rec: dict) -> None:
        """A record that happens after the demo's history (the lead's reply and what follows)."""
        ts = datetime.fromisoformat(rec["timestamp"].replace("Z", "+00:00"))
        self.files[rel].append((ts, json.dumps(rec) + "\n"))

    def advance(self, until: datetime) -> None:
        for rel, lines in self.files.items():
            i = self.done[rel]
            new = []
            while i < len(lines) and (lines[i][0] is None or lines[i][0] <= until):
                new.append(lines[i][1])
                i += 1
            if not new:
                continue
            if self.done[rel] == 0 and rel.endswith(".jsonl") and "subagents" in rel:
                meta = rel[: -len(".jsonl")] + ".meta.json"
                shutil.copy(os.path.join(self.src, meta), os.path.join(self.dst, meta))
            with open(os.path.join(self.dst, rel), "a") as f:
                f.writelines(new)
            self.done[rel] = i

    def lead_records(self, until: datetime) -> list[dict]:
        rel = f"{SESSION_ID}.jsonl"
        return [json.loads(line) for ts, line in self.files[rel] if ts is not None and ts <= until]


def _is_old_nudge(rec: dict) -> bool:
    """The demo's history has the lead already nudging #16; here the nudge comes from tracon."""
    msg = rec.get("message") or {}
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    for b in content:
        if b.get("type") == "tool_use" and b.get("name") == "SendMessage":
            return True
        if b.get("type") == "tool_result" and b.get("content") == "sent":
            return True
    return False


# -- the left pane: a Claude Code session ------------------------------------------

class Lead:
    """What the lead's terminal shows, rebuilt each frame."""

    def __init__(self, stage: Stage, start: datetime) -> None:
        self.stage = stage
        self.start = start
        self.typed = ""
        self.extra: list[list[Text]] = []  # blocks after the run's history
        self.busy: str | None = None  # spinner label

    def blocks(self, until: datetime) -> list[list[Text]]:
        out: list[list[Text]] = []
        for rec in self.stage.lead_records(until):
            content = (rec.get("message") or {}).get("content")
            if rec["type"] == "user" and isinstance(content, str):
                if content.startswith("<command-name>"):
                    out.append(user_block("/expedite checkout-v2"))
                    out.append(say("Seven cards on checkout-v2. Each goes to its own worktree agent as the "
                                   "cards it depends on land."))
                elif content.startswith("<task-notification>"):
                    out.append(say("#11 Orders table migration finished. Merging it before #17 starts."))
            elif rec["type"] == "assistant" and isinstance(content, list):
                for b in content:
                    if b.get("type") == "tool_use" and b["name"] == "Agent":
                        out.append(tool("Agent", b["input"]["description"], "Backgrounded · own worktree"))
                    elif b.get("type") == "tool_use" and b["name"] == "Bash":
                        out.append(tool("Bash", b["input"]["command"], "Merge made by the 'ort' strategy."))
                    elif b.get("type") == "text":
                        out.append(say(b["text"]))
        return out + self.extra

    def render(self, until: datetime, frame: int) -> list[Text]:
        lines: list[Text] = []
        for block in self.blocks(until):
            lines.extend(block)
            lines.append(Text(""))
        if self.busy:
            elapsed = int((until - self.start).total_seconds() // 60)
            t = Text(f"{SPINNER[frame % len(SPINNER)]} {self.busy}… ", style=ORANGE)
            t.append(f"({elapsed // 60}h {elapsed % 60:02d}m · esc to interrupt)" if elapsed >= 60 else
                     f"({elapsed}m · esc to interrupt)" if elapsed else "(esc to interrupt)", style=GREY)
            lines += [t, Text("")]
        box = input_box(self.typed)
        room = ROWS - len(box)
        return lines[-room:] + box if len(lines) > room else lines + box + [Text("")] * (room - len(lines))


def wrap(text: str, first: Text, indent: str, style: str = WHITE) -> list[Text]:
    console = Console(width=LEFT - len(indent) - 1)
    body = Text(text, style=style).wrap(console, LEFT - len(indent) - 1)
    out = []
    for i, line in enumerate(body):
        head = first.copy() if i == 0 else Text(indent)
        out.append(head + line)
    return out


def say(text: str) -> list[Text]:
    return wrap(text, Text("⏺ ", style=WHITE), "  ")


def tool(name: str, arg: str, result: str) -> list[Text]:
    head = Text("⏺ ", style="#4eba65")
    head.append(name, style=f"bold {WHITE}")
    head.append(f"({arg})", style=WHITE)
    return [head] + wrap(result, Text("  ⎿  ", style=GREY), "     ", style=GREY)


def user_block(text: str, more: int = 0) -> list[Text]:
    lines = wrap(text, Text("> ", style=GREY), "  ", style=GREY)
    if more:
        lines.append(Text(f"  … +{more} lines", style=DIM))
    return lines


def input_box(typed: str) -> list[Text]:
    inner = LEFT - 4
    content = Text("> ", style=WHITE) + Text(typed, style=WHITE) + Text("█", style=GREY)
    content.pad_right(inner - content.cell_len)
    return [
        Text("╭" + "─" * (LEFT - 2) + "╮", style=DIM),
        Text("│ ", style=DIM) + content + Text(" │", style=DIM),
        Text("╰" + "─" * (LEFT - 2) + "╯", style=DIM),
        Text("  ⏵⏵ auto mode on (shift+tab to cycle)", style=GREY),
    ]


# -- composing a frame --------------------------------------------------------------

def tracon_lines(app: LaneApp) -> list[list[Segment]]:
    console = Console(width=RIGHT, height=ROWS, file=io.StringIO(), force_terminal=True, color_system="truecolor",
                      record=True, legacy_windows=False, safe_box=False)
    console.print(app.screen._compositor.render_update(full=True, screen_stack=app._background_screens, simplify=True))
    return list(Segment.split_lines(console._record_buffer))[:ROWS]


class Frame:
    def __init__(self, left: list[Text], right: list[list[Segment]], bg: str) -> None:
        self.left, self.right, self.bg = left, right, bg

    def __rich_console__(self, console, options):
        bg = Style(bgcolor=self.bg)
        divider = Segment("│", Style(color="#3c3c3c", bgcolor=self.bg))
        for row in range(ROWS):
            text = self.left[row].copy()
            text.truncate(LEFT, pad=True)
            for seg in text.render(console):
                yield Segment(seg.text, bg + seg.style if seg.style else bg)
            yield Segment(" ", bg)
            yield divider
            yield from self.right[row]
            yield Segment.line()


def frame_svg(left: list[Text], right: list[list[Segment]], bg: str, title: str) -> str:
    console = Console(width=LEFT + 2 + RIGHT, height=ROWS, file=io.StringIO(), force_terminal=True,
                      color_system="truecolor", record=True, legacy_windows=False, safe_box=False)
    console.print(Frame(left, right, bg))
    return console.export_svg(title=title)


# -- the script -----------------------------------------------------------------------

async def record(tmp: str) -> list[tuple[str, int]]:
    demo = Demo(root=os.path.join(tmp, "full"))
    now = demo.now
    stage = Stage(demo, os.path.join(tmp, "live"))
    a16 = "a5e93b04"

    judge: Judge = demo.judge
    judge.latest = lambda aid, at=None: Judge.latest(judge, aid, at or Clock.v)

    start = now - timedelta(minutes=97.5)
    Clock.v = start
    stage.advance(Clock.v)
    lead = Lead(stage, now - timedelta(minutes=97))
    app = LaneApp([stage.ref], at=None, thresholds=Thresholds(), notify=False, judge=judge, title_prefix=None,
                  prober=demo.prober)
    frames: list[tuple[str, int]] = []
    n = 0

    async with app.run_test(size=(RIGHT, ROWS), notifications=True) as pilot:
        table = app.query_one("#agents")
        bg = app.screen.styles.background.hex

        async def shot(ms: int, title: str = "✳ checkout-v2 sprint") -> None:
            nonlocal n
            stage.advance(Clock.v)
            app.tick()
            await pilot.pause(0.05)
            frames.append((frame_svg(lead.render(Clock.v, n), tracon_lines(app), bg, title), ms))
            n += 1

        def at(minutes: float) -> None:
            Clock.v = now + timedelta(minutes=minutes)

        await pilot.pause(0.5)
        # 1. You prompt the lead.
        await shot(900)
        for i in range(1, len("/expedite checkout-v2") + 1, 2):
            lead.typed = "/expedite checkout-v2"[:i]
            await shot(70)
        lead.typed = "/expedite checkout-v2"
        await shot(500)
        lead.typed = ""
        lead.busy = "Orchestrating"
        at(-96.9)
        await shot(700)

        # 2. The run, an hour and a half in a few seconds.
        steps = 52
        for i in range(1, steps + 1):
            at(-96.9 + 96.9 * i / steps)
            await shot(150)
        lead.busy = None
        await shot(1400)

        # 3. You pick the stuck agent in tracon and press m to nudge the lead.
        title = "◎ checkout-v2 sprint"
        target = app._order.index(a16)
        while table.cursor_row < target:
            await pilot.press("down")
            await shot(260, title)
        await shot(900, title)
        await pilot.press("m")
        await pilot.pause(0.4)
        await shot(2400, title)  # the "nudged the lead" toast

        # 4. The note lands in the lead, which nudges the agent.
        note = SENT[-1] if SENT else ""
        first, rest = note.split("\n", 1) if "\n" in note else (note, "")
        lead.extra.append(user_block(first, more=len(rest.strip().splitlines())))
        at(0.15)
        await shot(1300, title)
        lead.busy = "Thinking"
        at(0.25)
        await shot(700, title)
        lead.busy = None
        reply = "Redis isn't running in the worktrees. Mock the client in the tests instead of calling the real job."
        lead.extra.append(say("#16 has retried the same dry-run six times against a Redis that isn't there. "
                              "Pointing it at a mock."))
        at(0.35)
        await shot(900, title)
        lead.extra.append(tool("SendMessage", "#16 Inventory sync job", reply))
        ts = lambda m: (now + timedelta(minutes=m)).isoformat().replace("+00:00", "Z")  # noqa: E731
        wt = demo.w[a16].cwd
        stage.add(f"{SESSION_ID}.jsonl", {"type": "assistant", "timestamp": ts(0.45), "cwd": demo.w[a16].cwd,
                                          "message": {"id": "msg_nudge", "content": [{"type": "tool_use", "id": "toolu_nudge",
                                                      "name": "SendMessage", "input": {"to": a16, "message": reply}}]}})
        stage.add(f"{SESSION_ID}.jsonl", {"type": "user", "timestamp": ts(0.46), "cwd": demo.w[a16].cwd, "message": {
            "content": [{"type": "tool_result", "tool_use_id": "toolu_nudge", "content": "sent", "is_error": False}]}})
        rel = f"{SESSION_ID}/subagents/agent-{a16}.jsonl"
        at(0.5)
        await shot(1300, title)

        # 5. The agent changes approach; tracon clears its flags.
        for i, (m, name, inp, result) in enumerate([
            (0.8, "Read", {"file_path": f"{wt}/tests/test_inventory_sync.py"}, "…"),
            (1.2, "Edit", {"file_path": f"{wt}/tests/test_inventory_sync.py", "old_string": "a", "new_string": "b"}, "ok"),
            (1.7, "Edit", {"file_path": f"{wt}/jobs/inventory_sync.py", "old_string": "a", "new_string": "b"}, "ok"),
            (2.3, "Bash", {"command": "uv run pytest tests/test_inventory_sync.py -q", "description": "Run tests"}, "7 passed"),
        ]):
            tid = f"toolu_after{i}"
            stage.add(rel, {"type": "assistant", "timestamp": ts(m), "cwd": wt, "message": {"id": f"msg_after{i}",
                            "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}],
                            "usage": {"input_tokens": 50, "cache_read_input_tokens": 118_000, "output_tokens": 300}}})
            stage.add(rel, {"type": "user", "timestamp": ts(m + 0.05), "cwd": wt, "message": {"content": [
                {"type": "tool_result", "tool_use_id": tid, "content": result, "is_error": False}]}})
        judge.history[a16].append(Judgment("on_track", "Mocking the Redis client in the sync tests", "", ts(2.0), 0, 0, 0.0012))
        for m in (0.9, 1.3, 1.8, 2.4):
            at(m)
            await shot(800, title)
        await shot(3500, title)
    return frames


def main() -> None:
    from PIL import Image

    with tempfile.TemporaryDirectory() as tmp:
        frames = asyncio.run(record(tmp))
        svgs = []
        for i, (svg, _) in enumerate(frames):
            path = os.path.join(tmp, f"{i:03d}.svg")
            with open(path, "w") as f:
                f.write(svg)
            svgs.append(path)
        import re

        _, _, w, h = map(float, re.search(r'viewBox="([^"]+)"', frames[0][0]).group(1).split())
        pngs = [p[:-4] + ".png" for p in svgs]
        with ThreadPoolExecutor(4) as pool:
            list(pool.map(lambda i: chrome_png("file://" + svgs[i], pngs[i], int(w + 0.99), int(h + 0.99), scale=1),
                          range(len(svgs))))
        imgs = [Image.open(p).convert("RGB") for p in pngs]
        if GIF_WIDTH and imgs[0].width > GIF_WIDTH:
            size = (GIF_WIDTH, round(imgs[0].height * GIF_WIDTH / imgs[0].width))
            imgs = [im.resize(size, Image.LANCZOS) for im in imgs]
        # One palette from a few representative frames keeps colours steady.
        picks = [imgs[0], imgs[len(imgs) // 2], imgs[-8], imgs[-1]]
        sheet = Image.new("RGB", (imgs[0].width, imgs[0].height * len(picks)))
        for k, im in enumerate(picks):
            sheet.paste(im, (0, k * im.height))
        pal = sheet.quantize(colors=200, method=Image.Quantize.MEDIANCUT)
        out = [im.quantize(palette=pal, dither=Image.Dither.NONE) for im in imgs]
        out[0].save(OUT, save_all=True, append_images=out[1:], optimize=True, loop=0,
                    duration=[ms for _, ms in frames])
    print(f"{OUT}: {len(frames)} frames, {sum(ms for _, ms in frames) / 1000:.1f}s")


if __name__ == "__main__":
    main()
