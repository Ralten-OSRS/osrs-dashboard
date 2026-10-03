"""Regression tests for boss hiscores ranks: parsing, snapshots and page data."""

import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

import osrs_dashboard
from osrs_dashboard import (
    HISCORES_SKILLS,
    MAX_XP,
    build_html,
    parse_hiscores_payload,
    scan_screenshots,
    update_xp_history,
)


def _payload():
    return {
        "skills": [
            {"id": 1, "name": "Attack", "rank": 1000, "level": 99, "xp": MAX_XP},
        ],
        "activities": [
            {"id": 0, "name": "LMS - Rank", "rank": 223874, "score": 40},
            {"id": 1, "name": "Clue Scrolls (all)", "rank": 5000, "score": 12},
            {"id": 2, "name": "General Graardor", "rank": 12279, "score": 1803},
            {"id": 3, "name": "Kraken", "rank": 145851, "score": 2021},
            # Kills on record but no position on the table.
            {"id": 4, "name": "Zulrah", "rank": -1, "score": 30},
            # Below the hiscores minimum: no kills and no rank.
            {"id": 5, "name": "Vorkath", "rank": -1, "score": -1},
            # A response with no rank field at all must still parse.
            {"id": 6, "name": "Cerberus", "score": 400},
        ],
    }


def _page_json(html, marker):
    """Decode the JSON literal that follows `marker` in the generated page."""
    start = html.index(marker) + len(marker)
    value, _ = json.JSONDecoder().raw_decode(html, start)
    return value


class ParseBossRankTests(unittest.TestCase):
    def setUp(self):
        self.result = parse_hiscores_payload(_payload())

    def test_rank_is_kept_beside_the_kill_count(self):
        self.assertEqual(self.result["bosses"]["General Graardor"], 1803)
        self.assertEqual(self.result["boss_ranks"]["General Graardor"], 12279)
        self.assertEqual(self.result["boss_ranks"]["Kraken"], 145851)

    def test_unranked_and_missing_ranks_are_left_out(self):
        self.assertIn("Zulrah", self.result["bosses"])
        self.assertNotIn("Zulrah", self.result["boss_ranks"])
        self.assertIn("Cerberus", self.result["bosses"])
        self.assertNotIn("Cerberus", self.result["boss_ranks"])
        self.assertNotIn("Vorkath", self.result["boss_ranks"])

    def test_non_boss_activities_never_gain_a_rank(self):
        self.assertNotIn("LMS - Rank", self.result["boss_ranks"])
        self.assertNotIn("Clue Scrolls (all)", self.result["boss_ranks"])

    def test_every_rank_belongs_to_a_boss_with_kills(self):
        self.assertLessEqual(set(self.result["boss_ranks"]), set(self.result["bosses"]))


class SnapshotRankTests(unittest.TestCase):
    def _snapshot(self, hiscores):
        with tempfile.TemporaryDirectory() as temp_dir:
            original = osrs_dashboard.XP_HISTORY_FILE
            osrs_dashboard.XP_HISTORY_FILE = str(Path(temp_dir) / "xp_history.json")
            try:
                return update_xp_history(hiscores)[-1]
            finally:
                osrs_dashboard.XP_HISTORY_FILE = original

    def test_snapshot_records_ranks(self):
        entry = self._snapshot(parse_hiscores_payload(_payload()))

        self.assertEqual(entry["kc"]["General Graardor"], 1803)
        self.assertEqual(entry["rank"], {"General Graardor": 12279, "Kraken": 145851})

    def test_snapshot_without_ranks_keeps_its_old_shape(self):
        hiscores = parse_hiscores_payload(_payload())
        hiscores["boss_ranks"] = {}

        entry = self._snapshot(hiscores)

        self.assertNotIn("rank", entry)
        self.assertIn("kc", entry)


class GeneratedBossRankTests(unittest.TestCase):
    def _build(self, hiscores):
        with tempfile.TemporaryDirectory() as temp_dir:
            levels_dir = Path(temp_dir) / "Levels"
            levels_dir.mkdir()
            (levels_dir / "Fishing(90) 2026-02-26_12-00-00.png").write_bytes(b"fixture")
            html = build_html(scan_screenshots(temp_dir), hiscores=hiscores)
            scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, flags=re.DOTALL)
            script_path = Path(temp_dir) / "dashboard-inline.js"
            script_path.write_text(scripts[-1], encoding="utf-8")
            result = subprocess.run(
                ["node", "--check", str(script_path)],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        return html

    def _hiscores(self, **overrides):
        hiscores = {
            "skills": {skill: {"level": 99, "xp": MAX_XP} for skill in HISCORES_SKILLS},
            "clues": {},
            "bosses": {"General Graardor": 1803, "Kraken": 2021, "Zulrah": 30},
            "boss_ranks": {"General Graardor": 12279, "Kraken": 145851},
            "collections_logged": 0,
        }
        hiscores.update(overrides)
        return hiscores

    def test_directory_lists_bosses_that_have_no_screenshots(self):
        html = self._build(self._hiscores())
        directory = {card["boss"]: card for card in _page_json(html, "try { BOSS_DATA = ")}

        self.assertEqual(directory["General Graardor"]["rank"], 12279)
        self.assertEqual(directory["General Graardor"]["evidence_count"], 0)
        self.assertEqual(directory["General Graardor"]["category"], "God Wars Dungeon")
        self.assertEqual(directory["Zulrah"]["rank"], 0)

    def test_bossing_record_carries_every_boss_with_its_rank(self):
        html = self._build(self._hiscores())
        record = _page_json(html, "const FAV_BOSSES = ")

        self.assertEqual([row["boss"] for row in record], ["Kraken", "General Graardor", "Zulrah"])
        self.assertEqual([row["rank"] for row in record], [145851, 12279, 0])

    def test_hiscores_without_ranks_still_build(self):
        hiscores = self._hiscores()
        del hiscores["boss_ranks"]

        html = self._build(hiscores)
        record = _page_json(html, "const FAV_BOSSES = ")

        self.assertTrue(record)
        self.assertTrue(all(row["rank"] == 0 for row in record))


if __name__ == "__main__":
    unittest.main()
