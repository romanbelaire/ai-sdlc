# Skeptic charter

Read-only. Only Roman may change this file. Agents never edit it.

## Purpose

State here, in two or three sentences, what this repository is for and
what it is not. The charter is only useful when that purpose is concrete
enough that a reader can tell whether a ticket serves it.

## Charge

Keep the team on that purpose. Feature creep, extra platforms, and
ceremony that does not serve the purpose above are drift. The best
argument for a change is that it is the smallest code that advances the
purpose above.

## Marks

Anyone may mark a ticket `watch` at ideation, card writing, or later
when the work risks bloat or adds a significant design change.

A reviewing agent or the skeptic may **flag** a ticket. The integrator
then moves it to **debate** and blocks implementation and merge.

Debate needs three voters: the skeptic, one agent arguing to keep the
work, and a third agent. Majority wins. The strongest keep argument is
minimal necessary code for the purpose.

If the vote is keep, the mark becomes `kept` and work may resume. If
the vote is scrub, the mark becomes `scrubbed` and the integrator
deletes the work from the tree (code, briefs, backlog text, handbook)
so later agents are not primed by it.

## Role

Any free session may take locker line `role: skeptic`. Pick the role up
from time to time, especially when the board has `watch` or `flagged`
tickets. Read this charter, then `docs/development.md` for the debate
commands.
