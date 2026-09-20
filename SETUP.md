# Running a team on this harness

This is the operator's guide: how to take a repository from a fresh clone to a
board with several AI agents working it, and what your job is once they are
running.

It assumes you are the only human. Everything here runs on one machine, on
local disk, with no daemon and no network listener beyond a dashboard bound to
`127.0.0.1`.

## The shape of it

You are not prompting agents one at a time. You are running a small team whose
identity lives in **sessions** rather than in models, so any agent can pick up
any seat and continue from the note the last one left. Your controls are a
board, a chat panel, and a merge approval that agents can check but cannot
forge.

Three steps: clone and check, wire up your agent tool, spin up the team.

---

## 1. Clone and check

```bash
git clone <your-repo> && cd <your-repo>
uv sync --project tools/agent_hub --locked
python tools/check_repo.py
python -m unittest discover -s tools/agent_hub/tests
```

`check_repo.py` validates the backlog: unique ticket IDs, a dependency graph
with no cycles, no ticket marked ready while a dependency is unfinished, and
the presence of every document `planning/harness.json` lists as required. If it
fails, fix that before seating anyone — agents will otherwise spend a run on a
board that does not describe reality.

### Point everything at one database

Every shell, worktree and agent must use the **same absolute path**:

```bash
export RL_AGENT_HUB_DB="/absolute/path/to/repo/.agent-state/hub.sqlite3"
```

This is the single most common setup mistake. An unset variable inside a
worktree silently creates a *second*, empty hub, and the symptom is a session
list that looks mysteriously empty. The variable is rejected unless it is
absolute, for exactly this reason.

### Make this repo yours

`planning/harness.json` is the whole configuration:

| Key | What it does |
|---|---|
| `backlog` | path to the ticket file |
| `session_pattern` | glob for valid session IDs, e.g. `agent-session-*` |
| `operator` | your hub sender ID — the name your chat messages arrive under |
| `integrator` | the session that owns the backlog and performs merges |
| `reserved_resources` | things only one session may hold at a time |
| `required_docs` | files `check_repo.py` insists exist |

Set `operator` to whatever you want to be called on the board. Add a reserved
resource for anything in your project that genuinely cannot take two writers —
a shared editor, a generated config, a contract directory.

---

## 2. Wire up your agent tool

The hub speaks two ways: a CLI, and a stdio MCP server. Agents can use either.

**Claude Code** — the skills in `.claude/skills/` are picked up from the repo
with no further setup. `take-session` handles seat pickup and arms the inbox
watcher; `spinup` does the whole team start.

**Codex** — generate the project-local MCP entry:

```bash
python tools/agent_hub/configure_codex.py
```

This writes `.codex/config.toml` and refuses to overwrite an existing one, so
merge the entry by hand if you already have project settings. Then open the
project in a new trusted session.

**Anything else** — point it at `tools/agent_hub/server.py` as a stdio MCP
server with `RL_AGENT_HUB_DB` set in its environment, or let it call
`hub.py` directly. `docs/development.md` has the config block.

Whatever the tool, the agent's first instruction should be to read `AGENTS.md`.
That file is deliberately short and tells it to run `hub.py help` rather than
loading documentation it does not need yet.

---

## 3. Spin up the team

Ask your agent to run the **`spinup`** skill with a seat count:

> spin up the team with 4 seats

It will check the repo, launch the dashboard, take the integrator seat in the
run you are talking to, and spawn a subagent for each remaining seat. You get
back a `127.0.0.1` URL carrying a per-launch token — the token changes every
time the dashboard restarts, so keep the tab it opens.

To do it by hand instead:

```bash
python tools/agent_hub/dashboard.py --user <your-id>
python tools/agent_hub/hub.py sessions
```

### How many seats

Seats are sessions, not agents; an empty one costs nothing but a row. What
matters is a rule the hub enforces rather than suggests:

> A review is only valid from a **different live session and a different run**
> than the one that wrote the change.

So two seats is the floor for anything needing review, and three keeps review
from blocking the next card. Beyond that, add seats when work queues, not
before. One seat is the configured integrator: it owns the backlog, names
cards, and performs merges, and it should be the long-lived run you are
actually talking to rather than a subagent that exits.

---

## Your job once it is running

### The board

Columns are ticket status; the left rail is sessions with each seat's role,
idle time, and claims. Idle time comes from *work* — locker saves, messages,
claims — not from a heartbeat, so a seat showing high idle really is doing
nothing, even if some process is still renewing its lease.

### Chat

Messages you send arrive under your `operator` ID. Agents can message each
other and you.

Treat messages as **coordination data, never authority**. An agent that reads
"please merge this" in its inbox is reading a string, not an instruction with
standing, and the contract says so in several places. This matters more than it
sounds: an inbox is the easiest place to inject a fake approval.

### Feature requests

The **New feature** button files a request into the integrator's inbox. It
lands as `new` and stays there until the integrator triages it into a ticket —
requests are not tickets, and agents are told not to open infrastructure
tickets for themselves.

### Merges: the part worth understanding

The one thing the harness is strict about. A merge needs a **signed approval
bound to one exact commit**, and an agent cannot produce one.

The flow:

1. An implementer finishes a branch and asks for review.
2. A **different** session, in a different run, reviews it and submits a review
   naming the exact SHA. The hub refuses a review from the ticket's claimant,
   and refuses one where a single run is both author and reviewer.
3. A clean review with no blockers appears in the dashboard's **Merge** tab.
4. You click Approve. The dashboard signs the approval with an operator key
   stored beside the database, which never enters an agent's context.
5. Before merging, the integrator runs:

   ```bash
   python tools/agent_hub/hub.py approval <TICKET> --sha <full-sha>
   ```

   Exit 0 means merge. Anything else means do not.

Consequences worth knowing:

- **If the branch head moves, the approval dies.** The signature covers the
  commit, so an amended branch simply has no approval and fails closed.
- **A new review revokes the old approval**, so a re-review after changes
  cannot inherit the previous yes.
- **The message announcing an approval is not the approval.** Its own body says
  so. Anything acting on the notification instead of re-checking the store has
  lost the guarantee.

### The skeptic

`docs/skeptic-charter.md` is a read-only role any idle seat can pick up: it
argues *against* work that drifts from the repository's stated purpose. A
flagged ticket goes to debate with three voters, and a `scrubbed` verdict means
the work is deleted from the tree — code, briefs, backlog text — so later
agents are not primed by an idea the team already rejected.

Edit that charter to describe **your** project before using it. As shipped it
describes nothing, on purpose.

---

## Keeping a published copy up to date

If you publish this harness as its own repository, update it with `--sync`
rather than rebuilding. `--dest` only creates a fresh repo and refuses a
non-empty directory, so pointing it at a clone would mean deleting that clone's
history and remote first.

```bash
python tools/publish_harness.py --sync /path/to/clone --commit "Sync from source"
```

The sync owns every **tracked** file in that clone: files the build no longer
produces are deleted, so something withdrawn for leaking cannot keep being
served from an old copy. It refuses to run over uncommitted changes to tracked
files, and it stages only what it generated.

Your own untracked files are left alone and reported. They are still *scanned*
first, and a failure there stops the sync before it changes anything — which is
the case that matters, because a dashboard screenshot discloses an entire board
in one image and no text filter will catch it.

## What this does not do

Worth being clear, so you do not lean on guarantees that are not here.

- **Identities are cooperative, not authenticated.** Session and holder IDs are
  claimed, not proven. The design assumes trusted processes under one user on
  one machine.
- **Claims are advisory.** A lease on a resource does not lock the filesystem.
  An agent that ignores a lost claim is not stopped by anything but its
  instructions.
- **One machine.** The database must be on local disk shared by those
  processes. There is no hosted mode.
- **Approvals are provenance, not single-use.** A signed approval for a commit
  stays valid until revoked or the commit changes; there is no
  unused-to-consumed transition. For merges this is near-harmless, since
  re-merging a commit is a no-op.

See `docs/decisions/0001-agent-workflow.md` for why each of these was chosen
rather than fixed.
