import datetime as dt

import pytest

from bigtree.workflows import app_product
from bigtree.workflows.app_product import ProductHierarchy

DAY_1 = dt.date(2026, 3, 1)
DAY_2 = dt.date(2026, 3, 2)


def build_conflict_hierarchy() -> ProductHierarchy:
    hierarchy = ProductHierarchy()
    hierarchy.add_observation("North", DAY_1, "SKU7", "Outlet", "Old name")
    hierarchy.add_observation("South", DAY_1, "SKU7", "Flagship", "Old name")
    hierarchy.add_observation("North", DAY_2, "SKU7", "Flagship", "Day 2 name")
    hierarchy.add_observation("South", DAY_2, "SKU7", "Flagship", "Day 2 name")
    hierarchy.add_decision("SKU7", "Outlet", DAY_1, DAY_2)
    return hierarchy


class TestObservationSelection:
    def test_latest_complete_observation_per_department_wins(self):
        hierarchy = ProductHierarchy()
        hierarchy.add_observation(
            "North",
            DAY_1,
            "SKU1",
            "Old store",
            "Old name",
            arrived_at="2026-03-01T08:00:00",
        )
        hierarchy.add_observation(
            "North",
            DAY_1,
            "SKU1",
            "New store",
            "New name",
            arrived_at="2026-03-01T12:00:00",
        )
        result = hierarchy.export_day(DAY_1)
        row = result.row_by_sku("SKU1")
        assert row.parent == "New store"
        assert row.name == "New name"
        assert row.status == app_product.STATUS_PUBLISHED

    def test_incomplete_observation_ignored(self):
        hierarchy = ProductHierarchy()
        hierarchy.add_observation("North", DAY_1, "SKU1", "Store", "Name")
        hierarchy.add_observation("North", DAY_1, "SKU1", "Other", None, complete=True)
        result = hierarchy.export_day(DAY_1)
        row = result.row_by_sku("SKU1")
        assert row.parent == "Store"
        assert row.name == "Name"

    def test_consensus_needs_no_decision(self):
        hierarchy = ProductHierarchy()
        hierarchy.add_observation("North", DAY_1, "SKU1", "Store", "Name")
        hierarchy.add_observation("South", DAY_1, "SKU1", "Store", "Name")
        result = hierarchy.export_day(DAY_1)
        row = result.row_by_sku("SKU1")
        assert row.status == app_product.STATUS_PUBLISHED
        assert row.parent == "Store"
        assert row.supported_by == ("North", "South")
        assert row.decision_parent is None
        assert result.disputes == []

    def test_conflict_without_decision_is_blocked(self):
        hierarchy = ProductHierarchy()
        hierarchy.add_observation("North", DAY_1, "SKU1", "Outlet", "Name")
        hierarchy.add_observation("South", DAY_1, "SKU1", "Flagship", "Name")
        result = hierarchy.export_day(DAY_1)
        row = result.row_by_sku("SKU1")
        assert row.status == app_product.STATUS_BLOCKED
        assert row.path is None
        assert row.warehouse_key is None
        assert result.blocked_rows == [row]
        assert [dispute.kind for dispute in result.disputes] == [
            "parent_conflict",
            "unresolved_conflict",
        ]


class TestDecisionAndFreeze:
    def test_decision_selects_parent_on_conflict(self):
        hierarchy = build_conflict_hierarchy()
        result = hierarchy.export_day(DAY_1)
        row = result.row_by_sku("SKU7")
        assert row.status == app_product.STATUS_PUBLISHED
        assert row.parent == "Outlet"
        assert row.supported_by == ("North",)
        assert row.decision_parent == "Outlet"

    def test_consensus_overrides_conflicting_decision_for_later_day(self):
        hierarchy = build_conflict_hierarchy()
        result = hierarchy.export_day(DAY_2)
        row = result.row_by_sku("SKU7")
        assert row.status == app_product.STATUS_PUBLISHED
        assert row.parent == "Flagship"
        assert row.name == "Day 2 name"
        assert row.supported_by == ("North", "South")

    def test_correction_freezes_published_decision_value(self):
        hierarchy = build_conflict_hierarchy()
        first = hierarchy.export_day(DAY_1)
        assert (first.row_by_sku("SKU7").parent, first.row_by_sku("SKU7").status) == (
            "Outlet",
            app_product.STATUS_PUBLISHED,
        )

        hierarchy.add_observation("North", DAY_1, "SKU7", "Flagship", "N7")
        corrected = hierarchy.export_day(DAY_1)
        row = corrected.row_by_sku("SKU7")
        assert row.status == app_product.STATUS_FROZEN
        assert row.frozen
        assert row.parent == "Outlet"
        assert row.name == "Old name"
        assert row.path == "/Catalog/2026-03-01/Outlet/SKU7"
        assert row.decision_parent == "Outlet"
        assert row.decision_supported is False
        assert row.candidates == ("Flagship",)
        assert row.supported_by == ()
        assert "no longer has any supporting observation" in row.reason
        assert corrected.status == app_product.STATUS_FROZEN

    def test_correction_keeps_original_dispute_evidence(self):
        hierarchy = build_conflict_hierarchy()
        hierarchy.export_day(DAY_1)
        hierarchy.add_observation("North", DAY_1, "SKU7", "Flagship", "N7")
        result = hierarchy.export_day(DAY_1)
        dispute = next(
            dispute
            for dispute in result.disputes
            if dispute.kind == "published_value_unsupported"
        )
        assert dispute.published_parent == "Outlet"
        assert dispute.candidates == ("Flagship",)
        assert dispute.selected_parent == "Flagship"
        assert dispute.decision is not None
        assert dispute.decision.parent == "Outlet"
        assert [obs.department for obs in dispute.observations] == [
            "North",
            "South",
            "North",
        ]
        assert [obs.parent for obs in dispute.observations] == [
            "Outlet",
            "Flagship",
            "Flagship",
        ]

    def test_later_day_still_publishes_supported_value_after_correction(self):
        hierarchy = build_conflict_hierarchy()
        hierarchy.export_day(DAY_1)
        hierarchy.export_day(DAY_2)
        hierarchy.add_observation("North", DAY_1, "SKU7", "Flagship", "N7")
        hierarchy.export_day(DAY_1)
        result = hierarchy.export_day(DAY_2)
        row = result.row_by_sku("SKU7")
        assert row.status == app_product.STATUS_PUBLISHED
        assert row.parent == "Flagship"
        assert row.name == "Day 2 name"

    def test_new_decision_releases_freeze(self):
        hierarchy = build_conflict_hierarchy()
        hierarchy.export_day(DAY_1)
        hierarchy.add_observation("North", DAY_1, "SKU7", "Flagship", "N7")
        frozen = hierarchy.export_day(DAY_1)
        assert frozen.row_by_sku("SKU7").status == app_product.STATUS_FROZEN

        hierarchy.add_decision("SKU7", "Flagship", DAY_1, DAY_2)
        released = hierarchy.export_day(DAY_1)
        row = released.row_by_sku("SKU7")
        assert row.status == app_product.STATUS_PUBLISHED
        assert row.parent == "Flagship"
        assert row.name == "N7"
        assert row.decision_parent == "Flagship"
        assert row.decision_supported is True
        assert not row.frozen
        assert any("freeze released" in warning for warning in released.warnings)

    def test_decision_without_support_cannot_release_freeze(self):
        hierarchy = build_conflict_hierarchy()
        hierarchy.export_day(DAY_1)
        hierarchy.add_observation("North", DAY_1, "SKU7", "Flagship", "N7")
        hierarchy.export_day(DAY_1)
        hierarchy.add_decision("SKU7", "Outlet", DAY_1, DAY_2)
        result = hierarchy.export_day(DAY_1)
        row = result.row_by_sku("SKU7")
        assert row.status == app_product.STATUS_FROZEN
        assert row.parent == "Outlet"
        assert row.decision_supported is False

    def test_earlier_decision_cannot_release_freeze(self):
        hierarchy = build_conflict_hierarchy()
        hierarchy.export_day(DAY_1)
        hierarchy.add_observation("North", DAY_1, "SKU7", "Flagship", "N7")
        hierarchy.export_day(DAY_1)
        hierarchy.add_decision("SKU7", "Flagship", DAY_1, DAY_2)
        hierarchy.add_decision("SKU7", "Outlet", DAY_1, DAY_2)
        result = hierarchy.export_day(DAY_1)
        row = result.row_by_sku("SKU7")
        assert row.status == app_product.STATUS_FROZEN
        assert row.decision_parent == "Outlet"

    def test_published_value_regaining_support_is_retained(self):
        hierarchy = build_conflict_hierarchy()
        hierarchy.export_day(DAY_1)
        hierarchy.add_observation("North", DAY_1, "SKU7", "Flagship", "N7")
        assert (
            hierarchy.export_day(DAY_1).row_by_sku("SKU7").status
            == app_product.STATUS_FROZEN
        )
        hierarchy.add_observation("South", DAY_1, "SKU7", "Outlet", "Old name")
        result = hierarchy.export_day(DAY_1)
        row = result.row_by_sku("SKU7")
        assert row.status == app_product.STATUS_PUBLISHED
        assert row.parent == "Outlet"
        assert row.supported_by == ("South",)
        assert row.decision_supported is True
        assert any("regained support" in warning for warning in result.warnings)

        hierarchy.add_decision("SKU7", "Outlet", DAY_1, DAY_2)
        released = hierarchy.export_day(DAY_1)
        row = released.row_by_sku("SKU7")
        assert row.status == app_product.STATUS_PUBLISHED
        assert row.parent == "Outlet"
        assert row.name == "Old name"

    def test_unsupported_decision_does_not_hide_supported_published_value(self):
        hierarchy = ProductHierarchy()
        hierarchy.add_observation("North", DAY_1, "SKU1", "Outlet", "Name")
        hierarchy.add_observation("South", DAY_1, "SKU1", "Flagship", "Name")
        hierarchy.add_decision("SKU1", "Outlet", DAY_1)
        first = hierarchy.export_day(DAY_1)
        assert first.row_by_sku("SKU1").parent == "Outlet"

        hierarchy.add_decision("SKU1", "Closed", DAY_1)
        result = hierarchy.export_day(DAY_1)
        row = result.row_by_sku("SKU1")
        assert row.status == app_product.STATUS_PUBLISHED
        assert row.parent == "Outlet"
        assert row.decision_parent == "Closed"
        assert row.decision_supported is False
        assert any(
            dispute.kind == "decision_unsupported" for dispute in result.disputes
        )

    def test_preview_does_not_publish(self):
        hierarchy = build_conflict_hierarchy()
        preview = hierarchy.preview_day(DAY_1)
        assert preview.row_by_sku("SKU7").status == app_product.STATUS_PUBLISHED
        assert hierarchy.publication(DAY_1, "SKU7") is None
        first_export = hierarchy.export_day(DAY_1)
        assert first_export.row_by_sku("SKU7").parent == "Outlet"

    def test_publication_accessor(self):
        hierarchy = build_conflict_hierarchy()
        hierarchy.export_day(DAY_1)
        publication = hierarchy.publication(DAY_1, "SKU7")
        assert publication is not None
        assert publication.parent == "Outlet"
        assert publication.warehouse_key == "2026-03-01/Outlet/SKU7"
        assert hierarchy.publication(DAY_2, "SKU7") is None
        assert [item.sku for item in hierarchy.publications] == ["SKU7"]


class TestWarehouseView:
    def test_duplicate_names_keep_distinct_skus(self):
        hierarchy = ProductHierarchy()
        hierarchy.add_observation("North", DAY_1, "SKU-A", "Store", "Same name")
        hierarchy.add_observation("North", DAY_1, "SKU-B", "Store", "Same name")
        result = hierarchy.export_day(DAY_1)
        rows = {row.sku: row for row in result.rows}
        assert rows["SKU-A"].name == rows["SKU-B"].name == "Same name"
        assert rows["SKU-A"].path == "/Catalog/2026-03-01/Store/SKU-A"
        assert rows["SKU-B"].path == "/Catalog/2026-03-01/Store/SKU-B"
        assert rows["SKU-A"].warehouse_key == "2026-03-01/Store/SKU-A"
        assert rows["SKU-B"].warehouse_key == "2026-03-01/Store/SKU-B"
        assert result.verify() == []
        assert result.row_by_path(rows["SKU-A"].path) == rows["SKU-A"]
        assert result.row_by_warehouse_key(rows["SKU-B"].warehouse_key) == rows["SKU-B"]

    def test_tree_and_rows_agree(self):
        hierarchy = build_conflict_hierarchy()
        result = hierarchy.export_day(DAY_2)
        assert result.verify() == []
        node = result.tree
        assert node.node_name == "Catalog"
        date_node = node.children[0]
        assert date_node.node_name == "2026-03-02"
        store_node = date_node.children[0]
        assert store_node.node_name == "Flagship"
        sku_node = store_node.children[0]
        assert sku_node.node_name == "SKU7"
        assert sku_node.get_attr("warehouse_key") == "2026-03-02/Flagship/SKU7"
        assert sku_node.get_attr("product_name") == "Day 2 name"

    def test_nested_parent_path(self):
        hierarchy = ProductHierarchy(name="Products")
        hierarchy.add_observation("North", DAY_1, "SKU1", "Region/Store", "Name")
        result = hierarchy.export_day(DAY_1)
        row = result.row_by_sku("SKU1")
        assert row.path == "/Products/2026-03-01/Region/Store/SKU1"
        assert row.warehouse_key == "2026-03-01/Region/Store/SKU1"
        assert result.verify() == []

    def test_to_dataframe(self):
        hierarchy = build_conflict_hierarchy()
        result = hierarchy.export_day(DAY_1)
        data = result.to_dataframe()
        assert list(data.columns) == [
            "business_date",
            "sku",
            "name",
            "parent",
            "path",
            "warehouse_key",
            "status",
            "supported_by",
            "candidates",
            "decision_parent",
            "decision_supported",
            "frozen",
            "reason",
        ]
        assert data.iloc[0]["sku"] == "SKU7"
        assert data.iloc[0]["status"] == "published"

    def test_dispute_tree_keeps_evidence(self):
        hierarchy = build_conflict_hierarchy()
        hierarchy.export_day(DAY_1)
        hierarchy.add_observation("North", DAY_1, "SKU7", "Flagship", "N7")
        tree = hierarchy.dispute_tree(DAY_1)
        assert tree.node_name == "Catalog Disputes"
        sku_node = tree.children[0].children[0]
        assert sku_node.node_name == "SKU7"
        labels = [child.node_name for child in sku_node.children]
        candidates_node = sku_node.children[0]
        assert candidates_node.node_name == "candidates"
        assert [child.node_name for child in candidates_node.children] == ["Flagship"]
        assert any(label.startswith("decision:") for label in labels)
        observation_labels = [label for label in labels if ":" in label]
        assert any(":North:Outlet:" in label for label in observation_labels)
        assert any(":North:Flagship:N7" in label for label in observation_labels)


class TestValidation:
    def test_invalid_name(self):
        with pytest.raises(ValueError):
            ProductHierarchy("")
        with pytest.raises(ValueError):
            ProductHierarchy("Bad/Name")

    def test_invalid_observation(self):
        hierarchy = ProductHierarchy()
        with pytest.raises(ValueError):
            hierarchy.add_observation("", DAY_1, "SKU1", "Store", "Name")
        with pytest.raises(ValueError):
            hierarchy.add_observation("North", DAY_1, "", "Store", "Name")
        with pytest.raises(ValueError):
            hierarchy.add_observation("North", DAY_1, "SKU1", "Bad//Store", "Name")
        with pytest.raises(ValueError):
            hierarchy.add_observation("North", DAY_1, "SKU1", "Store", "")
        with pytest.raises(ValueError):
            hierarchy.add_observation("North", "not-a-date", "SKU1", "Store", "Name")

    def test_invalid_decision_range(self):
        hierarchy = ProductHierarchy()
        with pytest.raises(ValueError):
            hierarchy.add_decision("SKU1", "Store", DAY_2, DAY_1)
        with pytest.raises(ValueError):
            hierarchy.add_decision("SKU1", "", DAY_1)

    def test_correction_is_recorded_even_when_conflict_disappears(self):
        hierarchy = ProductHierarchy()
        hierarchy.add_observation("North", DAY_1, "SKU1", "Old", "Old name")
        hierarchy.add_observation("North", DAY_1, "SKU1", "New", "New name")
        hierarchy.add_observation("South", DAY_1, "SKU1", "New", "New name")
        result = hierarchy.export_day(DAY_1)
        row = result.row_by_sku("SKU1")
        assert row.status == app_product.STATUS_PUBLISHED
        assert row.parent == "New"
        kinds = [dispute.kind for dispute in result.disputes]
        assert "corrected_observation" in kinds
        dispute = next(
            dispute
            for dispute in result.disputes
            if dispute.kind == "corrected_observation"
        )
        assert [observation.parent for observation in dispute.observations] == [
            "Old",
            "New",
            "New",
        ]

    def test_multiple_observations_helper(self):
        hierarchy = ProductHierarchy()
        observations = hierarchy.add_observations(
            [
                {
                    "department": "North",
                    "business_date": DAY_1,
                    "sku": "SKU1",
                    "parent": "Store",
                    "name": "Name",
                },
                {
                    "department": "South",
                    "business_date": DAY_1,
                    "sku": "SKU1",
                    "parent": "Store",
                    "name": "Name",
                },
            ]
        )
        assert len(observations) == 2
        assert hierarchy.observations == tuple(observations)

    def test_row_lookups_raise(self):
        hierarchy = ProductHierarchy()
        hierarchy.add_observation("North", DAY_1, "SKU1", "Store", "Name")
        result = hierarchy.export_day(DAY_1)
        with pytest.raises(KeyError):
            result.row_by_sku("SKU2")
        with pytest.raises(KeyError):
            result.row_by_path("/missing")
        with pytest.raises(KeyError):
            result.row_by_warehouse_key("missing")

    def test_no_rows_for_empty_day(self):
        hierarchy = ProductHierarchy()
        result = hierarchy.export_day(DAY_1)
        assert result.rows == []
        assert result.status == "empty"
        assert result.tree.node_name == "Catalog"
