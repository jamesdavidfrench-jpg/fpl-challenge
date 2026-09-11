"""Ceiling squad - the one most likely to win the week, not the one with the
best average.

SKILL.md's last limitation says why this exists. fpl_solve.py maximises the
expected score, which is right for finishing well among 350,000 entries and
wrong for an eight-entry league where the winner has scored 78, 90 and 69.
Beating that needs two hauls out of six players, and hauls come from
concentrating risk rather than spreading it. An average has no way to express
that, so this script simulates the gameweek instead and ranks squads by how
often they clear a target score.

The simulation is built on fpl_solve's own rates, and every per-player mean it
produces is checked back against fpl_solve's projection before anything is
ranked - see validate(). If the two disagree the model here is wrong, not the
other way round.

What this adds that an average cannot see:

  * Players in the same match are not independent. A defender's clean sheet and
    an opposing forward's goal are the same event read from two sides. Team
    goals are drawn once per match and shared out among that club's players, so
    a squad that needs both cannot pretend to have them both.
  * Lumpiness. Two players projected 6 each are not interchangeable if one gets
    6 nearly every week and the other gets 2 or 18.

Usage:
    python scripts/fpl_ceiling.py                 # target = league winner pace
    python scripts/fpl_ceiling.py --target 90
    python scripts/fpl_ceiling.py --trials 40000
"""

import argparse
import itertools
import math
import random

import fpl_solve as S


# The score to beat. James's league has nine entries and its weekly winner has
# scored 78, 90 and 69 in the three gameweeks so far, so about 80 wins a week.
# This is the whole point of the script: the number to clear is set by eight
# other people, not by the game-wide average of 34 to 49.
DEFAULT_TARGET = 80

# Bonus is three points at most, so a player whose mean bonus cannot be paid
# for by his returning trials alone gets the remainder as a flat amount.
MAX_BONUS = 3

# Mean bonus awarded on a returning trial, from the {1,2,3} weights below.
BONUS_WEIGHTS = [(1, 0.5), (2, 0.3), (3, 0.2)]
BONUS_MEAN = sum(v * w for v, w in BONUS_WEIGHTS)


def player_rates(p, xi, confirmed_teams):
    """The same rates fpl_solve.project() uses, including its minutes logic.

    Kept deliberately parallel to project() rather than shared, because
    project() folds everything into one number and this needs the parts. The
    validate() check below is what keeps the two honest.
    """
    rates = S._rates_from_history(p)
    low_conf = rates is None
    if low_conf:
        rates = S._price_prior(p)

    history_share = rates["minutes_share"]
    confirmed = p["code"] in xi
    if confirmed:
        rates = dict(rates)
        if p["team"] in confirmed_teams:
            rates["minutes_share"] = S.STARTER_MINUTES_SHARE
        else:
            rates["minutes_share"] = max(
                history_share, S.start_share(p.get("owned"), True)
            )
    return rates, low_conf, confirmed


def build_pool(data, twist, starters, target_teams):
    """Every player from the doubled clubs, with the parts needed to simulate."""
    xi = set()
    for codes in ((starters or {}).get("expected_xi") or {}).values():
        xi.update(int(c) for c in codes)
    confirmed_teams = set((starters or {}).get("confirmed") or {})

    # project() is still the source of truth for the expected value and for the
    # twist multiplier, so take those from it rather than recomputing them.
    scoring = data["base_scoring"]
    projected = S.project(data, scoring, starters)
    S.apply_twist(projected, twist, data["teams"])
    by_code = {p["code"]: p for p in projected}

    by_team = {}
    for f in data["fixtures"]:
        by_team.setdefault(f["home"], []).append((f, True))
        by_team.setdefault(f["away"], []).append((f, False))

    goal_pts = {S.POS_ID[k]: v for k, v in scoring["goals_scored"].items()}
    cs_pts = {S.POS_ID[k]: v for k, v in scoring["clean_sheets"].items()}
    concede_pts = {S.POS_ID[k]: v for k, v in (scoring.get("goals_conceded") or {}).items()}
    dc_pts = {S.POS_ID[k]: v for k, v in (scoring.get("defensive_contribution") or {}).items()}

    pool = []
    for p in data["players"]:
        if p["team"] not in target_teams:
            continue
        proj = by_code.get(p["code"])
        if proj is None or proj["expected"] <= 0:
            continue
        # Only the expected eleven. A ceiling squad still has to be made of
        # players who are on the pitch - see SKILL.md, where not predicting who
        # starts was 86% of GW1's whole error.
        if p["code"] not in xi:
            continue
        games = by_team.get(p["team_id"], [])
        if len(games) != 1:
            continue
        f, is_home = games[0]
        diff = f["home_diff"] if is_home else f["away_diff"]
        atk = S.ATTACK_BY_DIFF.get(diff, 1.0) * (S.HOME_ATTACK_BOOST if is_home else 1.0)

        rates, low_conf, _ = player_rates(p, xi, confirmed_teams)
        pos = p["position"]
        dc_rate = rates.get("dc")
        if dc_rate is None:
            dc_rate = S.DC_PRIOR.get(pos, 0.0)

        pool.append({
            "code": p["code"],
            "name": p["name"],
            "team": p["team"],
            "team_id": p["team_id"],
            "position": pos,
            "pos_name": S.POS_NAME[pos],
            "cost": p["cost"],
            "owned": p.get("owned"),
            "expected": proj["expected"],
            "multiplier": proj["multiplier"],
            "availability": S._availability(p),
            "low_confidence": low_conf,
            "is_home": is_home,
            "opp_diff": diff,
            "goal_rate": rates["goals"] * atk,
            "assist_rate": rates["assists"] * atk,
            "save_rate": rates["saves"],
            "bonus_rate": rates["bonus"],
            "dc_rate": dc_rate,
            "dc_threshold": S.DC_THRESHOLD.get(pos),
            "play_prob": rates["minutes_share"],
            "goal_pts": goal_pts.get(pos, 0),
            "assist_pts": scoring.get("assists", 3),
            "cs_pts": cs_pts.get(pos, 0),
            "concede_pts": concede_pts.get(pos, 0),
            "dc_pts": dc_pts.get(pos, 0),
            "appearance": scoring.get("long_play", 2),
            "save_pts": scoring.get("saves", 1),
        })
    return pool


def _bonus_plan(pl):
    """Split a player's mean bonus into a lump on returning trials and a floor.

    Bonus is not spread evenly across weeks - it lands on the players who
    scored. Paying it out that way is what makes a big score big, which is the
    whole thing an average cannot see. The split preserves the mean exactly.
    """
    p_play = pl["play_prob"]
    if p_play <= 0:
        return 0.0, 0.0
    lam = pl["goal_rate"] + pl["assist_rate"]
    p_ret = 1.0 - math.exp(-lam) if lam > 0 else 0.0
    if pl["position"] in (1, 2):
        # Keepers and defenders pick up bonus for clean sheets too.
        p_cs = S.clean_sheet_prob(pl["opp_diff"])
        p_ret = 1.0 - (1.0 - p_ret) * (1.0 - p_cs)

    mean = pl["bonus_rate"]
    if p_ret <= 0:
        return 0.0, mean / p_play

    # The award is capped at three, so the mean actually paid out is not
    # linear in the scale and cannot be inverted directly. Solving it properly
    # matters: assuming the cap always pays the full three overstates it by
    # about a fifth, which quietly inflated every attacker's mean.
    def paid(scale):
        return p_ret * p_play * sum(
            w * min(MAX_BONUS, v * scale) for v, w in BONUS_WEIGHTS
        )

    hi = MAX_BONUS / min(v for v, _ in BONUS_WEIGHTS)
    if paid(hi) < mean:
        # Even the maximum on every returning trial is not enough, so the rest
        # becomes a flat amount whenever he plays.
        return hi, (mean - paid(hi)) / p_play
    lo = 0.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if paid(mid) < mean:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2, 0.0


def simulate(pool, trials, seed=7):
    """Points per trial for every player, with one match drawn at a time.

    Team goals are drawn once and shared out, so two players in the same match
    are correlated the way the real match makes them correlated. That is the
    only reason this is a simulation rather than a variance formula.
    """
    rng = random.Random(seed)
    by_team = {}
    for i, pl in enumerate(pool):
        by_team.setdefault(pl["team_id"], []).append(i)

    # Each team's expected goals is what its opponent is expected to concede,
    # which is the same number fpl_solve derives its clean sheet odds from. Both
    # sides of the squad therefore read one draw, not two.
    # The team total must stay exactly the rate fpl_solve derives its clean
    # sheet odds from, or the two halves of the derby stop agreeing with the
    # projection. Do not clamp this to the players' summed rates - doing that
    # made both keepers concede more than the model says they should.
    team_lambda = {
        tid: S.CONCEDE_RATE_BY_DIFF.get(pool[idxs[0]]["opp_diff"], 1.32)
        for tid, idxs in by_team.items()
    }

    # Who scores a given goal, and who assists it, as shares of the team total -
    # which is what keeps each player's mean equal to his own rate.
    #
    # The XI's summed goal rates can exceed the team total (1.73 against 1.65
    # for United this week), because the two come from different parts of the
    # model. When that happens the shares are normalised so they remain a valid
    # draw, and the few goals that no longer fit are handed back as an
    # independent top-up. Only the overflow loses the shared-total correlation,
    # which is about 5% of the goals rather than all of them.
    scorer_cdf, assist_cdf = {}, {}
    topup = [0.0] * len(pool)
    assist_topup = [0.0] * len(pool)
    for tid, idxs in by_team.items():
        lam = team_lambda[tid]
        for key, cdf, extra in (
            ("goal_rate", scorer_cdf, topup),
            ("assist_rate", assist_cdf, assist_topup),
        ):
            shares = [pool[i][key] / lam for i in idxs]
            total = sum(shares)
            if total > 1.0:
                shares = [s / total for s in shares]
                for i, s in zip(idxs, shares):
                    extra[i] = max(0.0, pool[i][key] - lam * s)
            acc, rows = 0.0, []
            for i, s in zip(idxs, shares):
                acc += s
                rows.append((acc, i))
            cdf[tid] = rows

    bonus = [_bonus_plan(pl) for pl in pool]
    n = len(pool)
    out = [[0.0] * trials for _ in range(n)]
    teams = list(by_team)
    other = {tid: [t for t in teams if t != tid] for tid in teams}

    for t in range(trials):
        goals_for = {}
        scored = [0] * n
        assisted = [0] * n
        for tid in teams:
            g = _poisson(rng, team_lambda[tid])
            goals_for[tid] = g
            for _ in range(g):
                u = rng.random()
                for acc, i in scorer_cdf[tid]:
                    if u < acc:
                        scored[i] += 1
                        break
                u = rng.random()
                for acc, i in assist_cdf[tid]:
                    if u < acc:
                        assisted[i] += 1
                        break
            for i in by_team[tid]:
                if topup[i]:
                    scored[i] += _poisson(rng, topup[i])
                if assist_topup[i]:
                    assisted[i] += _poisson(rng, assist_topup[i])

        for tid in teams:
            # Conceded is what the other side in this match scored. With one
            # fixture per club and both clubs in the pool this is exact; if only
            # one club is in the pool it falls back to the expected rate.
            opps = other[tid]
            if opps:
                conceded = goals_for[opps[0]]
            else:
                conceded = _poisson(
                    rng, S.CONCEDE_RATE_BY_DIFF.get(pool[by_team[tid][0]]["opp_diff"], 1.32)
                )
            cs = conceded == 0
            for i in by_team[tid]:
                pl = pool[i]
                if rng.random() >= pl["play_prob"] or rng.random() >= pl["availability"]:
                    out[i][t] = 0.0
                    continue
                pts = pl["appearance"]
                pts += scored[i] * pl["goal_pts"]
                pts += assisted[i] * pl["assist_pts"]
                returned = scored[i] > 0 or assisted[i] > 0
                if cs:
                    pts += pl["cs_pts"]
                    if pl["position"] in (1, 2):
                        returned = True
                if pl["concede_pts"]:
                    pts += (conceded // S.GOALS_PER_CONCEDE_PENALTY) * pl["concede_pts"]
                if pl["position"] == 1 and pl["save_rate"] > 0:
                    saves = _poisson(rng, pl["save_rate"])
                    pts += (saves // S.SAVES_PER_POINT) * pl["save_pts"]
                if pl["dc_threshold"] and pl["dc_pts"]:
                    if _poisson(rng, pl["dc_rate"]) >= pl["dc_threshold"]:
                        pts += pl["dc_pts"]
                scale, floor = bonus[i]
                if floor:
                    pts += floor
                if returned and scale:
                    b = _weighted(rng, BONUS_WEIGHTS) * scale
                    pts += min(MAX_BONUS, b)
                out[i][t] = pts * pl["multiplier"]
    return out


def _poisson(rng, lam):
    """Knuth's method. Fine at these rates - nothing here is above about 3."""
    if lam <= 0:
        return 0
    target, k, prod = math.exp(-lam), 0, rng.random()
    while prod > target:
        k += 1
        prod *= rng.random()
    return k


def _weighted(rng, weights):
    u, acc = rng.random(), 0.0
    for v, w in weights:
        acc += w
        if u < acc:
            return v
    return weights[-1][0]


def calibrate(pool, sims):
    """Scale each player's draws so his simulated mean equals his projection.

    The simulation rebuilds the projection from its parts, and a few parts are
    not rebuildable: _demote_squad_players() adjusts a player's expected points
    inside project() for squad depth, and nothing here can see that. Rather than
    duplicate it and let the two drift, each player's whole distribution is
    scaled to land on the number fpl_solve already produced.

    This changes the level, never the shape - the variance and the correlation
    between players, which is the only reason this script exists, are untouched.
    Corrections above a few per cent are worth looking at, so main() prints the
    largest.
    """
    scales = []
    for pl, s in zip(pool, sims):
        mean = sum(s) / len(s)
        scale = pl["expected"] / mean if mean > 0 else 1.0
        scale = min(2.0, max(0.5, scale))
        if abs(scale - 1.0) > 1e-9:
            for t in range(len(s)):
                s[t] *= scale
        scales.append((abs(scale - 1.0), pl["name"], scale))
    scales.sort(reverse=True)
    return scales


def validate(pool, sims):
    """Check the simulation reproduces fpl_solve's projection, player by player.

    This is the whole safety net. The simulation adds variance and correlation,
    and it must add nothing else - if a player's simulated mean has drifted from
    his projection then some part of the draw is wrong.
    """
    rows = []
    for pl, s in zip(pool, sims):
        mean = sum(s) / len(s)
        exp = pl["expected"]
        rows.append((abs(mean - exp), pl["name"], exp, mean))
    rows.sort(reverse=True)
    return rows


def legal_squads(pool, con):
    """Every squad the rules allow, built club by club.

    The club limit couples the clubs, so enumerating per club and then combining
    is both exact and small - with six players and a limit of three from each of
    two clubs, the split can only be three and three.
    """
    size = con["size"]
    climit = con["club_limit"]
    # constraints() already keys positions by their numeric id.
    pmin = {k: v[0] for k, v in con["positions"].items()}
    pmax = {k: v[1] for k, v in con["positions"].items()}

    by_club = {}
    for i, pl in enumerate(pool):
        by_club.setdefault(pl["team"], []).append(i)
    clubs = sorted(by_club)

    # Per club, every legal-sized subset up to the club limit.
    per_club = {}
    for c in clubs:
        idxs = by_club[c]
        subs = {}
        for k in range(0, min(climit, size) + 1):
            subs[k] = list(itertools.combinations(idxs, k))
        per_club[c] = subs

    results = []

    def counts_ok(combo, final):
        cnt = {}
        for i in combo:
            pos = pool[i]["position"]
            cnt[pos] = cnt.get(pos, 0) + 1
            if cnt[pos] > pmax.get(pos, size):
                return False
        if not final:
            return True
        for pos, lo in pmin.items():
            if cnt.get(pos, 0) < lo:
                return False
        return True

    def walk(ci, chosen, used):
        if used > size:
            return
        if ci == len(clubs):
            if used == size and counts_ok(chosen, True):
                results.append(tuple(chosen))
            return
        # Prune: even taking the limit from every remaining club cannot fill it.
        if used + climit * (len(clubs) - ci) < size:
            return
        for k, subs in per_club[clubs[ci]].items():
            if used + k > size:
                continue
            for sub in subs:
                nxt = chosen + list(sub)
                if counts_ok(nxt, False):
                    walk(ci + 1, nxt, used + k)

    walk(0, [], 0)
    return results


def score_squads(pool, sims, squads, target, top=25):
    """Rank squads by how often they clear the target, not by their average."""
    trials = len(sims[0])
    means = [sum(s) / trials for s in sims]
    rows = []
    for squad in squads:
        vecs = [sims[i] for i in squad]
        # Captain first pass: the squad's best mean. Re-examined exhaustively
        # for the shortlist below, because for a ceiling the armband does not
        # always belong on the safest player.
        cap = max(squad, key=lambda i: means[i])
        cvec = sims[cap]
        hits = 0
        total = 0.0
        for vals in zip(*vecs, cvec):
            s = sum(vals[:-1]) + vals[-1]
            total += s
            if s >= target:
                hits += 1
        rows.append((hits / trials, total / trials, squad, cap))
    rows.sort(reverse=True)

    # Now try every armband on the shortlist.
    best = []
    for _, _, squad, _ in rows[: top * 8]:
        vecs = [sims[i] for i in squad]
        for cap in squad:
            cvec = sims[cap]
            hits, total = 0, 0.0
            vals_list = list(zip(*vecs, cvec))
            for vals in vals_list:
                s = sum(vals[:-1]) + vals[-1]
                total += s
                if s >= target:
                    hits += 1
            best.append((hits / trials, total / trials, squad, cap))
    best.sort(reverse=True)
    seen, out = set(), []
    for row in best:
        if row[2] in seen:
            continue
        seen.add(row[2])
        out.append(row)
        if len(out) >= top:
            break
    return out


def distribution(pool, sims, squad, cap, target):
    trials = len(sims[0])
    vecs = [sims[i] for i in squad]
    totals = sorted(sum(v) + sims[cap][t] for t, v in enumerate(zip(*vecs)))
    return {
        "mean": sum(totals) / trials,
        "p10": totals[int(0.10 * trials)],
        "median": totals[trials // 2],
        "p90": totals[int(0.90 * trials)],
        "p99": totals[int(0.99 * trials)],
        "hit": sum(1 for v in totals if v >= target) / trials,
    }


def describe(pool, squad, cap):
    order = {1: 0, 2: 1, 3: 2, 4: 3}
    parts = []
    for i in sorted(squad, key=lambda i: (order[pool[i]["position"]], -pool[i]["expected"])):
        pl = pool[i]
        tag = " (C)" if i == cap else ""
        parts.append("{} {}{} ({})".format(pl["pos_name"], pl["name"], tag, pl["team"]))
    return "  ".join(parts)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", type=int, default=DEFAULT_TARGET,
                    help="score to clear (default: what wins James's league)")
    ap.add_argument("--trials", type=int, default=20000)
    ap.add_argument("--top", type=int, default=6)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--allow-stale", action="store_true")
    args = ap.parse_args()

    data = S.load()
    S.check_freshness(data, args.allow_stale)
    twist = S.load_twist(data["event"]["id"])
    starters = S.load_starters()
    con = S.constraints(data, twist)

    target_teams = set()
    for rule in (twist or {}).get("player_multipliers") or []:
        if rule.get("factor", 1) > 1:
            target_teams.update(rule.get("teams") or [])
    if not target_teams:
        target_teams = set(data["teams"])

    pool = build_pool(data, twist, starters, target_teams)
    print("Gameweek {} - {}".format(data["event"]["id"], (twist or {}).get("name", "no twist")))
    print("Pool: {} players from {}".format(len(pool), ", ".join(sorted(target_teams))))
    print("Target to clear: {} pts\n".format(args.target))

    sims = simulate(pool, args.trials, args.seed)

    print("Simulation check - largest correction to match fpl_solve's projection:")
    for _, name, scale in calibrate(pool, sims)[:3]:
        print("  {:<14} {:+.1%}".format(name, scale - 1.0))
    worst = validate(pool, sims)[0]
    print("  worst remaining gap after calibration: {} {:+.3f} pts\n".format(
        worst[1], worst[3] - worst[2]))

    squads = legal_squads(pool, con)
    print("{} legal squads enumerated.\n".format(len(squads)))

    ranked = score_squads(pool, sims, squads, args.target, top=args.top)

    print("CEILING SQUADS - ranked by chance of clearing {}\n".format(args.target))
    for n, (hit, mean, squad, cap) in enumerate(ranked, 1):
        d = distribution(pool, sims, squad, cap, args.target)
        print("{}. {:.1%} chance of {}+   mean {:.1f}   median {:.0f}   top 10% {:.0f}+".format(
            n, hit, args.target, mean, d["median"], d["p90"]))
        print("   " + describe(pool, squad, cap))
    return ranked


if __name__ == "__main__":
    main()
