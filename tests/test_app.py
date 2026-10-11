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
