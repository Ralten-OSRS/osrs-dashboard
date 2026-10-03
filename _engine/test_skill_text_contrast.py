"""Regression test: skill names stay readable as text on the dark cards."""

import unittest

from osrs_dashboard import SKILL_COLORS, contrast_ratio, readable_text_color


class SkillTextContrastTests(unittest.TestCase):
    def test_every_skill_name_clears_the_body_text_floor(self):
        for skill, color in SKILL_COLORS.items():
            with self.subTest(skill=skill):
                self.assertGreaterEqual(contrast_ratio(readable_text_color(color), "#14130d"), 4.5)

    def test_a_color_that_already_reads_is_left_alone(self):
        self.assertEqual(readable_text_color("#E8C84A"), "#e8c84a")

    def test_a_dark_color_is_lifted_not_replaced(self):
        lifted = readable_text_color("#6A0572")
        self.assertNotEqual(lifted, "#6a0572")
        self.assertNotEqual(lifted, "#f0e6d0")


if __name__ == "__main__":
    unittest.main()
