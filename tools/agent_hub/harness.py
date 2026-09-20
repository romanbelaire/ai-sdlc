"""Product overlay config for the hub kernel. Missing or invalid files fail fast."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
MESSAGE_KINDS = ("fyi", "review", "wake", "death")
WAKE_SENTINEL = "AGENT_LOOP_WAKE_hub-inbox"
STOP_SENTINEL = "AGENT_LOOP_STOP_hub-inbox"
KEYS = ("backlog", "session_pattern", "operator", "integrator", "reserved_resources",
        "required_docs")


def config_path() -> Path:
    configured = os.environ.get("RL_HARNESS_CONFIG")
    return Path(configured) if configured else ROOT / "planning/harness.json"


def load_harness(path: Path | None = None) -> dict[str, Any]:
    """Read the overlay config. An absent file or empty session pattern is an error."""
    path = path or config_path()
    if not path.is_file():
        raise ValueError(f"Missing harness config: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in KEYS:
        data[key]
    if not data["session_pattern"].strip():
        raise ValueError("Harness session_pattern must be a non-empty GLOB")
    if data["required_docs"][0:0] != []:
        raise ValueError("Harness required_docs must be a list")
    return data
