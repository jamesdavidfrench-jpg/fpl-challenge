---
name: fpl-challenge
description: Pick the best FPL Challenge squad for the current gameweek. Reads the weekly challenge rules, projects points under Challenge scoring, solves for the best legal squad, and logs the pick so it can be scored later. Use when James says "FPL challenge", "pick my challenge team", "what's this week's challenge team", or "score last week's FPL".
---

# FPL Challenge picker

Picks a squad for the current FPL Challenge gameweek, explains each pick in one
line, and records the recommendation so its accuracy can be measured.

James enters the team himself. This skill does not submit anything - Challenge
login is bot-protected and scripted submission breaks without warning.

## Run it

Scripts are in `scripts/`, run them from the skill directory. Standard library
only, no installs.

```
python scripts/fpl_data.py      # refresh data (first run takes ~2 min, then cached)
python scripts/fpl_solve.py     # print the recommended squad
python scripts/fpl_track.py record
```

## Steps

1. **Refresh the data.** Run `fpl_data.py`. It writes `data/players.json` and
   reports which gameweek is open.

2. **Check the twist is known and current.** Look for
   `data/twists/gw<N>.json`. The gameweek's *squad* rules come from the API and
   are trustworthy. The *scoring* twist does not - `overrides.scoring` has been
   empty every week so far, so the twist only exists in prose.

   If the file is missing, or its `verified_on` date is older than this
   gameweek, look up the twist before solving:
   - `https://fplchallenge.premierleague.com/` states the current challenge on
     the home page (use the browser, not WebFetch - the site is a single-page
     app and WebFetch returns only the title).
   - premierleague.com news articles announce the challenges in batches.

   Then write the twist file (schema below) and **show James the twist and how
   you have encoded it before running the solver.** Getting the twist wrong
   makes every pick wrong, and it is the one step no API can verify.

3. **Solve.** Run `fpl_solve.py`. Show the recommended squad plus one or two
   alternatives. Keep the one-line reason per pick that the script produces.

4. **Record it.** Run `fpl_track.py record` so the week can be scored later.

5. **Score finished weeks.** If a previous gameweek has finished, run
   `fpl_track.py score` then `fpl_track.py report`.

## Starting elevens

`data/starters.json` holds the expected eleven for each club. When it exists the
picker trusts it completely and skips its own guesswork, so this is the main
lever for improving picks.

```
python scripts/fpl_starters.py            # draft all 20, flag the shaky calls
python scripts/fpl_starters.py --report   # re-flag without overwriting edits
python scripts/fpl_starters.py --set LEE --def "Bogle,Rodon,Muharemovic,Bijol,Justin"
```

`--set` takes any of `--gkp/--def/--mid/--fwd` and replaces only the positions
given. Names are matched without accents, so "Muharemovic" finds
"Muharemović". It refuses ambiguous or misspelled names rather than guessing,
and warns when a named player is injured.

Two things the draft cannot work out on its own, so check them:

- **Formation.** The draft assumes a back four. Clubs playing three at the back
  field five defenders in Fantasy terms, because wing-backs count as defenders.
  Correct those clubs by hand or the picker is choosing from the wrong four.
- **Holding midfielders.** They start every week but are barely owned, because
  ownership follows attacking returns. The draft under-rates them.

Premier League publish predicted line-ups for all 20 clubs before the season
(search "how every Premier League club could line up"). The squad lists in those
articles are ordered first-choice-first for goalkeepers, which is reliable - it
agreed with 18 of 20 drafted keepers and fixed the other two. Do not trust that
ordering for outfield players; it is not the starting eleven.

## Twist file schema

`data/twists/gw<N>.json`. Declarative on purpose - no executable rules.

```json
{
  "event": 2,
  "name": "Welcome Back",
  "description": "Double points for promoted-club players.",
  "player_multipliers": [
    {"factor": 2, "teams": ["COV", "IPS", "HUL"]}
  ],
  "scoring_multipliers": {"goals_scored": 2, "assists": 2},
  "source": "<url>",
  "verified_on": "2026-08-31"
}
```

`player_multipliers` entries can filter on any combination of:
`teams` (short names), `team_join_date_after` (ISO date), `positions`
(GKP/DEF/MID/FWD), `player_codes`. All listed filters must match.

`scoring_multipliers` scales the share of a projection attributable to that
stat. It is approximate - see limitations.

## Timing: there is no single deadline

Challenge does not lock the whole team at once. Players lock **individually when
their match kicks off**, and the team can be edited until then. So this is worth
running more than once a gameweek: at gameweek open, and again before each
batch of kickoffs.

`fpl_solve.py` automatically excludes players whose match has already started,
since they can no longer be added. Pass `--include-started` to see them anyway.

## Things that will silently go wrong if forgotten

- **A goalkeeper is always required, and the API does not say so.** The game's
  formation picker offers outfield shapes only (1-1-3, 1-2-2, 1-3-1, 2-1-2,
  2-2-1, 3-1-1) and every one sums to five, not six - the keeper is a fixed
  sixth slot outside the formation. The `element_types` override mirrors the
  picker, so it lists DEF/MID/FWD and omits GKP, which reads as "no keeper
  allowed" and produces an illegal team. `constraints()` adds the keeper back.
  Do not remove that.
- **Join on `code`, never `id`.** 25 of 590 players have different element ids
  in the two games. The scripts already do this; keep it that way in any new
  code.
- **Challenge scoring is not FPL scoring.** Goals are worth 10/6/5/4 by position
  (GKP/DEF/MID/FWD), saves are 1 each rather than 1 per 3, and there is no
  vice-captain. Forwards are worth noticeably less here than in normal FPL.
- **Unreleased gameweeks have placeholder rules.** Only trust `overrides` when
  the event has `released: true`. Rules for future weeks do change.

## Known limitations - be honest about these in output

- **Defensive contribution is modelled as a threshold, not a rate.** It pays 2
  points for crossing a per-match bar - 10 defensive actions for defenders, 12
  for midfielders and forwards - so the projection estimates the chance of
  clearing it via a Poisson draw on the player's per-90 rate. Two caveats.
  Poisson assumes variance equals the mean, but real match counts are more
  spread out, so players just under the bar are slightly understated and those
  just over it slightly overstated. And the rate is drawn only from 2024/25
  onward, because the stat did not exist before then and earlier seasons report
  zero - never let those zeros into the average, or every defender's rate
  halves. Replace with real per-match counts from `event/<n>/live/` once a few
  gameweeks of 2026/27 exist.
- **Expected goals are blended 70/30 with goals actually scored**, not used
  alone, because finishing ability is partly real and partly luck. They are
  rescaled per position before use - defenders convert only 0.85 of their
  expected goals, and forwards collect 2.37 times their expected assists. Those
  factors were measured over three seasons and should be re-measured
  occasionally; a flat league-wide figure marks forwards down by nearly half.
  Expected goals also only exist from 2022/23, so as with defensive
  contribution, earlier seasons must be kept out of the average.
- **Goals conceded is modelled per match, not per season.** The penalty is a
  point per second goal let in, applied per match and rounded down, so season
  totals cannot be halved to get it - a defender letting in one goal in each of
  twenty matches loses nothing, where the season total suggests ten. The
  expectation is taken over a per-match Poisson draw instead.
- **Clean sheets and conceding come from one number.** `CONCEDE_RATE_BY_DIFF`
  gives expected goals conceded per fixture and the clean sheet probability is
  derived from it, so the two cannot contradict each other. It is anchored on
  last season: keepers and defenders conceded 1.30 goals per 90 actual and 1.34
  expected, and the model's average fixture is 1.32, giving a clean sheet rate of
  0.267 against a real 0.274. Re-anchor it if the league's scoring rate shifts.
- **Team strength is not used** - only fixture difficulty. The API's team
  strength fields are all zero pre-season, so a good defence and a bad one at the
  same difficulty rating look identical. This is the biggest remaining gap in the
  defensive side of the model.
- **Cards, own goals, penalty saves and misses are unmodelled**, as is
  `short_play` - everyone who plays is credited the full appearance points, so
  substitutes are slightly overstated.
- **New signings have no Premier League history**, so they fall back to a
  price-based estimate. This matters most in gameweeks that double new
  signings, which is exactly when it is least reliable. These picks are flagged
  `no PL history, price-based estimate` - treat them as lower confidence.
- **Early season has no current-season data at all.** Projections lean on prior
  seasons until roughly GW5-6.
- **Whether the API applies the twist to reported points is unknown** until a
  gameweek finishes. The tracker records both interpretations and they can be
  reconciled against James's actual score after the first scored week.
- **The squad returned is exactly optimal when the budget is unlimited**, which
  is every Challenge gameweek so far. If a gameweek ever sets a real budget, the
  solver prices cost into each player and tunes that price until the squad fits,
  which is near-optimal rather than provably optimal, and takes ~20s instead of
  being instant.
