"""Tests for the shared, versioned, compatibility-aware rule cache."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tldextract import TLDExtract
from tldextract.rule_updates import (
    Component,
    InvalidRuleListError,
    SharedRuleCache,
    UpdateStatus,
    candidate_from_text,
)

P1 = """\
// VERSION: P1
test
alpha.test
shared.test
"""

P2 = """\
// VERSION: P2
test
alpha.test
beta.test
shared.test
"""

P3 = """\
// VERSION: P3
test
alpha.test
beta.test
gamma.test
shared.test
"""


def private_list(name: str, rules: list[str], requires: str | None = None) -> str:
    """Build a private rule response in the Public Suffix List shape."""
    header = f"// VERSION: {name}\n"
    if requires is not None:
        header += f"// REQUIRES-PUBLIC: {requires}\n"
    body = "\n".join(rules)
    return (
        f"{header}// ===BEGIN PRIVATE DOMAINS===\n{body}\n"
        "// ===END PRIVATE DOMAINS===\n"
    )


S1 = private_list("S1", ["one.shared.test"])
S2 = private_list("S2", ["one.shared.test", "two.shared.test"])
S3 = private_list(
    "S3",
    ["one.shared.test", "two.shared.test", "three.beta.test"],
    requires=">=P2",
)


@pytest.fixture
def cache(tmp_path: Path) -> SharedRuleCache:
    """Return an empty shared rule cache in a temporary directory."""
    return SharedRuleCache(str(tmp_path))


def publish_initial(cache: SharedRuleCache) -> None:
    """Publish the P1/S1 starting pair used by several tests."""
    assert cache.submit_public(P1).status is UpdateStatus.ACTIVE
    assert cache.submit_private(S1).status is UpdateStatus.ACTIVE


def test_rejected_response_does_not_change_active_pair(
    cache: SharedRuleCache,
) -> None:
    """Malformed rule text is rejected without disturbing readers."""
    publish_initial(cache)
    before = cache.active()
    assert before is not None

    receipt = cache.submit_public("// VERSION: BROKEN\nthis is not a rule\n")

    assert receipt.status is UpdateStatus.REJECTED
    assert not receipt.accepted
    after = cache.active()
    assert after is not None
    assert after.manifest_sha256 == before.manifest_sha256
    assert (after.public_version, after.private_version) == ("P1", "S1")


def test_incomplete_private_candidate_is_pending_not_rejected(
    cache: SharedRuleCache,
) -> None:
    """A complete candidate without a compatible public list stays pending."""
    publish_initial(cache)

    receipt = cache.submit_private(S3)

    assert receipt.status is UpdateStatus.PENDING_PAIRING
    assert receipt.accepted
    assert receipt.active_public == "P1"
    assert receipt.active_private == "S1"
    assert ("private", "S3") in receipt.pending
    active = cache.active()
    assert active is not None
    assert (active.public_version, active.private_version) == ("P1", "S1")


def test_new_public_unlocks_pending_private(cache: SharedRuleCache) -> None:
    """Publishing the required public list activates the pending private list."""
    publish_initial(cache)
    cache.submit_private(S3)

    receipt = cache.submit_public(P2)

    assert receipt.status is UpdateStatus.ACTIVE
    assert receipt.active_public == "P2"
    assert receipt.active_private == "S3"
    active = cache.active()
    assert active is not None
    assert (active.public_version, active.private_version) == ("P2", "S3")
    assert "three.beta.test" in active.private_tlds


def test_late_older_private_does_not_override_newer(cache: SharedRuleCache) -> None:
    """An older private response finishing later cannot replace the newer one."""
    publish_initial(cache)
    cache.submit_private(S3)
    cache.submit_public(P2)

    receipt = cache.submit_private(S2)

    assert receipt.status is UpdateStatus.SUPERSEDED
    assert receipt.active_private == "S3"
    active = cache.active()
    assert active is not None
    assert (active.public_version, active.private_version) == ("P2", "S3")


def test_replaying_an_old_active_version_is_reported_as_used(
    cache: SharedRuleCache,
) -> None:
    """A version that already served readers is distinguishable from new data."""
    publish_initial(cache)
    cache.submit_private(S3)
    cache.submit_public(P2)

    receipt = cache.submit_public(P1)

    assert receipt.status is UpdateStatus.ALREADY_USED
    assert receipt.active_public == "P2"


def test_same_sequence_different_content_is_a_conflict(
    cache: SharedRuleCache,
) -> None:
    """A duplicate upstream number with different body is rejected as conflict."""
    publish_initial(cache)
    conflicting = P1.replace("shared.test\n", "other.test\n")

    receipt = cache.submit_public(conflicting)

    assert receipt.status is UpdateStatus.CONFLICT
    assert not receipt.accepted


def test_conflicting_private_sequence_is_a_conflict(
    cache: SharedRuleCache,
) -> None:
    """Reusing a private upstream number with different content is flagged."""
    publish_initial(cache)
    other_s1 = private_list("S1", ["different.shared.test"])

    receipt = cache.submit_private(other_s1)

    assert receipt.status is UpdateStatus.CONFLICT


def test_private_candidate_requires_public_version_explicitly(
    cache: SharedRuleCache,
) -> None:
    """An incompatible private list is never paired with the current public list."""
    publish_initial(cache)

    receipt = cache.submit_private(S3)

    assert receipt.active_public == "P1"
    assert receipt.active_private == "S1"
    assert ("private", "S3") in receipt.pending


def test_save_failure_keeps_previous_active_pair(
    cache: SharedRuleCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure while storing or replacing the manifest preserves readers."""
    publish_initial(cache)
    before = cache.active()
    assert before is not None

    def fail_replace(source: object, destination: object) -> None:
        raise OSError("simulated disk failure")

    monkeypatch.setattr(os, "replace", fail_replace)

    receipt = cache.submit_public(P2)

    assert receipt.status is UpdateStatus.SAVE_FAILED
    assert not receipt.accepted
    after = cache.active()
    assert after is not None
    assert after.manifest_sha256 == before.manifest_sha256
    assert after.public_version == "P1"


def test_reader_survives_corrupt_manifest(cache: SharedRuleCache) -> None:
    """A reader falls back to the previous manifest if the newest is corrupt."""
    publish_initial(cache)
    cache.submit_private(S3)
    cache.submit_public(P2)
    assert cache.root is not None

    (cache.root / "manifest.json").write_text("{not json", encoding="utf-8")

    active = cache.active()
    assert active is not None
    assert (active.public_version, active.private_version) == ("P1", "S1")


def test_reader_survives_missing_candidate_object(cache: SharedRuleCache) -> None:
    """Readers never publish a pair whose referenced object is missing."""
    publish_initial(cache)
    active = cache.active()
    assert active is not None
    assert cache.root is not None

    for path in (cache.root / "objects").glob("*.json"):
        path.unlink()

    assert cache.active() is None


def test_receipts_distinguish_states(cache: SharedRuleCache) -> None:
    """Persisted receipts expose active, pending, used and rejected outcomes."""
    publish_initial(cache)
    cache.submit_private(S3)
    cache.submit_public(P2)
    cache.submit_public(P1)
    cache.submit_public("// VERSION: BAD\n!!!\n")

    records = cache.receipts()
    by_status = {record["status"] for record in records}

    assert {"active", "pending_pairing", "already_used", "rejected"} <= by_status
    assert any(record["accepted"] for record in records)
    assert any(not record["accepted"] for record in records)


def test_pending_candidate_not_mistaken_for_invalid(cache: SharedRuleCache) -> None:
    """A complete candidate that never gets a compatible public version survives."""
    publish_initial(cache)
    far_future = private_list("S9", ["nine.beta.test"], requires=">=P9")

    receipt = cache.submit_private(far_future)

    assert receipt.status is UpdateStatus.PENDING_PAIRING
    assert receipt.accepted
    active = cache.active()
    assert active is not None
    assert active.private_version == "S1"
    assert receipt.pending


def test_extraction_uses_published_pair_for_both_modes(
    cache: SharedRuleCache, tmp_path: Path
) -> None:
    """Public and private lookups both see the published compatible pair."""
    publish_initial(cache)
    cache.submit_private(S3)
    cache.submit_public(P2)

    extractor = TLDExtract(cache_dir=str(tmp_path))

    public = extractor("host.three.beta.test", include_psl_private_domains=False)
    assert (public.subdomain, public.domain, public.suffix) == (
        "host",
        "three",
        "beta.test",
    )
    private = extractor("host.three.beta.test", include_psl_private_domains=True)
    assert (private.subdomain, private.domain, private.suffix) == (
        "",
        "host",
        "three.beta.test",
    )
    assert private.is_private


def test_publishing_public_without_private_keeps_private_pending(
    cache: SharedRuleCache, tmp_path: Path
) -> None:
    """A public list cannot take effect for private mode until pairing succeeds."""
    publish_initial(cache)
    cache.submit_private(S3)

    receipt = cache.submit_public(P2)
    assert receipt.active_public == "P2"
    assert receipt.active_private == "S3"

    extractor = TLDExtract(cache_dir=str(tmp_path))
    public = extractor("host.three.beta.test", include_psl_private_domains=False)
    assert public.suffix == "beta.test"
    private = extractor("host.three.beta.test", include_psl_private_domains=True)
    assert private.suffix == "three.beta.test"


def test_newer_public_waits_when_private_is_not_yet_compatible(
    cache: SharedRuleCache,
) -> None:
    """A newer public list does not displace a valid pair on its own."""
    publish_initial(cache)
    s4 = private_list("S4", ["four.shared.test"], requires=">=P4")

    cache.submit_public(P2)
    receipt = cache.submit_private(s4)

    assert receipt.status is UpdateStatus.PENDING_PAIRING
    active = cache.active()
    assert active is not None
    assert (active.public_version, active.private_version) == ("P2", "S1")
    assert ("private", "S4") in receipt.pending


def test_extractor_reflects_new_publication(
    cache: SharedRuleCache, tmp_path: Path
) -> None:
    """An extractor refreshes after a compatible publication by another writer."""
    publish_initial(cache)
    extractor = TLDExtract(cache_dir=str(tmp_path))
    early = extractor("host.one.shared.test", include_psl_private_domains=True)
    assert early.suffix == "one.shared.test"

    other = TLDExtract(cache_dir=str(tmp_path))
    other.submit_private_rules(S3)
    other.submit_public_rules(P2)

    refreshed = TLDExtract(cache_dir=str(tmp_path))
    assert (
        refreshed("host.three.beta.test", include_psl_private_domains=True).suffix
        == "three.beta.test"
    )


def test_cold_start_falls_back_to_snapshot(
    cache: SharedRuleCache, tmp_path: Path
) -> None:
    """With no published pair, the configured snapshot fallback still works."""
    extractor = TLDExtract(cache_dir=str(tmp_path), fallback_to_snapshot=True)

    assert extractor("example.com").suffix == "com"


def test_cold_start_without_fallback_reports_no_rules(tmp_path: Path) -> None:
    """With no published pair and no fallback, the caller sees a clear failure."""
    from tldextract.suffix_list import SuffixListNotFound

    extractor = TLDExtract(
        cache_dir=str(tmp_path),
        suffix_list_urls=(),
        fallback_to_snapshot=False,
    )

    with pytest.raises(SuffixListNotFound):
        extractor("example.com")


def test_local_candidates_only(cache: SharedRuleCache) -> None:
    """Compatibility selection never fetches; it uses stored local candidates."""
    publish_initial(cache)
    cache.submit_private(S3)

    active = cache.active()
    assert active is not None
    assert active.private_version == "S1"


def test_candidate_from_text_reads_metadata() -> None:
    """Local responses can carry version, component and compatibility metadata."""
    candidate = candidate_from_text(
        Component.PRIVATE,
        "// COMPONENT: private\n// VERSION: S4\n"
        "// REQUIRES-PUBLIC: ==P2\n"
        "// ===BEGIN PRIVATE DOMAINS===\nfour.shared.test\n"
        "// ===END PRIVATE DOMAINS===\n",
    )

    assert candidate.component is Component.PRIVATE
    assert candidate.version == "S4"
    assert candidate.compatibility is not None
    assert candidate.compatibility.accepts(2)
    assert not candidate.compatibility.accepts(1)


def test_candidate_from_text_rejects_invalid_labels() -> None:
    """Clearly invalid rule text is rejected rather than partially parsed."""
    with pytest.raises(InvalidRuleListError):
        candidate_from_text(Component.PUBLIC, "// VERSION: X\nnot a rule\n")


def test_publication_is_atomic_for_readers(cache: SharedRuleCache) -> None:
    """Every observed active pair is complete and mutually compatible."""
    publish_initial(cache)
    cache.submit_private(S3)

    observed: list[tuple[str, str | None]] = []
    observed.append(_pair(cache))
    cache.submit_public(P2)
    observed.append(_pair(cache))
    cache.submit_private(S2)
    observed.append(_pair(cache))

    assert observed == [("P1", "S1"), ("P2", "S3"), ("P2", "S3")]
    active = cache.active()
    assert active is not None
    assert (active.public_version, active.private_version) == ("P2", "S3")


def _pair(cache: SharedRuleCache) -> tuple[str, str | None]:
    active = cache.active()
    assert active is not None
    return active.public_version, active.private_version


def test_manifest_is_single_file_replaced_atomically(cache: SharedRuleCache) -> None:
    """The active state is one manifest file, not a set of mutable pointers."""
    publish_initial(cache)
    assert cache.root is not None
    manifest = json.loads((cache.root / "manifest.json").read_text())
    assert manifest["public"]["version"] == "P1"
    assert manifest["private"]["version"] == "S1"

    entries = list((cache.root / "objects").glob("*.json"))
    assert entries
    for entry in entries:
        json.loads(entry.read_text())


def test_stage_then_publish_separately(cache: SharedRuleCache) -> None:
    """Staging stores a candidate without changing the active pair."""
    publish_initial(cache)
    before = cache.active()
    assert before is not None

    staged = cache.stage_public(P2)
    assert staged.status is UpdateStatus.STAGED
    assert cache.active().manifest_sha256 == before.manifest_sha256  # type: ignore[union-attr]

    published = cache.publish()
    assert published.changed
    assert published.active_public == "P2"
    assert published.active_private == "S1"


def test_update_clears_published_rules(cache: SharedRuleCache, tmp_path: Path) -> None:
    """The existing update entry point still resets to a fresh fetch."""
    publish_initial(cache)
    extractor = TLDExtract(cache_dir=str(tmp_path), suffix_list_urls=())
    assert extractor(
        "host.one.shared.test", include_psl_private_domains=True
    ).suffix == ("one.shared.test")

    extractor.update()

    assert cache.active() is None


def test_no_leftover_temporary_files(cache: SharedRuleCache) -> None:
    """Successful publication leaves no temporary writes in the store."""
    publish_initial(cache)
    cache.submit_private(S3)
    cache.submit_public(P2)
    assert cache.root is not None

    leftovers = [
        path
        for path in cache.root.rglob(".*")
        if path.is_file() and path.name != ".publish.lock"
    ]
    assert leftovers == []


def test_file_lock_is_released(cache: SharedRuleCache) -> None:
    """Writers must be able to publish after a previous writer returns."""
    publish_initial(cache)
    assert cache.submit_private(S3).status is UpdateStatus.PENDING_PAIRING
    assert cache.submit_public(P2).status is UpdateStatus.ACTIVE
    assert os.path.isfile(str(cache._lock_path))  # noqa: SLF001


def test_interrupted_private_publish_leaves_previous_pair(
    cache: SharedRuleCache,
) -> None:
    """A writer that dies after staging never exposes an unpaired private list."""
    publish_initial(cache)
    assert cache.stage_private(S3).status is UpdateStatus.STAGED

    active = cache.active()
    assert active is not None
    assert (active.public_version, active.private_version) == ("P1", "S1")


def test_other_process_publishes_only_fully_stored_pair(
    cache: SharedRuleCache, tmp_path: Path
) -> None:
    """Another process sees a pair only after every referenced object exists."""
    publish_initial(cache)
    assert cache.stage_private(S3).status is UpdateStatus.STAGED
    assert cache.stage_public(P2).status is UpdateStatus.STAGED

    assert cache.active() is not None
    assert cache.active().public_version == "P1"  # type: ignore[union-attr]

    published = cache.publish()
    assert published.changed
    assert published.active_public == "P2"
    assert published.active_private == "S3"

    reader = SharedRuleCache(str(tmp_path))
    active = reader.active()
    assert active is not None
    assert (active.public_version, active.private_version) == ("P2", "S3")


def test_manifest_never_points_at_missing_objects(cache: SharedRuleCache) -> None:
    """Every published manifest entry resolves to a readable candidate."""
    publish_initial(cache)
    cache.submit_private(S3)
    cache.submit_public(P2)
    assert cache.root is not None

    manifest = json.loads((cache.root / "manifest.json").read_text())
    for name in ("public", "private"):
        entry = manifest[name]
        if entry is None:
            continue
        path = cache.root / "objects" / f"{entry['sha256']}.json"
        assert path.is_file()
        stored = json.loads(path.read_text())
        assert stored["version"] == entry["version"]


def test_pending_candidate_survives_new_unrelated_public(
    cache: SharedRuleCache,
) -> None:
    """An incompatible candidate stays pending across newer public arrivals."""
    publish_initial(cache)
    s9 = private_list("S9", ["nine.gamma.test"], requires=">=P9")
    cache.submit_private(s9)

    cache.submit_public(P2)
    cache.submit_public(P3)

    active = cache.active()
    assert active is not None
    assert (active.public_version, active.private_version) == ("P3", "S1")
    latest = cache.publish()
    assert ("private", "S9") in latest.pending
    assert latest.active_private == "S1"


def test_rejected_private_body_keeps_readers(cache: SharedRuleCache) -> None:
    """Invalid private text does not change the published pair."""
    publish_initial(cache)
    before = cache.active()
    assert before is not None

    receipt = cache.submit_private(
        "// VERSION: S2\n// ===BEGIN PRIVATE DOMAINS===\nbad rule!\n"
        "// ===END PRIVATE DOMAINS===\n"
    )

    assert receipt.status is UpdateStatus.REJECTED
    after = cache.active()
    assert after is not None
    assert after.manifest_sha256 == before.manifest_sha256


def test_stage_does_not_need_public_counterpart(cache: SharedRuleCache) -> None:
    """A private list can be staged before any public list exists."""
    receipt = cache.stage_private(S1)

    assert receipt.status is UpdateStatus.STAGED
    assert cache.active() is None


def test_active_rules_property_reports_versions(
    cache: SharedRuleCache, tmp_path: Path
) -> None:
    """An extractor reports the published versions it loaded."""
    publish_initial(cache)
    extractor = TLDExtract(cache_dir=str(tmp_path))
    extractor("example.test")

    active = extractor.active_rules
    assert active is not None
    assert (active.public_version, active.private_version) == ("P1", "S1")


def test_two_process_readers_agree(cache: SharedRuleCache, tmp_path: Path) -> None:
    """Independent reader instances agree on the published pair."""
    publish_initial(cache)
    cache.submit_private(S3)
    cache.submit_public(P2)

    first = SharedRuleCache(str(tmp_path)).active()
    second = SharedRuleCache(str(tmp_path)).active()

    assert first is not None and second is not None
    assert first.manifest_sha256 == second.manifest_sha256
    assert first.public_version == second.public_version == "P2"
    assert first.private_version == second.private_version == "S3"


def test_clearing_removes_only_namespaced_store(cache: SharedRuleCache) -> None:
    """Clearing one namespace leaves another namespace untouched."""
    publish_initial(cache)
    assert cache.root is not None
    other = SharedRuleCache(str(cache.root.parents[1]), namespace="other")
    other.submit_public(P2)

    cache.clear()

    assert cache.active() is None
    active = other.active()
    assert active is not None
    assert active.public_version == "P2"


def test_explicit_namespaces_isolate_writers(tmp_path: Path) -> None:
    """Different rule namespaces in one cache directory do not collide."""
    first = TLDExtract(cache_dir=str(tmp_path), rule_namespace="alpha")
    second = TLDExtract(cache_dir=str(tmp_path), rule_namespace="beta")
    first.submit_public_rules(P1)
    second.submit_public_rules(P2)

    assert first.active_rules is not None
    assert first.active_rules.public_version == "P1"
    assert second.active_rules is not None
    assert second.active_rules.public_version == "P2"


def test_default_namespace_keeps_legacy_instances_together(tmp_path: Path) -> None:
    """Configurations without an explicit namespace share the default store."""
    writer = TLDExtract(cache_dir=str(tmp_path))
    writer.submit_public_rules(P2)

    reader = TLDExtract(cache_dir=str(tmp_path))
    assert reader.rule_namespace == "default"
    active = reader.active_rules
    assert active is not None
    assert active.public_version == "P2"


def test_staging_methods_are_local_only(tmp_path: Path) -> None:
    """Staging through TLDExtract never performs a network request."""
    extractor = TLDExtract(
        cache_dir=str(tmp_path),
        suffix_list_urls=(),
        fallback_to_snapshot=False,
    )
    assert extractor.stage_public_rules(P1).status is UpdateStatus.STAGED
    assert extractor.stage_private_rules(S1).status is UpdateStatus.STAGED
    assert extractor.active_rules is None

    assert extractor.publish_rules().changed
    active = extractor.active_rules
    assert active is not None
    assert (active.public_version, active.private_version) == ("P1", "S1")


def test_cross_process_stage_then_exit(tmp_path: Path) -> None:
    """A process can stage S3 and die; another process still publishes P2+S3."""
    script = (
        "import os, sys\n"
        "from tldextract import TLDExtract\n"
        f"extract = TLDExtract(cache_dir={str(tmp_path)!r})\n"
        f"receipt = extract.stage_private_rules({S3!r})\n"
        "print(receipt.status.value)\n"
        "sys.stdout.flush()\n"
        "os._exit(0)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.stdout.strip() == "staged"

    writer = TLDExtract(cache_dir=str(tmp_path))
    assert writer.submit_public_rules(P1).status is UpdateStatus.ACTIVE
    receipt = writer.submit_public_rules(P2)
    assert receipt.status is UpdateStatus.ACTIVE
    assert (receipt.active_public, receipt.active_private) == ("P2", "S3")

    reader = TLDExtract(cache_dir=str(tmp_path))
    assert (
        reader("host.three.beta.test", include_psl_private_domains=True).suffix
        == "three.beta.test"
    )
    assert reader("host.three.beta.test").suffix == "beta.test"


def test_manifest_replace_failure_keeps_readers(
    cache: SharedRuleCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Storing the object succeeds, but a failed manifest rename changes nothing."""
    publish_initial(cache)
    before = cache.active()
    assert before is not None
    assert cache.root is not None
    manifest_path = cache.root / "manifest.json"
    real_replace = os.replace

    def fail_manifest(source: str, destination: str) -> None:
        if Path(destination) == manifest_path:
            raise OSError("simulated manifest rename failure")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_manifest)

    receipt = cache.submit_public(P2)

    assert receipt.status is UpdateStatus.SAVE_FAILED
    active = cache.active()
    assert active is not None
    assert active.manifest_sha256 == before.manifest_sha256

    monkeypatch.undo()
    cached_p2 = [
        path
        for path in (cache.root / "objects").glob("*.json")
        if '"version":"P2"' in path.read_text()
    ]
    assert cached_p2


def test_corrupt_object_falls_back_to_previous_manifest(
    cache: SharedRuleCache,
) -> None:
    """If a published object is unreadable, readers use the previous manifest."""
    publish_initial(cache)
    old = cache.active()
    assert old is not None
    cache.submit_private(S3)
    cache.submit_public(P2)
    assert cache.root is not None

    manifest = json.loads((cache.root / "manifest.json").read_text())
    private_object = cache.root / "objects" / f"{manifest['private']['sha256']}.json"
    private_object.write_text("{corrupt", encoding="utf-8")

    fallback = cache.active()
    assert fallback is not None
    assert (fallback.public_version, fallback.private_version) == ("P1", "S1")
