# rl-agent-hub

A local coordination kernel for multiple AI coding agents working one
repository: durable work sessions, advisory claims, cursor inboxes, and a
**cryptographically signed human merge approval** bound to one exact commit.

Standard library only. One SQLite file. No daemon, no broker, no network
listener. Python 3.11+.

## The part that is not like the others

Most agent harnesses gate a merge on a message, a flag, or a UI click that the
agent itself can reach. This one does not. An approval is an HMAC over
`(ticket, branch, sha, approver, note, created, source, review_id, recorded_by,
recorded_holder)`, signed with an operator key that lives beside the database
and never enters an agent's context. Agents verify but cannot mint:

```
python tools/agent_hub/hub.py approval RL-017 --sha <full-sha>   # exit 0 or refuse
```

Consequences that fall out of binding the signature to a commit:

- Moving the branch head invalidates the approval. No "approved, then amended".
- A new independent review supersedes the old one and revokes its approval.
- The reviewer must be a different live session **and** a different run than
  the author. One process cannot be both.
- The hub message announcing an approval says, in its own body, that it is not
  the approval.

## Sessions, not agents

Identity is a work session (`agent-session-N`), not a model or a vendor. A
session owns its inbox, its locker, and its claims; any agent can pick one up
and continue. A run holds the session under a holder ID, and a durable
supersession ledger refuses writes from a holder that was taken over and later
resumes.

Liveness comes from holder-attributed writes, not from lease renewal, so a
watcher polling on behalf of a dead agent cannot keep a session looking alive.

## Layout

| Path | What it is |
|------|------------|
| `tools/agent_hub/hub.py` | kernel: sessions, claims, inboxes, reviews, approvals |
| `tools/agent_hub/server.py` | stdio MCP adapter |
| `tools/agent_hub/dashboard.py` | local operator UI on 127.0.0.1 |
| `tools/agent_hub/tests/` | kernel tests, including forgery and takeover cases |
| `AGENTS.md` | the contract an agent reads first |
| `HANDBOOK.md` | verified lessons, session routine |
| `docs/decisions/` | why it is shaped this way |
| `docs/skeptic-charter.md` | the anti-scope-creep role |

## Scope and limits

Cooperative identities, not authentication. One machine, one trusted user,
local disk. Claims are advisory leases and do not lock the filesystem. This is
deliberate: see `docs/decisions/0001-agent-workflow.md`.

This repository is an extract. It is generated from a private product
repository by `tools/publish_harness.py`, which excludes every product tree,
keeps only `INFRA` tickets, and scans the built tree for product vocabulary
before committing.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
