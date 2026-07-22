import base64
import configparser
import secrets
import requests
from pathlib import Path
from urllib.parse import urlencode
from flask import Flask, render_template, jsonify, request, redirect, session, url_for

CONFIG_PATH = Path(__file__).parent / "config.txt"

app = Flask(__name__)
_startup_cfg = configparser.ConfigParser()
_startup_cfg.read(CONFIG_PATH)
app.secret_key = _startup_cfg.get("flask", "secret_key", fallback=None) or secrets.token_hex(32)

YAHOO_API_BASE = "https://fantasysports.yahooapis.com/fantasy/v2"
YAHOO_AUTH_URL = "https://api.login.yahoo.com/oauth2/request_auth"
YAHOO_TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"


# ── Config helpers ─────────────────────────────────────────────────────────────

def _load_config() -> configparser.ConfigParser:
    config = configparser.ConfigParser()
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


def save_tokens(access_token: str, refresh_token: str) -> None:
    config = _load_config()
    if "yahoo" not in config:
        config["yahoo"] = {}
    config["yahoo"]["access_token"] = access_token
    config["yahoo"]["refresh_token"] = refresh_token
    with CONFIG_PATH.open("w") as f:
        config.write(f)


# ── Yahoo API ──────────────────────────────────────────────────────────────────

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


def yahoo_get(path: str, _retried: bool = False) -> dict:
    url = f"{YAHOO_API_BASE}{path}"
    creds = get_credentials()
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {creds['access_token']}"},
        params={"format": "json"},
        timeout=15,
    )
    if resp.status_code == 401 and not _retried:
        if _try_refresh():
            return yahoo_get(path, _retried=True)
        raise PermissionError("Yahoo authentication failed — please re-authenticate.")
    resp.raise_for_status()
    return resp.json()


# ── Data parsing ───────────────────────────────────────────────────────────────

def parse_stat_categories(data: dict) -> list[dict]:
    league = data["fantasy_content"]["league"]
    raw = league[1]["settings"]["stat_categories"]["stats"]
    cats = []
    for key in sorted(raw, key=lambda k: int(k) if k.isdigit() else 9999):
        s = raw[key]["stat"]
        if s.get("enabled") == "1":
            cats.append({
                "stat_id": s["stat_id"],
                "name": s["name"],
                "display_name": s.get("display_name", s["name"]),
                # "1" = higher is better, "0" = lower is better (ERA, WHIP, etc.)
                "sort_order": s.get("sort_order", "1"),
            })
    return cats


def parse_teams(data: dict) -> tuple[list[dict], str]:
    league = data["fantasy_content"]["league"]
    scoreboard = league[1]["scoreboard"]
    week = scoreboard.get("week", "?")
    matchups = scoreboard["0"]["matchups"]

    teams = []
    for key, val in matchups.items():
        if key == "count":
            continue
        matchup_teams = val["matchup"]["teams"]
        for slot in ("0", "1"):
            if slot not in matchup_teams:
                continue
            team_arr = matchup_teams[slot]["team"]

            # team_arr[0] is a list of dicts with team metadata
            meta = {}
            for item in team_arr[0]:
                if isinstance(item, dict):
                    meta.update(item)

            # team_arr[1] has the stat values
            stats = {}
            for skey, sval in team_arr[1].get("team_stats", {}).get("stats", {}).items():
                if skey == "count":
                    continue
                s = sval["stat"]
                stats[s["stat_id"]] = s.get("value", "-")

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
        t["name"]: {"name": t["name"], "stats": t["stats"], "ranks": {}, "total": 0.0}
        for t in teams
    }

    for cat in cats:
        sid = cat["stat_id"]
        higher_better = cat["sort_order"] == "1"

        present, missing = [], []
        for t in teams:
            value = _to_float(t["stats"].get(sid))
            (missing if value is None else present).append((t["name"], value))

        # Teams with no value for this stat are flagged (None) and score nothing.
        for name, _ in missing:
            result[name]["ranks"][sid] = None

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


@app.route("/api/rankings")
def api_rankings():
    league_id = request.args.get("league_id", "").strip()
    week = request.args.get("week", "").strip()

    if not league_id or not week:
        return jsonify({"error": "league_id and week are required"}), 400

    creds = get_credentials()
    if not creds["access_token"]:
        return jsonify({"error": "Not authenticated", "needs_auth": True}), 401

    try:
        settings = yahoo_get(f"/league/mlb.l.{league_id}/settings")
        cats = parse_stat_categories(settings)

        scoreboard = yahoo_get(f"/league/mlb.l.{league_id}/scoreboard;week={week}")
        teams, week_num = parse_teams(scoreboard)

        if not teams:
            return jsonify({"error": "No team data found for that week"}), 404

        ranked = compute_rankings(teams, cats)
        return jsonify({"week": week_num, "stat_categories": cats, "teams": ranked})

    except PermissionError as e:
        return jsonify({"error": str(e), "needs_auth": True}), 401
    except Exception as e:
        app.logger.exception("Error fetching rankings")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    # Yahoo OAuth requires an HTTPS redirect URI, so serve over TLS even locally.
    # 'adhoc' generates a throwaway self-signed cert (browser will warn — that's fine).
    app.run(debug=True, ssl_context="adhoc")
