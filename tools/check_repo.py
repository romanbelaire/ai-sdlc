"""Fast infrastructure validation; deliberately does not claim to test the product."""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools/agent_hub"))
from harness import load_harness


def main() -> None:
    required = load_harness()["required_docs"]
    for name in required:
        if not (ROOT / name).is_file():
            raise ValueError(f"Missing required file: {name}")
    backlog = json.loads((ROOT / "planning/backlog.json").read_text(encoding="utf-8"))
    tickets = backlog["tickets"]
    by_id = {t["id"]: t for t in tickets}
    if len(by_id) != len(tickets):
        raise ValueError("Duplicate ticket IDs")
    sprints = {s["id"] for s in backlog["sprints"]}
    statuses = {"backlog", "ready", "in_progress", "review", "blocked", "done"}
    skeptic = {"clear", "watch", "flagged", "debate", "kept", "scrubbed"}
    paused = {"flagged", "debate"}
    active = {"ready", "in_progress", "review", "done"}
    for t in tickets:
        if t["skeptic"] not in skeptic:
            raise ValueError(f"Invalid skeptic mark: {t['id']}")
        if t["skeptic"] == "scrubbed":
            continue
        if t["status"] not in statuses or t["sprint"] not in sprints:
            raise ValueError(f"Invalid status/sprint: {t['id']}")
        if t["skeptic"] in paused and t["status"] in active:
            raise ValueError(f"{t['id']} is {t['skeptic']} and cannot stay {t['status']}")
        for field in ("title", "paths", "acceptance", "validation"):
            if not t[field]:
                raise ValueError(f"Missing {field}: {t['id']}")
        for dependency in t["depends_on"]:
            if dependency not in by_id:
                raise ValueError(f"Unknown dependency {dependency}")
            if by_id[dependency]["skeptic"] == "scrubbed":
                raise ValueError(f"{t['id']} still depends on scrubbed {dependency}")
            if t["status"] in {"ready", "in_progress", "review", "done"} and by_id[dependency]["status"] != "done":
                raise ValueError(f"{t['id']} depends on unfinished {dependency}")
    visited, visiting = set(), set()

    def visit(ticket_id: str) -> None:
        if ticket_id in visiting:
            raise ValueError(f"Dependency cycle at {ticket_id}")
        if ticket_id in visited:
            return
        visiting.add(ticket_id)
        ticket = by_id[ticket_id]
        if ticket["skeptic"] != "scrubbed":
            for dependency in ticket["depends_on"]:
                visit(dependency)
        visiting.remove(ticket_id)
        visited.add(ticket_id)

    for ticket_id in by_id:
        visit(ticket_id)
    for folder in ("tools", "specs"):
        root = ROOT / folder
        if not root.is_dir():
            continue
        for source in root.rglob("*.py"):
            if ".venv" not in source.parts:
                ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    print(f"Infrastructure OK: {len(tickets)} tickets, dependency DAG, required docs, Python syntax.")
    print("Product test suites were not run by this check.")


if __name__ == "__main__":
    main()
