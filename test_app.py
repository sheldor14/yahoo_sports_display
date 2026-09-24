"""
Tests for the ranking logic and Yahoo response parsing in app.py.

Run with:
    python -m unittest

These use only the standard library (unittest) beyond the app's own
requirements. Importing `app` builds the Flask object and reads config.txt but
starts no server and makes no network calls, so it's safe to import here; the
API tests swap in canned Yahoo responses.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
from app import (
    _to_float,
    compute_all_play,
    compute_rankings,
    compute_season,
    get_credentials,
    parse_game_key,
    parse_league_meta,
    parse_stat_categories,
    parse_teams,
    save_tokens,
)


def make_teams(stat_id, values):
    """Build the team dicts compute_rankings expects from {name: value}."""
    return [{"name": name, "stats": {stat_id: v}} for name, v in values.items()]


def scores(ranked, stat_id):
    """Flatten compute_rankings output to {name: rank} for one stat."""
    return {t["name"]: t["ranks"][stat_id] for t in ranked}


class ToFloatTests(unittest.TestCase):
    def test_parses_numbers(self):
        self.assertEqual(_to_float("3.50"), 3.5)
        self.assertEqual(_to_float("12"), 12.0)
        self.assertEqual(_to_float(7), 7.0)

    def test_zero_is_a_real_value_not_missing(self):
        # "0" homers is a genuine value that should be ranked, not treated as missing.
        self.assertEqual(_to_float("0"), 0.0)

    def test_empty_and_dash_are_missing(self):
        # Yahoo returns "-" (and sometimes "") for rate stats with a zero denominator.
        self.assertIsNone(_to_float("-"))
        self.assertIsNone(_to_float(""))
        self.assertIsNone(_to_float(None))

    def test_garbage_is_missing(self):
        self.assertIsNone(_to_float("N/A"))


class RankingBasicsTests(unittest.TestCase):
    def test_higher_is_better_scores_1_to_n(self):
        teams = make_teams("1", {"A": 30, "B": 20, "C": 10})
        cats = [{"stat_id": "1", "sort_order": "1"}]
        s = scores(compute_rankings(teams, cats), "1")
        self.assertEqual(s, {"A": 3, "B": 2, "C": 1})

    def test_lower_is_better_reverses_order(self):
        # ERA-style: the lowest value is best and gets N points.
        teams = make_teams("era", {"A": "2.00", "B": "3.00", "C": "4.00"})
        cats = [{"stat_id": "era", "sort_order": "0"}]
        s = scores(compute_rankings(teams, cats), "era")
        self.assertEqual(s, {"A": 3, "B": 2, "C": 1})

    def test_single_team_scores_one(self):
        teams = make_teams("1", {"A": 5})
        cats = [{"stat_id": "1", "sort_order": "1"}]
        self.assertEqual(scores(compute_rankings(teams, cats), "1"), {"A": 1})

    def test_result_sorted_by_total_descending(self):
        teams = make_teams("1", {"A": 10, "B": 30, "C": 20})
        cats = [{"stat_id": "1", "sort_order": "1"}]
        order = [t["name"] for t in compute_rankings(teams, cats)]
        self.assertEqual(order, ["B", "C", "A"])


class TieAveragingTests(unittest.TestCase):
    def test_two_way_tie_splits_positions(self):
        # Original spec: a two-way tie for a spot worth 11 becomes 10.5 each
        # (positions 11 and 10 averaged). Here: 12 teams, B & C tie for 2nd.
        vals = {chr(ord("A") + i): 120 - i * 10 for i in range(12)}
        vals["C"] = vals["B"]  # B and C tie for the 2nd-best value
        teams = make_teams("1", vals)
        cats = [{"stat_id": "1", "sort_order": "1"}]
        s = scores(compute_rankings(teams, cats), "1")
        self.assertEqual(s["B"], 10.5)
        self.assertEqual(s["C"], 10.5)

    def test_three_way_tie_matches_spec_example(self):
        # User's example: 3-way tie for 2nd in a 12-team league occupies the
        # positions worth 11, 10, 9 -> (11+10+9)/3 = 10 each. The next team
        # down then drops to 8 because those three positions were consumed.
        vals = {"A": 100, "B": 90, "C": 90, "D": 90,
                "E": 80, "F": 70, "G": 60, "H": 50,
                "I": 40, "J": 30, "K": 20, "L": 10}
        teams = make_teams("1", vals)
        cats = [{"stat_id": "1", "sort_order": "1"}]
        s = scores(compute_rankings(teams, cats), "1")
        self.assertEqual(s["A"], 12)
        self.assertEqual((s["B"], s["C"], s["D"]), (10, 10, 10))
        self.assertEqual(s["E"], 8)

    def test_tie_for_worst(self):
        teams = make_teams("1", {"A": 30, "B": 10, "C": 10})
        cats = [{"stat_id": "1", "sort_order": "1"}]
        s = scores(compute_rankings(teams, cats), "1")
        self.assertEqual(s["A"], 3)
        self.assertEqual(s["B"], 1.5)  # positions 1 and 2 averaged
        self.assertEqual(s["C"], 1.5)


class MissingStatTests(unittest.TestCase):
    def test_missing_value_is_flagged_and_scores_zero(self):
        teams = make_teams("era", {"A": "2.00", "B": "3.00", "C": "4.00", "D": "-"})
        cats = [{"stat_id": "era", "sort_order": "0"}]
        ranked = compute_rankings(teams, cats)
        s = scores(ranked, "era")
        # Present teams ranked among themselves (3 of them), D not ranked.
        self.assertEqual(s["A"], 3)
        self.assertEqual(s["B"], 2)
        self.assertEqual(s["C"], 1)
        self.assertIsNone(s["D"])
        # D contributes 0 to its total and lands last.
        d = next(t for t in ranked if t["name"] == "D")
        self.assertEqual(d["total"], 0)
        self.assertEqual(ranked[-1]["name"], "D")

    def test_two_missing_teams_both_flagged(self):
        teams = make_teams("era", {"A": "2.00", "B": "-", "C": "-"})
        cats = [{"stat_id": "era", "sort_order": "0"}]
        s = scores(compute_rankings(teams, cats), "era")
        self.assertEqual(s["A"], 1)  # only one present team
        self.assertIsNone(s["B"])
        self.assertIsNone(s["C"])

    def test_zero_value_is_ranked_not_missing(self):
        # A team with 0 saves still has a real value and must be ranked.
        teams = make_teams("sv", {"A": "5", "B": "0"})
        cats = [{"stat_id": "sv", "sort_order": "1"}]
        s = scores(compute_rankings(teams, cats), "sv")
        self.assertEqual(s, {"A": 2, "B": 1})


class MultiStatTotalTests(unittest.TestCase):
    def test_total_sums_across_stats(self):
        teams = [
            {"name": "A", "stats": {"hr": "10", "era": "2.00"}},  # best HR, best ERA
            {"name": "B", "stats": {"hr": "5", "era": "3.00"}},
            {"name": "C", "stats": {"hr": "1", "era": "4.00"}},
        ]
        cats = [
            {"stat_id": "hr", "sort_order": "1"},   # higher better
            {"stat_id": "era", "sort_order": "0"},  # lower better
        ]
        ranked = compute_rankings(teams, cats)
        totals = {t["name"]: t["total"] for t in ranked}
        # A: 3 (HR) + 3 (ERA) = 6; B: 2 + 2 = 4; C: 1 + 1 = 2
        self.assertEqual(totals, {"A": 6, "B": 4, "C": 2})
        self.assertEqual([t["name"] for t in ranked], ["A", "B", "C"])


class ParseStatCategoriesTests(unittest.TestCase):
    def build(self, stats):
        return {"fantasy_content": {"league": [
            {"league_key": "mlb.l.1"},
            {"settings": {"stat_categories": {"stats": stats}}},
        ]}}

    def test_parses_enabled_stats_in_order(self):
        data = self.build({
            "1": {"stat": {"stat_id": "7", "name": "Home Runs",
                           "display_name": "HR", "sort_order": "1", "enabled": "1"}},
            "0": {"stat": {"stat_id": "3", "name": "Batting Avg",
                           "display_name": "AVG", "sort_order": "1", "enabled": "1"}},
        })
        cats = parse_stat_categories(data)
        # Keys "0" then "1" -> AVG before HR regardless of dict insertion order.
        self.assertEqual([c["display_name"] for c in cats], ["AVG", "HR"])

    def test_skips_disabled_stats(self):
        data = self.build({
            "0": {"stat": {"stat_id": "60", "name": "H/AB",
                           "display_name": "H/AB", "enabled": "0"}},
            "1": {"stat": {"stat_id": "7", "name": "Home Runs",
                           "display_name": "HR", "sort_order": "1", "enabled": "1"}},
        })
        cats = parse_stat_categories(data)
        self.assertEqual(len(cats), 1)
        self.assertEqual(cats[0]["stat_id"], "7")

    def test_sort_order_defaults_to_higher_better(self):
        data = self.build({
            "0": {"stat": {"stat_id": "7", "name": "Home Runs",
                           "display_name": "HR", "enabled": "1"}},
        })
        self.assertEqual(parse_stat_categories(data)[0]["sort_order"], "1")


class ParseTeamsTests(unittest.TestCase):
    def team(self, name, team_id, stats):
        stat_block = {
            str(i): {"stat": {"stat_id": sid, "value": val}}
            for i, (sid, val) in enumerate(stats.items())
        }
        return {"team": [
            [{"team_id": team_id}, {"name": name}],
            {"team_stats": {"stats": stat_block}},
        ]}

    def build(self, week="8"):
        return {"fantasy_content": {"league": [
            {"league_key": "mlb.l.1"},
            {"scoreboard": {
                "week": week,
                "0": {"matchups": {
                    "0": {"matchup": {"teams": {
                        "0": self.team("Team A", "1", {"7": "10", "50": "3.20"}),
                        "1": self.team("Team B", "2", {"7": "5", "50": "-"}),
                    }}},
                    "count": 1,
                }},
            }},
        ]}}

    def test_extracts_teams_names_and_stats(self):
        teams, week = parse_teams(self.build(week="8"))
        self.assertEqual(week, "8")
        self.assertEqual({t["name"] for t in teams}, {"Team A", "Team B"})
        by_name = {t["name"]: t for t in teams}
        self.assertEqual(by_name["Team A"]["stats"], {"7": "10", "50": "3.20"})
        # A missing rate stat comes through as "-" for compute_rankings to flag.
        self.assertEqual(by_name["Team B"]["stats"]["50"], "-")

    def test_pipes_into_compute_rankings(self):
        # End-to-end: parse then rank, confirming the missing "-" is handled.
        data = self.build()
        teams, _ = parse_teams(data)
        cats = [
            {"stat_id": "7", "sort_order": "1"},
            {"stat_id": "50", "sort_order": "0"},
        ]
        ranked = compute_rankings(teams, cats)
        a = next(t for t in ranked if t["name"] == "Team A")
        b = next(t for t in ranked if t["name"] == "Team B")
        self.assertIsNone(b["ranks"]["50"])   # Team B's ERA was "-"
        self.assertEqual(a["ranks"]["50"], 1)  # only present team for ERA


class DisplayOnlyStatTests(unittest.TestCase):
    def test_display_only_stats_are_not_ranked(self):
        # IP and H/AB are enabled but display-only: showing them is fine, ranking them isn't.
        data = ParseStatCategoriesTests().build({
            "0": {"stat": {"stat_id": "60", "name": "H/AB", "display_name": "H/AB",
                           "enabled": "1", "is_only_display_stat": "1"}},
            "1": {"stat": {"stat_id": "7", "name": "Home Runs", "display_name": "HR",
                           "sort_order": "1", "enabled": "1"}},
            "2": {"stat": {"stat_id": "50", "name": "Innings Pitched", "display_name": "IP",
                           "sort_order": "1", "enabled": "1", "is_only_display_stat": "1"}},
        })
        self.assertEqual([c["display_name"] for c in parse_stat_categories(data)], ["HR"])


class YahooListShapeTests(unittest.TestCase):
    """Yahoo sends some collections as lists and some as {"0": …, "count": n}."""

    def test_settings_and_stats_as_lists_with_int_ids(self):
        data = {"fantasy_content": {"league": [
            {"league_key": "mlb.l.1"},
            {"settings": [{"stat_categories": {"stats": [
                {"stat": {"stat_id": 7, "name": "Home Runs", "display_name": "HR",
                          "sort_order": "1", "enabled": "1"}},
                {"stat": {"stat_id": 26, "name": "ERA", "display_name": "ERA",
                          "sort_order": "0", "enabled": "1"}},
            ]}}]},
        ]}}
        cats = parse_stat_categories(data)
        # stat ids are normalised to strings so they match the scoreboard's keys
        self.assertEqual([c["stat_id"] for c in cats], ["7", "26"])

    def test_scoreboard_with_nested_matchup_and_list_stats(self):
        def team(name, team_id, hr):
            return {"team": [
                [{"team_key": f"mlb.l.1.t.{team_id}"}, {"team_id": team_id}, {"name": name}, []],
                {"team_stats": {"stats": [{"stat": {"stat_id": 7, "value": hr}}]},
                 "team_points": {"total": "5"}},
            ]}

        data = {"fantasy_content": {"league": [
            {"league_key": "mlb.l.1"},
            {"scoreboard": {"week": "3", "0": {"matchups": {
                "0": {"matchup": {"week": "3", "0": {"teams": {
                    "0": team("A", "1", "4"), "1": team("B", "2", "9"), "count": 2,
                }}}},
                "count": 1,
            }}}},
        ]}}
        teams, week = parse_teams(data)
        self.assertEqual(week, "3")
        self.assertEqual({t["name"]: t["stats"] for t in teams}, {"A": {"7": "4"}, "B": {"7": "9"}})


class DuplicateNameTests(unittest.TestCase):
    def test_teams_with_the_same_name_are_kept_apart(self):
        teams = [
            {"team_id": "1", "name": "Dingers", "stats": {"hr": "10"}},
            {"team_id": "2", "name": "Dingers", "stats": {"hr": "5"}},
            {"team_id": "3", "name": "Other", "stats": {"hr": "1"}},
        ]
        ranked = compute_rankings(teams, [{"stat_id": "hr", "sort_order": "1"}])
        self.assertEqual(len(ranked), 3)
        self.assertEqual([(t["team_id"], t["total"]) for t in ranked], [("1", 3), ("2", 2), ("3", 1)])


class AllPlayTests(unittest.TestCase):
    CATS = [{"stat_id": "hr", "sort_order": "1"}, {"stat_id": "era", "sort_order": "0"}]

    def test_records_against_every_other_team(self):
        teams = [
            {"name": "A", "stats": {"hr": "10", "era": "2.00"}},  # beats everyone
            {"name": "B", "stats": {"hr": "5", "era": "3.00"}},
            {"name": "C", "stats": {"hr": "1", "era": "4.00"}},   # loses to everyone
        ]
        r = compute_all_play(teams, self.CATS)
        self.assertEqual(r["A"], {"wins": 2, "losses": 0, "ties": 0})
        self.assertEqual(r["B"], {"wins": 1, "losses": 1, "ties": 0})
        self.assertEqual(r["C"], {"wins": 0, "losses": 2, "ties": 0})

    def test_split_categories_is_a_tie(self):
        teams = [
            {"name": "A", "stats": {"hr": "10", "era": "4.00"}},
            {"name": "B", "stats": {"hr": "5", "era": "3.00"}},
        ]
        r = compute_all_play(teams, self.CATS)
        self.assertEqual(r["A"], {"wins": 0, "losses": 0, "ties": 1})

    def test_missing_value_loses_the_category(self):
        teams = [
            {"name": "A", "stats": {"hr": "5", "era": "-"}},
            {"name": "B", "stats": {"hr": "5", "era": "9.00"}},
        ]
        r = compute_all_play(teams, self.CATS)
        self.assertEqual(r["B"], {"wins": 1, "losses": 0, "ties": 0})


class SeasonTests(unittest.TestCase):
    def test_sums_weeks_places_and_all_play(self):
        cats = [{"stat_id": "hr", "sort_order": "1"}]
        week1 = [{"team_id": "1", "name": "A", "stats": {"hr": "10"}},
                 {"team_id": "2", "name": "B", "stats": {"hr": "5"}}]
        week2 = [{"team_id": "1", "name": "A (renamed)", "stats": {"hr": "1"}},
                 {"team_id": "2", "name": "B", "stats": {"hr": "1"}}]
        season = compute_season([(1, week1), (2, week2)], cats)

        a = next(t for t in season if t["team_id"] == "1")
        self.assertEqual(a["name"], "A (renamed)")
        self.assertEqual(a["weekly"], {"1": {"total": 2, "place": 1}, "2": {"total": 1.5, "place": 1}})
        self.assertEqual(a["total"], 3.5)
        self.assertEqual(a["average"], 1.75)
        self.assertEqual(a["all_play"], {"wins": 1, "losses": 0, "ties": 1})
        self.assertEqual(season[0]["team_id"], "1")


class LeagueParsingTests(unittest.TestCase):
    def test_league_meta(self):
        data = {"fantasy_content": {"league": [
            {"league_key": "458.l.1", "name": "Dads", "season": "2025", "num_teams": 12,
             "current_week": "9", "start_week": "1", "end_week": "25", "is_finished": 0},
            {"settings": {}},
        ]}}
        meta = parse_league_meta(data)
        self.assertEqual(meta["name"], "Dads")
        self.assertEqual((meta["start_week"], meta["current_week"], meta["end_week"]), (1, 9, 25))
        self.assertFalse(meta["is_finished"])

    def test_game_key(self):
        data = {"fantasy_content": {"games": {"0": {"game": [
            {"game_key": "431", "code": "mlb", "season": "2024"}]}, "count": 1}}}
        self.assertEqual(parse_game_key(data), "431")

    def test_game_key_missing(self):
        self.assertIsNone(parse_game_key({"fantasy_content": {"games": []}}))


class SaveTokensTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "config.txt"
        patcher = mock.patch.object(app_module, "CONFIG_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.dir.cleanup)

    def test_updates_tokens_and_keeps_comments(self):
        self.path.write_text(
            "; Yahoo Developer App credentials\n"
            "[yahoo]\n"
            "client_id = abc\n"
            "; Filled in automatically\n"
            "access_token =\n"
            "refresh_token =\n"
            "\n"
            "; Flask session secret\n"
            "[flask]\n"
            "secret_key = s3cret\n"
        )
        save_tokens("AT%1", "RT2")
        text = self.path.read_text()
        self.assertIn("; Yahoo Developer App credentials", text)
        self.assertIn("; Filled in automatically", text)
        self.assertIn("; Flask session secret", text)
        # "%" must round-trip: configparser interpolation would reject it.
        self.assertEqual(get_credentials()["access_token"], "AT%1")
        self.assertEqual(get_credentials()["refresh_token"], "RT2")
        self.assertEqual(get_credentials()["client_id"], "abc")

    def test_adds_missing_keys_inside_yahoo_section(self):
        self.path.write_text("[yahoo]\nclient_id = abc\n\n[flask]\nsecret_key = x\n")
        save_tokens("AT", "RT")
        creds = get_credentials()
        self.assertEqual((creds["access_token"], creds["refresh_token"]), ("AT", "RT"))
        self.assertLess(self.path.read_text().index("access_token"), self.path.read_text().index("[flask]"))

    def test_creates_file_when_missing(self):
        save_tokens("AT", "RT")
        self.assertEqual(get_credentials()["access_token"], "AT")


# ── API routes, with Yahoo replaced by canned responses ────────────────────────

def _settings(current_week="3", is_finished=0):
    return {"fantasy_content": {"league": [
        {"league_key": "mlb.l.1", "name": "Test League", "season": "2026", "num_teams": 2,
         "current_week": current_week, "start_week": "1", "end_week": "25",
         "is_finished": is_finished},
        {"settings": {"stat_categories": {"stats": {
            "0": {"stat": {"stat_id": "7", "name": "Home Runs", "display_name": "HR",
                           "sort_order": "1", "enabled": "1"}},
            "1": {"stat": {"stat_id": "50", "name": "Innings Pitched", "display_name": "IP",
                           "enabled": "1", "is_only_display_stat": "1"}},
        }}}},
    ]}}


def _scoreboard(week, a_hr, b_hr):
    return {"fantasy_content": {"league": [
        {"league_key": "mlb.l.1"},
        {"scoreboard": {"week": str(week), "0": {"matchups": {
            "0": {"matchup": {"teams": {
                "0": ParseTeamsTests().team("Team A", "1", {"7": a_hr, "50": "40.0"}),
                "1": ParseTeamsTests().team("Team B", "2", {"7": b_hr, "50": "60.0"}),
            }}},
        }}}},
    ]}}


class ApiTests(unittest.TestCase):
    def setUp(self):
        app_module._cache.clear()
        self.client = app_module.app.test_client()
        self.calls = []
        self.responses = {
            "/league/mlb.l.1/settings": _settings(),
            "/league/mlb.l.1/scoreboard;week=1": _scoreboard(1, "5", "3"),
            "/league/mlb.l.1/scoreboard;week=2": _scoreboard(2, "1", "3"),
            "/league/mlb.l.1/scoreboard;week=3": _scoreboard(3, "2", "2"),
        }

        def fake_get(path):
            self.calls.append(path)
            return self.responses[path]

        patcher = mock.patch.object(app_module, "yahoo_get", side_effect=fake_get)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_rejects_non_numeric_ids(self):
        for qs in ("league_id=1/../2&week=1", "league_id=1&week=1;x", "league_id=1&week=1&season=20x5"):
            res = self.client.get(f"/api/rankings?{qs}")
            self.assertEqual(res.status_code, 400, qs)
        self.assertEqual(self.calls, [])

    def test_rankings_skip_display_stats_and_include_all_play(self):
        res = self.client.get("/api/rankings?league_id=1&week=1")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual([c["display_name"] for c in data["stat_categories"]], ["HR"])
        top = data["teams"][0]
        self.assertEqual((top["name"], top["total"]), ("Team A", 2))
        self.assertEqual(top["all_play"], {"wins": 1, "losses": 0, "ties": 0})
        self.assertEqual(data["league"]["name"], "Test League")

    def test_week_outside_schedule_is_rejected(self):
        self.assertEqual(self.client.get("/api/rankings?league_id=1&week=26").status_code, 400)
        # the current week is 3, so week 4 hasn't happened yet
        res = self.client.get("/api/rankings?league_id=1&week=4")
        self.assertEqual(res.status_code, 400)
        self.assertIn("current week is 3", res.get_json()["error"])

    def test_settings_and_finished_weeks_are_cached(self):
        for _ in range(2):
            self.client.get("/api/rankings?league_id=1&week=1")  # finished week
            self.client.get("/api/rankings?league_id=1&week=3")  # still in progress
        self.assertEqual(self.calls.count("/league/mlb.l.1/settings"), 1)
        self.assertEqual(self.calls.count("/league/mlb.l.1/scoreboard;week=1"), 1)
        self.assertEqual(self.calls.count("/league/mlb.l.1/scoreboard;week=3"), 2)

    def test_season_view(self):
        res = self.client.get("/api/season?league_id=1&start_week=1&end_week=3")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["weeks"], [1, 2, 3])
        b = next(t for t in data["teams"] if t["name"] == "Team B")
        self.assertEqual(b["total"], 1 + 2 + 1.5)
        self.assertEqual(b["all_play"], {"wins": 1, "losses": 1, "ties": 1})

    def test_season_range_must_be_in_order(self):
        res = self.client.get("/api/season?league_id=1&start_week=3&end_week=1")
        self.assertEqual(res.status_code, 400)

    def test_past_season_uses_that_years_game_key(self):
        self.responses["/games;game_codes=mlb;seasons=2024"] = {"fantasy_content": {"games": {
            "0": {"game": [{"game_key": "431"}]}, "count": 1}}}
        self.responses["/league/431.l.1/settings"] = _settings(current_week="25", is_finished=1)
        res = self.client.get("/api/league?league_id=1&season=2024")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["is_finished"])
        self.assertIn("/league/431.l.1/settings", self.calls)

    def test_not_authenticated(self):
        with mock.patch.object(app_module, "yahoo_get", side_effect=PermissionError("nope")):
            res = self.client.get("/api/league?league_id=1")
        self.assertEqual(res.status_code, 401)
        self.assertTrue(res.get_json()["needs_auth"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
