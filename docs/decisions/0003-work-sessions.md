# ADR-0003: Coordination identity belongs to work sessions

Status: Accepted (proposed by Roman)
Date: 2026-09-19

## Context

The hub keyed inboxes and claims to per-agent IDs (`agent-1-integrator`,
`agent-2`). Agents from different vendors (Claude Code, Codex, Cursor) join
and leave: one ran out of usage mid-sprint, and its role had to be handed to a
different tool. Per-agent IDs tie work to whichever agent started it, split
conversations across aliases, and make takeover a manual briefing exercise.

## Decision

Hub identity is a **work session** (`agent-session-N`). A session owns its
inbox, a locker (one overwritable note plus an inbox cursor), and its ticket
and resource claims. Any agent can run a session. The run holds a lease on
`session:<id>` under a holder ID unique to that run, so two agents cannot run
the same session at once, and while it is held only its holder can write the
locker. Roles such as integrator belong to sessions, not models. Git keeps
provenance: commits and reviews name the session and the holder's tool/model.

## Consequences

Takeover means starting a free session and reading its locker. Claims survive
a run that stops, so the next holder can renew them. Lockers must be written
tool-neutrally for readers with different tools and no shared transcript (see
`HANDBOOK.md`). Review independence is judged by session and run, not only by
model. Holder and session IDs are cooperative, as in ADR-0001, and not
authentication. Messages to retired per-agent IDs remain readable history.

When a new holder takes an expired session, the hub records the prior holder in
a durable supersession ledger. Session-owned claims, releases, and messages
must include the live holder ID. The ledger refuses later writes from a dropped
holder that resumes, including after the successor ends. It cannot fence direct
filesystem changes made outside the hub.
