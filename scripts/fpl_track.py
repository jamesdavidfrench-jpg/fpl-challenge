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
        log[key] = entry
        print(f"Scored {entry['event_name']}: {raw} pts "
              f"(average entry {ev.get('average_entry_score')})")

    _save_log(log)


def report():
    log = _load_log()
    done = [e for e in log.values() if "actual" in e]
    if not done:
        print("No finished gameweeks scored yet. Run 'score' after a gameweek ends.")
        return

    print(f"{'GW':>3}  {'Challenge':<18} {'Proj':>6} {'Actual':>7} {'Avg':>6} {'vs Avg':>7}")
    beat = 0
    for e in sorted(done, key=lambda x: x["event"]):
        avg = e.get("average_entry_score") or 0
        delta = e["actual"] - avg
        if delta > 0:
            beat += 1
        print(f"{e['event']:>3}  {str(e.get('twist') or '-'):<18} "
              f"{e['projected']:>6.1f} {e['actual']:>7} {avg:>6} {delta:>+7}")

    n = len(done)
    print()
    print(f"Beat the average in {beat} of {n} gameweeks.")
    avg_err = sum(abs(e["projected"] - e["actual"]) for e in done) / n
    print(f"Mean projection error: {avg_err:.1f} pts per gameweek.")
    print("A single week's error means little - watch the vs-Avg column over time.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    {"record": record, "score": score, "report": report}.get(cmd, report)()
