"""Regression tests for Maxing Journey forecast and layout contracts."""

from datetime import datetime
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

from osrs_dashboard import (
    HISCORES_SKILLS,
    MAX_XP,
    build_html,
    compute_max_forecast,
    scan_screenshots,
)


def _snapshot(date, **xp):
    return {"date": date, "xp": xp}


class MaxForecastTests(unittest.TestCase):
    def test_uses_eight_week_reduction_in_xp_remaining(self):
        history = [
            _snapshot("2026-01-01", Fishing=0, Runecraft=0),
            _snapshot("2026-02-26", Fishing=5_600_000, Runecraft=0),
        ]

        forecast = compute_max_forecast(history)

        self.assertEqual(forecast["span"], 56)
        self.assertEqual(forecast["progress"], 5_600_000)
        self.assertEqual(forecast["rate"], 100_000)
        self.assertEqual(
            forecast["remaining"],
            (MAX_XP - 5_600_000) + MAX_XP,
        )

    def test_post_99_xp_does_not_accelerate_forecast(self):
        history = [
            _snapshot("2026-01-01", Ranged=MAX_XP, Runecraft=0),
            _snapshot("2026-02-26", Ranged=MAX_XP + 8_000_000, Runecraft=5_600_000),
        ]

        forecast = compute_max_forecast(history)

        self.assertEqual(forecast["progress"], 5_600_000)
        self.assertEqual(forecast["rate"], 100_000)

    def test_eta_is_anchored_to_latest_snapshot(self):
        history = [
            _snapshot("2026-01-01", Fishing=0),
            _snapshot("2026-02-26", Fishing=5_600_000),
        ]

        forecast = compute_max_forecast(history)

        self.assertEqual(forecast["eta_days"], 74)
        self.assertEqual(forecast["eta_date"], datetime(2026, 5, 11))

    def test_requires_four_weeks_before_projecting(self):
        history = [
            _snapshot("2026-01-01", Fishing=0),
            _snapshot("2026-01-21", Fishing=2_000_000),
        ]

        forecast = compute_max_forecast(history)

        self.assertEqual(forecast["span"], 20)
        self.assertIsNone(forecast["eta_date"])


class JourneyLayoutContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = Path(__file__).with_name("osrs_dashboard.py").read_text(encoding="utf-8")

    def test_hidden_tab_width_does_not_depend_on_client_width(self):
        self.assertNotIn("map.parentElement.clientWidth", self.source)
        self.assertIn("width:max(100%,var(--journey-min-width,760px))", self.source)

    def test_all_skills_remains_the_default_map_mode(self):
        self.assertIn("let journeyMode = 'all';", self.source)


class GeneratedDashboardTests(unittest.TestCase):
    def test_synthetic_dashboard_javascript_parses(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            levels_dir = Path(temp_dir) / "Levels"
            levels_dir.mkdir()
            (levels_dir / "Fishing(90) 2026-02-26_12-00-00.png").write_bytes(b"fixture")

            data = scan_screenshots(temp_dir)
            skills = {
                skill: {"level": 99, "xp": MAX_XP}
                for skill in HISCORES_SKILLS
            }
            skills["Fishing"] = {"level": 90, "xp": 5_600_000}
            hiscores = {
                "skills": skills,
                "clues": {},
                "bosses": {},
                "collections_logged": 0,
            }
            history = [
                _snapshot("2026-01-01", **{
                    skill: (0 if skill == "Fishing" else MAX_XP)
                    for skill in HISCORES_SKILLS
                }),
                _snapshot("2026-02-26", **{
                    skill: (5_600_000 if skill == "Fishing" else MAX_XP)
                    for skill in HISCORES_SKILLS
                }),
            ]

            html = build_html(data, hiscores=hiscores, xp_history=history)
            scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, flags=re.DOTALL)

            self.assertIn("Projected Max · 56-day trend", html)
            self.assertTrue(scripts)
            script_path = Path(temp_dir) / "dashboard-inline.js"
            script_path.write_text(scripts[-1], encoding="utf-8")
            result = subprocess.run(
                ["node", "--check", str(script_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
