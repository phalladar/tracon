import asyncio

from tracon.app import LaneApp, ToolGroup, detail_blocks
from tracon.demo import Demo
from tracon.signals import Thresholds


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
            seen = [[app.tools_open(drifter, k) for k in groups]]
            await pilot.click("#detail-feed-text", offset=(9, 1))  # the ▸ of the first run (row 0 is the brief)
            await pilot.pause(0.1)
            seen.append([app.tools_open(drifter, k) for k in groups])
            await pilot.press("e")
            await pilot.pause(0.1)
            seen.append([app.tools_open(drifter, k) for k in groups])
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
            return app.tools_open(drifter, last), preview.max_scroll_y > 0

    opened, scrolls = asyncio.run(run())
    assert opened and scrolls  # its 23 calls no longer fit in the pane
