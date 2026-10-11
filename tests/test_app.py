import asyncio

from datetime import datetime, timezone

from rich.console import Console

from tracon.app import LaneApp, ToolGroup, detail_blocks, render_detail
from tracon.demo import Demo
from tracon.signals import Thresholds
from tracon.transcript import Event


def test_rebuilding_the_table_keeps_the_mouse_hover(tmp_path):
    """The table is rebuilt every tick; the hovered row must not jump back to the first one."""
    demo = Demo(root=str(tmp_path))

    async def run():
        app = LaneApp([demo.ref], at=None, thresholds=Thresholds(), notify=False, judge=demo.judge,
                      title_prefix=None, prober=demo.prober)
        async with app.run_test(size=(216, 30)) as pilot:
            await pilot.pause(0.5)
            table = app.query_one("#agents")
            await pilot.hover("#agents", offset=(20, 5))  # header is line 0, so body row 4
            await pilot.pause(0.1)
            assert table.hover_coordinate.row == 4
            app.render_table()
            await pilot.pause(0.1)
            return table.hover_coordinate.row

    assert asyncio.run(run()) == 4


def test_d_dismisses_the_drift_and_d_again_restores_it(tmp_path):
    demo = Demo(root=str(tmp_path))
    drifter = "a2b84f61"  # the demo's agent restyling the admin pages

    async def run():
        app = LaneApp([demo.ref], at=None, thresholds=Thresholds(), notify=False, judge=demo.judge,
                      title_prefix=None, prober=demo.prober)
        async with app.run_test(size=(216, 30)) as pilot:
            await pilot.pause(0.5)
            app.query_one("#agents").move_cursor(row=app._order.index(drifter))
            seen = []
            for _ in range(2):
                await pilot.press("d")
                await pilot.pause(0.1)
                st = app.statuses[drifter]
                seen.append((st.dismissed, "DRIFT" in [f.code for f in st.flags]))
            return seen

    assert asyncio.run(run()) == [(True, False), (False, True)]
    assert not (tmp_path / "dismissed.json").exists()


def test_detail_shows_what_the_agent_said_and_folds_the_tool_calls(tmp_path):
    demo = Demo(root=str(tmp_path))
    drifter = "a2b84f61"  # says something twice between runs of tool calls

    async def run():
        app = LaneApp([demo.ref], at=None, thresholds=Thresholds(), notify=False, judge=demo.judge,
                      title_prefix=None, prober=demo.prober)
        async with app.run_test(size=(216, 40)) as pilot:
            await pilot.pause(0.5)
            app.query_one("#agents").move_cursor(row=app._order.index(drifter))
            await pilot.press("enter")
            await pilot.pause(0.2)
            blocks = detail_blocks(app.session.agents[drifter], app.session, None)
            groups = [b.key for b in blocks if isinstance(b, ToolGroup)]
            said = [b[0].text for b in blocks if not isinstance(b, ToolGroup) and b[0].kind == "text"]
            seen = [[app.is_open(drifter, k) for k in groups]]
            await pilot.click("#detail-feed-text", offset=(9, 1))  # the ▸ of the first run (row 0 is the brief)
            await pilot.pause(0.1)
            seen.append([app.is_open(drifter, k) for k in groups])
            await pilot.press("e")
            await pilot.pause(0.1)
            seen.append([app.is_open(drifter, k) for k in groups])
            return [type(b).__name__ if isinstance(b, ToolGroup) else b[0].kind for b in blocks], said, seen

    kinds, said, seen = asyncio.run(run())
    assert kinds == ["brief", "ToolGroup", "text", "ToolGroup", "text", "ToolGroup"]
    assert "\n\nI'll tidy the dashboard layout" in said[1]  # in full, line breaks kept
    assert seen == [[False] * 3, [True, False, False], [True] * 3]


def test_the_preview_under_the_table_folds_tool_calls_too(tmp_path):
    demo = Demo(root=str(tmp_path))
    drifter = "a2b84f61"

    async def run():
        app = LaneApp([demo.ref], at=None, thresholds=Thresholds(), notify=False, judge=demo.judge,
                      title_prefix=None, prober=demo.prober)
        async with app.run_test(size=(216, 40)) as pilot:
            await pilot.pause(0.5)
            app.query_one("#agents").move_cursor(row=app._order.index(drifter))
            await pilot.pause(0.3)
            preview = app.query_one("#preview")
            assert preview.max_scroll_y == 0  # folded, the whole feed fits
            last = [b.key for b in detail_blocks(app.session.agents[drifter], app.session, None) if isinstance(b, ToolGroup)][-1]
            # brief, run, said, run, said (three lines), run: the newest run is row 7
            await pilot.click("#preview-text", offset=(9, 7))
            await pilot.pause(0.1)
            return app.is_open(drifter, last), preview.max_scroll_y > 0

    opened, scrolls = asyncio.run(run())
    assert opened and scrolls  # its 23 calls no longer fit in the pane


def test_a_long_brief_or_lead_message_shows_three_lines_until_opened():
    ts = datetime(2026, 10, 11, 17, 0, tzinfo=timezone.utc)
    brief = Event(ts, "brief", text="You are the board clerk. " * 40 + "The very last instruction.")
    msg = Event(ts, "tool_use", tool="SendMessage", extra={"summary": "Change of plan", "message": "line\n" * 9})
    short = Event(ts, "brief", text="Fix the login form.")
    blocks = [(brief, False, 0), (msg, True, -1), (short, False, 1)]

    def shown(opened: set[int]) -> str:
        console = Console(width=100, record=True)
        console.print(render_detail(blocks, "a1", lambda _owner, key: key in opened, 100))
        return console.export_text()

    folded = [line.rstrip() for line in shown(set()).splitlines()]
    assert len(folded) == 3 + 3 + 1
    assert folded[2].endswith("more lines") and "very last" not in "".join(folded)
    assert "▸ 7 more lines" in folded[5]  # summary, then 9 lines: 3 shown
    assert "Fix the login form." in folded[6]
    opened = shown({0, -1})
    assert "The very last instruction." in opened and opened.count("▴ less") == 2


class FakeChooser:
    def __init__(self, refs, neighbour=None):
        self.list, self.neighbour = refs, neighbour

    def refs(self):
        return self.list


def other_session(tmp_path, sid, agents=0, mtime=1000.0):
    from test_choose import write_session

    from tracon.session import session_ref

    write_session(tmp_path / "projects", "/work/repo", sid, agents=agents, title=f"title of {sid}", mtime=mtime)
    return session_ref(sid, "/work/repo", str(tmp_path / "projects"))


def test_follows_the_split_and_new_sessions_but_not_older_ones(tmp_path):
    demo = Demo(root=str(tmp_path / "demo"))
    old = other_session(tmp_path, "old-one", agents=1)
    beside = other_session(tmp_path, "beside")
    fresh = other_session(tmp_path, "fresh", agents=1)
    chooser = FakeChooser([demo.ref, old])

    async def run():
        app = LaneApp([demo.ref, old], at=None, thresholds=Thresholds(), notify=False, judge=demo.judge,
                      title_prefix=None, prober=demo.prober, chooser=chooser)
        seen = []
        async with app.run_test(size=(216, 30)) as pilot:
            await pilot.pause(0.3)
            for refs, neighbour in [([old, demo.ref], None), ([beside, demo.ref], "beside"), ([fresh, beside], None)]:
                chooser.list, chooser.neighbour = refs, neighbour
                app.follow_tick()
                await app.workers.wait_for_complete()
                await pilot.pause(0.2)
                seen.append(app.session.ref.session_id)
        return seen

    # An older session with new activity doesn't steal the view; the split and a new session do.
    assert asyncio.run(run()) == [demo.ref.session_id, "beside", "fresh"]


def test_s_lists_sessions_newest_first_and_picking_one_stops_following(tmp_path, monkeypatch):
    import tracon.app as app_mod

    demo = Demo(root=str(tmp_path / "demo"))
    old = other_session(tmp_path, "old-one", agents=1, mtime=1000)
    older = other_session(tmp_path, "older-one", agents=2, mtime=500)
    monkeypatch.setattr(app_mod, "find_sessions", lambda: [old, older])
    chooser = FakeChooser([demo.ref])

    async def run():
        app = LaneApp([demo.ref], at=None, thresholds=Thresholds(), notify=False, judge=demo.judge,
                      title_prefix=None, prober=demo.prober, chooser=chooser)
        async with app.run_test(size=(216, 30)) as pilot:
            await pilot.pause(0.3)
            await pilot.press("s")
            await pilot.pause(0.2)
            table = app.screen.query_one("#sessions")
            rows = [table.get_row_at(i) for i in range(table.row_count)]
            listed = [(str(r[0]), str(r[3])) for r in rows]
            await pilot.press("down", "enter")
            await pilot.pause(0.2)
            return listed, app.session.ref.session_id, app.chooser

    listed, watching, chooser_after = asyncio.run(run())
    # The watched demo session is current (●), and newest, so first; the cursor starts on it.
    assert [mark for mark, _ in listed] == ["●", "", ""]
    assert [title for _, title in listed][1:] == ["title of old-one", "title of older-one"]
    assert watching == "old-one" and chooser_after is None
