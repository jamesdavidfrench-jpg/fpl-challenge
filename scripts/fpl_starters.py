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

# A conventional shape. Clubs vary, but this is the right ballpark and the
# review step is where the odd 3-at-the-back side gets corrected.
SHAPE = {1: 1, 2: 4, 3: 4, 4: 2}


def last_season_minutes(p):
    for h in reversed(p.get("history_past", [])):
        if h.get("season_name") == "2025/26":
            return h.get("minutes", 0)
    return None


def start_score(p, group_max_cost):
    """How likely this player is to be in the starting eleven.

    Price carries the most weight because the game sets it knowing the depth
    chart, and unlike ownership it is not distorted by bench-fodder buying.
    """
    price = p["cost"] / group_max_cost if group_max_cost else 0
    rates = S._rates_from_history(p)
    minutes = rates["minutes_share"] if rates else 0.0
    owned = min(p.get("owned") or 0.0, 30.0) / 30.0
    return 0.50 * price + 0.30 * minutes + 0.20 * owned


def draft(data):
    """Best-guess eleven per club, plus the flags worth a human look."""
    by_team = {}
    for p in data["players"]:
        by_team.setdefault(p["team"], []).append(p)

    xi, readable, flags = {}, {}, []

    for team in sorted(by_team):
        squad = by_team[team]
        picked = []
        for pos, n in SHAPE.items():
            group = [p for p in squad if p["position"] == pos]
            if not group:
                continue
            gmax = max(p["cost"] for p in group)
            for p in group:
                p["_score"] = start_score(p, gmax)

            fit = [p for p in group if S._availability(p) > 0]
            out = [p for p in group if S._availability(p) == 0]
            fit.sort(key=lambda p: -p["_score"])
            chosen, benched = fit[:n], fit[n:]
            picked += chosen

            # An injured player who would otherwise walk into the side is worth
            # knowing about - he may be back, or his deputy may be a bargain.
            for p in out:
                if p["_score"] >= (chosen[-1]["_score"] if chosen else 0):
                    flags.append((team, "injured, would otherwise start", p,
                                  (p.get("news") or "no detail")[:48]))

            if chosen and benched:
                margin = chosen[-1]["_score"] - benched[0]["_score"]
                if margin < 0.06:
                    flags.append((team, "too close to call", chosen[-1],
                                  f"vs {benched[0]['name']} "
                                  f"({benched[0]['cost']/10:.1f}m, "
                                  f"{benched[0].get('owned') or 0:.1f}% owned)"))

            for p in benched:
                mins = last_season_minutes(p)
                if mins and mins >= 2000:
                    flags.append((team, "left out despite playing a lot", p,
                                  f"{mins} mins in 25/26"))

            for p in chosen:
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
            f"({p['cost']/10:.1f}m, {p.get('owned') or 0:.1f}%)"
            for p in sorted(picked, key=lambda x: x["position"])
        ]

    return xi, readable, flags


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

    current = [byc[c] for c in st["expected_xi"].get(team, [])]

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

    xi, readable, flags = draft(data)

    if not args.report:
        with open(PATH, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "note": "Best guess, not confirmed. Edit expected_xi to "
                            "correct it - the picker trusts this over its own "
                            "heuristics. Codes are FPL player codes; readable "
                            "is a human-facing mirror and is not read back.",
                    "expected_xi": xi,
                    "readable": readable,
                },
                f,
                indent=1,
                ensure_ascii=False,
            )
        print(f"Wrote {PATH}: {sum(len(v) for v in xi.values())} players "
              f"across {len(xi)} clubs.\n")

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

    order = ["too close to call", "left out despite playing a lot",
             "picked but did not play last season", "injured, would otherwise start",
             "no Premier League record at all"]
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
