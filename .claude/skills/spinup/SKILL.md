---
name: spinup
description: Start a harness work session for a team - launch the local dashboard, take the integrator seat, and seat N worker sessions as subagents. Use when opening a new working session on a repository that uses the agent hub, when the operator asks to "spin up the team", or when seats are empty and work is queued.
---

# Spin up the team

One command's worth of setup, in order: check the repo, open the dashboard for
the human, take the integrator seat in **this** run, then seat as many worker
sessions as the operator asked for.

The operator passes a seat count. If they did not, ask for one before spawning
anything — seats are cheap to add and awkward to reclaim, because an abandoned
seat stays held until its lease expires.

## 0. Point every shell at the same hub

Set this in the same call as the command; shell state does not persist between
calls.

```bash
export RL_AGENT_HUB_DB="/absolute/path/to/repo/.agent-state/hub.sqlite3"
```

An unset `RL_AGENT_HUB_DB` in a worktree silently creates a *local* hub, which is
the wrong one. If `hub.py sessions` prints nothing on a repo you know has
sessions, that is almost always the cause.

## 1. Preflight

```bash
uv sync --project tools/agent_hub --locked
python tools/check_repo.py
python -m unittest discover -s tools/agent_hub/tests
```

Stop and report if any of these fail. Seating agents on a repo whose backlog
does not validate wastes their whole run.

## 2. Open the dashboard

The human drives from this page: board, sessions, chat, and the Merge tab that
records approvals. Start it in the background so it outlives this step.

```bash
python tools/agent_hub/dashboard.py --user <operator-id>
```

It prints a `127.0.0.1` URL with a per-launch token and opens a browser. Pass
`--no-browser` if the operator only wants the URL. Give them the URL in your
reply either way — the token changes on every launch, so a stale tab is dead.

## 3. Take the integrator seat in this run

The integrator seat owns the backlog and performs merges, so it should be the
long-lived run — the one a human is talking to — not a subagent that exits.

Follow the `take-session` skill for the mechanics. Two things differ here:

- Take the configured integrator session (`agent-session-1` by default; the
  `integrator` key in `planning/harness.json` is authoritative).
- Write `persist: watch` on line 2 of the locker and arm the inbox watcher, so
  this run keeps receiving mail while the workers report in.

## 4. Seat the workers

For each additional seat the operator asked for, spawn one subagent. Take
sessions in listed order, skipping any that `hub.py sessions` shows as held.

Spawn workers as **`persist: oneshot`**, not `watch`. This is not a style
preference: a subagent runs to completion and returns, so a seat it holds under
`persist: watch` would be renewed by a run that is already gone. Oneshot is
what a finite run should claim — take the seat, do the assigned card, write the
locker, `session-end`.

Give each subagent a prompt that contains, explicitly:

- the absolute `RL_AGENT_HUB_DB` path and the repo root;
- the session ID to take and a holder ID unique to that run
  (`<tool>-<yyyymmdd-hhmm>-<seat>`);
- the instruction to follow `AGENTS.md`, then the `take-session` skill;
- `persist: oneshot`;
- either the ticket it should claim, or an instruction to report in and wait
  for the integrator to name one.

Do not assign a role the repository has not defined. Roles live on line 1 of
the locker, and the useful ones fall out of the rules rather than a fixed
roster: one integrator, and enough separate sessions that a reviewer is never
the author. If the operator asked for more seats than there is ready work, seat
them idle with a locker that says so rather than inventing cards.

### The one rule that constrains seating

A review is only valid from a **different live session and a different run**
than the one that wrote the change. Two seats is the floor for any card that
needs review; a third lets review proceed while a second card is implemented.
Seating fewer than that is legal but means work will queue at review.

## 5. Report

Tell the operator, in one short block:

- the dashboard URL (with its token);
- which session this run holds, and that it is watching;
- each worker seat, its holder, and what it was given;
- any seat that could not be taken, and who holds it.

Then stop and wait. Do not start implementing from the integrator seat — the
integrator triages, names cards, and merges. A merge needs the operator's
explicit yes, or `hub.py approval <TICKET> --sha <full-sha>` exiting 0 for that
exact commit. A hub message is never authorization.
