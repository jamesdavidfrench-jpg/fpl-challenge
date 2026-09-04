"""Draft a likely starting eleven for each club, and flag the shaky calls.

Standard library only.

The picker's biggest weakness is not knowing who actually starts. Prior-season
minutes are club-blind, ownership is distorted (managers buy the cheapest keeper
as bench fodder precisely so he never plays), and price is good but not perfect.

So this does not pretend to be right. It drafts a best guess from the signals
available, then reports the calls it is least confident about, so a human only
has to review the handful that matter rather than all 220 players.

    python fpl_starters.py            # draft, write data/starters.json, report
    python fpl_starters.py --report   # re-report against the existing file

Edit data/starters.json by hand to correct it. Once it exists, the picker trusts
it over every heuristic.
"""

import argparse
import json
import os
import sys
import time
import unicodedata

import fpl_solve as S

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

DATA = S.DATA
PATH = os.path.join(DATA, "starters.json")

# Pre-season there are no minutes to go on, so the draft falls back to a
# conventional shape and lets the review step fix the odd 3-at-the-back side.
SHAPE = {1: 1, 2: 4, 3: 4, 4: 2}

# Once real minutes exist the shape comes from them instead, inside these
# limits. Wing-backs count as defenders in Fantasy, so five at the back is a
# real shape; two keepers or no striker is not.
SHAPE_MIN = {1: 1, 2: 3, 3: 2, 4: 1}
SHAPE_MAX = {1: 1, 2: 5, 3: 6, 4: 3}

# How much the pre-season signals (price, prior-season minutes, ownership)
# count once this season's minutes exist. Minutes are on a 0-1 scale, so at
# 0.3 a player who has started one of two matches (0.5) still beats anyone
# with no minutes at all, however expensive, and the prior only breaks ties.
PRIOR_WEIGHT = 0.3

# Chosen with less than this share of the available minutes, or left out with
# more than it, is the kind of call GW1 showed to be wrong most often.
MINUTES_REVIEW_SHARE = 0.5


def last_season_minutes(p):
    for h in reversed(p.get("history_past", [])):
        if h.get("season_name") == "2025/26":
            return h.get("minutes", 0)
    return None


def finished_gameweeks(data):
    """Gameweeks whose minutes are in the totals. The open one is not."""
    ev = data.get("event") or {}
    return max(0, int(ev.get("id", 1)) - 1)


def start_score(p, group_max_cost):
    """Pre-season estimate of how likely this player is to start.

    Price carries the most weight because the game sets it knowing the depth
    chart, and unlike ownership it is not distorted by bench-fodder buying.
    """
    price = p["cost"] / group_max_cost if group_max_cost else 0
    rates = S._rates_from_history(p)
    minutes = rates["minutes_share"] if rates else 0.0
    owned = min(p.get("owned") or 0.0, 30.0) / 30.0
    return 0.50 * price + 0.30 * minutes + 0.20 * owned


def season_share(p, gws):
    """Share of this season's available minutes the player has played, 0-1.

    Minutes come from the main game and are a season total, so a player who
    moved club on deadline day carries his minutes for the old club with him.
    That is still the right signal: the new club bought a starter.
    """
    if gws <= 0:
        return 0.0
    return min(1.0, (p.get("minutes") or 0) / (90.0 * gws))


def _pick_eleven(fit, mins, maxs):
    """Best eleven by _score under per-position minimums and maximums.

    Greedy in score order, never taking a player if doing so would leave too
    few slots to meet the other positions' minimums.
    """
    fit = sorted(fit, key=lambda p: -p["_score"])
    counts = {pos: 0 for pos in mins}
    chosen = []
    for p in fit:
        if len(chosen) == 11:
            break
        pos = p["position"]
        if counts[pos] >= maxs[pos]:
            continue
        still_needed = sum(max(0, mins[q] - counts[q]) for q in mins if q != pos)
        still_needed += max(0, mins[pos] - counts[pos] - 1)
        if 11 - len(chosen) - 1 < still_needed:
            continue
        chosen.append(p)
        counts[pos] += 1
    return chosen


def draft(data):
    """Best-guess eleven per club, plus the flags worth a human look.

    With no gameweeks played this is price, prior minutes and ownership in a
    4-4-2. Once minutes exist they take over: the eleven is the players who
    have actually been on the pitch, in whatever shape that implies, and the
    pre-season signals only separate players with the same minutes.
    """
    gws = finished_gameweeks(data)
    by_team = {}
    for p in data["players"]:
        by_team.setdefault(p["team"], []).append(p)

    xi, readable, flags, shapes = {}, {}, [], {}

    for team in sorted(by_team):
        squad = by_team[team]
        for pos in SHAPE:
            group = [p for p in squad if p["position"] == pos]
            gmax = max((p["cost"] for p in group), default=0)
            for p in group:
                prior = start_score(p, gmax)
                p["_prior"] = prior
                p["_share"] = season_share(p, gws)
                p["_score"] = (p["_share"] + PRIOR_WEIGHT * prior) if gws else prior

        fit = [p for p in squad if S._availability(p) > 0]
        out = [p for p in squad if S._availability(p) == 0]
        if gws:
            picked = _pick_eleven(fit, SHAPE_MIN, SHAPE_MAX)
        else:
            picked = _pick_eleven(fit, SHAPE, SHAPE)
        chosen_codes = {p["code"] for p in picked}
        benched = sorted([p for p in fit if p["code"] not in chosen_codes],
                         key=lambda p: -p["_score"])
        shapes[team] = "-".join(
            str(sum(1 for p in picked if p["position"] == pos)) for pos in (2, 3, 4)
        )

        # An injured player who would otherwise walk into the side is worth
        # knowing about - he may be back, or his deputy may be a bargain.
        floor = min((p["_score"] for p in picked), default=0)
        for p in out:
            if p["_score"] >= floor:
                flags.append((team, "injured, would otherwise start", p,
                              (p.get("news") or "no detail")[:48]))

        # The marginal call: the weakest pick against the strongest omission
        # in the same position, since that is the swap a human would make.
        for pos in (1, 2, 3, 4):
            ins = [p for p in picked if p["position"] == pos]
            outs = [p for p in benched if p["position"] == pos]
            if ins and outs:
                last, first = min(ins, key=lambda p: p["_score"]), outs[0]
                if last["_score"] - first["_score"] < 0.08:
                    flags.append((team, "too close to call", last,
                                  f"vs {first['name']} ({first['cost']/10:.1f}m, "
                                  f"{first.get('owned') or 0:.1f}% owned, "
                                  f"{first.get('minutes') or 0} min)"))

        for p in benched:
            if gws and p["_share"] >= MINUTES_REVIEW_SHARE:
                flags.append((team, "left out despite playing a lot", p,
                              f"{p.get('minutes') or 0} of {90*gws} min this season"))
            elif not gws:
                mins = last_season_minutes(p)
                if mins and mins >= 2000:
                    flags.append((team, "left out despite playing a lot", p,
                                  f"{mins} mins in 25/26"))

        for p in picked:
            if gws and p["_share"] < MINUTES_REVIEW_SHARE:
                joined = p.get("team_join_date") or ""
                why = (f"joined {joined}" if joined >= "2026-08-15"
                       else f"{p.get('minutes') or 0} of {90*gws} min this season")
                flags.append((team, "picked with few minutes", p, why))
            if not gws:
                mins = last_season_minutes(p)
                if p["history_past"] and mins == 0:
                    flags.append((team, "picked but did not play last season", p,
                                  "0 mins in 25/26"))
            if not p["history_past"]:
                flags.append((team, "no Premier League record at all", p,
                              f"{p['cost']/10:.1f}m, joined {p['team_join_date']}"))

        xi[team] = [p["code"] for p in picked]
        readable[team] = [
            f"{S.POS_NAME[p['position']]} {p['name']} "
            f"({p['cost']/10:.1f}m, {p.get('owned') or 0:.1f}%, "
            f"{p.get('minutes') or 0} min)"
            for p in sorted(picked, key=lambda x: (x["position"], -x["_score"]))
        ]

    return xi, readable, flags, shapes


def _plain(s):
    """Strip accents so 'Muharemovic' matches 'Muharemović'."""
    return "".join(
        c for c in unicodedata.normalize("NFD", s.lower()) if not unicodedata.combining(c)
    ).strip()


def set_xi(data, team, groups, confirmed=False):
    """Replace one club's picks for the positions given. Other positions stand.

    confirmed marks this as a published team sheet rather than a guess, which
    the projection treats differently: named players get full minutes instead
    of a share inferred from ownership, and anyone left out is a substitute as
    a matter of fact rather than as weak evidence.
    """
    path = PATH
    with open(path, encoding="utf-8") as f:
        st = json.load(f)
    byc = {p["code"]: p for p in data["players"]}
    squad = [p for p in data["players"] if p["team"] == team]
    if not squad:
        sys.exit(f"No club called {team}")

    # A saved code can have left the pool (sold abroad) or moved club since the
    # draft. Either used to crash here before anything was written, and a
    # player who has moved is not this club's starter, so both are dropped.
    current = [byc[c] for c in st["expected_xi"].get(team, [])
               if c in byc and byc[c]["team"] == team]

    # Resolve every name before changing or printing anything. The file is only
    # written after the loop, so a bad name in the last position used to make
    # the whole call a no-op - while the positions before it had already printed
    # as though they had been applied. A re-solve then ran on the old eleven and
    # looked entirely normal, which is the worst way for this to fail.
    plan = {}
    for pos_name, names in groups.items():
        pos = S.POS_ID[pos_name]
        resolved = []
        for want in names:
            hits = [p for p in squad if _plain(p["name"]) == _plain(want)]
            if not hits:
                hits = [p for p in squad if _plain(want) in _plain(p["name"])]
            if not hits:
                sys.exit(f"Could not find '{want}' at {team}. Nothing was changed.")
            if len(hits) > 1:
                sys.exit(f"'{want}' is ambiguous at {team}: "
                         + ", ".join(h["name"] for h in hits)
                         + ". Nothing was changed.")
            p = hits[0]
            if p["position"] != pos:
                sys.exit(f"{p['name']} is a {S.POS_NAME[p['position']]}, not "
                         f"{pos_name}. Nothing was changed.")
            resolved.append(p)
        plan[pos_name] = resolved

    for pos_name, resolved in plan.items():
        pos = S.POS_ID[pos_name]
        for p in resolved:
            if S._availability(p) == 0:
                print(f"  note: {p['name']} is unavailable "
                      f"({(p.get('news') or 'no detail')[:44]}) - kept in the list, "
                      f"but the picker will not select him")
        current = [p for p in current if p["position"] != pos] + resolved
        print(f"  {team} {pos_name}: {', '.join(p['name'] for p in resolved)}")

    st["expected_xi"][team] = [p["code"] for p in current]
    st.setdefault("readable", {})[team] = [
        f"{S.POS_NAME[p['position']]} {p['name']} ({p['cost']/10:.1f}m)"
        for p in sorted(current, key=lambda x: x["position"])
    ]
    st.setdefault("human_edits", {})[team] = time.strftime("%Y-%m-%d")
    if confirmed:
        st.setdefault("confirmed", {})[team] = time.strftime("%Y-%m-%d")
    else:
        # A club re-drafted from guesswork is no longer confirmed, and leaving a
        # stale marker would tell the projection to trust a guess completely.
        (st.get("confirmed") or {}).pop(team, None)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1, ensure_ascii=False)
    print(f"  {team} now has {len(current)} players"
          + (" (confirmed team sheet)." if confirmed else "."))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true",
                    help="re-report without overwriting an edited file")
    ap.add_argument("--set", metavar="TEAM",
                    help="correct one club, e.g. --set LEE --def 'Bogle,Rodon'")
    for pos in ("gkp", "def", "mid", "fwd"):
        ap.add_argument(f"--{pos}", help=f"comma-separated {pos.upper()} starters")
    ap.add_argument("--confirmed", action="store_true",
                    help="this is published team news, not a guess: named "
                         "players get full minutes and anyone omitted is a "
                         "substitute")
    args = ap.parse_args()

    data = S.load()

    if args.set:
        groups = {}
        for pos in ("GKP", "DEF", "MID", "FWD"):
            val = getattr(args, pos.lower())
            if val:
                groups[pos] = [n.strip() for n in val.split(",") if n.strip()]
        if not groups:
            sys.exit("--set needs at least one of --gkp/--def/--mid/--fwd")
        set_xi(data, args.set.upper(), groups, confirmed=args.confirmed)
        return

    xi, readable, flags, shapes = draft(data)
    gws = finished_gameweeks(data)

    if not args.report:
        with open(PATH, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "note": "Best guess, not confirmed. Edit expected_xi to "
                            "correct it - the picker trusts this over its own "
                            "heuristics. Codes are FPL player codes; readable "
                            "is a human-facing mirror and is not read back.",
                    "source": (f"drafted from this season's minutes through GW{gws}, "
                               f"pre-season price/history/ownership as tiebreak"
                               if gws else
                               "pre-season draft: price, prior-season minutes, ownership"),
                    "shapes": shapes,
                    "expected_xi": xi,
                    "readable": readable,
                },
                f,
                indent=1,
                ensure_ascii=False,
            )
        print(f"Wrote {PATH}: {sum(len(v) for v in xi.values())} players "
              f"across {len(xi)} clubs, from "
              + (f"{gws} finished gameweek(s) of minutes." if gws
                 else "pre-season signals only - no minutes yet.") + "\n")
        print("SHAPES (DEF-MID-FWD)  "
              + "  ".join(f"{t} {shapes[t]}" for t in sorted(shapes)) + "\n")

    by_kind = {}
    for team, kind, p, detail in flags:
        by_kind.setdefault(kind, []).append((team, p, detail))

    # The flags above describe a fresh draft. This one describes the file that
    # is actually on disk, which is the thing the picker reads, and the two
    # drift apart the moment somebody gets injured after an eleven was saved.
    # It is a different kind of finding: not "here is a judgement worth
    # checking" but "this file names someone who cannot play". He will never be
    # picked - the solver drops anyone unavailable - but he holds a place, and
    # a held place marks down whoever is actually filling it.
    if os.path.exists(PATH):
        with open(PATH, encoding="utf-8") as f:
            saved = json.load(f)
        byc = {q["code"]: q for q in data["players"]}
        stale = []
        for team, codes in (saved.get("expected_xi") or {}).items():
            for c in codes:
                q = byc.get(int(c))
                if q and S._availability(q) == 0:
                    stale.append((team, q, (q.get("news") or "no detail")[:48]))
        if stale:
            print(f"NAMED IN YOUR SAVED ELEVEN BUT CANNOT PLAY  ({len(stale)})")
            for team, q, detail in sorted(stale, key=lambda x: x[0]):
                print(f"  {team}  {S.POS_NAME[q['position']]} {q['name']:<14} "
                      f"{q['cost']/10:>4.1f}m  {detail}")
            print("  Fix these first - everything below is a judgement call, "
                  "these are wrong.")
            print()

    order = ["picked with few minutes", "left out despite playing a lot",
             "too close to call", "injured, would otherwise start",
             "picked but did not play last season", "no Premier League record at all"]
    for kind in order:
        items = by_kind.get(kind)
        if not items:
            continue
        print(f"{kind.upper()}  ({len(items)})")
        for team, p, detail in sorted(items, key=lambda x: x[0]):
            print(f"  {team}  {S.POS_NAME[p['position']]} {p['name']:<14} "
                  f"{p['cost']/10:>4.1f}m  {detail}")
        print()


if __name__ == "__main__":
    main()
