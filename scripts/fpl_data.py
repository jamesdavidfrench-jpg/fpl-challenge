"""Fetch and cache everything the FPL Challenge picker needs.

Standard library only - no pip installs.

Two separate games are involved:
  - fplchallenge.premierleague.com  -> the rules, prices, availability, join dates
  - fantasy.premierleague.com       -> expected goals and prior-season history

They share player `code` values but NOT `id` values (25 of 590 differ), so every
join in here is on `code`. Joining on `id` silently returns the wrong player.

Output: data/players.json - one merged record per player, plus the current
event's rules and fixtures.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor

CHALLENGE = "https://fplchallenge.premierleague.com/api"
MAIN = "https://fantasy.premierleague.com/api"

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(HERE, "data")
CACHE = os.path.join(DATA, "cache")

# Prior-season history never changes, so it can be cached hard.
# Everything else moves during the week.
TTL_LIVE = 30 * 60
TTL_HISTORY = 30 * 24 * 3600


def _get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch(url, cache_name, ttl):
    """GET with an on-disk cache. Falls back to stale cache if the network fails."""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, cache_name)
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < ttl:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    try:
        data = _get(url)
    except Exception as e:
        if os.path.exists(path):
            print(f"  warn: {url} failed ({e}); using stale cache", file=sys.stderr)
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        raise
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return data


def current_event(events):
    """The gameweek we should be picking for.

    Challenge locks players individually at kickoff rather than at one deadline,
    so 'current' means the first one still open to entry.
    """
    for e in events:
        if e.get("is_current") and e.get("can_enter"):
            return e
    for e in events:
        if e.get("can_enter"):
            return e
    for e in events:
        if e.get("is_next"):
            return e
    return events[0]


def _history(args):
    """Prior-season totals for one player, from the main game."""
    code, main_id = args
    try:
        s = fetch(
            f"{MAIN}/element-summary/{main_id}/",
            f"hist_{code}.json",
            TTL_HISTORY,
        )
        return code, s.get("history_past", [])
    except Exception:
        return code, []


def build(verbose=True):
    os.makedirs(DATA, exist_ok=True)

    if verbose:
        print("Fetching challenge rules and prices...")
    chal = fetch(f"{CHALLENGE}/bootstrap-static/", "chal_bootstrap.json", TTL_LIVE)
    if verbose:
        print("Fetching main-game stats...")
    main = fetch(f"{MAIN}/bootstrap-static/", "main_bootstrap.json", TTL_LIVE)
    if verbose:
        print("Fetching fixtures...")
    fixtures = fetch(f"{CHALLENGE}/fixtures/", "chal_fixtures.json", TTL_LIVE)

    ev = current_event(chal["events"])
    teams = {t["id"]: t for t in chal["teams"]}
    main_by_code = {p["code"]: p for p in main["elements"]}

    # Only players who could actually be picked are worth pulling history for.
    selectable = [p for p in chal["elements"] if p.get("can_select")]
    if verbose:
        print(
            f"Gameweek {ev['id']} - {len(selectable)} selectable players. "
            f"Loading prior-season history (cached after first run)..."
        )

    pairs = [
        (p["code"], main_by_code[p["code"]]["id"])
        for p in selectable
        if p["code"] in main_by_code
    ]
    hist = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for i, (code, h) in enumerate(pool.map(_history, pairs), 1):
            hist[code] = h
            if verbose and i % 100 == 0:
                print(f"  {i}/{len(pairs)}")

    players = []
    for p in selectable:
        m = main_by_code.get(p["code"], {})
        t = teams[p["team"]]
        past = hist.get(p["code"], [])
        players.append(
            {
                "code": p["code"],
                "challenge_id": p["id"],
                "main_id": m.get("id"),
                "name": p["web_name"],
                "team": t["short_name"],
                "team_id": p["team"],
                "position": p["element_type"],  # 1 GKP 2 DEF 3 MID 4 FWD
                "cost": p["now_cost"],  # tenths of a million
                "status": p.get("status"),
                "chance_next": p.get("chance_of_playing_next_round"),
                "news": p.get("news", ""),
                "team_join_date": p.get("team_join_date"),
                "penalties_order": p.get("penalties_order"),
                "form": _f(p.get("form")),
                "minutes": p.get("minutes", 0),
                "total_points": p.get("total_points", 0),
                # Ownership comes from the main game, which is live and priced
                # by a few million managers. The Challenge copy reads 0.0 before
                # the season starts. For goalkeepers this is the clearest signal
                # available of who is actually first choice.
                "owned": _f(m.get("selected_by_percent")),
                # underlying stats live only in the main game
                "xg90": _f(m.get("expected_goals_per_90")),
                "xa90": _f(m.get("expected_assists_per_90")),
                "xgi90": _f(m.get("expected_goal_involvements_per_90")),
                "starts_per_90": _f(m.get("starts_per_90")),
                "history_past": past,
            }
        )

    out = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": {
            "id": ev["id"],
            "name": ev["name"],
            "deadline_time": ev["deadline_time"],
            "released": ev.get("released"),
            "overrides": ev.get("overrides", {}),
        },
        "base_rules": chal["game_config"]["rules"],
        "base_scoring": chal["game_config"]["scoring"],
        "teams": {t["short_name"]: t["id"] for t in chal["teams"]},
        "team_names": {t["id"]: t["short_name"] for t in chal["teams"]},
        "fixtures": [
            {
                "event": f["event"],
                "kickoff": f["kickoff_time"],
                "home": f["team_h"],
                "away": f["team_a"],
                "home_diff": f["team_h_difficulty"],
                "away_diff": f["team_a_difficulty"],
                "started": f.get("started"),
                "finished": f.get("finished"),
            }
            for f in fixtures
            if f.get("event") == ev["id"]
        ],
        "players": players,
    }

    path = os.path.join(DATA, "players.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f)
    if verbose:
        print(f"Wrote {path}: {len(players)} players, {len(out['fixtures'])} fixtures")
    return out


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    build()
