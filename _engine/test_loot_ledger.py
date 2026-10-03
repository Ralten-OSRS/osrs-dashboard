"""Regression test: the Loot Log's rows add up to its headline total."""

import json
from pathlib import Path
import tempfile
import unittest

from osrs_dashboard import build_html, scan_screenshots


class LootLedgerTests(unittest.TestCase):
    def test_completed_assemblies_are_rows_in_the_ledger(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            drops = Path(temp_dir) / "Valuable Drops"
            drops.mkdir()
            (drops / "Valuable drop Bandos hilt (17,534,292 coins) 2025-10-01_14-28-40.png").write_bytes(b"fixture")
            logs = Path(temp_dir) / "Collection Log"
            logs.mkdir()
            component = "Collection Log/Collection log (Ancient icon) 2023-01-16_11-04-52.png"
            (Path(temp_dir) / component).write_bytes(b"fixture")
            economic_value = {
                "events": [{
                    "label": "Ancient essence",
                    "value": 500,
                    "completed_at": "2023-01-16T11:04:52",
                    "evidence": [{"item": "Ancient icon", "rel_path": component}],
                }],
                "realized_total": 500,
                "pending": [],
                "latent_total": 0,
                "prices_updated_at": None,
                "prices_stale": True,
            }

            html = build_html(scan_screenshots(temp_dir), economic_value=economic_value)

        marker = "const DROPS = "
        rows, _ = json.JSONDecoder().raw_decode(html, html.index(marker) + len(marker))
        self.assertEqual(sum(row["value"] for row in rows), 17_534_292 + 500)
        assembled = [row for row in rows if row["kind"] == "assembled"]
        self.assertEqual(len(assembled), 1)
        self.assertEqual(assembled[0]["item"], "Ancient essence")
        self.assertEqual(assembled[0]["shots"], [{"src": component, "label": "Ancient icon"}])
        self.assertIn("1 drop and 1 completed assembly", html)


if __name__ == "__main__":
    unittest.main()
