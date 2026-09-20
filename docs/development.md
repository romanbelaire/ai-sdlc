# Development workflow

## Start here

Read `AGENTS.md`, `HANDBOOK.md`, and `planning/backlog.json`. Take a work session first (see Work sessions below).
The efficient loop is **spec -> bounded ticket -> implementation -> evidence ->
independent review -> serial integration -> demo**. Plan by dependencies and
demonstrable outcomes. Use a short rolling sprint; do not create a separate
standing agent for every ceremony.

Sequence sprints so contracts land before the work that depends on them, and
so independent tracks can proceed against fixtures instead of waiting on each
other. Sprint membership is a plan, not completion.

## Roles and limits

- **Integrator:** triage requests, edit backlog, resolve dependency order,
  maintain decisions, assign owners, integrate reviewed changes.
- **Implementer:** own one ticket and worktree; deliver acceptance evidence.
- **Reviewer:** inspect the final diff and tests independently; report defects
  with file/line references. Can be another session, not necessarily a daemon.
  Independent means a different session and a different run (holder) from the
  one that wrote the change; record both in the review. A reviewer may
  **flag** a ticket for skeptic debate when it risks bloat or a significant
  design change.
- **Skeptic:** pick-up role (any free session, locker first line
  `role: skeptic`). Read the read-only `docs/skeptic-charter.md`. Keep the
  team on the purpose named in the charter. Flag drift. Sit as one of three
  voters in debate. Do not edit the charter. Re-read the charter in full
  after any compaction or summary, and at pickup; a summarized charter
  does not count.

Roles belong to sessions, and the first line of a session's locker names its
role. Whichever agent holds the integrator session is the integrator.

Start with one implementer. The integrator names at most one next card
per free implementer session. Implementers do not claim from a buffet.
A reserved resource stays a single lease, so cards that edit it queue. A
card touching no reserved resource may run in parallel with one that does.
The reviewer stays on review until an author sends a branch/SHA and does
not fill idle with implementer cards. Name one owner at a time for any
shared editor or generated configuration. No scheduler daemon, sweeper,
auto-claimer, or priority scores (RL-045 keep, 2026-09-20).

## Planning and feature intake

Use `planning/feature-template.md` or the GitHub feature template. The integrator
assigns an RL-NNN ID and records spec links in acceptance text, scope, paths,
dependencies, checks and sprint in `planning/backlog.json`. Split tickets that
cannot be reviewed as one coherent behavior. Select only enough work for the
next demonstrable outcome; replan at that boundary or when blocked.

States: `backlog -> ready -> in_progress -> review -> done`; use `blocked` when
an external decision/dependency stops work, returning to `ready` when resolved.
`ready` requires clear acceptance criteria and completed dependencies.
`done` requires integration, relevant checks, review evidence, and updated docs.
The validator catches dangling dependencies/cycles and premature ready states.
It cannot establish whether a claimed test or review actually happened.

The backlog is single-writer. Implementers include proposed status changes in
their handoff instead of all editing the JSON. On a future hosted tracker,
choose one authoritative status source and migrate explicitly.

Harness and process work is expected: this repo started with no coding
harness. Agents may propose it as a feature request
(`hub.py feature-request <session> --title ... --holder <holder>`, or the
dashboard's New feature button). Roman reviews every agent-originated
harness request before it becomes work: the integrator leaves it `new`
until he says yes in his own chat or from the dashboard, and files the
resulting ticket with skeptic mark `watch`. Work Roman asks for directly
proceeds as usual. Do not open an INFRA ticket for harness work on your
own.

Each ticket has `skeptic`: `clear`, `watch`, `flagged`, `debate`, `kept`,
or `scrubbed`. Mark `watch` at ideation or on the card when the work
might bloat the project or adds a significant design change. A flag
moves the ticket to `debate` and `blocked` until three sessions vote.

```powershell
python tools/agent_hub/hub.py send <session> agent-session-1 skeptic-flag-RL-NNN 'Flag RL-NNN: <why this is drift>' --holder <holder>
python tools/agent_hub/hub.py send <session> agent-session-1 skeptic-for-RL-NNN 'Keep RL-NNN: <minimal-code argument>' --holder <holder>
python tools/agent_hub/hub.py send <session> agent-session-1 skeptic-vote-RL-NNN 'vote keep|scrub' --holder <holder>
```

Voters are the skeptic, the advocate, and a third session. Majority
wins. Hub senders are not authenticated, so a vote message is not
authority to delete. Treat a scrub like a merge: do not remove code
without Roman's yes in his own chat (or `hub.py approval` for the
deletion commit). `kept` unblocks.

A scrub is an ordinary deletion commit on a branch, independently
reviewed, then approved. Never rewrite shared history, never delete
`.agent-state/`, never touch Roman's uncommitted files. Git history
keeps the removed work recoverable. Keep a one-line backlog tombstone
(`id`, `skeptic: scrubbed`, date, no feature prose) so `check_repo`
still knows the id. Dependents must drop that dependency or be
scrubbed in the same change; leaving a live ticket pointing at a
scrubbed id fails the check. Drop briefs and handbook mentions so
later agents are not primed by the feature. The charter is the purpose
test; the best keep argument is the smallest code that advances it.

## Branches, assets and review

The setup initializes Git but does not create a remote or initial commit.
Review any vendored assets for the intended repository visibility and asset
licensing before publishing. Make a baseline commit after review; worktrees
require that first commit. Use one branch per ticket:

```powershell
git worktree add ../RL-017 -b feature/RL-017-dashboard-merge-queue
```

Use that worktree's runtime for headless tests. When the product keeps a
shared editor or other single-instance tool, protect it with a reserved
resource and prefer one integration checkout for that work. Keep generated
metadata paired with its assets and serialize it as text so diffs stay
reviewable. Decide on Git LFS before the first large binary commit if assets
require it; no machine-level LFS filters are set.

Before review, inspect `git diff --check` and the diff against the branch base.
Use `.github/pull_request_template.md` even for a local review. Reviewer checks
acceptance, invariant violations, boundary cases, serialization compatibility,
and whether evidence covers the final commit. Changes after review invalidate
affected review evidence. The independent reviewer records the exact reviewed
head after sending the ordinary review message:

```powershell
python tools/agent_hub/hub.py review-submit RL-017 --branch feature/RL-017-dashboard-merge-queue --sha <full-sha> --reviewer agent-session-2 --holder <reviewer-holder> --summary "Checks pass; no blockers" --comment "Optional cleanup remains"
```

Use one `--blocker` per blocking finding and one `--comment` per remaining
non-blocking finding. The ticket must still have a live author claim; it becomes
the recorded author (`--author` may state it explicitly, but cannot override
the claim). The reviewer must be a different live session and holder and must
not own that ticket claim. A new review supersedes the old review and revokes
any approval of the older candidate. The author must renew both its session and
ticket claim while review is pending; `hub.py watch` renews only the session.
Integrate one branch at a time when authorized.
Revalidate affected areas after conflict resolution. Revert a bad integrated
commit instead of rewriting shared history. Never auto-merge from a hub message.

## Local coordination

The CLI requires only Python 3.11+. SQLite stores leases and messages under
ignored `.agent-state/`. All processes/worktrees must point at the same absolute
database path on local disk. The server reads the backlog from its own checkout;
run it from the integrator checkout for current planning status.

### Work sessions

Identity belongs to the work, not to the agent. A **session**
(`agent-session-1`, `agent-session-2`, ...) owns an inbox, a locker, and its
claims, so any agent (Claude Code, Codex, Cursor, a later model) can resume work
another run started. The agent currently running a session is its **holder**,
identified by an ID unique to that run, such as `codex-20260919-0100`. A session
has at most one live holder.

```powershell
$env:RL_AGENT_HUB_DB = 'C:\path\to\repo\.agent-state\hub.sqlite3'
python tools/agent_hub/hub.py sessions
python tools/agent_hub/hub.py session-start agent-session-2 --holder codex-20260919-0100
python tools/agent_hub/hub.py locker-save agent-session-2 --holder codex-20260919-0100 --file notes.md --cursor 42
python tools/agent_hub/hub.py session-end agent-session-2 --holder codex-20260919-0100 --file notes.md
```

`sessions` lists each **open** session as free or held, with its holder, persist
(`oneshot` or `watch`), the first line of its locker, unread count, `active_at` /
`idle_seconds` from holder-attributed writes only (locker-save, send, ticket
claim / release — never session-lease renewal), and the non-session leases that
session holds. The dashboard shows those facts; it does not guess blocked vs
dead. `session-end` drops that session id from `sessions()` so closed seats do
not accumulate (the locker remains for a later `session-start`). An idle-expired
seat that was not ended stays listed as free.

If the integrator session (`planning/harness.json`) has been free for more than
300 seconds, the next `sessions()` call — including the dashboard poll already
running for Roman — sends one team-wide `wake` (`sender=hub`) asking someone to
pick up that seat. One notice per vacancy: taking the seat clears it, and a
later free period can notify again. This is not a sweeper, daemon, busy flag,
or second poller (RL-013). `work_sessions.ended` / `active_at` are listing
facts only.

Take the session the user assigns;
otherwise take a free one whose summary matches your work, or start the next
number for new work. `session-start` refuses a held session with exit code 2,
reporting only the current holder and expiry. On success it returns the locker,
messages after its saved cursor, and the session's leases, with expired ones
flagged. Repeat it with the same holder every 10 minutes to renew (30-minute
default lease).

The **locker** is one overwritable note (max 8000 characters) plus the inbox
cursor. Line two is `persist: oneshot` or `persist: watch`; an absent persist
line means oneshot. `locker-save` rejects any other persist value. While a
session is held, only its holder can write the locker.
`session-end` saves it and frees the session. `spinup <session>` and
`locker <session>` read a session's context without taking it. See
`HANDBOOK.md` for what to keep in a locker.

`persist: oneshot` means do the assigned work, then `session-end` or let
idle-exit free the seat; returning a final answer is correct.
`persist: watch` means this holder run's remaining lifetime is `hub.py watch`.
The process that will handle the next `AGENT_LOOP_WAKE_hub-inbox` must still
be that run. Returning a final answer while watch is only a child is a
protocol violation (orphan watcher). Stop on `AGENT_LOOP_STOP_hub-inbox`;
re-arm only if this same run will handle the next wake (`rearm` true). Death,
session-end, and superseded (`rearm` false) end persist. Product listeners
(Cursor, Codex, and later tools) are docs-only adapters on those watch
sentinels; the hub adds no per-vendor loop, daemon, sweeper, busy flag, table,
or schema. Do not invent a second poller or a `session-start` heartbeat.

### Claims and messages

Claims and messages use the session ID plus the live holder ID. A later run
uses the same session ID and its own holder ID, so it can renew session-owned
claims without allowing a dropped predecessor to resume writes.

```powershell
python tools/agent_hub/hub.py board
python tools/agent_hub/hub.py claim RL-001 agent-session-2 --holder codex-20260919-0100
python tools/agent_hub/hub.py claim contracts agent-session-2 --holder codex-20260919-0100
python tools/agent_hub/hub.py leases
```

Check `acquired` before editing; conflict also returns CLI exit code 2. Default
lease is 30 minutes. Renew with the same call every 10 minutes during active
work. Acquire all required resources before editing; release partial acquisitions
on conflict and retry later. Stop if a renewal, including the session renewal,
fails or its lease expires. Before taking over expired work, inspect its
branch/handoff. Taking over an expired session durably supersedes its prior
holder, and all session-owned writes require the live holder; a restarted old
run is refused by the hub. This is still cooperative: it cannot prevent an old
process from modifying files directly outside the hub.

```powershell
python tools/agent_hub/hub.py send agent-session-2 agent-session-1 review-RL001-1 'RL-001 ready; branch/head, evidence and handoff path here.' --holder codex-20260919-0100
python tools/agent_hub/hub.py inbox agent-session-1 --after-id 0 --limit 20
python tools/agent_hub/hub.py release contracts agent-session-2 --holder codex-20260919-0100
python tools/agent_hub/hub.py release RL-001 agent-session-2 --holder codex-20260919-0100
```

`request_id` deduplicates retries per sender; a different payload with the same
ID is rejected. Inbox reads are non-destructive. Consumers persist `next_cursor`
after handling messages; reprocessing must be safe. `send` stores `kind`
(`fyi` | `review` | `wake` | `death`, default `fyi`). Operator `roman` can send
without `--holder`. Overlay settings live in `planning/harness.json` (backlog
path, session pattern, reserved resources, `check_repo` required docs). Missing
or invalid config fails fast.

Use one inbox watcher, not a second IDE loop, and not a `session-start`
heartbeat. `sessions()` persist is `oneshot` or `watch`; name it on the locker.

```powershell
python tools/agent_hub/hub.py watch --session agent-session-1 --holder cursor-20260919-1459
```

It renews only that session lease and prints one `AGENT_LOOP_WAKE_hub-inbox`
line per new message id. A superseded holder prints
`AGENT_LOOP_STOP_hub-inbox` and exits nonzero; do not re-arm that holder.

A watcher does not keep a dead session alive. After `--idle-exit` seconds
(default 600) with no holder-attributed write, it stops renewing and exits 0
with `AGENT_LOOP_STOP_hub-inbox` and `"rearm": true`. Holder-attributed writes
are `locker-save`, `send`, ticket `claim` / `release`, and an inbox cursor
advance; renewing the session lease and incoming mail are not. A live agent
re-arms the watcher and keeps working; a dead one stops renewing, so the
session reaches its lease expiry and any agent can take it with
`session-start`. Worst case for takeover is the idle window plus the remaining
lease, which is the price of having no sweeper. There is no way to disable
idle-exit: a watcher that renews forever is the problem it prevents.
No background wakeup,
scheduler, push notifications, automatic sprint planning, or PR merge is
implied. Messages remain until local state is intentionally retired. Copy
lasting decisions into Git. Stop all clients before copying the SQLite file
for a backup.

### Team dashboard

Roman works with the team from a local page:

```powershell
python tools/agent_hub/dashboard.py            # opens http://127.0.0.1:8770/?token=...
```

It shows backlog tickets as cards by status (detail: acceptance, handoff,
related messages), the sessions with their holders and unread counts, and a
chat where Roman writes as hub sender `roman` to one session or all of them,
plus a feed of all team traffic. **New feature** records a feature request and
messages the integrator session. Agents may file harness requests this way;
leave them `new` until Roman reviews them. The integrator triages an approved
request into an RL ticket (the backlog stays single-writer) and links it:

```powershell
python tools/agent_hub/hub.py features --status new
python tools/agent_hub/hub.py feature-update 3 --status ticketed --ticket RL-012 --owner agent-session-1 --holder <holder>
```

The requester is told the outcome. The page binds to 127.0.0.1, needs the
per-launch token for every API call, and rejects non-local Host headers and
non-JSON posts. Messages render as text only. Run it from the integrator
checkout so the board shows the current backlog.

**Merge approval.** The dedicated Merge tab lists only the newest independent
review for each ticket when it has no blockers and the local branch still points
at the reviewed SHA. Arbitrary unmerged branches never appear. Each candidate
shows its review summary and remaining non-blocking comments. **Approve**
records approval of that exact ticket, branch and commit. **Send with comments**
withholds/revokes approval, records the feedback, and tells the author and
integrator to obtain a new review after changes.

Approvals are signed with the operator key (`operator.key` beside the hub
database, created by the dashboard). The dashboard never performs a Git merge.
The integrator merges only if the check passes immediately before the merge:

```powershell
python tools/agent_hub/hub.py approval RL-011 --sha <full sha from git rev-parse>   # exit 0 only if approved
python tools/agent_hub/hub.py approvals --ticket RL-011
```

MCP clients call `hub_approval`. A new push, rebase, newer review, feedback, or
**Revoke** voids the approval; review and approve the new head again. A hub
message never counts as approval, even one whose sender field says `roman`.

Roman can also approve in the integrator's direct chat. The live integrator
records that decision, bound to the newest clear review and exact SHA, so it is
visible in the same audit trail with `source=chat`:

```powershell
python tools/agent_hub/hub.py chat-approval RL-017 --branch feature/RL-017-dashboard-merge-queue --sha <full-sha> --recorder agent-session-1 --holder <integrator-holder> --note "Roman approved in direct chat"
```

No other session can record a chat approval. The key stops accidental and
injected approvals; it is not a boundary against a local process that reads it
on purpose, so agents never read it or edit approval records directly. The
recording integrator session and holder are stored and signed; notifications
come from that session while `approver=roman` preserves who made the decision.

### MCP adapter

The initial setup generated an ignored, machine-specific `.codex/config.toml`
for this checkout. It does not change global settings. New trusted Codex
sessions can load the `rl_agent_hub` entry; this running session is not hot-reloaded.
Verify with `codex mcp get rl_agent_hub --json`. Project-scoped registration
follows the [official MCP configuration guide](https://developers.openai.com/codex/mcp/).
After cloning elsewhere, sync dependencies below and run
`python tools/agent_hub/configure_codex.py`. It refuses to overwrite an existing
config. For multiple worktrees, configure their clients to launch the integrator
checkout's server with the same database path, rather than creating separate hubs.

```powershell
uv --cache-dir .agent-state/uv-cache sync --project tools/agent_hub --locked
uv --cache-dir .agent-state/uv-cache run --project tools/agent_hub --locked python tools/agent_hub/smoke_mcp.py
```

Configure an MCP client to launch the project-local Python executable with
`server.py` as its argument. Example generic client entry (adjust absolute paths
for another machine; this file is documentation, not installed client config):

```json
{
  "mcpServers": {
    "rl-agent-hub": {
      "command": "C:/path/to/repo/tools/agent_hub/.venv/Scripts/python.exe",
      "args": ["C:/path/to/repo/tools/agent_hub/server.py"],
      "env": {"RL_AGENT_HUB_DB": "C:/path/to/repo/.agent-state/hub.sqlite3"}
    }
  }
}
```

On Linux/macOS use `.venv/bin/python`. The client starts and stops its stdio
server. Multiple servers share SQLite; no always-on listener is needed.
Tool names live in `tools/agent_hub/server.py` (do not hard-code a count). This is a trusted local-user
service with advisory session and holder IDs, not a security boundary between
hostile agents. No HTTP port is exposed.

### Harness extract repo

The hub can be published as its own git tree, without the game:

```powershell
python tools/publish_harness.py --dest C:\path\to\rl-agent-hub
```

The extract keeps `tools/agent_hub`, hub tests, dashboard, `check_repo`, and
coordination docs. It excludes every product tree, keeps only `INFRA`
tickets and their handoffs, and scans the built tree for product
vocabulary before committing. The origin repository remains the living
copy of the product.
The publisher initializes a local git repo and prints the HEAD SHA. It does
not push. Do not copy `.agent-state/operator.key` into the extract.

## Validation ladder

1. **Every infrastructure PR:** `python tools/check_repo.py` and
   `python -m unittest discover -s tools/agent_hub/tests -v`.
2. **MCP changes:** locked dependency sync and `smoke_mcp.py` (real initialize,
   discovery, calls, retry behavior and shared state across subprocesses).
3. **Product changes:** the checks the owning ticket defines. An absent
   product test suite is NOT a passing product test.
4. **Milestone:** full affected suites and a reproducible demo. Long
   performance runs are explicit experiments with hardware and config
   recorded, not per-edit gates.

GitHub infrastructure CI is supplied but only runs after hosting the repo.
Product CI is deliberately deferred to the tickets that create real test
suites; there are no placeholder green jobs. Protect the main branch with
required checks/review once a remote is configured. None of these hosted
settings are configured.

Use `planning/handoff-template.md` to persist outcomes. Useful efficiency
measures are ticket cycle time, review rework, blocked time, and failed checks
after integration. Add services only to address a demonstrated bottleneck.
