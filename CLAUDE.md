# FPL Challenge picker

Picks a squad for the current Challenge gameweek. The method, the scoring
differences and the known limitations are in SKILL.md. Read it before doing
anything this file does not cover.

James enters the team himself. Never try to submit one.

## The common case: James pastes line-ups and says go

He means confirmed team news has landed, and he wants to know whether the squad
he already entered still stands. Do all of this without being asked for it.

1. Read `data/tracking.json`. The highest-numbered entry is the squad he
   entered, not necessarily the solver's own answer.
2. For each club he gave you, replace that club's eleven:
   `python scripts/fpl_starters.py --set HUL --gkp "..." --def "..." --mid "..." --fwd "..."`
   Pass only the positions he gave you. Names match without accents. It refuses
   an ambiguous or misspelled name rather than guessing - fix the name, do not
   work around it.
3. Re-solve: `python scripts/fpl_solve.py`
4. Say plainly whether his squad changed. If it did, name the swap and the
   points difference. If it did not, say so in one line and stop.
5. If it changed: `python scripts/fpl_track.py record`
6. Commit and push `data/starters.json` and `data/tracking.json`.

He will usually only send the clubs that matter. Do not ask for the other
eighteen.

## Things that will silently go wrong

**Never refresh players.json from a cloud or phone session.** The container's
egress proxy blocks premierleague.com, so `fpl_data.py` fails with a 403.
Retrying changes nothing. Solve from the committed file - that is the whole
reason it is committed rather than ignored.

**Push before the session ends.** A cloud container is discarded when you are
done. An uncommitted correction to the eleven is lost, and the next run on the
desktop starts again from stale data without anything warning it.

**The recorded pick may deliberately differ from the solver.** Check
`chosen_by` and `deviation_reason` on the entry before treating the solver's
answer as the one to beat. The solver maximises the average, which is the wrong
objective for an eight-entry league - see the last item in SKILL.md's
limitations.

**Ownership comes from the main game, not Challenge.** Challenge reports 0.0%
for every player, so there is nothing else to read. It measures whether main-game
managers buy a player, which for a promoted club is about the club being
unfashionable rather than about who starts. It understates promoted-club players,
worst at the lowest ownership. Prefer observed minutes wherever they exist.

## Replies land on a phone

The squad, what changed, what to do next. Short sentences. Not an essay.
