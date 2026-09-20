# ADR-0001: Git-backed planning with a small local coordination hub

Status: Accepted for the initial repository setup
Date: 2026-09-18
Decider: Initial agent infrastructure implementation under the setup request

## Context

This is a local project with a specified but unimplemented second component.
There is no configured Git remote or hosted tracker. The main risks are
contract drift across languages, concurrent edits to shared assets, lost
handoffs, and expensive validation performed too late.

## Decision

Use small dependency-driven tickets, a versioned JSON backlog, one integrator,
isolated implementation worktrees, independent review, and serial integration.
Keep authoritative decisions and acceptance evidence in Git. Use a local SQLite
hub only for expiring advisory resource claims and durable cursor-based messages.
Expose it through the CLI and an SDK-backed stdio MCP adapter.

## Options considered

| Option | Complexity | Strength | Cost |
| --- | --- | --- | --- |
| Files alone | Low | Portable, auditable | No atomic cross-process claims |
| Git + local SQLite/MCP | Low-medium | Durable coordination without daemon operations | One machine, cooperative agents |
| Hosted issues/projects + PRs | Medium | Team visibility and remote enforcement | Needs remote, identity and permissions |
| Custom HTTP workflow platform | High | Central scheduling and event delivery | Authentication, lifecycle, retries, UI and maintenance |

The selected hub uses stdio subprocesses sharing an absolute database path.
It does not need a TCP port, process supervisor, message broker, or Docker.
Product services stay separate from the hub. Native agent tools can provide
immediate wakeups; the hub provides persistence across sessions.

## Consequences

Claims do not enforce filesystem locks. Owner IDs are cooperative identities,
not authentication. The database must remain on a local disk shared by trusted
processes under the same user. Expiration requires a worker to stop and reclaim
before resuming. Git worktrees isolate edits; a shared editor still needs a
single owner. Messages never authorize merges or arbitrary commands.

Planning is intentionally single-writer. If moving to GitHub Issues/Projects,
migrate live status there and stop editing duplicate status in the JSON backlog.
Keep contracts and ADRs in Git. Add a hosted or authenticated HTTP coordinator only
when workers need different machines or measured polling costs justify it.

The MCP adapter currently pins the tested 1.x SDK API with a lockfile. A future
2.x migration should change the adapter and protocol smoke tests together.

## Follow-up

- Resolve the contract ticket before parallel implementation.
- Configure a remote and branch checks when the hosting destination is known.
- Add product CI as those test suites are implemented.

References: [MCP transport contract](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports),
[Python SDK 1.x](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x).
