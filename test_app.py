"""
Tests for the ranking logic and Yahoo response parsing in app.py.

Run with:
    python -m unittest

These use only the standard library (unittest) so there's nothing to install.
Importing `app` builds the Flask object and reads config.txt but starts no
server and makes no network calls, so it's safe to import here.
"""

import unittest

from app import (
    _to_float,
    compute_rankings,
    parse_stat_categories,
    parse_teams,
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
