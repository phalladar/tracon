# tracon

Air traffic control for parallel Claude Code subagents. Read-only monitor for Claude Code subagents: tails session transcripts under
`~/.claude/projects/`, probes worktrees, and shows state/flags in a Textual TUI and a local
web page. Python 3.12+, managed with uv.

## Commands

```sh
uv run pytest                                   # all tests; fast, offline, no Claude Code needed
uv run tracon --once --no-judge                 # snapshot of the newest session, no TUI
uv run tracon --once --no-judge --at "HH:MM"    # replay a moment (needs a real session)
uv run tracon web --no-judge                    # browser view only
uv run tracon --demo                            # made-up run; no session needed
uv run --with pillow python scripts/screenshots.py  # regenerate docs/screenshots + replay.gif
uv run --with pillow python scripts/hero.py         # regenerate the README hero (split terminal)
```

`tracon` on PATH is an editable `uv tool` install, so code changes apply immediately.

## Architecture (data flows top to bottom)

- `transcript.py`: JSONL to `Event`s, read incrementally by byte offset. **The only module that
  knows the transcript format.** Format fixes go here and nowhere else.
- `session.py`: session discovery (`find_sessions`), `Session` = lead `Transcript` + `Agent`s,
  plus lead-side facts (messages to an agent, `TaskStop`, merges, lane name).
- `probe.py`: `lsof` (macOS) or `/proc` (Linux) + `ps` + `git` per worktree. Process ownership is decided by walking
  ancestors: a Claude shell-snapshot or `claude` ancestor means agent; a login/terminal means
  the user's own shell (ignored); reparented to pid 1 (or a `systemd --user` subreaper) means orphan.
- `signals.py`: `assess(agent, session, now, probe)` returns a `Status` (state + flags). Pure:
  no I/O, no clock except `now_utc()` (tests patch it). `now=None` is live; a datetime is
  replay and ignores probes.
- `lane.py`: `assess_all()` is the entry point every view uses. It adds the cross-agent
  facts (file collisions via `file_matrix`) and the judge's verdicts on top of `assess`.
- `judge.py`: optional LLM check. Two backends: SDK (`api`, needs a key and the `[api]`
  extra) or `claude -p` (`cli`). Scheduling (`due`), budget, and an on-disk cache in
  `~/.cache/tracon/` (verdicts, and the user's dismissals from `d`). Failures are recorded
  on the `Judgment`, never raised. Each `Judgment` carries `PROMPT_VERSION` (a hash of
  `SYSTEM`, the schema and `PROMPT_FORMAT`); bump `PROMPT_FORMAT` when `build_prompt`'s
  layout changes so old verdicts get re-checked.
- `demo.py`: `--demo`. Writes a synthetic lead + seven agents to a temp dir (times relative to
  now), with a canned `DemoProber` and `DemoJudge` (never calls a model) and a ticker thread
  that keeps busy agents busy. `scripts/screenshots.py` drives it for the README images;
  `scripts/hero.py` runs it on a virtual clock next to a scripted Claude Code pane (sending is
  stubbed there) for the hero GIF. Re-run both after UI changes.
- `advice.py`: drafts the nudge `n` sends the lead (state, flags, judge concern, advice
  mirroring README's "What to do"). Pure.
- `inbox.py`: finds the lead's live process in `~/.claude/sessions/<pid>.json` and writes one
  JSON line to its `messagingSocketPath`. **The only module that knows the inbox format.**
- `app.py`: TUI. `tick()` every 2 s, probes every 5 s and the judge every 10 s run in
  `@work(thread=True)` workers and hand results back with `call_from_thread`.
- `web.py` + `static/index.html`: stdlib `ThreadingHTTPServer` on 127.0.0.1. `LaneState` owns
  its own `Session` behind a lock, so it never shares objects with the TUI thread.

## Rules

- Never write to `~/.claude/` or to a watched repo. The only writes are the judge cache
  (verdicts and dismissals) and a nudge to the lead's inbox socket, sent only when the user
  presses `n` (one keypress, no preview; a toast confirms it). Never send automatically, never send to a subagent, and never read
  the `~/.claude/sessions/*.key` files (auth is optional on macOS/Linux).
- Keep `signals.py` pure; anything that needs several agents goes in `lane.py`.
- A new state or flag needs a test in `tests/` built from a synthetic transcript (see the
  `Builder` in `test_signals.py` and `Lane` in `test_lane.py`), and a row in README.md.
- Flags on finished agents drop to `info` (history, not action). Only `OUTSIDE` and `ORPHAN`
  stay loud.
- "Finished" has several sources, strongest first: the lead's `<task-notification>` (status
  completed/failed/killed/stopped; a later event means it was resumed), a `SubagentHandback`
  call, or a final `end_turn` text with nothing pending. A completion while the agent's own
  `run_in_background` shell or background agent hasn't reported back (a `<task-notification>`
  with its `<tool-use-id>` in the agent's transcript) is WAITING (parked mid-task), not DONE.
  Detached shells (`nohup`, `&`) never report back, so they don't park an agent.
- Don't infer the branch from a transcript's `gitBranch` field: it doesn't follow the agent's
  worktree. Use the live probe.
- Web page: build DOM with `textContent` / `createElement`, never `innerHTML`. Transcript text
  is untrusted.
- Colours: activity kinds use categorical slots 1-3 (edit blue, shell orange, read aqua).
  Status uses the fixed good/warning/serious/critical set and always carries an icon or
  label. Don't reuse one family for the other.
- Keep "Claude" and "Anthropic" out of names (package, command, UI titles); "for Claude Code"
  in descriptive text is fine. Keep the not-affiliated note in the README.
- Judge cost guardrails (budget, `min_new_calls`, `min_interval_s`, active agents only) stay
  on by default.

## Gotchas

- Textual `RichLog` wraps lines at the width when written, which is 80 columns before layout.
  The detail feed uses a `Static` in a `VerticalScroll` instead so it reflows.
- The terminal title is set with OSC 2 through the Textual driver; the title stack is pushed
  (`CSI 22;2t`) on mount and popped (`CSI 23;2t`) by `cli.py` after `app.run()` returns.
  Verify escape output by running `tracon` in a `pty` and reading the bytes.
- `DataTable` is rebuilt on every tick; keep the cursor by row key (`_order`).
- Checking UI changes: drive the app with `App.run_test()` + `pilot`, `save_screenshot()` to
  SVG, then `qlmanage -t -s 1800 -o <dir> file.svg` to get a PNG. Headless Chrome screenshots
  of the web page work, but Chrome may not exit afterwards (the page polls), so kill it.
- A fork's transcript opens with a `fork-context-ref` record, then a copy of the lead's message
  that spawned it (a background `Agent` call). `Transcript` skips that copy. Task notifications
  can also arrive as `attachment` records (`queued_command`), in the lead and in agents.
- Inbox wire format (Claude Code 2.1.296, `peerProtocol: 1`): one line
  `{"type":"user","message":{"role":"user","content":"..."}}`, optionally preceded by
  `{"type":"auth","token":...}`. The socket closes a connection that sends no complete line
  within a deadline and never replies, so a silent socket is not an error. Delivery shows in
  the lead as a peer message; it is held for review under `crossSessionInbound: hold`.
- `claude -p` runs with `cwd=~/.cache/tracon`, `--setting-sources ""` and
  `--no-session-persistence`, so judging never loads a project's CLAUDE.md or hooks and
  never creates sessions that tracon would then list.
- README images use absolute `raw.githubusercontent.com` URLs so they also render on PyPI.
  The sdist is an explicit include list in `pyproject.toml`; keep `.claude/` and images out.
