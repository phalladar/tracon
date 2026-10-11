"""Regenerate the README screenshots from the `--demo` run.

    uv run --with pillow python scripts/screenshots.py [out_dir]     # default docs/screenshots

Drives the TUI with Textual's test pilot (SVG) and renders each SVG, and the web view, to PNG
with headless Chrome at 2x. `replay.gif` is the web timeline at successive `?at=` times,
stitched with Pillow. macOS paths; set CHROME to override.
"""

import asyncio
import os
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from urllib.parse import quote

from tracon.app import LaneApp
from tracon.demo import Demo
from tracon.signals import Thresholds
from tracon.web import serve

OUT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "docs/screenshots")
CHROME = os.environ.get("CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


def chrome_png(url: str, out: str, width: int, height: int, scale: float = 2) -> None:
    """Chrome may not exit after --screenshot when the page polls, so wait for the file and kill it."""
    if os.path.exists(out):
        os.remove(out)
    with tempfile.TemporaryDirectory() as profile:
        p = subprocess.Popen(
            [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--force-device-scale-factor={scale}",
             f"--user-data-dir={profile}", f"--window-size={width},{height}", "--virtual-time-budget=4000",
             f"--screenshot={out}", url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(60):
            if os.path.exists(out) and os.path.getsize(out):
                break
            time.sleep(0.5)
        time.sleep(0.5)
        p.kill()
        p.wait()


def svg_to_png(svg: str) -> None:
    with open(svg) as f:
        _, _, w, h = map(float, re.search(r'viewBox="([^"]+)"', f.read()).group(1).split())
    chrome_png("file://" + svg, svg[:-4] + ".png", int(w + 0.99), int(h + 0.99))
    os.remove(svg)


def app_for(demo: Demo) -> LaneApp:
    return LaneApp([demo.ref], at=None, thresholds=Thresholds(), notify=False, judge=demo.judge,
                   title_prefix=None, prober=demo.prober)


async def tui(demo: Demo) -> list[str]:
    shots = []

    def shoot(app, name):
        app.save_screenshot(name + ".svg", OUT)
        shots.append(os.path.join(OUT, name + ".svg"))

    app = app_for(demo)
    async with app.run_test(size=(216, 26)) as pilot:
        await pilot.pause(3)
        table = app.query_one("#agents")

        def select(aid):
            table.move_cursor(row=app._order.index(aid))

        select("a5e93b04")  # #16, stuck in a loop: its feed is all red
        await pilot.pause(0.5)
        shoot(app, "tui-overview")
        select("a2b84f61")  # #13, drifting
        await pilot.press("enter")
        await pilot.pause(1)
        shoot(app, "tui-detail")
        await pilot.press("escape", "t")
        await pilot.pause(1)
        shoot(app, "tui-timeline")
        await pilot.press("escape")

    app = app_for(demo)
    async with app.run_test(size=(110, 26)) as pilot:
        await pilot.pause(2)
        await pilot.press("x", "a")
        await pilot.pause(1)
        shoot(app, "tui-files")
    return shots


def replay_gif(demo: Demo, url: str, frames: int = 44, frame_ms: int = 220, hold_ms: int = 3000) -> None:
    """The web timeline played back: one frame per `?at=` step from the first spawn to now."""
    from PIL import Image

    first = demo.now - timedelta(minutes=92)  # a few minutes after the lead starts dispatching
    times = [first + (demo.now - first) * i / (frames - 1) for i in range(frames)]
    with tempfile.TemporaryDirectory() as tmp:
        paths = [os.path.join(tmp, f"{i:03d}.png") for i in range(frames)]
        with ThreadPoolExecutor(4) as pool:
            list(pool.map(lambda i: chrome_png(f"{url}?at={quote(times[i].isoformat())}", paths[i], 1500, 450, scale=1), range(frames)))
        imgs = [Image.open(p).convert("RGB") for p in paths]
        # One shared palette keeps colours steady from frame to frame.
        pal = imgs[-1].quantize(colors=128, method=Image.Quantize.MEDIANCUT)
        out = [im.quantize(palette=pal, dither=Image.Dither.NONE) for im in imgs]
        out[0].save(os.path.join(OUT, "replay.gif"), save_all=True, append_images=out[1:], optimize=True, loop=0,
                    duration=[frame_ms] * (frames - 1) + [hold_ms])


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    demo = Demo()  # no ticker: the shots should match each other
    for svg in asyncio.run(tui(demo)):
        svg_to_png(svg)
    url = serve(demo.ref, Thresholds(), demo.judge, port=8790, probes=demo.prober.all)
    chrome_png(url, os.path.join(OUT, "web-timeline.png"), 1500, 450)
    replay_gif(demo, url)
    print(f"screenshots in {OUT}")


if __name__ == "__main__":
    main()
