# ADR-0004: Hub kernel versus product overlay

Status: Accepted (proposed by Roman)
Date: 2026-09-19

## Context

The local hub (sessions, holders, claims, lockers, MCP) is an SDLC
coordinator. The origin repository also contains the product it was built
for. `AGENTS.md` and `check_repo.py` currently mix those layers: a clone
that is not that product still has to pretend to have its contract files,
and a new agent is told product invariants before it can take a session.
Inbox watchers were invented per IDE. The same workflow is useful for
other products.

## Decision

Split the harness into a **kernel** and a **product overlay**. Stay in
`tools/agent_hub/` in this repo; do not publish a package yet. A later
product repo copies or vendors that tree and drops a small config plus
overlay help topics.

**Kernel:** work sessions, holders, lockers, advisory claims, cursor
inboxes, supersession, `hub.py help` for session/claim/inbox/mcp/startup/watch,
`hub.py watch`, message kinds, operator send, backlog path and session
pattern from config, `check_repo` required-docs from config, feature and
handoff templates, stdio MCP.

**Overlay (the product repo):** product invariants in its own brief,
resources such as a shared editor lease, product help topics, product
specs, and any product service ports.

Product services are not part of the kernel. A master process that later
supervises `watch` is not part of this decision.

## Consequences

RL-011 (dashboard) and RL-013 (idle-exit watch) consume kernel message
kinds and watch; they are not product tickets. Vendor MCP adapters (Codex, Cursor)
remain thin launchers. Extracting a pip package, Telegram, and hosted
HTTP stay out until a second repo needs them.

## Follow-up

Tickets RL-014 (watch), RL-015 (config and message kinds), RL-016
(AGENTS overlay). RL-013 depends on RL-015.
