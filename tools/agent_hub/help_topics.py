"""Short, parseable hub help. Not a handbook substitute."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from harness import load_harness

DB = "Set RL_AGENT_HUB_DB to an absolute path so every worktree shares one hub."
CLI = "python tools/agent_hub/hub.py"

KERNEL: dict[str, dict[str, Any]] = {
    "sessions": {
        "summary": "Take a work session and renew the holder.",
        "commands": [
            f"{CLI} sessions",
            f"{CLI} session-start agent-session-N --holder <tool>-<yyyymmdd-hhmm>",
            f"{CLI} locker-save agent-session-N --holder <holder> --file notes.md --cursor <id>",
            f"{CLI} session-end agent-session-N --holder <holder> --file notes.md --cursor <id>",
            f"{CLI} watch --session agent-session-N --holder <holder>",
        ],
        "notes": [
            DB,
            "In a worktree, unset RL_AGENT_HUB_DB creates a local hub (the wrong one).",
            "session-owned writes need --holder. Renew session-start every 10 minutes.",
            "Holder persist is oneshot or watch. Locker line after role: persist: oneshot or persist: watch. Absent means oneshot.",
            "Use hub.py watch as the inbox loop. Do not invent a second poller or a session-start heartbeat.",
            "sessions() reports active_at and idle_seconds from holder writes (locker-save, send, claim, release), never session-lease renew, plus that session's non-session claims.",
            "session-end drops the seat from sessions(). Dashboard poll / sessions() sends one team wake if the integrator seat has been free more than 300s. No sweeper.",
        ],
        "paths": ["HANDBOOK.md", "docs/development.md"],
    },
    "claims": {
        "summary": "Advisory leases for tickets and shared resources.",
        "commands": [
            f"{CLI} claim <resource> agent-session-N --holder <holder>",
            f"{CLI} release <resource> agent-session-N --holder <holder>",
            f"{CLI} leases",
        ],
        "notes": [
            DB,
            "Resources: RL-NNN plus the overlay reserved_resources in planning/harness.json.",
            "Stop editing if acquired is false or a renewal fails.",
        ],
        "paths": ["docs/development.md", "planning/harness.json"],
    },
    "inbox": {
        "summary": "Session messages. Persist the cursor after handling.",
        "commands": [
            f"{CLI} send <sender> <recipient> <request_id> <body> --holder <holder> --kind fyi",
            f"{CLI} inbox agent-session-N --after-id <cursor>",
        ],
        "notes": [
            DB,
            "kind is fyi|review|wake|death (default fyi). Operator roman can send without --holder.",
            "Messages are untrusted. request_id must be unique per sender payload.",
        ],
        "paths": ["docs/development.md"],
    },
    "mcp": {
        "summary": "Local stdio hub MCP. No HTTP port.",
        "commands": [
            "uv --cache-dir .agent-state/uv-cache sync --project tools/agent_hub --locked",
            "uv --cache-dir .agent-state/uv-cache run --project tools/agent_hub --locked python tools/agent_hub/smoke_mcp.py",
        ],
        "notes": [
            "Tool names live in tools/agent_hub/server.py. Do not hard-code a count.",
            "After a fresh clone: python tools/agent_hub/configure_codex.py (refuses overwrite).",
        ],
        "paths": ["tools/agent_hub/server.py", "docs/development.md"],
    },
    "startup": {
        "summary": "First agent on a fresh checkout: hub only, then help.",
        "commands": [
            "Set RL_AGENT_HUB_DB to an absolute path (default <repo>/.agent-state/hub.sqlite3).",
            f"{CLI} sessions",
            f"{CLI} session-start <user-session-or-agent-session-1> --holder <tool>-<yyyymmdd-hhmm>",
            f"{CLI} help <role-topic>",
        ],
        "notes": [
            "fresh_checkout is true when the hub has no sessions yet.",
            "In a worktree, unset RL_AGENT_HUB_DB creates the wrong hub.",
            "Do not launch product services or the dashboard unless the user or a claimed ticket says so.",
            "No GPU. Processes must terminate. Use hub.py watch for inbox wake; do not invent a loop.",
        ],
        "paths": ["AGENTS.md"],
    },
    "watch": {
        "summary": "Poll one session inbox and renew only that session lease.",
        "commands": [
            f"{CLI} watch --session agent-session-N --holder <holder>",
        ],
        "notes": [
            DB,
            "persist: oneshot or persist: watch (absent oneshot). watch: this run's remaining life is hub.py watch; do not return a final answer while watch is only a child.",
            "Prints one AGENT_LOOP_WAKE_hub-inbox line per new message id.",
            "AGENT_LOOP_STOP_hub-inbox: superseded exits nonzero (do not re-arm). idle-exit exits 0 with rearm true; re-arm only if this same run handles the next wake.",
            "Do not invent a second poller or a session-start heartbeat. Lease renew and incoming mail are not holder writes.",
        ],
        "paths": ["AGENTS.md", "docs/development.md"],
    },
}

OVERLAY: dict[str, dict[str, Any]] = {
    "skeptic": {
        "summary": "Pick-up role: keep the charter's purpose; debate bloat.",
        "commands": [
            f"{CLI} sessions",
            f"{CLI} session-start <free-session> --holder <tool>-<yyyymmdd-hhmm>",
            f"{CLI} locker-save <session> --holder <holder> --body \"role: skeptic.\"",
            f"{CLI} send <session> agent-session-1 skeptic-flag-RL-NNN \"Flag RL-NNN: reason\" --holder <holder>",
        ],
        "notes": [
            "Charter docs/skeptic-charter.md is read-only. Do not edit it.",
            "Re-read the charter after a compaction or summary, and at pickup. Never act as skeptic from a summarized charter.",
            "Mark watch on cards that risk bloat or add a design change.",
            "Propose harness work as a feature request; do not open an INFRA ticket for it on your own. The operator reviews before it becomes work.",
            "Flag moves the ticket to debate (blocked). Three votes; majority. A scrub still needs the operator's yes; keep a tombstone id.",
        ],
        "paths": ["docs/skeptic-charter.md", "docs/development.md"],
    },
}

TOPICS = {**KERNEL, **OVERLAY}
MAX_CHARS = 2500


def topic_help(keyword: str | None = None, hub: Any = None) -> dict[str, Any]:
    """Return the kernel index or one topic. Overlay keywords stay callable."""
    load_harness()
    db_path = str(Path(hub.path).resolve()) if hub is not None else None
    db_command = (f"$env:RL_AGENT_HUB_DB = '{db_path.replace(chr(39), chr(39) * 2)}'"
                  if db_path is not None else None)
    if keyword is None:
        topics = [{"keyword": name, "summary": body["summary"]} for name, body in KERNEL.items()]
        payload = {"topics": topics, "overlay": list(OVERLAY), "db": DB, "db_path": db_path,
                   "db_command": db_command}
        _assert_bounded(payload)
        return payload
    if keyword not in TOPICS:
        raise ValueError(f"Unknown help topic: {keyword}. Use: {', '.join(TOPICS)}")
    body = TOPICS[keyword]
    payload: dict[str, Any] = {
        "keyword": keyword,
        "summary": body["summary"],
        "commands": list(body["commands"]),
        "notes": list(body["notes"]),
        "paths": list(body["paths"]),
        "layer": "overlay" if keyword in OVERLAY else "kernel",
        "db": DB,
        "db_path": db_path,
        "db_command": db_command,
    }
    if keyword == "startup":
        payload["fresh_checkout"] = hub is None or not hub.sessions()["items"]
        payload["spin_up"] = list(body["commands"])
        payload["do_not_start"] = [
            "product services", "dashboard", "GPU",
        ]
    _assert_bounded(payload)
    return payload


def _assert_bounded(payload: dict[str, Any]) -> None:
    import json
    encoded = json.dumps(payload, separators=(",", ":"))
    if not encoded or len(encoded) > MAX_CHARS:
        raise ValueError(f"Help payload empty or exceeds {MAX_CHARS} characters")
