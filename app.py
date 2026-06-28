import os
import base64
import secrets
import requests
from pathlib import Path
from urllib.parse import urlencode
from flask import Flask, render_template, jsonify, request, redirect, session, url_for
from dotenv import load_dotenv, set_key

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)

YAHOO_API_BASE = "https://fantasysports.yahooapis.com/fantasy/v2"
YAHOO_AUTH_URL = "https://api.login.yahoo.com/oauth2/request_auth"
YAHOO_TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
ENV_PATH = Path(__file__).parent / ".env"


# ── Config helpers ─────────────────────────────────────────────────────────────

def get_credentials() -> dict:
    return {
        "client_id": os.getenv("YAHOO_CLIENT_ID", ""),
        "client_secret": os.getenv("YAHOO_CLIENT_SECRET", ""),
        "access_token": os.getenv("YAHOO_ACCESS_TOKEN", ""),
        "refresh_token": os.getenv("YAHOO_REFRESH_TOKEN", ""),
    }


def _basic_auth_header() -> str:
    creds = get_credentials()
    encoded = base64.b64encode(
        f"{creds['client_id']}:{creds['client_secret']}".encode()
    ).decode()
    return f"Basic {encoded}"


def save_tokens(access_token: str, refresh_token: str) -> None:
    if not ENV_PATH.exists():
        ENV_PATH.touch()
    set_key(str(ENV_PATH), "YAHOO_ACCESS_TOKEN", access_token)
    set_key(str(ENV_PATH), "YAHOO_REFRESH_TOKEN", refresh_token)
    os.environ["YAHOO_ACCESS_TOKEN"] = access_token
    os.environ["YAHOO_REFRESH_TOKEN"] = refresh_token


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
    Rank each team 1–N for every stat (N = best).
    Tied teams share the average of the positions they occupy.
    Teams missing a stat value are ranked last (rank 1).
    """
    n = len(teams)
    result = {
        t["name"]: {"name": t["name"], "stats": t["stats"], "ranks": {}, "total": 0.0}
        for t in teams
    }

    for cat in cats:
        sid = cat["stat_id"]
        higher_better = cat["sort_order"] == "1"

        pairs = [(t["name"], _to_float(t["stats"].get(sid))) for t in teams]

        # Sort worst→best so index 0 = rank 1, index n-1 = rank n.
        # None values are always worst (pushed to the front).
        def sort_key(p):
            v = p[1]
            if v is None:
                return (1, 0)
            return (0, v if higher_better else -v)

        ordered = sorted(pairs, key=sort_key)

        # Assign ranks with tie-averaging
        i = 0
        while i < n:
            j = i
            while (
                j + 1 < n
                and ordered[j][1] is not None
                and ordered[j][1] == ordered[j + 1][1]
            ):
                j += 1
            avg_rank = (i + 1 + j + 1) / 2
            for k in range(i, j + 1):
                result[ordered[k][0]]["ranks"][sid] = avg_rank
            i = j + 1

    for r in result.values():
        r["total"] = sum(r["ranks"].values())

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
        return "YAHOO_CLIENT_ID not configured in .env", 400
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
    app.run(debug=True)
