"""Record each week's recommendation, then score it once the gameweek finishes.

Standard library only.

Without this the picker can never improve - it would just feel confident. The
useful comparison is not projected-vs-actual (a projection is an average, so it
will always look wrong on a single week) but recommendation-vs-field: did this
beat the average entry score, week after week.

Usage:
    python fpl_track.py record            # save this week's recommendation
    python fpl_track.py score             # score any finished weeks
    python fpl_track.py report            # how it has done so far
"""

import json
import os
import sys
import time

import fpl_data
import fpl_solve

# Accented player names would crash on print under the default Windows console
# encoding.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(HERE, "data")
LOG = os.path.join(DATA, "tracking.json")

# James's league. Two competitions run off the same weekly score, and they
# reward different things:
#
#   the points table  - cumulative total, so the average is the right target
#   the league table  - 3 points for finishing first that week, 2 for second,
#                       1 for third, and nothing for the rest
#
# The league table is the one that matters to him, and it is decided by eight
# specific people rather than by the field. Comparing against the game-wide
# average answered the wrong question and answered it flatteringly: GW1 was
# recorded as +14 and a win, in a week he finished eighth of nine and scored no
# league points at all.
LEAGUE_ID = 818
ENTRY_ID = 82078  # "Claude FC"
LEAGUE_POINTS = {1: 3, 2: 2, 3: 1}


def _league_standings(event_id):
    """Every entry's score for one specific gameweek, best first.

    The standings endpoint carries `event_total`, which is tempting and wrong:
    it means the gameweek in progress, so it reads 0 for everyone until the
    current week scores, and using it ranked a last-placed entry first. Each
    entry's own history carries the per-gameweek points, so the score for a
    given week is taken from there.
    """
    data = fpl_data.fetch(
        f"{fpl_data.CHALLENGE}/leagues-classic/{LEAGUE_ID}/standings/",
        f"league_{LEAGUE_ID}_standings.json",
        900,
    )
    rows = []
    for r in data["standings"]["results"]:
        try:
            hist = fpl_data.fetch(
                f"{fpl_data.CHALLENGE}/entry/{r['entry']}/history/",
                f"entry_{r['entry']}_history_gw{event_id}.json",
                0,
            )
        except Exception:
            continue
        wk = next((c for c in hist.get("current") or []
                   if c.get("event") == event_id), None)
        if wk is None:
            continue
        rows.append({**r, "week_points": wk.get("points") or 0})
    return data["league"]["name"], rows


def _rival_squads(event_id, entries):
    """What each entry actually picked, once the gameweek has locked.

    Before the deadline this endpoint answers with the shape of a squad and
    every element id zeroed, so it can only ever be read backwards. That is the
    whole reason this is collected week by week: the ownership figure the
    picker needs - how many of these eight own a player - has no live source,
    and the only way to it is a record of what this field has done before.
    """
    out = {}
    for e in entries:
        try:
            d = fpl_data.fetch(
                f"{fpl_data.CHALLENGE}/entry/{e['entry']}/event/{event_id}/picks/",
                f"picks_{e['entry']}_{event_id}.json",
                0,
            )
        except Exception:
            continue  # an entry that skipped the week has no picks at all
        picks = d.get("picks") or []
        if any(x.get("element") for x in picks):
            out[str(e["entry"])] = {
                "player_name": e.get("player_name"),
                "picks": [x["element"] for x in picks],
                "captain": next((x["element"] for x in picks
                                 if x.get("is_captain")), None),
            }
    return out


def _load_log():
    if os.path.exists(LOG):
        with open(LOG, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_log(log):
    os.makedirs(DATA, exist_ok=True)
    with open(LOG, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=1)


def record():
    """Save the current recommendation so it can be scored later."""
    data = fpl_solve.load()
    ev = data["event"]
    twist = fpl_solve.load_twist(ev["id"])
    con = fpl_solve.constraints(data, twist)

    scoring = dict(data["base_scoring"])
    for k, v in ((ev.get("overrides") or {}).get("scoring") or {}).items():
        scoring[k] = v
    for k, v in ((twist or {}).get("scoring_overrides") or {}).items():
        scoring[k] = v

    players = fpl_solve.project(data, scoring, fpl_solve.load_starters())
    players = fpl_solve.apply_twist(players, twist, data["teams"])
    players = [p for p in players if p["available"] > 0 and p["n_fixtures"] > 0]

    best = fpl_solve.solve(players, con)
    if not best:
        print("No legal squad to record.", file=sys.stderr)
        sys.exit(1)

    score, picks, cap = best
    log = _load_log()
    key = str(ev["id"])
    entry = log.get(key, {})
    entry.update(
        {
            "event": ev["id"],
            "event_name": ev["name"],
            "twist": (twist or {}).get("name"),
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "projected": round(score, 2),
            "captain_code": cap,
            "picks": [
                {
                    "code": p["code"],
                    "challenge_id": p["challenge_id"],
                    "name": p["name"],
                    "team": p["team"],
                    "position": fpl_solve.POS_NAME[p["position"]],
                    "projected": p["expected"],
                    "multiplier": p["multiplier"],
                    "low_confidence": p["low_confidence"],
                }
                for p in picks
            ],
        }
    )
    log[key] = entry
    _save_log(log)
    print(f"Recorded {ev['name']} recommendation: "
          f"{len(picks)} players, {score:.1f} projected.")


def _ordinal(n):
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def score():
    """Score every recorded week whose gameweek has finished."""
    log = _load_log()
    if not log:
        print("Nothing recorded yet.")
        return

    chal = fpl_data.fetch(
        f"{fpl_data.CHALLENGE}/bootstrap-static/", "chal_bootstrap.json", 0
    )
    events = {e["id"]: e for e in chal["events"]}

    for key in sorted(log, key=int):
        entry = log[key]
        ev = events.get(entry["event"])
        if not ev or not ev.get("finished"):
            continue
        if "actual" in entry:
            continue

        live = fpl_data.fetch(
            f"{fpl_data.CHALLENGE}/event/{entry['event']}/live/",
            f"live_{entry['event']}.json",
            0,
        )
        # The points the API reports already include the weekly twist. This was
        # an open question until GW1 finished; the scoring breakdown settles it.
        # Every affected stat carries a points_modification field holding the
        # extra the twist added - Lacroix's assist reads 6 points with a
        # modification of 3, so the twist is in the 6. Doubling applies to the
        # negatives too: his two goals conceded cost 2, not 1.
        pts = {e["id"]: e["stats"]["total_points"] for e in live["elements"]}

        raw = 0
        for p in entry["picks"]:
            base = pts.get(p["challenge_id"], 0)
            mult = 2 if p["code"] == entry["captain_code"] else 1
            raw += base * mult
            p["actual"] = base

        entry["actual"] = raw
        entry["average_entry_score"] = ev.get("average_entry_score")
        entry["highest_score"] = ev.get("highest_score")

        # Where he finished among the people he is actually playing against.
        # Scores come from the league table rather than being recomputed, so a
        # bug in the picks above cannot quietly move his position too.
        try:
            name, results = _league_standings(entry["event"])
            week = sorted(
                ((r["week_points"], r) for r in results),
                key=lambda t: -t[0],
            )
            scores = [t[0] for t in week]
            # His position follows the team he entered, which is not always the
            # team recommended here - he overrides it, and should. Taking the
            # score from the league table keeps the two apart: "actual" is what
            # the recommendation would have scored, "entered" is what he did.
            mine = next((r for r in results
                         if r.get("entry") == ENTRY_ID), None)
            entered = mine["week_points"] if mine else raw
            entry["entered"] = entered
            # Rank by how many scored strictly more, so ties share a place -
            # two on 63 are both fifth, and the next entry is seventh.
            pos = sum(1 for sc in scores if sc > entered) + 1
            entry["league_name"] = name
            entry["league_size"] = len(results)
            entry["league_position"] = pos
            entry["league_points"] = LEAGUE_POINTS.get(pos, 0)
            entry["league_scores"] = [
                {"player_name": r.get("player_name"), "score": sc}
                for sc, r in week
            ]
            entry["league_winner"] = week[0][0] if week else None
            entry["league_third"] = scores[2] if len(scores) > 2 else None
            entry["rival_squads"] = _rival_squads(entry["event"], results)
        except Exception as e:
            print(f"  warn: league {LEAGUE_ID} unavailable ({e})", file=sys.stderr)

        log[key] = entry
        lp = entry.get("league_points")
        where = (f", {_ordinal(entry['league_position'])} of "
                 f"{entry['league_size']} in the league for {lp} league "
                 f"point{'' if lp == 1 else 's'}"
                 if entry.get("league_position") else "")
        print(f"Scored {entry['event_name']}: {raw} pts "
              f"(average entry {ev.get('average_entry_score')}){where}")

    _save_log(log)


def report():
    log = _load_log()
    done = [e for e in log.values() if "actual" in e]
    if not done:
        print("No finished gameweeks scored yet. Run 'score' after a gameweek ends.")
        return

    done.sort(key=lambda x: x["event"])
    scored = [e for e in done if e.get("league_position")]

    # The league table first, because it is the one that decides the season.
    # The game-wide average is kept, but demoted to what it is: a sanity check
    # that the picker is not simply bad, not a measure of whether he is winning.
    print(f"{'GW':>3}  {'Challenge':<18} {'Proj':>6} {'Score':>6} "
          f"{'Pos':>6} {'Pts':>4} {'3rd':>5} {'Gap':>6} {'Avg':>5}")
    for e in done:
        pos = e.get("league_position")
        size = e.get("league_size")
        third = e.get("league_third")
        gap = (e.get("entered", e["actual"]) - third) if third is not None else None
        print(f"{e['event']:>3}  {str(e.get('twist') or '-'):<18} "
              f"{e['projected']:>6.1f} {e.get('entered', e['actual']):>6} "
              f"{(f'{pos}/{size}' if pos else '-'):>6} "
              f"{(e.get('league_points') if pos else '-'):>4} "
              f"{(third if third is not None else '-'):>5} "
              f"{(f'{gap:+d}' if gap is not None else '-'):>6} "
              f"{e.get('average_entry_score') or 0:>5}")

    n = len(done)
    print()
    if scored:
        total = sum(e.get("league_points") or 0 for e in scored)
        placed = sum(1 for e in scored if (e.get("league_points") or 0) > 0)
        best = min(e["league_position"] for e in scored)
        print(f"League table: {total} point{'' if total == 1 else 's'} from "
              f"{len(scored)} gameweek{'' if len(scored) == 1 else 's'}, "
              f"finishing in the points {placed} time"
              f"{'' if placed == 1 else 's'}. Best finish {_ordinal(best)}.")
        gaps = [e.get("entered", e["actual"]) - e["league_third"] for e in scored
                if e.get("league_third") is not None]
        if gaps:
            short = [g for g in gaps if g < 0]
            if short:
                print(f"Missed third place by {abs(sum(short) / len(short)):.0f} "
                      f"points on average, across {len(short)} of {len(gaps)} "
                      f"gameweeks. That is the number to close.")
            else:
                print("Has not finished outside the points yet.")
    else:
        print("No league positions recorded yet - run 'score' to fetch them.")

    beat = sum(1 for e in done
               if e["actual"] > (e.get("average_entry_score") or 0))
    avg_err = sum(abs(e["projected"] - e["actual"]) for e in done) / n
    print(f"Points table: beat the game-wide average in {beat} of {n}. "
          f"Mean projection error {avg_err:.1f} pts.")
    print("Beating that average is not the target - it is only a check that "
          "the picker works. The league table is decided by eight other people.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    {"record": record, "score": score, "report": report}.get(cmd, report)()
