"""Tests for sparse, versioned suffix list lookups."""

from tldextract.tldextract import _PublicSuffixListTLDExtractor
from tldextract.versioned import (
    PreparedSuffixList,
    Section,
    SuffixListStore,
    add_private,
    add_public,
    move_rule,
    remove_private,
    remove_public,
)

BASE_PUBLIC = ["com", "ck", "*.ck", "uk", "co.uk", "platform.example"]
BASE_PRIVATE = ["blogspot.com", "!www.ck", "apps.platform.example"]

HOSTNAMES = [
    "com",
    "www.com",
    "ck",
    "www.ck",
    "a.www.ck",
    "b.ck",
    "uk",
    "co.uk",
    "x.co.uk",
    "blogspot.com",
    "waiterrant.blogspot.com",
    "platform.example",
    "apps.platform.example",
    "a.apps.platform.example",
    "alpha.com",
    "www.alpha.com",
    "apps.alpha.com",
    "unknown.zz",
]


def _assert_equivalent_to_full_read(
    prepared: PreparedSuffixList,
    public: set[str],
    private: set[str],
    hostnames: list[str],
) -> None:
    """Assert a version answers exactly like a standalone full-list read."""
    reference = _PublicSuffixListTLDExtractor(sorted(public), sorted(private), [])
    for include_private in (False, True):
        for hostname in hostnames:
            labels = hostname.split(".")
            expected = reference.suffix_index(labels, include_private)
            got = prepared.suffix_index(labels, include_private)
            assert (got is None) == (expected is None), (hostname, include_private)
            if got is None or expected is None:
                continue
            (expected_suffix_idx, expected_node), (expected_reg_idx, _) = expected
            (got_suffix_idx, got_node), (got_reg_idx, _) = got
            assert (got_suffix_idx, got_node.is_private, got_reg_idx) == (
                expected_suffix_idx,
                expected_node.is_private,
                expected_reg_idx,
            ), (hostname, include_private)


def test_sparse_changes_match_full_reload() -> None:
    """Sparse updates, including section moves and exception deletion, match a full reload."""
    store = SuffixListStore()
    baseline, load_stats = store.load_full(BASE_PUBLIC, BASE_PRIVATE)
    assert load_stats.rules_accessed == len(BASE_PUBLIC) + len(BASE_PRIVATE)
    assert baseline.rule_count == len(BASE_PUBLIC) + len(BASE_PRIVATE)

    changes = [
        *move_rule("blogspot.com", Section.PRIVATE, Section.PUBLIC),
        remove_private("!www.ck"),
        add_public("alpha.com"),
        add_private("apps.alpha.com"),
        remove_public("co.uk"),
    ]
    revised, stats = store.apply(baseline, changes)
    assert stats.rules_accessed == len(changes)

    expected_public = set(BASE_PUBLIC) - {"co.uk"} | {"blogspot.com", "alpha.com"}
    expected_private = set(BASE_PRIVATE) - {"blogspot.com", "!www.ck"} | {
        "apps.alpha.com"
    }
    assert revised.rule_count == len(expected_public) + len(expected_private)

    # Interleave reads of the revised version and the pre-change original.
    revised_prepared = store.prepare(revised)
    baseline_prepared = store.prepare(baseline)
    for hostname in HOSTNAMES:
        _assert_equivalent_to_full_read(
            revised_prepared, expected_public, expected_private, [hostname]
        )
        _assert_equivalent_to_full_read(
            baseline_prepared, set(BASE_PUBLIC), set(BASE_PRIVATE), [hostname]
        )


def test_parallel_branches_converge_and_share_prepared_lookup() -> None:
    """Parallel branches stay readable and converging paths share prepared results."""
    store = SuffixListStore()
    baseline, _ = store.load_full(BASE_PUBLIC, BASE_PRIVATE)
    baseline_prepared = store.prepare(baseline)

    # Two parallel branches from the same parent, each changing one public
    # and one private rule.
    branch_a, _ = store.apply(
        baseline, [add_public("alpha.com"), remove_private("!www.ck")]
    )
    branch_b, _ = store.apply(
        baseline,
        move_rule("apps.platform.example", Section.PRIVATE, Section.PUBLIC),
    )

    public_a = set(BASE_PUBLIC) | {"alpha.com"}
    private_a = set(BASE_PRIVATE) - {"!www.ck"}
    public_b = set(BASE_PUBLIC) | {"apps.platform.example"}
    private_b = set(BASE_PRIVATE) - {"apps.platform.example"}

    # Interleaved reads across both branches, exceptions, and the original.
    for hostname in HOSTNAMES:
        _assert_equivalent_to_full_read(
            store.prepare(branch_a), public_a, private_a, [hostname]
        )
        _assert_equivalent_to_full_read(
            store.prepare(branch_b), public_b, private_b, [hostname]
        )
        _assert_equivalent_to_full_read(
            baseline_prepared, set(BASE_PUBLIC), set(BASE_PRIVATE), [hostname]
        )

    # The unchanged branch is never re-prepared.
    assert store.prepare(baseline) is baseline_prepared

    # Branch A continues, reaching branch B's exact rules in a different order.
    converged, stats = store.apply(
        branch_a,
        [
            add_private("!www.ck"),
            *move_rule("apps.platform.example", Section.PRIVATE, Section.PUBLIC),
            remove_public("alpha.com"),
        ],
    )
    assert stats.nodes_created < 20
    assert converged == branch_b
    assert converged.version_id == branch_b.version_id
    assert store.prepare(converged) is store.prepare(branch_b)

    # The baseline is still intact and equivalent after all of the above.
    _assert_equivalent_to_full_read(
        baseline_prepared, set(BASE_PUBLIC), set(BASE_PRIVATE), HOSTNAMES
    )


def test_add_then_remove_returns_identical_version() -> None:
    """Adding then removing a rule returns the exact original, shared version."""
    store = SuffixListStore()
    baseline, _ = store.load_full(BASE_PUBLIC, BASE_PRIVATE)

    extended, _ = store.apply(baseline, [add_public("temp.example")])
    restored, _ = store.apply(extended, [remove_public("temp.example")])

    assert restored == baseline
    assert restored.version_id == baseline.version_id
    assert store.prepare(restored) is store.prepare(baseline)


def test_temporary_private_override_applies_per_lookup() -> None:
    """A private-domain override affects only the lookup it is passed to."""
    store = SuffixListStore()
    baseline, _ = store.load_full(BASE_PUBLIC, BASE_PRIVATE)
    prepared = store.prepare(baseline)

    default = prepared.host_keys("waiterrant.blogspot.com")
    overridden = prepared.host_keys(
        "waiterrant.blogspot.com", include_psl_private_domains=True
    )

    assert default is not None
    assert default.tenant_key == "blogspot.com"
    assert default.organization_key == "blogspot.com"
    assert not default.is_private

    assert overridden is not None
    assert overridden.tenant_key == "waiterrant.blogspot.com"
    assert overridden.organization_key == "blogspot.com"
    assert overridden.is_private

    # The override does not stick to the version or its prepared lookup.
    assert prepared.host_keys("waiterrant.blogspot.com") == default
    assert store.prepare(baseline) is prepared


def test_host_keys_for_unknown_suffix() -> None:
    """Hostnames with no known suffix have no tenant or organization keys."""
    store = SuffixListStore()
    baseline, _ = store.load_full(BASE_PUBLIC, BASE_PRIVATE)
    prepared = store.prepare(baseline)

    assert prepared.host_keys("unknown.zz") is None
    assert prepared.host_keys("unknown.zz", include_psl_private_domains=True) is None


def test_sparse_update_stats_do_not_scale_with_list_size() -> None:
    """Sparse updates touch a handful of rules and nodes, never the whole list."""
    store = SuffixListStore()
    public = [f"tenant{i}.example{i % 100}" for i in range(20_000)]
    private = [f"app{i}.hosted{i % 50}" for i in range(5_000)]

    baseline, load_stats = store.load_full(public, private)
    assert load_stats.rules_accessed == 25_000
    assert load_stats.nodes_created > 20_000
    assert baseline.rule_count == 25_000

    changes = [
        add_public("new.example1"),
        remove_private("app0.hosted0"),
        add_private("staff.example5"),
        remove_public("tenant6.example6"),
    ]
    revised, stats = store.apply(baseline, changes)
    assert stats.rules_accessed == len(changes)
    assert stats.nodes_visited < 100
    assert stats.nodes_created < 100
    assert revised.rule_count == 25_000

    expected_public = (set(public) | {"new.example1"}) - {"tenant6.example6"}
    expected_private = (set(private) | {"staff.example5"}) - {"app0.hosted0"}
    spot_checks = [
        "new.example1",
        "x.new.example1",
        "app0.hosted0",
        "www.app1.hosted1",
        "staff.example5",
        "x.staff.example5",
        "tenant6.example6",
        "tenant106.example6",
    ]
    _assert_equivalent_to_full_read(
        store.prepare(revised), expected_public, expected_private, spot_checks
    )

    # Re-applying an already-present rule builds nothing and yields the
    # identical version; unchanged branches are not re-read or re-enumerated.
    same, noop_stats = store.apply(revised, [add_public("new.example1")])
    assert noop_stats.nodes_created == 0
    assert same == revised
    assert same.version_id == revised.version_id
    assert store.prepare(same) is store.prepare(revised)
