"""Project Challenge points and pick the best legal squad.

Standard library only.

Three stages:
  1. project()  - expected Challenge points per player for this gameweek
  2. apply_twist() - the weekly rule change, from a small declarative file
  3. solve()    - exact best squad under the gameweek's constraints

The optimiser is a dynamic program over clubs. Club limit is the only constraint
that couples positions together, so processing one club at a time and tracking
(position counts, spend) as the state covers it exactly. Dominance pruning keeps
the state frontier small enough that this runs in well under a second.

Usage:
    python fpl_solve.py                 # pick for the current gameweek
    python fpl_solve.py --alternatives 5
"""

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone

# Player names carry accents (Milenkovic, Sesko, Muller) that the default
# Windows console encoding cannot represent, which would crash on print.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(HERE, "data")

POS_NAME = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
POS_ID = {v: k for k, v in POS_NAME.items()}

# Fixture difficulty -> attacking output multiplier, and clean sheet probability.
ATTACK_BY_DIFF = {1: 1.25, 2: 1.12, 3: 1.00, 4: 0.88, 5: 0.75}
HOME_ATTACK_BOOST = 1.08

# Expected goals conceded per match, by fixture difficulty. Clean sheets and
# conceding penalties are both derived from this one number rather than being
# set separately, because they are the same quantity viewed two ways - a clean
# sheet is just no goals - and parameterising them apart lets them drift into
# contradicting each other.
#
# Anchored on last season: goalkeepers and defenders conceded 1.30 goals per 90
# actual, 1.34 expected, so an average fixture sits at 1.32. The earlier
# hand-set clean sheet figures implied 1.14, which was too kind by about 15%.
CONCEDE_RATE_BY_DIFF = {1: 0.85, 2: 1.05, 3: 1.32, 4: 1.65, 5: 2.05}


def clean_sheet_prob(difficulty):
    """Chance of no goals conceded, from the rate above."""
    return math.exp(-CONCEDE_RATE_BY_DIFF.get(difficulty, 1.32))

# Defensive contribution points are awarded once per match for crossing a
# threshold of defensive actions, not per action. Defenders need 10 clearances,
# blocks, interceptions and tackles; midfielders and forwards need 12, with
# recoveries also counting. The API's defensive_contribution field already
# applies the right definition per position, so it can be used as-is.
DC_THRESHOLD = {2: 10, 3: 12, 4: 12}

# Fallback rate per 90 for players with no Premier League record. Set below the
# thresholds above, so an unknown player earns a little but is not assumed to be
# a ball-winner.
DC_PRIOR = {1: 0.0, 2: 7.0, 3: 6.0, 4: 3.0}

# How much to trust expected goals over goals actually scored. Expected goals
# predict future scoring better than past scoring does, because a season of
# finishing luck does not repeat, but genuine finishing ability is real and
# persistent - hence a blend rather than replacing one with the other.
XG_WEIGHT = 0.7

# Expected goals and assists have to be rescaled before use, and the correction
# differs sharply by position. Measured over the last three seasons across every
# player-season of 450 minutes or more:
#
#   goals / expected goals    DEF 0.85   MID 1.03   FWD 0.99
#   assists / expected assists DEF 1.30  MID 1.32   FWD 2.37
#
# Two things stand out. Defenders convert well below their expected goals, most
# of their chances being scrambled set-piece efforts. And forwards pick up well
# over twice the assists their expected assists imply. League-wide the assist
# correction averages 1.40, but applying that flat figure marks forwards down by
# nearly half, so the split is kept.
#
# Goalkeepers score and assist so rarely that the sample is meaningless; they get
# a neutral 1.0 and it makes no practical difference to their projection.
XG_SCALE = {1: 1.00, 2: 0.848, 3: 1.034, 4: 0.995}
XA_SCALE = {1: 1.00, 2: 1.303, 3: 1.323, 4: 2.369}


# ---------------------------------------------------------------- projection


def _rates_from_history(p):
    """Per-90 scoring rates from prior seasons, most recent weighted heaviest.

    Scoring rates and playing time are drawn from different samples on purpose.
    A rate needs a decent sample to mean anything, so seasons under 450 minutes
    are excluded from the per-90 figures. Playing time is the opposite: a recent
    season spent on the bench is the single most informative thing about whether
    someone will start, so every recent season counts towards minutes, zeros
    very much included. Excluding them made bench players look like starters.

    Returns None when there is no usable history - new signings and youth
    players fall through to a price-based prior instead.
    """
    history = p.get("history_past", [])
    rate_seasons = [h for h in history if h.get("minutes", 0) >= 450]
    if not rate_seasons:
        return None

    rate_seasons = rate_seasons[-3:]
    weights = [0.2, 0.3, 0.5][-len(rate_seasons):]
    tw = sum(weights)

    def per90(key):
        return sum(
            w * (s.get(key, 0) / s["minutes"] * 90)
            for s, w in zip(rate_seasons, weights)
        ) / tw

    mins_seasons = history[-3:]
    mw = [0.2, 0.3, 0.5][-len(mins_seasons):]
    minutes_share = sum(
        w * min(1.0, s.get("minutes", 0) / (38 * 90))
        for s, w in zip(mins_seasons, mw)
    ) / sum(mw)

    # Defensive contribution was only introduced in 2024/25. Earlier seasons
    # report zero because the stat did not exist, not because the player did
    # nothing, so averaging them in halves every defender's rate. Only seasons
    # that actually recorded it count - any player with 450+ minutes racks up
    # some defensive actions, so a zero here means "not measured".
    dc_seasons = [s for s in rate_seasons if s.get("defensive_contribution", 0) > 0]
    if dc_seasons:
        dcw = [0.2, 0.3, 0.5][-len(dc_seasons):]
        dc = sum(
            w * (s["defensive_contribution"] / s["minutes"] * 90)
            for s, w in zip(dc_seasons, dcw)
        ) / sum(dcw)
    else:
        dc = None  # caller falls back to the per-position prior

    # Expected goals only exist from 2022/23. As with defensive contribution,
    # earlier seasons report zero because nothing was measured, so they must be
    # kept out of the average. Any player with 450+ minutes registers some
    # expected goals or assists, so both being zero means "not recorded".
    xg_seasons = [
        s for s in rate_seasons
        if float(s.get("expected_goals") or 0) > 0
        or float(s.get("expected_assists") or 0) > 0
    ]
    goals, assists = per90("goals_scored"), per90("assists")
    if xg_seasons:
        xw = [0.2, 0.3, 0.5][-len(xg_seasons):]
        twx = sum(xw)

        def xper90(key, scale):
            return scale * sum(
                w * (float(s.get(key) or 0) / s["minutes"] * 90)
                for s, w in zip(xg_seasons, xw)
            ) / twx

        pos = p["position"]
        goals = (
            XG_WEIGHT * xper90("expected_goals", XG_SCALE.get(pos, 1.0))
            + (1 - XG_WEIGHT) * goals
        )
        assists = (
            XG_WEIGHT * xper90("expected_assists", XA_SCALE.get(pos, 1.0))
            + (1 - XG_WEIGHT) * assists
        )

    return {
        "goals": goals,
        "assists": assists,
        "saves": per90("saves"),
        "bonus": per90("bonus"),
        "dc": dc,
        "minutes_share": minutes_share,
    }


# Goals conceded costs a point for every second goal let in during a match.
GOALS_PER_CONCEDE_PENALTY = 2


def _expected_concede_units(cs_prob):
    """Expected number of conceding penalties in one match.

    The rounding is the whole problem here. The penalty applies per match, not
    per season, and it rounds down - so a defender letting in exactly one goal in
    each of twenty matches loses nothing at all, while the season total of twenty
    would suggest ten points lost. Dividing season totals gets this badly wrong,
    so the expectation has to be taken over a per-match distribution.

    Goals conceded are treated as Poisson at the rate implied by the clean sheet
    probability, which is itself derived from the same per-fixture rate, so the
    two can never contradict each other.
    """
    lam = -math.log(max(cs_prob, 1e-6))
    total, term = 0.0, math.exp(-lam)
    for k in range(0, 15):
        if k:
            term *= lam / k
        total += (k // GOALS_PER_CONCEDE_PENALTY) * term
    return total


def _poisson_at_least(k, lam):
    """Chance of at least k defensive actions in a match, given a per-90 rate.

    Defensive contribution is a threshold award, so what matters is how often a
    player clears the bar, not their average. A player averaging 9.5 actions is
    worth far more than one averaging 5, and far less than double one averaging
    4.75 - the average alone cannot express that, but this can.

    Poisson treats the variance as equal to the mean. Real match-by-match counts
    are more spread out than that, which means this slightly understates players
    sitting just below the threshold and overstates those just above it. Good
    enough to rank on, and it can be replaced with real per-match counts from
    event/<n>/live once a few gameweeks of 2026/27 exist.
    """
    if lam <= 0:
        return 0.0
    term, cumulative = math.exp(-lam), math.exp(-lam)
    for i in range(1, k):
        term *= lam / i
        cumulative += term
    return max(0.0, min(1.0, 1.0 - cumulative))


# A player named in the starting eleven plays this match. Not 1.0, because some
# starters come off before the final whistle - but nowhere near the season-long
# share of 38 games that minutes_share otherwise carries.
STARTER_MINUTES_SHARE = 0.92

# Ownership uplift for players with no Premier League record. Capped so a
# bandwagon cannot run away with a projection that is already a guess.
OWNERSHIP_CAP = 30.0
OWNERSHIP_UPLIFT = 0.5


def _price_prior(p):
    """Fallback for players with no Premier League history.

    Price is the game's own estimate of expected output, so it is the least-bad
    prior available. Deliberately conservative - these are flagged low-confidence
    in the output.
    """
    cost = p["cost"]
    pts90 = 1.8 + max(0, cost - 40) / 10.0 * 0.45
    # Price is the game's guess before a ball is kicked; ownership is money
    # main-game managers actually committed after seeing pre-season. A heavily
    # owned player with no Premier League record is one the market expects to
    # return, and price alone missed that - it marked Tzolis (24.7% owned,
    # confirmed Arsenal starter) as an unknown and left him out of GW1.
    # This factor is a judgement, not a measured one. Re-fit it against real
    # returns once a few gameweeks of 2026/27 exist.
    owned = min(p.get("owned") or 0.0, OWNERSHIP_CAP)
    pts90 *= 1.0 + owned / OWNERSHIP_CAP * OWNERSHIP_UPLIFT
    pos = p["position"]
    if pos == 4:
        goals, assists = pts90 * 0.075, pts90 * 0.045
    elif pos == 3:
        goals, assists = pts90 * 0.055, pts90 * 0.055
    elif pos == 2:
        goals, assists = pts90 * 0.020, pts90 * 0.030
    else:
        goals, assists = 0.0, 0.0
    return {
        "goals": goals,
        "assists": assists,
        "saves": 3.0 if pos == 1 else 0.0,
        "bonus": pts90 * 0.10,
        "dc": DC_PRIOR.get(pos, 0.0),
        "minutes_share": 0.55,
        "low_confidence": True,
    }


def _availability(p):
    """0-1 multiplier for injury / suspension / rotation risk."""
    status = p.get("status")
    if status in ("i", "s", "u", "n"):  # injured, suspended, unavailable, not in squad
        return 0.0
    chance = p.get("chance_next")
    if chance is not None:
        return chance / 100.0
    if status == "d":  # doubtful with no percentage given
        return 0.5
    return 1.0


def project(data, scoring, starters=None):
    """Expected Challenge points this gameweek, per player."""
    # The confirmed eleven is read here as well as in _demote_squad_players.
    # That function only ever pushes players down; this pushes named starters
    # up, which is the half that was missing.
    xi = set()
    for codes in ((starters or {}).get("expected_xi") or {}).values():
        xi.update(int(c) for c in codes)

    fixtures = data["fixtures"]
    by_team = {}
    for f in fixtures:
        by_team.setdefault(f["home"], []).append((f, True))
        by_team.setdefault(f["away"], []).append((f, False))

    goal_pts = {POS_ID[k]: v for k, v in scoring["goals_scored"].items()}
    cs_pts = {POS_ID[k]: v for k, v in scoring["clean_sheets"].items()}
    assist_pts = scoring.get("assists", 3)
    save_pts = scoring.get("saves", 1)
    appearance = scoring.get("long_play", 2)
    dc_pts = {POS_ID[k]: v for k, v in (scoring.get("defensive_contribution") or {}).items()}
    concede_pts = {POS_ID[k]: v for k, v in (scoring.get("goals_conceded") or {}).items()}

    out = []
    for p in data["players"]:
        avail = _availability(p)
        rates = _rates_from_history(p)
        low_conf = False
        if rates is None:
            rates = _price_prior(p)
            low_conf = True

        # A named starter plays this match, whatever his record says about how
        # often he played across a whole season. minutes_share is a share of 38
        # games, so a player who spent last year on the bench drags that share
        # into a week he is confirmed to start - it cut Trafford, Leeds' number
        # one, to 27% of his projection and made him unpickable. Raise it,
        # never lower it: being named in the eleven is evidence he plays, never
        # evidence he plays less than his history suggests.
        confirmed_starter = p["code"] in xi
        if confirmed_starter:
            rates = dict(rates)
            rates["minutes_share"] = max(
                rates["minutes_share"], STARTER_MINUTES_SHARE
            )

        games = by_team.get(p["team_id"], [])
        total, notes = 0.0, []
        for f, is_home in games:
            diff = f["home_diff"] if is_home else f["away_diff"]
            atk = ATTACK_BY_DIFF.get(diff, 1.0) * (HOME_ATTACK_BOOST if is_home else 1.0)
            cs_prob = clean_sheet_prob(diff)

            pos = p["position"]
            pts = appearance
            pts += rates["goals"] * atk * goal_pts.get(pos, 0)
            pts += rates["assists"] * atk * assist_pts
            pts += cs_prob * cs_pts.get(pos, 0)
            if pos == 1:
                pts += rates["saves"] * save_pts
            # Goals conceded hits keepers and defenders alike. Previously only
            # keepers carried it, and as a flat guess rather than the real
            # per-match rounding.
            if concede_pts.get(pos):
                pts += _expected_concede_units(cs_prob) * concede_pts[pos]
            # Defensive contribution: chance of clearing the threshold this
            # match, times the points for doing so. Worth 2 every week, and the
            # game can raise it - "The Shield" in GW5 makes it 10.
            threshold = DC_THRESHOLD.get(pos)
            if threshold and dc_pts.get(pos):
                dc_rate = rates.get("dc")
                if dc_rate is None:
                    dc_rate = DC_PRIOR.get(pos, 0.0)
                pts += _poisson_at_least(threshold, dc_rate) * dc_pts[pos]
            pts += rates["bonus"]
            total += pts * rates["minutes_share"]

        exp = total * avail
        out.append(
            {
                **p,
                "expected": round(exp, 3),
                "base_expected": round(exp, 3),
                "multiplier": 1.0,
                "low_confidence": low_conf,
                "n_fixtures": len(games),
                "available": avail,
                "backup_keeper": False,
                "depth_rank": None,
                "confirmed_starter": confirmed_starter,
            }
        )

    _demote_squad_players(out, starters)
    return out


def load_starters():
    """Confirmed starting elevens, if someone has reviewed them."""
    path = os.path.join(DATA, "starters.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return None


# A player who moved club within this many days has a prior-season minutes
# record that describes a different job at a different club.
RECENT_MOVE_DAYS = 400

# Below this ownership, the market is saying it does not expect a starter.
UNRATED_OWNERSHIP = 2.0


def _demote_squad_players(players, starters=None):
    """Scale down players whose minutes history describes a job they no longer have.

    Two different problems, handled two different ways.

    Goalkeepers are structural: exactly one plays, so every keeper below first
    choice at his club is cut hard, to 12%. First choice is decided on price,
    with ownership only breaking ties. Ownership alone gets this badly wrong,
    because main-game managers buy the cheapest available keeper as bench fodder
    precisely so they never have to play him - so the most-owned keeper at a club
    is often the £4.0m reserve. That pattern picked Dubravka over Kinsky at
    Tottenham, and the same mistake at Coventry, Hull and Ipswich. Price does not
    have that flaw: the game prices keepers on expected returns, and price alone
    identifies the right starter at all twenty clubs.

    Outfield players are not structural, and ranking them by ownership does not
    work. Ownership tracks attacking returns rather than starts, so holding
    midfielders and defensive full-backs who play every single week sit near the
    bottom of their club's ownership table. Ranking that way demoted Odegaard,
    Rodri, Zubimendi and Mac Allister, which is plainly wrong.

    So outfield demotion targets only the case that actually misleads the model:
    a player who has recently changed club, carries a big minutes record from
    somewhere else, and whom the market does not rate as a starter at the new
    club. Everyone settled at their club keeps their projection, however
    unfashionable they are.
    """
    today = datetime.now(timezone.utc)

    # An explicit starting eleven beats every heuristic below, so when one
    # exists it is used on its own and the guesswork is skipped entirely.
    xi = set()
    for codes in ((starters or {}).get("expected_xi") or {}).values():
        xi.update(int(c) for c in codes)
    if xi:
        for p in players:
            if p["code"] in xi:
                continue
            is_keeper = p["position"] == POS_ID["GKP"]
            p["expected"] = round(p["expected"] * (0.12 if is_keeper else 0.30), 3)
            p["not_expected_to_start"] = True
            if is_keeper:
                p["backup_keeper"] = True
        return

    keepers_by_team = {}
    for p in players:
        if p["position"] == POS_ID["GKP"]:
            keepers_by_team.setdefault(p["team_id"], []).append(p)

    for keepers in keepers_by_team.values():
        # Most expensive first; ownership breaks ties between equally priced
        # keepers, which is what separates Kinsky from Vicario at Tottenham.
        keepers.sort(key=lambda p: (-p["cost"], -(p.get("owned") or 0.0)))
        for rank, p in enumerate(keepers):
            if rank == 0:
                continue
            p["expected"] = round(p["expected"] * 0.12, 3)
            p["backup_keeper"] = True
            p["depth_rank"] = rank + 1

    for p in players:
        if p["position"] == POS_ID["GKP"]:
            continue
        # No history means the projection already rests on a cautious price
        # prior, so there is nothing inflated to correct.
        if p["low_confidence"]:
            continue
        if (p.get("owned") or 0.0) >= UNRATED_OWNERSHIP:
            continue
        if not _moved_recently(p, today):
            continue
        p["expected"] = round(p["expected"] * 0.5, 3)
        p["rotation_risk"] = True


def _moved_recently(p, today):
    joined = p.get("team_join_date")
    if not joined:
        return False
    try:
        d = datetime.strptime(joined, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return (today - d).days <= RECENT_MOVE_DAYS


# ---------------------------------------------------------------- the twist


def apply_twist(players, twist, team_ids):
    """Apply the weekly rule change.

    The twist file is deliberately declarative rather than executable code, so a
    bad week's parse cannot do anything worse than pick a poor team.

    Supported keys:
      player_multipliers: [{factor, teams?, team_join_date_after?,
                            positions?, player_codes?}]
      scoring_multipliers: {goals_scored: 2, assists: 2, ...}  (approximate)
    """
    if not twist:
        return players

    # Scoring multipliers are applied approximately, by scaling the share of a
    # player's projection that comes from the affected stat.
    # Prefer scoring_overrides in the twist file, which replaces a scoring value
    # outright and is exact. scoring_multipliers below is the rough fallback for
    # stats the projection does not model directly.
    sm = twist.get("scoring_multipliers") or {}
    if sm:
        share = {"goals_scored": 0.42, "assists": 0.22, "clean_sheets": 0.20,
                 "saves": 0.25, "bonus": 0.10}
        boost = 1.0
        for stat, mult in sm.items():
            boost += share.get(stat, 0.1) * (mult - 1)
        for p in players:
            p["expected"] = round(p["expected"] * boost, 3)
            p["multiplier"] *= boost

    for rule in twist.get("player_multipliers") or []:
        factor = rule.get("factor", 1.0)
        teams = set(rule.get("teams") or [])
        after = rule.get("team_join_date_after")
        positions = set(rule.get("positions") or [])
        codes = set(rule.get("player_codes") or [])
        for p in players:
            if teams and p["team"] not in teams:
                continue
            if after and not (p.get("team_join_date") or "") >= after:
                continue
            if positions and POS_NAME[p["position"]] not in positions:
                continue
            if codes and p["code"] not in codes:
                continue
            p["expected"] = round(p["expected"] * factor, 3)
            p["multiplier"] *= factor
    return players


# ---------------------------------------------------------------- constraints


def constraints(data, twist=None):
    """Merge base rules with this gameweek's overrides.

    One trap here. The game's formation picker lists outfield shapes only -
    2-2-1, 3-1-1 and so on - and those sum to squad size minus one, because a
    goalkeeper is always required and is not part of the formation. The API's
    element_types override mirrors the picker, so it lists DEF/MID/FWD and omits
    GKP entirely. Taken literally that reads as "no goalkeeper allowed", which
    produces an illegal team. The keeper has to be added back.
    """
    base = dict(data["base_rules"])
    ov = data["event"].get("overrides") or {}
    base.update({k: v for k, v in (ov.get("rules") or {}).items() if v is not None})

    ets = ov.get("element_types")
    if ets:
        allowed = {
            t["id"]: (t.get("squad_min_select") or 0, t.get("squad_max_select") or 0)
            for t in ets
        }
    else:
        # No override - fall back to the standard 15-player shape.
        allowed = {1: (2, 2), 2: (5, 5), 3: (5, 5), 4: (3, 3)}

    if POS_ID["GKP"] not in allowed:
        allowed[POS_ID["GKP"]] = (1, 1)

    # Escape hatch: if a gameweek ever really does ban keepers, or changes the
    # position bounds in a way the API does not express, the twist file can say
    # so - e.g. "positions": {"GKP": [0, 0]}.
    for name, bounds in ((twist or {}).get("positions") or {}).items():
        allowed[POS_ID[name]] = tuple(bounds)
    allowed = {k: v for k, v in allowed.items() if v[1] > 0}

    return {
        "size": base.get("squad_squadsize", 15),
        "budget": base.get("squad_total_spend", 1000),
        "club_limit": base.get("squad_team_limit", 3),
        "positions": allowed,
    }


# ---------------------------------------------------------------- optimiser


def solve(players, con, forced=(), banned=(), captain_bonus=True):
    """Best legal squad. Returns (true_score, picks, captain_code) or None.

    Budget is handled by charging a penalty per unit of cost rather than by
    tracking spend as a dimension of the dynamic program. Tracking spend makes
    the state space explode; charging for it keeps every solve fast. The penalty
    is raised until the squad comes in under budget.

    Every Challenge gameweek so far has run with an unlimited budget, in which
    case the penalty is zero and the result is exactly optimal.
    """
    pool = [p for p in players if p["code"] not in set(banned)]
    pool = [p for p in pool if p["position"] in con["positions"]]
    if not pool:
        return None

    if not _budget_binds(pool, con):
        return _best_captained(pool, con, forced, 0.0, captain_bonus)

    # Raise the cost penalty until the squad fits. Higher penalty -> cheaper
    # squad, so the smallest penalty that fits is the one that gives up least.
    lo, hi, best = 0.0, 1.0, None
    for _ in range(20):
        r = _best_captained(pool, con, forced, hi, captain_bonus)
        if r and _cost(r[1]) <= con["budget"]:
            break
        hi *= 2
    else:
        return None

    for _ in range(14):
        mid = (lo + hi) / 2
        r = _best_captained(pool, con, forced, mid, captain_bonus)
        if r and _cost(r[1]) <= con["budget"]:
            best, hi = r, mid
            # Once the squad spends nearly the whole budget there is nothing
            # left to gain from a finer penalty, so stop early.
            if con["budget"] - _cost(r[1]) <= 2:
                break
        else:
            lo = mid
    return best


def _cost(picks):
    return sum(p["cost"] for p in picks)


def _budget_binds(pool, con):
    """True if the budget could actually stop us picking the best players."""
    costs = sorted((p["cost"] for p in pool), reverse=True)[: con["size"]]
    return con["budget"] < sum(costs)


def _best_captained(pool, con, forced, penalty, captain_bonus=True):
    """Best squad with one player captained, found by branch and bound.

    Captaincy doubles one player, which the dynamic program cannot track (it
    would need the max over the chosen set). So each candidate captain is forced
    in turn. Forcing a captain can only ever score the uncaptained best plus that
    captain's own value, so once that ceiling drops below the best squad already
    found, no remaining candidate can win and the search stops. Usually only a
    handful of candidates are tried.
    """
    base = _solve_once(pool, con, forced, penalty=penalty)
    if not base:
        return None
    if not captain_bonus:
        return (_true_score(base[1], None), base[1], None)

    best, best_adj = None, float("-inf")
    for c in sorted(pool, key=lambda p: -_val(p, penalty)):
        ceiling = base[0] + _val(c, penalty)
        if best is not None and ceiling <= best_adj:
            break
        r = _solve_once(pool, con, list(forced) + [c["code"]], c["code"], penalty)
        if r and r[0] > best_adj:
            best_adj, best = r[0], (_true_score(r[1], c["code"]), r[1], c["code"])
    return best


def _val(p, penalty):
    return p["expected"] - penalty * p["cost"]


def _true_score(picks, captain_code):
    """Score in real points, with the cost penalty removed."""
    return sum(
        p["expected"] * (2 if p["code"] == captain_code else 1) for p in picks
    )


def _solve_once(pool, con, forced=(), captain_code=None, penalty=0.0):
    """Exact best squad for a given cost penalty, by dynamic program over clubs.

    Club limit is the only constraint that couples positions, so processing one
    club at a time with position counts as the state covers it exactly.
    """
    forced = set(forced)
    size, climit = con["size"], con["club_limit"]
    pmin = {k: v[0] for k, v in con["positions"].items()}
    pmax = {k: v[1] for k, v in con["positions"].items()}
    positions = sorted(con["positions"])

    forced_players = [p for p in pool if p["code"] in forced]
    if len(forced_players) != len(forced):
        return None

    seed_counts = {q: 0 for q in positions}
    seed_score, seed_clubs = 0.0, {}
    for p in forced_players:
        seed_counts[p["position"]] += 1
        seed_score += _val(p, penalty) * (2 if p["code"] == captain_code else 1)
        seed_clubs[p["team"]] = seed_clubs.get(p["team"], 0) + 1
        if seed_counts[p["position"]] > pmax[p["position"]] or seed_clubs[p["team"]] > climit:
            return None

    by_club = {}
    for p in pool:
        if p["code"] not in forced:
            by_club.setdefault(p["team"], []).append(p)

    # state: position-count tuple -> (adjusted score, picked codes)
    states = {
        tuple(seed_counts[q] for q in positions):
            (seed_score, tuple(p["code"] for p in forced_players))
    }

    for club, members in by_club.items():
        room = climit - seed_clubs.get(club, 0)
        if room <= 0:
            continue
        options = _club_options(members, positions, pmax, room, penalty)
        nxt = dict(states)
        for counts, (score, picks) in states.items():
            for dc, dscore, dpicks in options:
                nc = tuple(counts[i] + dc[i] for i in range(len(positions)))
                if sum(nc) > size:
                    continue
                if any(nc[i] > pmax[positions[i]] for i in range(len(positions))):
                    continue
                val = score + dscore
                cur = nxt.get(nc)
                if cur is None or val > cur[0]:
                    nxt[nc] = (val, picks + dpicks)
        states = nxt

    best = None
    for counts, (score, picks) in states.items():
        if sum(counts) != size:
            continue
        if any(counts[i] < pmin[positions[i]] for i in range(len(positions))):
            continue
        if best is None or score > best[0]:
            best = (score, picks)
    if not best:
        return None

    by_code = {p["code"]: p for p in pool}
    return (best[0], [by_code[c] for c in best[1]], captain_code)


def _club_options(members, positions, pmax, room, penalty):
    """Every way to take 0..room players from one club, as position-count deltas.

    For a fixed number of players from one club, taking the highest-valued is
    optimal: the club limit constrains them all identically, and cost is already
    priced into the value, so nothing else distinguishes them.
    """
    ranked = {
        q: sorted([m for m in members if m["position"] == q],
                  key=lambda m: -_val(m, penalty))[: pmax[q]]
        for q in positions
    }
    options = []

    def rec(i, counts, score, picks, taken):
        if i == len(positions):
            options.append((tuple(counts), score, picks))
            return
        q = positions[i]
        avail = ranked[q]
        for take in range(0, min(len(avail), pmax[q], room - taken) + 1):
            chosen = avail[:take]
            rec(
                i + 1,
                counts + [take],
                score + sum(_val(c, penalty) for c in chosen),
                picks + tuple(c["code"] for c in chosen),
                taken + take,
            )

    rec(0, [], 0.0, (), 0)
    return options


# ---------------------------------------------------------------- reasoning


def _ordinal(n):
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def reason(p, con, twist_name):
    """One line explaining why this player is in the squad."""
    bits = []
    if p["multiplier"] > 1.01:
        bits.append(f"{p['multiplier']:.0f}x from the {twist_name} rule")
    if p["n_fixtures"] > 1:
        bits.append(f"{p['n_fixtures']} fixtures")
    if p.get("backup_keeper"):
        bits.append("likely backup keeper")
    elif p.get("depth_rank"):
        bits.append(f"{_ordinal(p['depth_rank'])} choice at his club, may not start")
    if p["low_confidence"]:
        bits.append("no PL history, price-based estimate")
    if p.get("available", 1) < 1:
        bits.append(f"{int(p['available'] * 100)}% fit")
    if p.get("penalties_order") == 1:
        bits.append("on penalties")
    core = f"{p['expected']:.1f} projected"
    return core + (" - " + ", ".join(bits) if bits else "")


# ---------------------------------------------------------------- entrypoint


def load():
    with open(os.path.join(DATA, "players.json"), encoding="utf-8") as f:
        return json.load(f)


def load_twist(event_id):
    path = os.path.join(DATA, "twists", f"gw{event_id}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return None


# players.json used to be rebuilt immediately before every solve, so how old it
# was never mattered. Once it is committed and read on a machine that cannot
# reach premierleague.com to refresh it, age becomes the main way this gives a
# confident wrong answer: prices, injuries and the gameweek itself all move
# underneath a file that still looks perfectly valid.
STALE_AFTER_HOURS = 12


def check_freshness(data, allow_stale=False):
    """Warn if players.json is old, refuse outright if its gameweek has finished."""
    ev = data["event"]
    fixtures = data.get("fixtures") or []

    # Every match played means this file describes a gameweek that is over. The
    # solver would still happily return a squad for it, which is the one failure
    # worth making fatal.
    if fixtures and all(f.get("finished") for f in fixtures):
        print(
            f"{ev['name']} has finished - every fixture in players.json is played.\n"
            f"Refresh with `python scripts/fpl_data.py` (needs network access), "
            f"or pass --stale-ok to solve for it anyway.",
            file=sys.stderr,
        )
        if not allow_stale:
            sys.exit(1)
        return

    try:
        built = datetime.strptime(data["generated"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except (KeyError, ValueError):
        return
    hours = (datetime.now(timezone.utc) - built).total_seconds() / 3600
    if hours >= STALE_AFTER_HOURS:
        print(
            f"warning: players.json is {hours:.0f} hours old (built "
            f"{data['generated']}). Prices, injuries and expected lineups have "
            f"probably moved since. Run `python scripts/fpl_data.py` to refresh.\n",
            file=sys.stderr,
        )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--alternatives", type=int, default=3)
    ap.add_argument("--include-started", action="store_true",
                    help="include players whose match has already kicked off "
                         "(they are normally locked and cannot be added)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--stale-ok", action="store_true",
                    help="solve even if players.json is for a finished gameweek")
    args = ap.parse_args()

    data = load()
    check_freshness(data, allow_stale=args.stale_ok)
    ev = data["event"]
    twist = load_twist(ev["id"])
    con = constraints(data, twist)

    scoring = dict(data["base_scoring"])
    for k, v in ((ev.get("overrides") or {}).get("scoring") or {}).items():
        scoring[k] = v
    # The twist can restate a scoring value outright, which is how "The Shield"
    # in GW5 turns defensive contribution from 2 points into 10.
    for k, v in ((twist or {}).get("scoring_overrides") or {}).items():
        scoring[k] = v

    starters = load_starters()
    if starters:
        n = sum(len(v) for v in (starters.get("expected_xi") or {}).values())
        print(f"using reviewed starting elevens ({n} players)\n")
    players = project(data, scoring, starters)
    players = apply_twist(players, twist, data["teams"])

    # Players lock at kickoff, so anyone whose match has started cannot be added.
    if not args.include_started:
        started = {f["home"] for f in data["fixtures"] if f.get("started")}
        started |= {f["away"] for f in data["fixtures"] if f.get("started")}
        locked = [p for p in players if p["team_id"] in started]
        players = [p for p in players if p["team_id"] not in started]
        if locked:
            print(f"note: {len(locked)} players excluded - match already kicked off\n")

    players = [p for p in players if p["available"] > 0 and p["n_fixtures"] > 0]

    best = solve(players, con)
    if not best:
        print("No legal squad found. Check the gameweek rules.", file=sys.stderr)
        sys.exit(1)

    twist_name = (twist or {}).get("name", "gameweek")
    # Ban the highest-projected pick from each squad in turn, so alternatives
    # differ in shape rather than swapping one marginal player.
    results = [best]
    banned = []
    for _ in range(args.alternatives):
        banned.append(max(results[-1][1], key=lambda p: p["expected"])["code"])
        alt = solve(players, con, banned=banned)
        if not alt:
            break
        results.append(alt)

    if args.json:
        print(json.dumps(
            [
                {
                    "score": r[0],
                    "captain": r[2],
                    "picks": [
                        {k: p[k] for k in
                         ("code", "name", "team", "position", "cost", "expected",
                          "multiplier", "low_confidence")}
                        for p in r[1]
                    ],
                }
                for r in results
            ], indent=1))
        return

    print(f"{ev['name']} - {twist_name}")
    print(f"Rules: {con['size']} players, "
          f"{'no budget limit' if con['budget'] >= 9999 else f'budget {con['budget']/10:.1f}m'}, "
          f"max {con['club_limit']} per club, "
          f"positions " + ", ".join(
              f"{POS_NAME[q]} {a}-{b}" for q, (a, b) in sorted(con["positions"].items())))
    print()

    for idx, (score, picks, cap) in enumerate(results):
        label = "RECOMMENDED SQUAD" if idx == 0 else f"Alternative {idx}"
        counts = {q: sum(1 for p in picks if p["position"] == q) for q in POS_NAME}
        # Formation is outfield only, matching the game's own picker.
        shape = f"{counts[2]}-{counts[3]}-{counts[4]}"
        print(f"{label}  -  formation {shape}  -  projected {score:.1f} pts")
        for p in sorted(picks, key=lambda x: (x["position"], -x["expected"])):
            c = " (C)" if p["code"] == cap else ""
            # Ownership is shown on every pick because it is the quickest way to
            # spot a player the model thinks will start and the market does not.
            own = p.get("owned")
            own_s = f", {own:.1f}% owned" if own is not None else ""
            print(f"  {POS_NAME[p['position']]} {p['name']}{c} ({p['team']}, "
                  f"{p['cost']/10:.1f}m{own_s})")
            print(f"      {reason(p, con, twist_name)}")
        print()


if __name__ == "__main__":
    main()
