# Team Harness

A local coordination kernel for multiple AI coding agents working one
repository: durable work sessions, advisory claims, cursor inboxes, and a
**cryptographically signed human merge approval** bound to one exact commit.

Standard library only. One SQLite file. No daemon, no broker, no network
listener. Python 3.11+.

<!-- Hosted on GitHub rather than committed: an image lands in every clone and
     in history forever. Source issue: /issues/1 -->
![The local dashboard: session seats with their roles and claims, the ticket
board, and the team feed](https://github.com/user-attachments/assets/b34df7a6-9a83-4467-8419-3d0d40a17003)

## Verified Human-in-the-Loop

Most agent harnesses gate a merge on a message, a flag, or a UI click that the
agent itself can reach. Here, an approval is an HMAC over
`(ticket, branch, sha, approver, note, created, source, review_id, recorded_by,
recorded_holder)`, signed with an operator key that lives beside the database
and never enters an agent's context. Agents verify but cannot mint:

```
python tools/agent_hub/hub.py approval RL-017 --sha <full-sha>   # exit 0 or refuse
```

Binding the signature to a commit means:

- Moving the branch head invalidates the approval. No "approved, then amended".
- A new independent review supersedes the old one and revokes its approval.
- The reviewer must be a different live session **and** a different run than
  the author. This splits approval across independent(ish) context windows.

## Agent-Agnostic Sessions

Author identity is a work session (`agent-session-N`), not a model or a vendor. A
session owns its inbox, its locker, and its task claims, while any agent can pick one up
and continue. 

Liveness comes from holder-attributed writes, not from polling activity, so a
watcher polling on behalf of a dead agent cannot keep a session alive.

## Layout

| Path | Contents |
|------|------------|
| `tools/agent_hub/hub.py` | kernel: sessions, claims, inboxes, reviews, approvals |
| `tools/agent_hub/server.py` | stdio MCP adapter |
| `tools/agent_hub/dashboard.py` | local operator UI on 127.0.0.1 |
| `tools/agent_hub/tests/` | kernel tests, including forgery and takeover cases |
| `AGENTS.md` | the contract an agent reads first |
| `HANDBOOK.md` | verified lessons, session routine |
| `docs/decisions/` | design decisions |
| `docs/skeptic-charter.md` | the anti-scope-creep role |

## Scope and limits

This was developed on one machine, one trusted user,
local disk. Claims are advisory leases and do not lock the filesystem (subject to change). See `docs/decisions/0001-agent-workflow.md`.

This repository is an extract generated from a private product
repository by `tools/publish_harness.py`, which excludes every product tree,
keeps only `INFRA` tickets, and scans the built tree for product vocabulary
before committing. The publishing script can be used to locate and commit in-place changes to the harness from whatever your use case is. 

## License

Apache License 2.0 — see [LICENSE](LICENSE).
