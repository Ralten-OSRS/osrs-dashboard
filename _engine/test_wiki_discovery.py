"""Regression tests for safe Wiki-backed item classification."""

import unittest

from wiki_discovery import classify_item


def _page(*, tradeable, categories=(), products=()):
    tradeable_text = "Yes" if tradeable else "No"
    product_rows = ""
    if products:
        product_rows = "<h2 id='Products'>Products</h2><table>"
        product_rows += "<tr><th>Product</th><th>Materials</th></tr>"
        for output, materials in products:
            material_html = "".join(
                f"<li>{qty} <a href='/w/{item.replace(' ', '_')}'>{item}</a></li>"
                for item, qty in materials.items()
            )
            product_rows += (
                "<tr><td><a href='/w/" + output.replace(" ", "_") + "'>" + output
                + "</a></td><td><ul>" + material_html + "</ul></td></tr>"
            )
        product_rows += "</table>"
    return {
        "html": f"<table><tr><th>Tradeable</th><td>{tradeable_text}</td></tr></table>{product_rows}",
        "categories": list(categories),
        "wikitext": " ".join(categories),
    }


class WikiDiscoveryTests(unittest.TestCase):
    def _classify(self, seed, pages):
        return classify_item(seed, fetch_page=lambda item: pages[item])

    def test_soulreaper_component_enrols_exact_assembly(self):
        materials = {
            "Eye of the duke": 1,
            "Fragment of seren": 1,
            "Soulreaper component C": 1,
            "Soulreaper component D": 1,
        }
        pages = {item: _page(tradeable=False) for item in materials}
        pages["Eye of the duke"] = _page(
            tradeable=False, products=[("Soulreaper axe", materials)]
        )
        pages["Soulreaper axe"] = _page(tradeable=True)
        recipe, classification, _reason = self._classify("Eye of the duke", pages)
        self.assertEqual(classification, "assembly")
        self.assertEqual(recipe["output"], "Soulreaper axe")
        self.assertEqual(recipe["components"], materials)

    def test_abyssal_bludgeon_component_enrols_exact_assembly(self):
        materials = {"Bludgeon axon": 1, "Bludgeon spine": 1, "Bludgeon claw": 1}
        pages = {item: _page(tradeable=False) for item in materials}
        pages["Bludgeon axon"] = _page(
            tradeable=False, products=[("Abyssal bludgeon", materials)]
        )
        pages["Abyssal bludgeon"] = _page(tradeable=True)
        recipe, classification, _reason = self._classify("Bludgeon axon", pages)
        self.assertEqual(classification, "assembly")
        self.assertEqual(recipe["output"], "Abyssal bludgeon")

    def test_noxious_halberd_component_enrols_exact_assembly(self):
        materials = {"Noxious point": 1, "Noxious blade": 1, "Noxious pommel": 1}
        pages = {item: _page(tradeable=False) for item in materials}
        pages["Noxious blade"] = _page(
            tradeable=False, products=[("Noxious halberd", materials)]
        )
        pages["Noxious halberd"] = _page(tradeable=True)
        recipe, classification, _reason = self._classify("Noxious blade", pages)
        self.assertEqual(classification, "assembly")
        self.assertEqual(recipe["output"], "Noxious halberd")

    def test_clue_reward_is_permanently_excluded(self):
        pages = {"Ancient platebody": _page(tradeable=True, categories=("Treasure Trails",))}
        recipe, classification, reason = self._classify("Ancient platebody", pages)
        self.assertIsNone(recipe)
        self.assertEqual(classification, "excluded")
        self.assertIn("Treasure Trails", reason)

    def test_tradeable_direct_reward_stays_unpriced(self):
        pages = {"Abyssal dagger": _page(tradeable=True)}
        recipe, classification, _reason = self._classify("Abyssal dagger", pages)
        self.assertIsNone(recipe)
        self.assertEqual(classification, "direct_unpriced")

    def test_multiple_valid_products_remain_ambiguous(self):
        pages = {
            "Crystal shard": _page(
                tradeable=False,
                products=[
                    ("Product A", {"Crystal shard": 1}),
                    ("Product B", {"Crystal shard": 1}),
                ],
            ),
            "Product A": _page(tradeable=True),
            "Product B": _page(tradeable=True),
        }
        recipe, classification, _reason = self._classify("Crystal shard", pages)
        self.assertIsNone(recipe)
        self.assertEqual(classification, "ambiguous")


if __name__ == "__main__":
    unittest.main()
