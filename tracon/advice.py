"""A note to the lead about one agent: what tracon sees and what the lead can do about it.
Mirrors the "What to do" columns in README.md, phrased for the lead's own tools."""

from __future__ import annotations

from .signals import Status

STATE_ADVICE = {
    "BLOCKED": "It is waiting on a permission prompt you can't answer. Tell the user which prompt it is, "
    "or TaskStop it and re-dispatch with that tool allowed or avoided.",
    "IDLE": "It has gone quiet with nothing running. SendMessage it to ask where it is and what it needs, "
    "or TaskStop it and re-dispatch the rest.",
    "STALLED": "Its process has run a long time with no file change: likely a hung test or a watcher. "
    "Tell it to kill that process and run with a timeout, or TaskStop it.",
    "ORPHANED": "A detached process it started is still running with nobody watching it. Kill that process.",
    "WAITING": "It is waiting on background work it launched. Usually fine; if that work is stuck, treat it as STALLED.",
}

FLAG_ADVICE = {
    "LOOP": "It is repeating itself. SendMessage it the piece it is missing, or TaskStop it.",
    "ERRORS": "Its recent tool calls failed. Check whether it is recovering before you step in.",
    "DENIED": "Tool calls were denied. If it needs that permission, ask the user to allow it, then tell the agent to retry.",
    "OUTSIDE": "It edited the main checkout instead of its worktree. Check `git status` there and undo or move those edits.",
    "BRANCH": "Its worktree is on a different branch from the one it was given. Check it hasn't committed to the wrong branch before merging.",
    "STALE-BASE": "It started editing on a base that has since moved. Tell it to merge the base branch before going further.",
    "COLLIDE": "Another agent edited the same file concurrently. Decide which agent owns it, or plan the merge order.",
    "OFF-BRIEF": "It edited files the brief doesn't name. Worth a question only if they look unrelated.",
    "BG": "It launched background runs. If its brief says foreground, remind it.",
    "UNCOMMITTED": "Its edits have been uncommitted a long time. Tell it to commit.",
    "CTX": "Its context is large; it will slow down and forget instructions. Tell it to wrap up and hand back, "
    "or re-dispatch the rest fresh.",
    "ORPHAN": "A detached process is still running from its worktree. Kill it once nothing needs it.",
    "DRIFT": "It may be off the brief (the judge's concern above). If so, SendMessage it to get back to the brief.",
    "STUCK": "It may be stuck (the judge's concern above). Give it the missing piece, or TaskStop it and re-dispatch.",
}


def compose(st: Status) -> str:
    a = st.agent
    lines = [f'[tracon] Agent "{a.description}" (agentId {a.id}) is {st.state}: {st.detail}'.rstrip(": ")]
    flags = [f for f in st.flags if f.severity != "info"] or st.flags
    if flags:
        lines += ["", "Flags:"] + [f"- {f.code}: {f.detail}" for f in flags]
    j = st.judgment
    concern = getattr(j, "concern", "") if j is not None and not getattr(j, "error", "") else ""
    if concern and not any(f.detail == concern for f in flags):  # DRIFT/STUCK already carry it
        lines += ["", f"Judge ({j.verdict}): {concern}"]
    tips = [STATE_ADVICE[st.state]] if st.state in STATE_ADVICE else []
    tips += [FLAG_ADVICE[c] for c in dict.fromkeys(f.code for f in flags) if c in FLAG_ADVICE]
    if tips:
        lines += ["", "Suggested:"] + [f"- {t}" for t in tips]
    lines += ["", "Sent by the user from tracon, which monitors this session's subagents. Verify before acting."]
    return "\n".join(lines)
