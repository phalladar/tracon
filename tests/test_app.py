import asyncio

from tracon.app import LaneApp
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
