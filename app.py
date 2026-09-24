import base64
import configparser
import functools
import os
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from itertools import combinations
from pathlib import Path
from urllib.parse import urlencode

import requests
from flask import Flask, render_template, jsonify, request, redirect, session, url_for

CONFIG_PATH = Path(__file__).parent / "config.txt"

app = Flask(__name__)
_startup_cfg = configparser.ConfigParser(interpolation=None)
_startup_cfg.read(CONFIG_PATH)
app.secret_key = _startup_cfg.get("flask", "secret_key", fallback=None) or secrets.token_hex(32)

YAHOO_API_BASE = "https://fantasysports.yahooapis.com/fantasy/v2"
YAHOO_AUTH_URL = "https://api.login.yahoo.com/oauth2/request_auth"
YAHOO_TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"

# League settings (stat categories, current week) barely change, so reuse them
# for an hour instead of re-fetching on every request.
SETTINGS_TTL = 3600
# Upper bound on how many weeks the season view fetches in one go.
MAX_SEASON_WEEKS = 30


class ApiError(Exception):
    """An error to report to the browser as JSON with the given HTTP status."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ── Config helpers ─────────────────────────────────────────────────────────────

def _load_config() -> configparser.ConfigParser:
    # No interpolation: a "%" in a token or secret must be read back literally.
    config = configparser.ConfigParser(interpolation=None)
    config.read(CONFIG_PATH)
    return config


def get_credentials() -> dict:
    config = _load_config()
    yahoo = config["yahoo"] if "yahoo" in config else {}
    return {
        "client_id": yahoo.get("client_id", ""),
        "client_secret": yahoo.get("client_secret", ""),
        "access_token": yahoo.get("access_token", ""),
        "refresh_token": yahoo.get("refresh_token", ""),
    }


def _basic_auth_header() -> str:
    creds = get_credentials()
    encoded = base64.b64encode(
        f"{creds['client_id']}:{creds['client_secret']}".encode()
    ).decode()
    return f"Basic {encoded}"


_KEY_LINE = re.compile(r"\s*([^=:\s;#\[][^=:]*?)\s*[=:]")


def save_tokens(access_token: str, refresh_token: str) -> None:
    """
    Write new tokens into the [yahoo] section of config.txt.

    Only the two token lines are touched, so the file's comments and layout
    survive — configparser.write() would silently drop every comment.
    """
    updates = {"access_token": access_token, "refresh_token": refresh_token}
    lines = CONFIG_PATH.read_text().splitlines() if CONFIG_PATH.exists() else []

    section = None
    insert_at = None  # just after the last key line of [yahoo]
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip()
            if section == "yahoo":
                insert_at = i + 1
            continue
        if section != "yahoo" or not stripped or stripped[0] in "#;":
            continue
        m = _KEY_LINE.match(line)
        if not m:
            continue
        key = m.group(1).lower()
        if key in updates:
            lines[i] = f"{key} = {updates.pop(key)}"
        insert_at = i + 1

    missing = [f"{k} = {v}" for k, v in updates.items()]
    if missing:
        if insert_at is None:
            lines += ([""] if lines else []) + ["[yahoo]"] + missing
        else:
            lines[insert_at:insert_at] = missing

    # Write to a temp file and swap it in so a crash can't leave config.txt half-written.
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.replace(tmp, CONFIG_PATH)


# ── Yahoo API ──────────────────────────────────────────────────────────────────

_refresh_lock = threading.Lock()


def _try_refresh() -> bool:
    creds = get_credentials()
    if not creds["refresh_token"]:
        return False
    resp = requests.post(
        YAHOO_TOKEN_URL,
        data={"grant_type": "refresh_token", "refresh_token": creds["refresh_token"]},
        headers={"Authorization": _basic_auth_header()},
        timeout=10,
    )
    if resp.ok:
        data = resp.json()
        save_tokens(data["access_token"], data.get("refresh_token", creds["refresh_token"]))
        return True
    return False


def _yahoo_error_message(resp: requests.Response) -> str:
    try:
        desc = resp.json()["error"]["description"]
    except (ValueError, KeyError, TypeError):
        desc = resp.reason or "unknown error"
    return f"Yahoo returned {resp.status_code}: {desc}"


def yahoo_get(path: str, _retried: bool = False) -> dict:
    token = get_credentials()["access_token"]
    if not token:
        raise PermissionError("Not authenticated with Yahoo.")
    resp = requests.get(
        f"{YAHOO_API_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params={"format": "json"},
        timeout=15,
    )
    if resp.status_code == 401 and not _retried:
        # Several requests can hit an expired token at once (the season view
        # fetches weeks in parallel); only one of them should refresh it.
        with _refresh_lock:
            refreshed = get_credentials()["access_token"] != token or _try_refresh()
        if refreshed:
            return yahoo_get(path, _retried=True)
        raise PermissionError("Yahoo authentication failed — please re-authenticate.")
    if not resp.ok:
        raise ApiError(_yahoo_error_message(resp), 502)
    return resp.json()


# ── Caching ────────────────────────────────────────────────────────────────────

_cache: dict = {}
_cache_lock = threading.Lock()


def _cached(key, ttl: float | None, fetch):
    """Return fetch()'s result memoised under key; ttl=None keeps it forever."""
    with _cache_lock:
        hit = _cache.get(key)
    if hit and (hit[0] is None or hit[0] > time.monotonic()):
        return hit[1]
    value = fetch()
    expires = None if ttl is None else time.monotonic() + ttl
    with _cache_lock:
        _cache[key] = (expires, value)
    return value


# ── Data parsing ───────────────────────────────────────────────────────────────

def _yahoo_list(node) -> list:
    """
    Yahoo's JSON encodes collections either as a plain list or as a dict keyed
    "0", "1", … alongside a "count" entry. Return the items in order either way.
    """
    if isinstance(node, list):
        return node
    if isinstance(node, dict):
        return [node[k] for k in sorted((k for k in node if str(k).isdigit()), key=int)]
    return []


def _merge(node) -> dict:
    """Yahoo often splits one object across a list of small dicts; merge them."""
    if isinstance(node, dict):
        return node
    merged = {}
    for item in node or []:
        if isinstance(item, dict):
            merged.update(item)
    return merged


def _as_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_game_key(data: dict) -> str | None:
    games = data["fantasy_content"].get("games")
    items = _yahoo_list(games)
    if not items:
        return None
    game = _merge(items[0].get("game"))
    return game.get("game_key")


def parse_league_meta(data: dict) -> dict:
    meta = _merge(data["fantasy_content"]["league"][0])
    return {
        "league_key": meta.get("league_key"),
        "name": meta.get("name"),
        "season": meta.get("season"),
        "num_teams": _as_int(meta.get("num_teams")),
        "current_week": _as_int(meta.get("current_week")),
        "start_week": _as_int(meta.get("start_week")),
        "end_week": _as_int(meta.get("end_week")),
        "is_finished": str(meta.get("is_finished", "0")) == "1",
    }


def parse_stat_categories(data: dict) -> list[dict]:
    league = data["fantasy_content"]["league"]
    settings = _merge(league[1]["settings"])
    cats = []
    for item in _yahoo_list(settings["stat_categories"]["stats"]):
        s = item["stat"]
        if str(s.get("enabled")) != "1":
            continue
        # Display-only stats (IP, H/AB, …) appear on the scoreboard but aren't
        # scoring categories, so they must not be ranked.
        if str(s.get("is_only_display_stat", "0")) == "1":
            continue
        cats.append({
            "stat_id": str(s["stat_id"]),
            "name": s["name"],
            "display_name": s.get("display_name", s["name"]),
            # "1" = higher is better, "0" = lower is better (ERA, WHIP, etc.)
            "sort_order": str(s.get("sort_order", "1")),
        })
    return cats


def parse_teams(data: dict) -> tuple[list[dict], str]:
    league = data["fantasy_content"]["league"]
    scoreboard = league[1]["scoreboard"]
    week = scoreboard.get("week", "?")
    matchups = scoreboard["0"]["matchups"]

    teams = []
    for entry in _yahoo_list(matchups):
        matchup = entry["matchup"]
        # The teams sit either directly on the matchup or one level down under "0".
        matchup_teams = matchup.get("teams") or matchup.get("0", {}).get("teams", {})
        for slot in _yahoo_list(matchup_teams):
            team_arr = slot["team"]

            # team_arr[0] is a list of dicts with team metadata
            meta = _merge(team_arr[0])

            # the rest of team_arr holds the stat values (plus points/projections)
            extra = _merge(team_arr[1:])
            stats = {}
            for sval in _yahoo_list(extra.get("team_stats", {}).get("stats", {})):
                s = sval["stat"]
                stats[str(s["stat_id"])] = s.get("value", "-")

            teams.append({
                "name": meta.get("name", f"Team {meta.get('team_id', '?')}"),
                "team_id": meta.get("team_id"),
                "stats": stats,
            })

    return teams, week


# ── Ranking logic ──────────────────────────────────────────────────────────────

def _to_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _team_key(t: dict):
    """Identify a team by its Yahoo id; names aren't unique within a league."""
    return t.get("team_id") or t["name"]


def compute_rankings(teams: list[dict], cats: list[dict]) -> list[dict]:
    """
    Score each team 1–N for every stat (N = best) and sum across stats.

    Teams are ordered worst→best, so the team in position k (1-indexed)
    scores k points. When teams tie on a stat, they split the point values
    of the positions they collectively occupy — each tied team gets the
    average. Example: a 3-way tie for 2nd in a 12-team league occupies the
    positions worth 11, 10, and 9, so each of the three scores (11+10+9)/3 = 10.

    A team with no value for a stat (e.g. ERA with 0 innings pitched, which
    Yahoo returns as "-") is not ranked for it: its score is recorded as None
    so the UI can flag it, and it contributes 0 to the team's total. Only the
    teams that do have a value are ranked, among themselves.
    """
    result = {
        _team_key(t): {
            "team_id": t.get("team_id"), "name": t["name"], "stats": t["stats"],
            "ranks": {}, "total": 0.0,
        }
        for t in teams
    }

    for cat in cats:
        sid = cat["stat_id"]
        higher_better = cat["sort_order"] == "1"

        present, missing = [], []
        for t in teams:
            value = _to_float(t["stats"].get(sid))
            (missing if value is None else present).append((_team_key(t), value))

        # Teams with no value for this stat are flagged (None) and score nothing.
        for key, _ in missing:
            result[key]["ranks"][sid] = None

        # Rank only the teams that have a value, worst->best (position k = k points).
        present.sort(key=lambda p: p[1] if higher_better else -p[1])
        m = len(present)
        i = 0
        while i < m:
            j = i
            while j + 1 < m and present[j + 1][1] == present[j][1]:
                j += 1

            # This group occupies positions i+1 .. j+1; each team gets their average.
            positions = range(i + 1, j + 2)
            score = sum(positions) / len(positions)
            for k in range(i, j + 1):
                result[present[k][0]]["ranks"][sid] = score
            i = j + 1

    for r in result.values():
        r["total"] = sum(v for v in r["ranks"].values() if v is not None)

    return sorted(result.values(), key=lambda x: x["total"], reverse=True)


def _category_winner(a: float | None, b: float | None, higher_better: bool) -> int:
    """1 if a wins the category, -1 if b does, 0 for a tie."""
    if a is None and b is None:
        return 0
    if a is None:
        return -1
    if b is None:
        return 1
    if a == b:
        return 0
    return 1 if (a > b) == higher_better else -1


def compute_all_play(teams: list[dict], cats: list[dict]) -> dict:
    """
    The record each team would have had that week if it had played every
    other team, deciding each pairing category by category as a head-to-head
    matchup does. A team with no value for a stat loses that category (to
    match the ranking, where it scores 0); if neither team has one it's a tie.

    Returns {team_key: {"wins": w, "losses": l, "ties": t}}.
    """
    values = {
        _team_key(t): {c["stat_id"]: _to_float(t["stats"].get(c["stat_id"])) for c in cats}
        for t in teams
    }
    records = {key: {"wins": 0, "losses": 0, "ties": 0} for key in values}

    for a, b in combinations(values, 2):
        margin = sum(
            _category_winner(values[a][c["stat_id"]], values[b][c["stat_id"]], c["sort_order"] == "1")
            for c in cats
        )
        if margin > 0:
            records[a]["wins"] += 1
            records[b]["losses"] += 1
        elif margin < 0:
            records[a]["losses"] += 1
            records[b]["wins"] += 1
        else:
            records[a]["ties"] += 1
            records[b]["ties"] += 1

    return records


def rank_week(teams: list[dict], cats: list[dict]) -> list[dict]:
    """compute_rankings, with each team's all-play record attached."""
    ranked = compute_rankings(teams, cats)
    all_play = compute_all_play(teams, cats)
    for r in ranked:
        r["all_play"] = all_play[_team_key(r)]
    return ranked


def compute_season(weeks: list[tuple[int, list[dict]]], cats: list[dict]) -> list[dict]:
    """
    Combine several weeks: each team's weekly total and place (1 = best that
    week, tied teams share a place), the season total and average of those
    weekly totals, and the combined all-play record. Sorted by season total.
    """
    season = {}
    for week, teams in weeks:
        ranked = rank_week(teams, cats)
        for r in ranked:
            entry = season.setdefault(_team_key(r), {
                "team_id": r["team_id"], "name": r["name"], "weekly": {}, "total": 0.0,
                "all_play": {"wins": 0, "losses": 0, "ties": 0},
            })
            entry["name"] = r["name"]  # keep the latest name if a team renamed itself
            place = 1 + sum(1 for o in ranked if o["total"] > r["total"])
            entry["weekly"][str(week)] = {"total": r["total"], "place": place}
            entry["total"] += r["total"]
            for k in entry["all_play"]:
                entry["all_play"][k] += r["all_play"][k]

    for entry in season.values():
        entry["average"] = entry["total"] / len(entry["weekly"])

    return sorted(season.values(), key=lambda e: e["total"], reverse=True)


# ── League lookups ─────────────────────────────────────────────────────────────

def resolve_league_key(league_id: str, season: str) -> str:
    """"mlb" means the current season; a past season needs that year's game key."""
    if not season:
        return f"mlb.l.{league_id}"

    def fetch():
        game_key = parse_game_key(yahoo_get(f"/games;game_codes=mlb;seasons={season}"))
        if not game_key:
            raise ApiError(f"Yahoo has no fantasy baseball game for the {season} season.", 404)
        return game_key

    return f"{_cached(('game', season), None, fetch)}.l.{league_id}"


def get_league_settings(league_key: str) -> dict:
    return _cached(("settings", league_key), SETTINGS_TTL,
                   lambda: yahoo_get(f"/league/{league_key}/settings"))


def get_week_teams(league_key: str, week: int, final: bool) -> list[dict]:
    """A week's teams and stats; a finished week can't change, so it's cached for good."""
    def fetch():
        teams, _ = parse_teams(yahoo_get(f"/league/{league_key}/scoreboard;week={week}"))
        return teams

    if final:
        return _cached(("scoreboard", league_key, week), None, fetch)
    return fetch()


def _week_is_final(meta: dict, week: int) -> bool:
    return meta["is_finished"] or (meta["current_week"] is not None and week < meta["current_week"])


def _check_week(meta: dict, week: int) -> None:
    start, end, current = meta["start_week"], meta["end_week"], meta["current_week"]
    if start is not None and end is not None and not start <= week <= end:
        raise ApiError(f"Week {week} is outside this league's season (weeks {start}–{end}).")
    if not meta["is_finished"] and current is not None and week > current:
        raise ApiError(f"Week {week} hasn't started yet — the current week is {current}.")


# ── Request helpers ────────────────────────────────────────────────────────────

def _arg(name: str, pattern: str, label: str, required: bool = True) -> str:
    value = request.args.get(name, "").strip()
    if not value:
        if required:
            raise ApiError(f"{label} is required.")
        return ""
    if not re.fullmatch(pattern, value):
        raise ApiError(f"{label} must be a number.")
    return value


def _league_from_args() -> tuple[str, dict, dict]:
    """Validate league_id/season and return (league_key, league meta, raw settings)."""
    league_id = _arg("league_id", r"\d{1,10}", "League ID")
    season = _arg("season", r"\d{4}", "Season", required=False)
    league_key = resolve_league_key(league_id, season)
    settings = get_league_settings(league_key)
    return league_key, parse_league_meta(settings), settings


def json_api(fn):
    """Return the view's dict as JSON, turning failures into JSON errors."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return jsonify(fn(*args, **kwargs))
        except ApiError as e:
            return jsonify({"error": str(e)}), e.status
        except PermissionError as e:
            return jsonify({"error": str(e), "needs_auth": True}), 401
        except requests.RequestException as e:
            app.logger.warning("Yahoo request failed: %s", e)
            return jsonify({"error": "Couldn't reach Yahoo — please try again."}), 502
        except Exception as e:
            app.logger.exception("Error handling %s", request.path)
            return jsonify({"error": str(e)}), 500
    return wrapper


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    creds = get_credentials()
    return render_template(
        "index.html",
        is_configured=bool(creds["client_id"] and creds["client_secret"]),
        is_authenticated=bool(creds["access_token"]),
    )


@app.route("/auth/start")
def auth_start():
    creds = get_credentials()
    if not creds["client_id"]:
        return "client_id not configured in config.txt", 400
    state = secrets.token_urlsafe(16)
    session["oauth_state"] = state
    callback_url = url_for("auth_callback", _external=True)
    qs = urlencode({
        "client_id": creds["client_id"],
        "redirect_uri": callback_url,
        "response_type": "code",
        "state": state,
    })
    return redirect(f"{YAHOO_AUTH_URL}?{qs}")


@app.route("/auth/callback")
def auth_callback():
    code = request.args.get("code", "")
    state = request.args.get("state", "")
    if state != session.pop("oauth_state", None):
        return "OAuth state mismatch — please try authenticating again.", 400

    callback_url = url_for("auth_callback", _external=True)
    resp = requests.post(
        YAHOO_TOKEN_URL,
        data={"grant_type": "authorization_code", "redirect_uri": callback_url, "code": code},
        headers={"Authorization": _basic_auth_header()},
        timeout=10,
    )
    if resp.ok:
        data = resp.json()
        save_tokens(data["access_token"], data["refresh_token"])
        return redirect(url_for("index"))
    return f"Token exchange failed: {resp.text}", 400


@app.route("/api/league")
@json_api
def api_league():
    _, meta, _ = _league_from_args()
    return meta


@app.route("/api/rankings")
@json_api
def api_rankings():
    week = int(_arg("week", r"\d{1,2}", "Week"))
    league_key, meta, settings = _league_from_args()
    _check_week(meta, week)

    cats = parse_stat_categories(settings)
    teams = get_week_teams(league_key, week, _week_is_final(meta, week))
    if not teams:
        raise ApiError("No team data found for that week.", 404)

    return {"league": meta, "week": week, "stat_categories": cats, "teams": rank_week(teams, cats)}


@app.route("/api/season")
@json_api
def api_season():
    start = int(_arg("start_week", r"\d{1,2}", "From week"))
    end = int(_arg("end_week", r"\d{1,2}", "To week"))
    if end < start:
        raise ApiError("'To week' must be on or after 'From week'.")
    if end - start + 1 > MAX_SEASON_WEEKS:
        raise ApiError(f"Pick at most {MAX_SEASON_WEEKS} weeks.")

    league_key, meta, settings = _league_from_args()
    _check_week(meta, start)
    _check_week(meta, end)

    cats = parse_stat_categories(settings)
    weeks = list(range(start, end + 1))
    # A few weeks at a time: fast enough without hammering Yahoo.
    with ThreadPoolExecutor(max_workers=4) as pool:
        teams_by_week = list(pool.map(
            lambda w: get_week_teams(league_key, w, _week_is_final(meta, w)), weeks))

    played = [(w, teams) for w, teams in zip(weeks, teams_by_week) if teams]
    if not played:
        raise ApiError("No team data found for those weeks.", 404)

    return {
        "league": meta,
        "weeks": [w for w, _ in played],
        "teams": compute_season(played, cats),
    }


if __name__ == "__main__":
    # Yahoo OAuth requires an HTTPS redirect URI, so serve over TLS even locally.
    # 'adhoc' generates a throwaway self-signed cert (browser will warn — that's fine).
    app.run(debug=True, ssl_context="adhoc")
