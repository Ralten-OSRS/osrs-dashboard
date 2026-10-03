"""Regression tests for the readable titles shown on screenshot cards."""

import unittest

from osrs_dashboard import display_title


class DisplayTitleTests(unittest.TestCase):
    def assertTitle(self, filename, category, expected):
        self.assertEqual(display_title(filename, category), expected)

    def test_timestamp_is_removed_in_every_runelite_format(self):
        self.assertTitle("Death 2026-02-28_20-54-07.png", "Deaths", "Death")
        self.assertTitle("Pet 2018-12-19 at 10.41.47 AM.png", "Pets", "Pet")
        self.assertTitle("Kingdom 2023-02-17 2023-02-17_13-36-24.png", "Kingdom Rewards", "Kingdom")
        self.assertTitle("Untradeable drop  Cache of runes 2024-01-15_13-56-04(1).png",
                         "Untradeable Drops", "Cache of runes")

    def test_category_prefix_is_dropped_because_the_card_names_it(self):
        self.assertTitle("Collection log (Golden pheasant egg) 2026-09-29_19-41-22.png",
                         "Collection Log", "Golden pheasant egg")
        self.assertTitle("Combat task (Not So Great After All) 2022-01-06_14-14-49.png",
                         "Combat Tasks", "Not So Great After All")
        self.assertTitle("Quest(Song of the Elves).png", "Quests", "Song of the Elves")
        self.assertTitle("Valuable drop 175 x Onyx bolts (e) (1,520,575 coins) 2023-12-19_10-10-10.png",
                         "Valuable Drops", "175 x Onyx bolts (e) (1,520,575 coins)")

    def test_item_names_keep_their_own_parentheses(self):
        self.assertTitle("Collection log (Master scroll book (empty)) 2023-08-06_12-38-22.png",
                         "Collection Log", "Master scroll book (empty)")

    def test_levels_and_clues_read_as_words(self):
        self.assertTitle("Firemaking(96) 2026-10-02_20-41-35.png", "Level-Ups", "Firemaking level 96")
        self.assertTitle("Farming(93).png", "Level-Ups", "Farming level 93")
        self.assertTitle("Elite(52) 2024-07-31_17-51-42.png", "Clue Scroll Rewards", "Elite clue 52")
        self.assertTitle("Tombs of Amascut Expert Mode(60) 2026-01-21_08-07-28.png",
                         "Boss Kills", "Tombs of Amascut Expert Mode (60)")

    def test_a_name_that_is_only_a_timestamp_falls_back_to_the_category(self):
        self.assertTitle("2026-10-01_15-57-01.png", "Manual Screenshots", "Manual screenshot")
        self.assertTitle("2022-02-14 (5).png", "Manual Screenshots", "Manual screenshot")
        self.assertTitle("Screenshot 2023-12-16 081723.png", "Manual Screenshots", "Manual screenshot")
        self.assertTitle("2024-03-12_12-20-10.png", "Wilderness Loot Chest", "Wilderness Loot Chest")

    def test_other_names_pass_through_untouched(self):
        self.assertTitle("Kill Somebody 2024-03-10_17-52-12.png", "PvP Kills", "Kill Somebody")
        self.assertTitle("Loot key 2024-03-12_12-20-10.png", "Wilderness Loot Chest", "Loot key")


if __name__ == "__main__":
    unittest.main()
