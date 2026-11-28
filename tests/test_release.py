# mypy: disallow_untyped_defs=False
import copy
import json
import os
import tempfile
import unittest

from extruct import extract as extruct_extract
from extruct.release import (
    ExtractionBudgetExceeded,
    PageRenderer,
    ReleaseManifest,
    document_base,
    plan_release,
)

OLD = "https://old.example/"
NEW = "https://new.example/"


def page_a_template():
    return (
        '<!DOCTYPE html><html><head><base href="https://old.example/">'
        '<script type="application/ld+json">'
        '{"@context":{"@base":"https://old.example/"},"@type":"Product",'
        '"sku":"a","url":"offer/a"}</script></head>'
        '<body vocab="http://schema.org/">'
        '<div typeof="Product"><span property="sku">a</span>'
        '<a property="url" href="offer/a">offer</a></div>'
        '</body></html>'
    )


def page_a_new_absolute():
    return (
        '<!DOCTYPE html><html><head><base href="https://old.example/">'
        '<script type="application/ld+json">'
        '{"@context":{"@base":"https://old.example/"},"@type":"Product",'
        '"sku":"a","url":"https://new.example/offer/a"}</script></head>'
        '<body vocab="http://schema.org/">'
        '<div typeof="Product"><span property="sku">a</span>'
        '<a property="url" href="https://new.example/offer/a">offer</a>'
        '</div></body></html>'
    )


def page_b_template():
    return (
        '<!DOCTYPE html><html><head>'
        '<script type="application/ld+json">'
        '{"@context":"https://schema.org","@type":"Product","sku":"b",'
        '"url":"https://inv.example/keep/b"}</script></head>'
        '<body vocab="http://schema.org/">'
        '<div typeof="Product"><span property="sku">b</span>'
        '<a property="url" href="https://inv.example/keep/b">x</a>'
        '</div></body></html>'
    )


def base_manifest():
    return {
        "extraction_limit": 2000,
        "pages": [
            {
                "id": "A",
                "url": "https://shop.example/pages/a",
                "syntaxes": ["json-ld", "rdfa"],
                "template": page_a_template(),
                "slots": {
                    "head-base": '<base href="https://old.example/">',
                    "jsonld-base": '"@base":"https://old.example/"',
                },
            },
            {
                "id": "B",
                "url": "https://shop.example/pages/b",
                "syntaxes": ["json-ld", "rdfa"],
                "template": page_b_template(),
            },
        ],
        "fields": [
            {
                "site": "s1",
                "page": "A",
                "sku": "a",
                "kind": "migrating",
                "old_value": "https://old.example/offer/a",
                "new_value": "https://new.example/offer/a",
            },
            {
                "site": "s1",
                "page": "B",
                "sku": "b",
                "kind": "retained",
                "old_value": "https://inv.example/keep/b",
            },
        ],
        "units": [
            {
                "id": "H",
                "versions": [
                    {
                        "id": "H1",
                        "pages": [
                            {
                                "page": "A",
                                "fragments": {
                                    "head-base": '<base href="https://new.example/">'
                                },
                            }
                        ],
                    }
                ],
            },
            {
                "id": "J",
                "versions": [
                    {
                        "id": "J1",
                        "pages": [
                            {
                                "page": "A",
                                "fragments": {
                                    "jsonld-base": '"@base":"https://new.example/"'
                                },
                            }
                        ],
                    }
                ],
            },
            {
                "id": "K",
                "versions": [
                    {
                        "id": "K1",
                        "pages": [
                            {"page": "A", "content": page_a_new_absolute()},
                            {"page": "B", "content": page_b_template()},
                        ],
                    }
                ],
            },
        ],
    }


class TestDocumentBase(unittest.TestCase):
    def test_falls_back_to_final_url(self):
        html = "<html><head></head><body>x</body></html>"
        self.assertEqual(
            document_base("https://shop.example/p", html),
            "https://shop.example/p",
        )

    def test_uses_first_valid_base_href(self):
        html = (
            '<html><head><base><base href="https://old.example/dir/">'
            '<base href="https://ignored.example/"></head></html>'
        )
        self.assertEqual(
            document_base("https://shop.example/p", html),
            "https://old.example/dir/",
        )


class TestReferenceResolution(unittest.TestCase):
    def test_jsonld_inline_base_object_id_and_nested_override(self):
        html = (
            '<html><head><base href="https://old.example/dir/">'
            '<script type="application/ld+json">'
            '{"@context":{"@base":"https://old.example/dir/"},'
            '"@type":"Product","sku":"a",'
            '"sameAs":{"@id":"offer/a"},'
            '"offers":[{"@context":{"@base":"https://old.example/dir/sub/"},'
            '"@type":"Offer","sku":"c","url":"offer/c"}]}</script></head>'
            "<body></body></html>"
        )
        state = PageRenderer().render(
            "A", html, "https://shop.example/a", ("json-ld",)
        )
        self.assertEqual(
            state.readings[("json-ld", "a", "sameAs")],
            ["https://old.example/dir/offer/a"],
        )
        self.assertEqual(
            state.readings[("json-ld", "c", "url")],
            ["https://old.example/dir/sub/offer/c"],
        )

    def test_jsonld_falls_back_to_document_base(self):
        html = (
            '<html><head><base href="https://old.example/dir/">'
            '<script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"Product",'
            '"sku":"a","url":"offer/a"}</script></head><body></body></html>'
        )
        state = PageRenderer().render(
            "A", html, "https://shop.example/a", ("json-ld",)
        )
        self.assertEqual(
            state.readings[("json-ld", "a", "url")],
            ["https://old.example/dir/offer/a"],
        )

    def test_rdfa_uses_native_resource_identifiers_not_literal_text(self):
        html = (
            '<html><head></head><body vocab="http://schema.org/">'
            '<div typeof="Product"><span property="sku">a</span>'
            '<a property="url" href="offer/a">offer</a>'
            '<p property="description">inventory stays at '
            "https://old.example/offer/a in prose</p></div></body></html>"
        )
        state = PageRenderer().render(
            "A", html, "https://old.example/page", ("rdfa",)
        )
        self.assertEqual(
            state.readings[("rdfa", "a", "url")],
            ["https://old.example/offer/a"],
        )
        self.assertNotIn(("rdfa", "a", "description"), state.readings)

    def test_microdata_resolves_against_document_base(self):
        html = (
            '<html><body><div itemscope itemtype="http://schema.org/Offer">'
            '<span itemprop="sku">a</span>'
            '<a itemprop="url" href="offer/a">x</a></div></body></html>'
        )
        state = PageRenderer().render(
            "A", html, "https://old.example/dir/page", ("microdata",)
        )
        self.assertEqual(
            state.readings[("microdata", "a", "url")],
            ["https://old.example/dir/offer/a"],
        )

    def test_nested_offer_keeps_its_own_sku_in_jsonld(self):
        html = (
            '<html><head><base href="https://old.example/dir/">'
            '<script type="application/ld+json">'
            '{"@context":{"@base":"https://old.example/dir/"},'
            '"@type":"Product","sku":"a","url":"product/a",'
            '"offers":[{"@type":"Offer","sku":"c","url":"offer/c"}]}'
            "</script></head><body></body></html>"
        )
        state = PageRenderer().render(
            "A", html, "https://shop.example/a", ("json-ld",)
        )
        self.assertEqual(
            state.readings[("json-ld", "a", "url")],
            ["https://old.example/dir/product/a"],
        )
        self.assertEqual(
            state.readings[("json-ld", "c", "url")],
            ["https://old.example/dir/offer/c"],
        )
        # The nested offer URL must not be attributed to the parent SKU.
        self.assertNotIn(("json-ld", "a", "offers"), state.readings)

    def test_nested_microdata_offer_inherits_parent_sku(self):
        html = (
            '<html><body><div itemscope itemtype="http://schema.org/Product">'
            '<span itemprop="sku">a</span>'
            '<div itemprop="offers" itemscope '
            'itemtype="http://schema.org/Offer">'
            '<a itemprop="url" href="offer/a">x</a></div></div></body></html>'
        )
        state = PageRenderer().render(
            "A", html, "https://old.example/dir/page", ("microdata",)
        )
        self.assertEqual(
            state.readings[("microdata", "a", "url")],
            ["https://old.example/dir/offer/a"],
        )


class TestReleasePlanning(unittest.TestCase):
    def test_publishes_unit_k_alone_and_republishes_a_and_b(self):
        verdict = plan_release(ReleaseManifest(base_manifest()))
        self.assertTrue(verdict["safe"])
        self.assertEqual(len(verdict["batches"]), 1)
        self.assertEqual(verdict["batches"][0]["units"], {"K": "K1"})
        self.assertEqual(
            verdict["batches"][0]["republished_pages"], ["A", "B"]
        )
        self.assertEqual(verdict["republished_pages"], ["A", "B"])
        self.assertEqual(verdict["republished_page_count"], 2)

    def test_rejects_h_and_j_even_in_one_batch_with_mixed_readings(self):
        data = base_manifest()
        data["units"] = [
            unit for unit in data["units"] if unit["id"] in ("H", "J")
        ]
        verdict = plan_release(ReleaseManifest(data))
        self.assertFalse(verdict["safe"])
        self.assertEqual(verdict["batches"], [])
        reasons = [blocker["reason"] for blocker in verdict["blockers"]]
        self.assertTrue(
            any("different addresses" in reason for reason in reasons)
        )
        readings = [
            blocker.get("readings")
            for blocker in verdict["blockers"]
            if blocker.get("readings")
        ]
        flattened = {
            value
            for reading in readings
            for per in reading.values()
            for value in per
        }
        self.assertEqual(
            flattened,
            {
                "https://old.example/offer/a",
                "https://new.example/offer/a",
            },
        )

    def test_two_units_need_separate_batches_when_each_half_switches(self):
        # Page A carries two slots switched by two different units. Each unit
        # alone produces a mixed-address page, so neither can go first: the
        # release is impossible rather than silently batched together.
        data = base_manifest()
        data["units"] = [
            unit for unit in data["units"] if unit["id"] in ("H", "J")
        ]
        verdict = plan_release(ReleaseManifest(data))
        self.assertFalse(verdict["safe"])
        active = [
            tuple(sorted(blocker.get("active_versions", {}).items()))
            for blocker in verdict["blockers"]
        ]
        self.assertIn((("H", "H1"),), active)
        self.assertIn((("J", "J1"),), active)

    def test_retained_sku_change_blocks_release(self):
        data = base_manifest()
        broken_b = page_b_template().replace(
            "https://inv.example/keep/b", "https://new.example/keep/b"
        )
        k_unit = next(unit for unit in data["units"] if unit["id"] == "K")
        k_page_b = next(
            page for page in k_unit["versions"][0]["pages"] if page["page"] == "B"
        )
        k_page_b["content"] = broken_b
        verdict = plan_release(ReleaseManifest(data))
        self.assertFalse(verdict["safe"])
        self.assertTrue(
            any(
                blocker["page"] == "B"
                and blocker["sku"] == "b"
                and "retained SKU changed" in blocker["reason"]
                for blocker in verdict["blockers"]
            )
        )

    def test_extraction_is_deduplicated_per_distinct_render(self):
        data = base_manifest()
        manifest = ReleaseManifest(data)
        planner_renderer = PageRenderer(limit=2000)
        from extruct.release import ReleasePlanner

        planner = ReleasePlanner(manifest)
        planner.renderer = planner_renderer
        planner.plan()
        # Baseline A/B, H on A, J on A, K on A, K on B: six distinct renders.
        self.assertEqual(planner_renderer.extraction_count, 6)
        # Re-rendering an identical combination costs nothing.
        before = planner_renderer.extraction_count
        planner.render_selected("A", {"K": manifest.units["K"][0]})
        self.assertEqual(planner_renderer.extraction_count, before)

    def test_independent_sites_share_no_extractions(self):
        data = base_manifest()
        # A second, disconnected site with its own unit and extra versions.
        data["pages"].append(
            {
                "id": "C",
                "url": "https://other.example/c",
                "syntaxes": ["json-ld"],
                "template": (
                    '<html><head><script type="application/ld+json">'
                    '{"@context":"https://schema.org","@type":"Product",'
                    '"sku":"z","url":"https://old.example/z"}'
                    "</script></head><body></body></html>"
                ),
                "slots": {
                    "z-url": '"url":"https://old.example/z"',
                },
            }
        )
        data["fields"].append(
            {
                "site": "s2",
                "page": "C",
                "sku": "z",
                "kind": "migrating",
                "old_value": "https://old.example/z",
                "new_value": "https://new.example/z",
            }
        )
        data["units"].append(
            {
                "id": "Z",
                "versions": [
                    {
                        "id": "Z1",
                        "pages": [
                            {
                                "page": "C",
                                "fragments": {
                                    "z-url": '"url":"https://new.example/z"'
                                },
                            }
                        ],
                    },
                    {
                        "id": "Z2",
                        "pages": [
                            {
                                "page": "C",
                                "fragments": {
                                    "z-url": '"url":"https://new.example/z"'
                                },
                            }
                        ],
                    },
                ],
            }
        )
        baseline = plan_release(ReleaseManifest(copy.deepcopy(data)))
        # Adding another approved version on the disconnected site must not
        # change any extraction performed for the original site's pages.
        data["units"][-1]["versions"].append(
            {
                "id": "Z3",
                "pages": [
                    {
                        "page": "C",
                        "fragments": {
                            "z-url": '"url":"https://new.example/z"'
                        },
                    }
                ],
            }
        )
        extended = plan_release(ReleaseManifest(copy.deepcopy(data)))
        self.assertEqual(
            baseline["extraction_count"], extended["extraction_count"]
        )
        self.assertTrue(extended["safe"])

    def test_extraction_budget_is_enforced(self):
        data = base_manifest()
        data["extraction_limit"] = 2
        with self.assertRaises(ExtractionBudgetExceeded):
            plan_release(ReleaseManifest(data))

    def test_prefers_atomic_unit_over_unit_that_mixes_a_page(self):
        # U2 switches every R reference atomically and is safe alone; U1 only
        # switches one slot and mixes the page. The planner must pick U2 and
        # republish just R, never U1.
        page_r = (
            '<!DOCTYPE html><html><head><base href="https://old.example/">'
            '<script type="application/ld+json">'
            '{"@context":{"@base":"https://old.example/"},"@type":"Product",'
            '"sku":"r","url":"offer/r"}</script></head>'
            '<body vocab="http://schema.org/">'
            '<div typeof="Product"><span property="sku">r</span>'
            '<a property="url" href="offer/r">r</a></div>'
            "</body></html>"
        )
        data = {
            "extraction_limit": 2000,
            "pages": [
                {
                    "id": "R",
                    "url": "https://shop.example/pages/r",
                    "syntaxes": ["json-ld", "rdfa"],
                    "template": page_r,
                    "slots": {
                        "r-head": '<base href="https://old.example/">',
                        "r-jsonld": '"@base":"https://old.example/"',
                    },
                }
            ],
            "fields": [
                {
                    "site": "s1",
                    "page": "R",
                    "sku": "r",
                    "kind": "migrating",
                    "old_value": "https://old.example/offer/r",
                    "new_value": "https://new.example/offer/r",
                }
            ],
            # U2 switches both R slots atomically and stays safe on its own;
            # U1 switches a single slot and therefore mixes the page.
            "units": [
                {
                    "id": "U2",
                    "versions": [
                        {
                            "id": "U2v1",
                            "pages": [
                                {
                                    "page": "R",
                                    "fragments": {
                                        "r-head": '<base href="https://new.example/">',
                                        "r-jsonld": '"@base":"https://new.example/"',
                                    },
                                }
                            ],
                        }
                    ],
                },
                {
                    "id": "U1",
                    "versions": [
                        {
                            "id": "U1v1",
                            "pages": [
                                {
                                    "page": "R",
                                    "fragments": {
                                        "r-head": '<base href="https://new.example/">'
                                    },
                                }
                            ],
                        }
                    ],
                },
            ],
        }
        verdict = plan_release(ReleaseManifest(data))
        self.assertTrue(verdict["safe"])
        self.assertEqual(len(verdict["batches"]), 1)
        self.assertEqual(verdict["batches"][0]["units"], {"U2": "U2v1"})
        self.assertEqual(verdict["republished_pages"], ["R"])

    def test_cli_returns_nonzero_and_lists_blockers(self):
        from extruct.release import main

        data = base_manifest()
        data["units"] = [
            unit for unit in data["units"] if unit["id"] in ("H", "J")
        ]
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = os.path.join(directory, "manifest.json")
            output_path = os.path.join(directory, "verdict.json")
            with open(manifest_path, "w", encoding="UTF-8") as handle:
                json.dump(data, handle)
            code = main([manifest_path, "--output", output_path])
            self.assertEqual(code, 1)
            with open(output_path, encoding="UTF-8") as handle:
                verdict = json.load(handle)
        self.assertFalse(verdict["safe"])
        self.assertTrue(verdict["blockers"])

    def test_manifest_validation_reports_missing_migration_target(self):
        data = base_manifest()
        del data["fields"][0]["new_value"]
        with self.assertRaises(ValueError):
            ReleaseManifest(data)

    def test_search_picks_one_unit_when_two_own_full_page_content(self):
        done = page_a_new_absolute()
        data = base_manifest()
        data["units"] = [
            {
                "id": "X",
                "versions": [
                    {
                        "id": "X1",
                        "pages": [
                            {"page": "A", "content": done},
                            {"page": "B", "content": page_b_template()},
                        ],
                    }
                ],
            },
            {
                "id": "Y",
                "versions": [
                    {
                        "id": "Y1",
                        "pages": [
                            {"page": "A", "content": done},
                            {"page": "B", "content": page_b_template()},
                        ],
                    }
                ],
            },
        ]
        verdict = plan_release(ReleaseManifest(data))
        self.assertTrue(verdict["safe"])
        self.assertEqual(len(verdict["batches"][0]["units"]), 1)
        self.assertEqual(verdict["republished_pages"], ["A", "B"])

    def test_extract_call_is_unchanged_by_the_release_module(self):
        # Importing the release module must not alter the existing extract API.
        import extruct.release  # noqa: F401

        html = page_a_template()
        result = extruct_extract(
            html,
            base_url="https://old.example/",
            syntaxes=["json-ld", "rdfa"],
        )
        self.assertEqual(
            result["json-ld"][0]["url"], "offer/a"
        )
        rdfa_urls = [
            node["http://schema.org/url"]
            for node in result["rdfa"]
            if "http://schema.org/url" in node
        ]
        self.assertEqual(
            rdfa_urls,
            [[{"@id": "https://old.example/offer/a"}]],
        )


if __name__ == "__main__":
    unittest.main()
