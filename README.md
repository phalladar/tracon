# tracon

**Air traffic control for parallel Claude Code subagents.** A live terminal (and browser)
monitor that flags stuck, blocked and drifting agents, and the worktree file conflicts
headed for your merge.

When one Claude Code session orchestrates several subagents (a tech-lead skill dispatching
tasks to worktree agents, say), the status line tells you almost nothing. Like a
[TRACON](https://en.wikipedia.org/wiki/Terminal_radar_approach_control_facilities) controller
watching every aircraft on one scope, `tracon` shows every agent at once: what it is doing,
whether it is holding, whether it has drifted off its brief, and whether two of them are on
a collision course in the same file. Run it in a terminal split next to your session.

It reads only what Claude Code already writes to disk. It never writes to your sessions or
your repository.

> tracon is an independent project. It is not affiliated with, endorsed by or sponsored by
> Anthropic. "Claude" and "Claude Code" are trademarks of Anthropic.

## What it catches

| Problem | How it shows up |
|---|---|
| An agent waiting on a permission prompt nobody can answer | **⛔ BLOCKED**: an instant tool call (Edit, Read, `git status`) has been open for 45 s |
| An agent that went quiet | **⏸ IDLE**: no transcript activity for 5 min and nothing running |
| A test run that hangs | **🐌 STALLED**: a process runs for 20 min with no file change or commit |
| Work left behind | **👻 ORPHANED**: a detached process it started is still running after its parent died |
| Loops | **LOOP**: the same call 3× in a row with no edit in between |
| Permission denials | **DENIED**, with the classifier's reasons (e.g. "Modify Shared Resources") |
| Editing the wrong checkout | **OUTSIDE**: a worktree agent edited the main checkout |
| Working on an old base | **STALE-BASE**: it edited before merging the base branch, after the lead had merged something |
| Two agents changing one file | **COLLIDE**, plus a file heatmap: concurrent edits that will meet at merge |
| Ignoring the brief | **BG** (detached or background runs when the brief says foreground), **OFF-BRIEF** files |
| Drift a rule can't see | **↯ DRIFT / ⟳ STUCK** from an optional LLM check of brief vs. recent activity |
| Context bloat, uncommitted work | **CTX** above 300k / 450k tokens, **UNCOMMITTED** edits older than 45 min |

While it runs, a macOS notification fires when an agent needs you.

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
git clone <this repo> tracon && cd tracon
uv tool install --editable .            # puts `tracon` on your PATH
# optional: judge through the Anthropic API instead of `claude -p`
uv tool install --editable '.[api]'
```

## Use

```sh
cd your-project && tracon     # newest session with subagents in this project (or anywhere)
tracon web                    # browser view only
tracon --list                 # recent sessions that spawned subagents
tracon -s 4f2a                # pick a session by id prefix
tracon --at "2026-10-11 07:20"   # replay a session as it stood at that time
tracon --once                 # print one snapshot and exit
```

A good layout: your orchestrating session on one side, `tracon` in a split next to it
(in Ghostty, `cmd+d`).

### Terminal keys

| Key | |
|---|---|
| `enter` | agent detail: state, judge verdict, flags, files, live processes, full activity feed with the lead's messages |
| `t` | timeline: every agent across the run, coloured by activity, with the lead's merges and messages |
| `x` | files heatmap: which agents edited which files, concurrent edits marked `!` |
| `w` | open the browser view |
| `j` | ask the judge about the selected agent now |
| `[` `]` / `{` `}` | rewind / forward 5 / 30 min (enters replay) · `space` play · `l` back to live |
| `o` / `c` | open the agent's worktree in a new Ghostty window / copy its path |
| `h` / `s` / `q` | hide finished agents / next session / quit |

### Browser view

`w` in the terminal, or `tracon web`, serves a page on `127.0.0.1`:

- **Timeline**: zoom with the scroll wheel, drag to pan, double-click to fit. Hover any bar
  for the calls it covers. Silent stretches past the idle threshold are drawn in orange.
- **Files**: heatmap of edits per file per agent, shared files first.
- **Replay scrubber**: drag through the run, or press ▶ to play it back.
- Click an agent for its detail drawer and live activity feed.

## The judge (optional, on by default)

Rules catch mechanical failures. For drift, `tracon` periodically asks a small model
(Claude Haiku 5.5) to read an agent's brief, the lead's messages to it, and its last 40
actions, and answer `on_track`, `drifting`, `stuck` or `unclear` with a one-line summary.

- **When:** only active agents, at most every 3 min each, and only after 5 new tool calls.
  Finished and idle agents are never judged.
- **Cost:** about $0.001–0.002 per check. A busy run of five agents over several hours stays
  well under a dollar. A per-run budget (default $1.00) stops judging when reached, and the
  spend is shown in the header.
- **Backends:** with `ANTHROPIC_API_KEY` set and the `[api]` extra installed, it uses the
  Anthropic SDK. Otherwise it uses `claude -p` on your Claude Code login, counting against
  your plan.
- **Privacy:** checks send the brief and summaries of recent tool calls (commands, file
  paths, error text) to Anthropic, as your sessions already do. `--no-judge` turns it off.

Judgments are cached in `~/.cache/tracon/judgments.json`, so replays and restarts show them.

## Options

| Flag | Default | |
|---|---|---|
| `--project PATH` | current dir | which project's sessions to watch |
| `--session ID` | newest | session id or prefix |
| `--at TIME` | live | replay as of a local time (`07:20` or `2026-10-11 07:20`) |
| `--idle MIN` | 5 | silence before IDLE |
| `--stall MIN` | 20 | running without change before STALLED |
| `--no-notify` | | no macOS notifications |
| `--no-judge` | | turn the LLM check off |
| `--judge-model` | `claude-haiku-5-5` | model for the check |
| `--judge-backend` | `auto` | `api` or `cli` |
| `--judge-budget USD` | 1.00 | stop judging after this much per run |
| `--judge-every MIN` | 3 | minimum time between checks of one agent |
| `--port` | 8765 | browser view port (the next free one if taken) |

## How it works

Claude Code writes, per session:

```
~/.claude/projects/<encoded project path>/
  <session>.jsonl                     the orchestrating ("lead") session
  <session>/subagents/agent-<id>.jsonl  each subagent's transcript
  <session>/subagents/agent-<id>.meta.json  description, model, worktree, branch
```

`tracon` tails these files incrementally. Every few seconds it also checks, per worktree,
which processes are running there (`lsof` and `ps`, keeping only what Claude launched, plus
orphans) and the git state (branch, commits ahead, dirty files). States and flags are derived
from both.

```
tracon/
  transcript.py  JSONL -> compact events (all knowledge of the transcript format lives here)
  session.py     find sessions, track the lead and its agents
  probe.py       live processes and git state per worktree
  signals.py     one agent -> state + flags (pure, testable)
  lane.py        whole-run view: file collisions, judge results, assess_all()
  judge.py       the optional LLM check, its scheduling, budget and cache
  app.py         Textual TUI
  web.py         local HTTP server + JSON API; static/index.html is the page
  cli.py         entry point
```

## Limitations

- Transcript files are Claude Code internals, not a public API. A format change may break
  parsing; the fix belongs in `transcript.py`.
- Built and tested on macOS. Notifications (`osascript`), copy (`pbcopy`) and "open in
  Ghostty" are macOS-only; the rest should work on Linux with `lsof` installed.
- In replay there is no process data, so a long-running shell call shows as **PENDING**
  rather than RUNNING or BLOCKED.

## Development

```sh
uv sync --group dev
uv run pytest                      # synthetic transcripts; no Claude Code or network needed
uv run tracon --once --no-judge   # quick check against your latest session
```

See [CLAUDE.md](CLAUDE.md) for architecture notes and conventions.

## License

[MIT](LICENSE)
