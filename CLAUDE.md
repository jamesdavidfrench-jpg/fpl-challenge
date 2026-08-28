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
2. For each club he gave you, replace that club's eleven, with --confirmed:
   `python scripts/fpl_starters.py --set HUL --confirmed --gkp "..." --def "..." --mid "..." --fwd "..."`
   Pass only the positions he gave you. Names match without accents. It refuses
   an ambiguous or misspelled name rather than guessing - fix the name, do not
   work around it.

   `--confirmed` is what makes team news worth having. It tells the projection
   this is a published sheet, so named players get full minutes instead of a
   share guessed from ownership, and anyone omitted is a substitute. Never pass
   it for a line-up you inferred rather than read.
3. Re-solve: `python scripts/fpl_solve.py`. This always solves the whole squad
   from scratch - there is no incremental mode, and you should not build one.
   Do not reason about who replaces whom. Ask what the best six are now.
4. Report the full six either way, then say what changed against the recorded
   squad and by how many points. If nothing changed, still list the six, in one
   short block.

   The answer is often not a substitution. A squad built as a stack - three
   players from one club, chosen because their good weeks arrive together - can
   stop making sense entirely when that club's sheet changes, and the right
   response is to move the whole shape to another club, captain included. A
   replacement-shaped answer cannot find that, and will hand him a patched
   squad whose reason for existing has gone.
5. If it changed: `python scripts/fpl_track.py record`
6. Commit and push `data/starters.json` and `data/tracking.json`. If you
   changed neither - a hypothetical line-up, or a sheet that changed nothing -
   say so explicitly, so an empty push is a stated outcome rather than a step
   that looks like it was skipped.

He will usually send both clubs in a fixture, not one. That is deliberate: if a
club's sheet is badly changed the whole shape of the squad may want to move to
the other side of that match, and judging it needs both elevens. Do not ask for
the other eighteen.

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
