"""An LLM read of whether an agent is on track: one cheap Haiku call per agent, now and then.

Two backends, picked automatically:
- "api": the Anthropic SDK, when ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN is set and the
  `anthropic` package is installed (`uv tool install --editable '.[api]'`).
- "cli": `claude -p` on the user's Claude Code login. No key needed; counts against the plan.

Calls are rate-limited per agent, skipped for finished or quiet agents, capped by a session
budget, and cached on disk so a restart or a replay reuses them.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime

from .session import Agent, Session
from .transcript import EDIT_TOOLS, HANDBACK_TOOLS, iter_until, parse_ts

DEFAULT_MODEL = "claude-haiku-5-5"
CACHE_DIR = os.path.expanduser("~/.cache/lanectl")
VERDICTS = ("on_track", "drifting", "stuck", "unclear")

SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "doing": {"type": "string"},
        "concern": {"type": "string"},
    },
    "required": ["verdict", "doing", "concern"],
    "additionalProperties": False,
}

SYSTEM = """You watch one coding agent for the human who dispatched it, and say in one glance whether it is still doing the job its brief gives it.

Judge only from the brief and the activity shown. Verdicts:
- on_track: the recent actions serve the brief's steps, even if slowly.
- drifting: it is doing work the brief does not ask for, widening scope, or breaking a rule the brief states (for example running tests in the background when told to run them in the foreground, or editing files the brief says not to touch).
- stuck: it repeats the same actions without progress, retries a failure without changing approach, or waits on something that will not come.
- unclear: too little activity to tell.

Fields:
- doing: what it is doing right now, at most 12 words, concrete (name the file, test or step). No preamble.
- concern: for drifting or stuck, one sentence of at most 25 words naming the problem and the evidence. Empty string for on_track and unclear."""


@dataclass
class Judgment:
    verdict: str
    doing: str
    concern: str
    as_of: str  # ISO timestamp of the last event the judgment saw
    n_calls: int  # tool calls seen
    made: float  # epoch seconds
    cost: float = 0.0
    error: str = ""

    @property
    def as_of_dt(self) -> datetime:
        return parse_ts(self.as_of)


def pick_backend(requested: str = "auto") -> str:
    if requested != "auto":
        return requested
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        try:
            import anthropic  # noqa: F401

            return "api"
        except ImportError:
            pass
    return "cli" if shutil.which("claude") else "off"


class Judge:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        backend: str = "auto",
        budget_usd: float = 1.0,
        min_interval_s: float = 180,
        min_new_calls: int = 5,
        cache_dir: str = CACHE_DIR,
    ) -> None:
        self.model = model
        self.backend = pick_backend(backend)
        self.budget_usd = budget_usd
        self.min_interval_s = min_interval_s
        self.min_new_calls = min_new_calls
        self.spent = 0.0
        self.in_flight: set[str] = set()
        self._lock = threading.Lock()
        self._client = None
        os.makedirs(cache_dir, exist_ok=True)
        self.cache_path = os.path.join(cache_dir, "judgments.json")
        self.history: dict[str, list[Judgment]] = self._load()

    # -- cache --------------------------------------------------------------

    def _load(self) -> dict[str, list[Judgment]]:
        try:
            with open(self.cache_path) as f:
                raw = json.load(f)
            return {k: [Judgment(**j) for j in v] for k, v in raw.items()}
        except (OSError, ValueError, TypeError):
            return {}

    def _save(self) -> None:
        tmp = self.cache_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({k: [asdict(j) for j in v] for k, v in self.history.items()}, f)
        os.replace(tmp, self.cache_path)

    def reload(self) -> None:
        """Pick up judgments another lanectl process wrote."""
        with self._lock:
            self.history = self._load()

    def latest(self, agent_id: str, at: datetime | None = None) -> Judgment | None:
        for j in reversed(self.history.get(agent_id, [])):
            if not j.error and (at is None or j.as_of_dt <= at):
                return j
        return None

    # -- scheduling ---------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self.backend != "off"

    @property
    def over_budget(self) -> bool:
        return self.spent >= self.budget_usd

    def due(self, agent: Agent, finished: bool) -> bool:
        if not self.enabled or self.over_budget or finished or agent.id in self.in_flight:
            return False
        calls = sum(1 for e in agent.transcript.events if e.kind == "tool_use")
        last = self.history.get(agent.id, [])
        if not last:
            return calls >= self.min_new_calls
        prev = last[-1]
        return calls - prev.n_calls >= self.min_new_calls and time.time() - prev.made >= self.min_interval_s

    # -- the call -----------------------------------------------------------

    def run(self, agent: Agent, session: Session, state_line: str = "", at: datetime | None = None) -> Judgment:
        """Blocking; call from a worker thread. `at` judges the agent as it stood then (backfill)."""
        with self._lock:
            self.in_flight.add(agent.id)
        try:
            prompt, n_calls, as_of = build_prompt(agent, session, state_line, at)
            try:
                data, cost = self._call_api(prompt) if self.backend == "api" else self._call_cli(prompt)
                j = Judgment(data["verdict"], data["doing"].strip(), data["concern"].strip(), as_of, n_calls, time.time(), cost)
            except Exception as exc:  # recorded and shown; a failed check never breaks the view
                j = Judgment("unclear", "", "", as_of, n_calls, time.time(), error=str(exc)[:300])
            with self._lock:
                self.spent += j.cost
                hist = self.history.setdefault(agent.id, [])
                hist.append(j)
                hist.sort(key=lambda x: x.as_of)
                self._save()
            return j
        finally:
            with self._lock:
                self.in_flight.discard(agent.id)

    def _call_cli(self, prompt: str) -> tuple[dict, float]:
        proc = subprocess.run(
            [
                "claude", "-p",
                "--model", self.model,
                "--effort", "low",
                "--output-format", "json",
                "--json-schema", json.dumps(SCHEMA),
                "--system-prompt", SYSTEM,
                "--tools", "",
                "--no-session-persistence",
                "--setting-sources", "",
                "--strict-mcp-config",
            ],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=CACHE_DIR,  # keep the project's CLAUDE.md and hooks out of it
        )
        out = json.loads(proc.stdout or "{}")
        if out.get("is_error") or "structured_output" not in out:
            raise RuntimeError(out.get("result") or proc.stderr.strip() or "no structured output")
        return out["structured_output"], float(out.get("total_cost_usd") or 0.0)

    def _call_api(self, prompt: str) -> tuple[dict, float]:
        import anthropic

        if self._client is None:
            self._client = anthropic.Anthropic()
        try:
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=4000,
                system=SYSTEM,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            )
        except anthropic.RateLimitError as e:
            raise RuntimeError("rate limited; will retry on the next interval") from e
        except anthropic.APIStatusError as e:
            raise RuntimeError(f"API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise RuntimeError("network error reaching the API") from e
        if resp.stop_reason == "refusal":
            raise RuntimeError("model declined to judge this transcript")
        if resp.stop_reason == "max_tokens":
            raise RuntimeError("judgment cut off at max_tokens")
        text = next(b.text for b in resp.content if b.type == "text")
        u = resp.usage
        # Haiku 5.5 list price; cache reads/writes are small here and folded into input.
        tokens_in = u.input_tokens + (u.cache_creation_input_tokens or 0) + (u.cache_read_input_tokens or 0)
        cost = tokens_in * 0.10 / 1e6 + u.output_tokens * 0.50 / 1e6
        return json.loads(text), cost


def build_prompt(agent: Agent, session: Session, state_line: str = "", at: datetime | None = None, last_n: int = 40):
    """The brief in full, the lead's messages, files edited, and the last `last_n` actions."""
    events = list(iter_until(agent.transcript.events, at))
    tool_uses = [e for e in events if e.kind == "tool_use"]
    lines = []
    shown = 0
    for e in reversed(events):
        if e.kind == "tool_use":
            shown += 1
            tag = "REPORT" if e.tool in HANDBACK_TOOLS else e.tool
            lines.append(f"{_hm(e.ts)} {tag}: {e.summary}" + ("  [background]" if e.background else ""))
        elif e.kind == "tool_result" and e.is_error:
            lines.append(f"{_hm(e.ts)}   failed: {e.summary[:160]}")
        elif e.kind == "text":
            lines.append(f"{_hm(e.ts)}   (agent says) {e.summary}")
        elif e.kind == "coordinator":
            lines.append(f"{_hm(e.ts)}   (lead says) {e.summary}")
        if shown >= last_n:
            break
    lines.reverse()
    edited = sorted({e.path for e in tool_uses if e.tool in EDIT_TOOLS and e.path})
    root = agent.workdir.rstrip("/") + "/"
    edited = [p[len(root):] if p.startswith(root) else p for p in edited]
    lead = [f"- {_hm(e.ts)} {(e.extra or {}).get('summary') or ''}: {(e.extra or {}).get('message', '')[:600]}" for e in session.lead_messages(agent.id, at)]
    prompt = "\n\n".join(
        part
        for part in [
            f"Agent: {agent.description} (model {agent.model}), {len(tool_uses)} tool calls so far.",
            f"Monitor's mechanical state: {state_line}" if state_line else "",
            "Brief:\n" + agent.transcript.brief,
            "Lead's messages to it:\n" + "\n".join(lead) if lead else "",
            "Files edited so far: " + (", ".join(edited) if edited else "none"),
            f"Last {shown} actions, oldest first:\n" + "\n".join(lines),
            "Is this agent on track?",
        ]
        if part
    )
    as_of = (events[-1].ts if events else datetime.now()).isoformat()
    return prompt, len(tool_uses), as_of


def _hm(ts: datetime) -> str:
    return ts.astimezone().strftime("%H:%M")
