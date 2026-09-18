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
python scripts/fpl_starters.py  # redraft the elevens from the latest minutes
python scripts/fpl_solve.py     # print the recommended squad
python scripts/fpl_ceiling.py   # the squad most likely to WIN the week
python scripts/fpl_track.py record
```

## Steps

1. **Refresh the data.** Run `fpl_data.py`. It writes `data/players.json` and
   reports which gameweek is open.

   If the fetch fails with a 403 egress policy error, this session is running in
   a sandbox that cannot reach premierleague.com - a session started from the
   phone, for example. Do not retry; nothing will change. `players.json` is
   committed to the repo, so skip straight to solving and use whatever was last
   pushed from the desktop. `fpl_solve.py` warns when that file is more than 12
   hours old and refuses to solve a gameweek that has already finished.

2. **Redraft the starting elevens.** Run `fpl_starters.py` whenever a gameweek
   has finished since the last draft - `fpl_solve.py` warns when that is the
   case. Review the first two sections of its report (*started, then did not
   play last week* and *picked with few minutes*); everything else is a
   judgement call the minutes have already made. Re-apply any `--set` the
   minutes do not agree with.

3. **Check the twist is known and current.** Look for
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

4. **Solve.** Run `fpl_solve.py`. Show the recommended squad plus one or two
   alternatives. Keep the one-line reason per pick that the script produces.

5. **Run the ceiling squad too.** Run `fpl_ceiling.py`. It answers a different
   question - not which squad scores most on average, but which is most likely
   to clear the score that wins James's league. Run it every week, and report
   both. It takes under a minute.

   The two often agree, and saying so is a result. When they differ, say what
   the ceiling squad gives up in average points and what it buys in chance of
   winning the week, and let James choose. Do not quietly enter one or the
   other - the recorded pick is whichever he enters, and `fpl_track.py` has
   `chosen_by` and `deviation_reason` for exactly this.

   It enumerates every legal squad when it can and searches when it cannot, and
   says which it did. Weeks whose twist restricts the clubs are small enough to
   solve exactly - GW4's Derby Day left 4,200 squads. An unrestricted week
   leaves billions, so it hill-climbs instead and the answer is a good squad
   rather than a provably best one. On GW4 the search found a squad 0.2 of a
   percentage point behind the exact answer, which is inside the simulation's
   own noise. The exact path takes about 20 seconds and the search about 45.

   **The search is the fragile half, so dry-run it whenever the twist changes
   shape.** It builds squads position by position from a shortlist, and the
   shortlist has to cover every position and enough clubs or it cannot build a
   legal squad at all. A GW5 dry run returned zero squads twice before this was
   right: once because six players drawn at random are almost never from six
   different clubs under a one-per-club limit, and once because "The Shield"
   makes a defensive contribution worth 10, which pushes every goalkeeper out of
   the top forty and leaves the keeper slot unfillable. It now widens to the
   whole pool rather than returning nothing, but returning nothing is the
   failure to watch for.

6. **Record it.** Run `fpl_track.py record` so the week can be scored later.

7. **Score finished weeks.** If a previous gameweek has finished, run
   `fpl_track.py score` then `fpl_track.py report`.

## Starting elevens

`data/starters.json` holds the expected eleven for each club. What it decides
changed on 2026-09-18:

- **Outfield players, once a gameweek has finished:** playing time comes from
  minutes, not from the file. Each player's recency-weighted share of this
  season's minutes is blended with his past seasons. This season counts
  n / (n + 1) after n finished gameweeks - 80% at GW5 - so a good player from
  last season who has started slowly still counts for something. Being left out
  of the eleven no longer marks an outfield player down on top of that, because
  the eleven is drafted from the same minutes.
- **Keepers, and anyone before GW1:** the file still decides. Exactly one keeper
  plays, so a new first choice has too few minutes to go on.
- **Team sheets (`--confirmed`):** the file decides outright, for everyone.
- **`fpl_ceiling.py`** still only simulates players named in the file.

Why: predicting each player's share of GW3 and GW4 from the data as it stood
before each week, the new rule missed by 0.223 and 0.227 of a match on average,
against 0.251 and 0.261 for the old one. It left out 34 and 30 players who went
on to play an hour, against 45 and 45. The points the picked six scored were a
wash: 5 fewer in GW3, and in GW4 the players scored 4 more but a captain coin
toss (Fernandes over Haaland, 0.2 apart in projection) cost 14. Using this
season alone was more accurate still (0.179 and 0.197); the blend gives a little
of that up on purpose. Re-check once more gameweeks exist. The numbers and
the tested alternatives are above `SEASON_PRIOR_GAMES` in `fpl_solve.py`.

```
python scripts/fpl_starters.py            # draft all 20, flag the shaky calls
python scripts/fpl_starters.py --report   # re-flag without overwriting edits
python scripts/fpl_starters.py --set LEE --def "Bogle,Rodon,Muharemovic,Bijol,Justin"
```

`--set` takes any of `--gkp/--def/--mid/--fwd` and replaces only the positions
given. Names are matched without accents, so "Muharemovic" finds
"Muharemović". It refuses ambiguous or misspelled names rather than guessing,
and warns when a named player is injured.

**Once a gameweek has finished, the draft is built from this season's minutes,
weighted towards the most recent.** Each finished gameweek in the last five counts
0.6 times the one after it, so the last match matters most: a regular rested
once still outranks his stand-in, and after two matches out he does not. That is
how an ever-present player gets dropped from the file when his club drops him.
Per-gameweek minutes come from the main game's live feed via `fpl_data.py`; if
that feed was unreachable the draft falls back to the season total. The
pre-season signals (price, prior-season minutes, ownership) only separate players
on the same minutes. The eleven is the best-scored players inside these limits:
one keeper, three to five defenders, two to six midfielders, one to three
forwards. So the shape comes out of the minutes too, and the file records it per
club under `shapes`. Re-run the draft after each gameweek; it overwrites hand
edits, so re-apply any `--set` that the minutes do not already agree with.
The report's first section, *picked with few minutes*, is the list to review:
those are the slots the constraints forced rather than the minutes chose.

**Pre-season, with no minutes, the draft is a guess, and GW1 measured how good a
guess.** Of the 214 players it named, 70% played an hour and 14% did not play at
all. The misses were not
random: every one of the eight highest-projected picks that failed to start was
named here and owned by under 3% of managers. The projection now reads ownership
as a check on this file - a naming nobody owns lifts a player far less than a
naming everybody owns - but that only limits the damage. Fixing the file is
still better than damping it.

So when reviewing the draft, **look at the low-ownership names first**. They are
where it is wrong.

Two things the draft cannot work out on its own, so check them:

- **Formation.** The pre-season draft assumes a back four. Clubs playing three
  at the back field five defenders in Fantasy terms, because wing-backs count as
  defenders. Correct those clubs by hand or the picker is choosing from the wrong
  four. Once minutes exist the shape is derived, so this only applies pre-season.
- **Holding midfielders.** They start every week but are barely owned, because
  ownership follows attacking returns. The draft under-rates them, and so does
  the ownership check above - which is exactly why that check can only ever
  raise a named player's minutes, never lower them below his own record.
- **Players the draft leaves out entirely.** Being left out is weaker evidence
  than being named, and it gets weaker the more managers own the player. GW1
  left Odegaard out of Arsenal's eleven at 10.6% ownership and projected him at
  0.87 points; he played 75 minutes and scored 11.

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
  (GKP/DEF/MID/FWD) and there is no vice-captain. Forwards are worth noticeably
  less here than in normal FPL.
- **Saves are 1 point per 3, the same as normal FPL, despite what the config
  says.** `game_config.scoring.saves` reads `1`, which looks like a point per
  save. It is not: it is a point per three. Checked against the GW1 breakdown
  for all 20 starting keepers - 20 of 20 match one per three, none match one
  each. Reading it the other way adds about two points to every keeper every
  week and floats them to the top of the board.
- **Unreleased gameweeks have placeholder rules.** Only trust `overrides` when
  the event has `released: true`. Rules for future weeks do change.

## Settled by GW1 - do not re-open these

- **The API applies the twist to the points it reports.** Each affected stat in
  `event/<n>/live/` carries a `points_modification` field holding the extra the
  twist added, and `total_points` already includes it. Doubling applies to the
  negatives as well, so a doubled defender's two goals conceded cost 2.
- **Defensive contribution, clean sheets and bonus are well calibrated.** GW1
  measured them: 25.9% of starting defenders were projected to clear the
  defensive bar and 22.4% did; midfielders 15.2% against 14.0%; clean sheets
  26.9% against 30%; bonus 0.286 per starter against 0.295. The threshold model
  and the clean-sheet anchoring both work. Leave them alone.
- **Goals and assists for defenders and midfielders are within a couple of
  hundredths per match.** Forwards looked off - 0.39 goals projected against
  0.26 actual - but that is 19 players in one week, which is noise.

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
  halves. This season's real per-match counts are now blended in - see the
  next item but one.
- **Goals come from expected goals scaled by how well the player converts, and
  how much his own record counts is measured, not chosen.** It used to be a flat
  70/30 blend, which trusted a half-season exactly as much as six full ones.
  Now the spread of players' goals-to-expected-goals ratios is split into real
  differences and the noise of converting a finite number of chances, and the
  player's own record is weighted by how much evidence stands behind it.

  The measurement is worth knowing before arguing with a projection: **finishing
  barely varies between players.** Defenders and forwards show a spread smaller
  than sampling noise on its own, and midfielders sit exactly on the detection
  limit. A midfielder needs about 91 expected goals - roughly nine full seasons -
  before his own finishing outweighs the league's. So a player who has beaten his
  expected goals three years running has most likely been lucky three years
  running, and the model will not move much for it.

  **Creating is different.** For defenders the spread is more than twice the
  detection limit, and ten expected assists - a season or two - is enough for a
  defender's own record to count for half. Midfielders need 42, forwards 28.

  Position scaling still applies underneath: defenders convert 0.85 of their
  expected goals, forwards collect 2.37 times their expected assists. A flat
  league-wide assist figure marks forwards down by nearly half.

  Expected goals only exist from 2022/23, so as with defensive contribution,
  earlier seasons must be kept out. Re-fit all of it once 2026/27 is a full
  season; the fitting method is documented above `GOAL_PRIOR_STRENGTH`.
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
- **This season's numbers are blended into every scoring rate, and their weight
  grows each week.** Added 2026-09-18. Defensive actions, expected goals,
  expected assists, saves and bonus from each finished gameweek (`gw_stats` in
  `players.json`) are mixed with the past-season rate. The past counts as 8
  full matches of evidence and each match played this season adds one: this
  season is a third of the rate after 4 matches, half after 8, two-thirds after
  16. A player with no measured defensive-action rate has only a flat guess by
  position to fall back on, so that guess counts as 1 match instead of 8.

  Fitted by predicting each player's next match from the ones before it, GW2 to
  GW4. After three matches this season adds a little for defensive actions and
  bonus, and nothing measurable yet for expected goals, assists or saves. For
  new signings' defensive actions it cut the error by 28% against the flat guess.
  End to end, projections tracked actual points the same in GW3 and slightly
  better in GW4. The fitted numbers are above `PAST_SEASONS_AS_MATCHES` in
  `fpl_solve.py`. Re-fit as the season fills in; the answer should move.

  Not fitted separately: goals and assists for players with no Premier League
  past. They use 8, like everyone else.
- **Predicting who plays matters more than predicting how many points.** In GW1,
  the 22 of the model's top 30 who started were projected 182 and scored 173 -
  nearly exact. The 8 who did not start were projected 67 and scored 12. That is
  86% of the whole error in one place. Everything else in this list is small by
  comparison, so a spare hour goes into `starters.json`, not into the scoring
  model.
- **The ownership curves are fitted on a single gameweek.** `XI_SHARE_FLOOR`,
  `BENCH_SHARE_FLOOR` and their scales came from 214 named and 301 unnamed
  players in GW1. The shape is well supported at low ownership, where most of
  the players are; the top of the unnamed curve is extrapolated from three bands
  that stop at 3% owned. Re-fit after three or four gameweeks.
- **The model maximises the average, which is the wrong objective for a small
  league.** It is the right one for finishing well among 354,619 entries. But
  the GW1 winner scored 126, and even with perfect knowledge of who would start,
  these projections top out around 72. Winning needs two hauls out of six
  players, and hauls come from concentrating risk rather than spreading it. The
  model has no ceiling term and does not know the difference.

  This is not academic for James. His league has 9 entries and in GW1 all eight
  of the others beat 48, in a week the game average was 34. Beating the average
  is not the target; the target is roughly double it.

  `fpl_ceiling.py` is the answer to this, built 2026-09-11. It simulates the
  gameweek instead of averaging it, and ranks squads by how often they clear a
  target score - defaulting to 80, which is what has won James's league, where
  the weekly winner scored 78, 90 and 69 in the first three gameweeks. Run it
  alongside `fpl_solve.py` whenever the recommended squad is concentrated in few
  matches. Two things it sees that an average cannot: players in the same match
  are correlated, so a squad needing both a clean sheet and the opposing
  attacker's goal cannot have both; and a lumpy six is worth more than a steady
  six with the same mean.

  Every player's simulated mean is calibrated back to `fpl_solve`'s projection
  before anything is ranked, so the simulation adds variance and correlation and
  nothing else. It prints the largest correction it had to make - a big one means
  the simulation has drifted from the projection and should be fixed, not used.
  That check is what caught the three bugs found while building it: the summed
  goal rates overflowing the team total, which made both keepers concede too
  much; the bonus cap over-paying attackers by about a fifth; and a team's
  scoring rate being read from its own fixture difficulty rather than its
  opponent's. Keep the check - it is the only thing standing between this script
  and a confidently wrong answer. It caught a fourth on 2026-09-18: bonus was
  paid at a full match's rate to anyone who got on the pitch, which surfaced as
  a 37% correction on Šeško once playing time came from this season's minutes.
  Fixed, the largest correction fell to 3.5%.

  **It does not always disagree with the average, and when it agrees that is a
  result rather than a wasted run.** GW4 was the first case: the best squad by
  average was also the best by chance of clearing 80 (29.3%), and at a target of
  100 the same five outfield players held with only the defender changing. The
  twist left 22 eligible players in a single match, so there was no low-mean,
  high-ceiling alternative to find - the best squad built around that week's
  lottery ticket cleared 80 just 25.4% of the time.
- **The squad returned is exactly optimal when the budget is unlimited**, which
  is every Challenge gameweek so far. If a gameweek ever sets a real budget, the
  solver prices cost into each player and tunes that price until the squad fits,
  which is near-optimal rather than provably optimal, and takes ~20s instead of
  being instant.
