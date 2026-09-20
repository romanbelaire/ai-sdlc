# Running a team on this harness

You do not configure this by hand. You clone the repo, point an agent at the
folder, and the agent drives the rest through `AGENTS.md` and `hub.py help`.

## 1. Clone and check

```bash
git clone <your-repo> && cd <your-repo>
uv sync --project tools/agent_hub --locked
python tools/check_repo.py
python -m unittest discover -s tools/agent_hub/tests
```

`check_repo.py` validates the backlog. It should pass before you start.

## 2. Point an agent at the repo

Any of these work:

- **Claude Code** — run `claude` in a terminal at the repo folder. The skills
  in `.claude/skills/` are picked up with no further setup.
- **Cursor** — open the folder in Cursor.
- **Codex** — run `python tools/agent_hub/configure_codex.py` to write the
  project's MCP config, then open the folder in a new trusted session.

## 3. Tell the agent where to start

Tell it to read `AGENTS.md`, or ask it to run the **`spinup`** skill with a
seat count:

> spin up the team with 4 seats

`spinup` launches the dashboard and hands back a local URL.

## Next

`README.md` explains what this system is and its limits. `docs/development.md`
covers the full workflow.
