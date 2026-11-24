__all__ = ["ReworkResult", "build_rework"]

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .documents import (
    deep_copy,
    delete_path,
    get_object_attr,
    get_path,
    has_path,
    set_path,
)
from .entry import (
    ImportEntry,
    import_document,
)
from .influence import WILDCARD, InfluenceMap, build_influence
from .model import (
    ConflictPair,
    ConflictReport,
    Missing,
    Null,
    PatchOperation,
    Path,
    Remove,
    ReworkPatch,
    SourceChange,
    TargetExpectation,
)


@dataclass(frozen=True)
class ResolvedExpectation:
    """An audit expectation bound to its object attribute and JSON alias.

    Warehouse values are compared on the imported object, after every local
    conversion; explicit null and absence stay document-level conditions.
    Several warehouse fields behind one flattened alias keep distinct entries
    even though they read the same JSON location.
    """

    expectation: TargetExpectation
    attr_path: Path
    alias_path: Path

    def holds(self, document: Any, imported: Any) -> bool:
        value = self.expectation.value
        if value is Null:
            return has_path(document, self.alias_path) and get_path(
                document, self.alias_path
            ) is None
        if value is Missing:
            return not has_path(document, self.alias_path)
        if imported is None:
            return False
        try:
            return get_object_attr(imported, self.attr_path) == value
        except (AttributeError, KeyError, TypeError):
            return False


@dataclass(frozen=True)
class ReworkResult:
    patches: Tuple[ReworkPatch, ...]
    report: Optional[ConflictReport]

    @property
    def feasible(self) -> bool:
        return bool(self.patches)


def build_rework(
    entry: ImportEntry,
    original: Any,
    expectations: Sequence[TargetExpectation],
    source_changes: Sequence[SourceChange],
    *,
    max_patches: int = 8,
) -> ReworkResult:
    """Produce the shortest returnable amendments for one audit sheet.

    Only the source paths listed in ``source_changes`` are ever modified;
    untouched source items are forwarded as-is. Patch size is the number of
    source paths actually changed. The search is confined to the paths that
    can influence an audited warehouse field, so volumes carrying many
    unrelated details never get their combinations enumerated.
    """

    expectations = tuple(expectations)
    source_changes = tuple(source_changes)
    _validate_inputs(expectations, source_changes)
    influence = build_influence(entry.target_type)

    resolved, unknown = _resolve_expectations(expectations, influence)
    indexed_changes = {change.path: change for change in source_changes}
    relevant = _relevant_paths(resolved, influence, indexed_changes)
    ordered_changes = [indexed_changes[path] for path in relevant]

    baseline = import_document(entry, original)
    if unknown:
        return ReworkResult(
            (),
            _analyze_conflicts(
                entry,
                original,
                resolved,
                unknown,
                ordered_changes,
                influence,
                baseline,
            ),
        )
    if baseline.ok and _all_hold(original, baseline.imported, resolved):
        patch = ReworkPatch(
            target_type=entry.target_type,
            operations=(),
            document=deep_copy(original),
            changed_paths=(),
            imported=baseline.imported,
        )
        return ReworkResult((patch,), None)

    found = _search(entry, original, resolved, ordered_changes, max_patches)
    if found:
        return ReworkResult(tuple(found), None)
    report = _analyze_conflicts(
        entry,
        original,
        resolved,
        unknown,
        ordered_changes,
        influence,
        baseline,
    )
    return ReworkResult((), report)


def _validate_inputs(
    expectations: Sequence[TargetExpectation],
    source_changes: Sequence[SourceChange],
) -> None:
    seen: Dict[Path, None] = {}
    for change in source_changes:
        if change.path in seen:
            raise ValueError(f"duplicate source change for {change.path}")
        seen.setdefault(change.path, None)
    seen_expectations: Dict[Path, None] = {}
    for expectation in expectations:
        if expectation.path in seen_expectations:
            raise ValueError(f"duplicate target expectation for {expectation.path}")
        seen_expectations.setdefault(expectation.path, None)


def _resolve_expectations(
    expectations: Sequence[TargetExpectation], influence: InfluenceMap
) -> Tuple[List[ResolvedExpectation], List[TargetExpectation]]:
    resolved: List[ResolvedExpectation] = []
    unknown: List[TargetExpectation] = []
    for expectation in expectations:
        resolved_pair = influence.resolve_both(expectation.path)
        if resolved_pair is None:
            unknown.append(expectation)
        else:
            attr_path, alias_path = resolved_pair
            resolved.append(ResolvedExpectation(expectation, attr_path, alias_path))
    return resolved, unknown


def _relevant_paths(
    expectations: Sequence[ResolvedExpectation],
    influence: InfluenceMap,
    indexed_changes: Dict[Path, SourceChange],
) -> List[Path]:
    relevant: Dict[Path, None] = {}
    for resolved in expectations:
        for source_path in influence.influencers(resolved.expectation.path):
            for change_path in indexed_changes:
                if _related(change_path, source_path):
                    relevant.setdefault(change_path, None)
    return list(relevant)


def _related(left: Path, right: Path) -> bool:
    """Whether touching ``left`` can alter the value read at ``right``.

    Equal paths interact, replacing an ancestor container does, and a
    wildcard list slot matches any concrete index.
    """

    if left == right:
        return True
    short, long_ = sorted((left, right), key=len)
    if long_[: len(short)] != short:
        return False
    if len(short) == len(long_):
        return all(
            a == b or a == WILDCARD or b == WILDCARD
            for a, b in zip(left, right)
        )
    return True


def _adjustable(
    field_path: Path,
    influence: InfluenceMap,
    indexed: Dict[Path, SourceChange],
) -> List[Path]:
    candidates: Dict[Path, None] = {}
    for source_path in influence.influencers(field_path):
        for change_path in indexed:
            if _related(change_path, source_path):
                candidates.setdefault(change_path, None)
    return list(candidates)


def _all_hold(
    document: Any,
    imported: Any,
    expectations: Sequence[ResolvedExpectation],
) -> bool:
    return all(expectation.holds(document, imported) for expectation in expectations)


def _search(
    entry: ImportEntry,
    original: Any,
    expectations: Sequence[ResolvedExpectation],
    changes: Sequence[SourceChange],
    max_patches: int,
) -> List[ReworkPatch]:
    best: List[ReworkPatch] = []
    best_size = len(changes) + 1

    def recurse(
        index: int,
        document: Any,
        changed: Tuple[Path, ...],
        ops: Tuple[PatchOperation, ...],
    ) -> None:
        nonlocal best, best_size
        if len(changed) > best_size:
            return
        if index == len(changes):
            result = import_document(entry, document)
            if result.ok and _all_hold(document, result.imported, expectations):
                patch = ReworkPatch(
                    target_type=entry.target_type,
                    operations=ops,
                    document=deep_copy(document),
                    changed_paths=changed,
                    imported=result.imported,
                )
                if not best or patch.size < best_size:
                    best = [patch]
                    best_size = patch.size
                elif patch.size == best_size and len(best) < max_patches:
                    if _distinct(patch, best):
                        best.append(patch)
            return
        change = changes[index]
        # Option 1: forward this source item untouched.
        recurse(index + 1, document, changed, ops)
        for candidate in change.candidates:
            if len(changed) + 1 > best_size:
                break
            trial = deep_copy(document)
            if not _apply_candidate(trial, change, candidate):
                continue
            if candidate is Remove:
                operation = PatchOperation(change.path, "remove")
            else:
                operation = PatchOperation(change.path, "set", candidate)
            recurse(index + 1, trial, changed + (change.path,), ops + (operation,))

    recurse(0, deep_copy(original), (), ())
    return best


def _apply_candidate(document: Any, change: SourceChange, candidate: Any) -> bool:
    if candidate is Remove:
        return delete_path(document, change.path)
    set_path(document, change.path, candidate)
    return True


def _distinct(patch: ReworkPatch, patches: Sequence[ReworkPatch]) -> bool:
    paths = set(patch.changed_paths)
    return all(set(other.changed_paths) != paths for other in patches)


def _analyze_conflicts(
    entry: ImportEntry,
    original: Any,
    expectations: Sequence[ResolvedExpectation],
    unknown: Sequence[TargetExpectation],
    changes: Sequence[SourceChange],
    influence: InfluenceMap,
    baseline,
) -> ConflictReport:
    indexed = {change.path: change for change in changes}
    blocked: List[TargetExpectation] = list(unknown)
    baseline_imported = baseline.imported if baseline.ok else None
    for resolved in expectations:
        adjustable = _adjustable(
            resolved.expectation.path, influence, indexed
        )
        if not adjustable:
            if not resolved.holds(original, baseline_imported):
                blocked.append(resolved.expectation)
            continue
        if not _single_feasible(entry, original, resolved, indexed, adjustable):
            blocked.append(resolved.expectation)

    conflicts: List[ConflictPair] = []
    for i, first in enumerate(expectations):
        for second in expectations[i + 1 :]:
            first_sources = set(influence.influencers(first.expectation.path))
            second_sources = set(influence.influencers(second.expectation.path))
            shared = first_sources & second_sources
            if not shared:
                continue
            if _pair_feasible(
                entry, original, first, second, indexed, shared, influence, baseline
            ):
                continue
            conflicts.append(
                ConflictPair(
                    first=first.expectation,
                    second=second.expectation,
                    source_paths=tuple(sorted(shared)),
                    options={
                        str(path): tuple(indexed[path].candidates)
                        for path in sorted(shared)
                        if path in indexed
                    },
                )
            )

    influences = {
        str(resolved.expectation.path): tuple(
            str(path) for path in influence.influencers(resolved.expectation.path)
        )
        for resolved in expectations
    }
    return ConflictReport(
        target_type=entry.target_type,
        blocked=tuple(blocked),
        conflicts=tuple(conflicts),
        influences=influences,
        errors=baseline.errors if not baseline.ok else (),
    )


def _single_feasible(
    entry: ImportEntry,
    original: Any,
    resolved: ResolvedExpectation,
    indexed: Dict[Path, SourceChange],
    adjustable: Sequence[Path],
) -> bool:
    def recurse(index: int, document: Any) -> bool:
        if index == len(adjustable):
            result = import_document(entry, document)
            return result.ok and resolved.holds(document, result.imported)
        path = adjustable[index]
        if recurse(index + 1, document):
            return True
        for candidate in indexed[path].candidates:
            trial = deep_copy(document)
            if not _apply_candidate(trial, indexed[path], candidate):
                continue
            if recurse(index + 1, trial):
                return True
        return False

    return recurse(0, deep_copy(original))


def _pair_feasible(
    entry: ImportEntry,
    original: Any,
    first: ResolvedExpectation,
    second: ResolvedExpectation,
    indexed: Dict[Path, SourceChange],
    shared: set,
    influence: InfluenceMap,
    baseline,
) -> bool:
    adjustable_set: Dict[Path, None] = {}
    for source_path in shared:
        for change_path in indexed:
            if _related(change_path, source_path):
                adjustable_set.setdefault(change_path, None)
    adjustable = list(adjustable_set)
    if not adjustable:
        return first.holds(
            original, baseline.imported if baseline.ok else None
        ) and second.holds(original, baseline.imported if baseline.ok else None)

    def recurse(index: int, document: Any) -> bool:
        if index == len(adjustable):
            result = import_document(entry, document)
            return (
                result.ok
                and first.holds(document, result.imported)
                and second.holds(document, result.imported)
            )
        path = adjustable[index]
        if recurse(index + 1, document):
            return True
        for candidate in indexed[path].candidates:
            trial = deep_copy(document)
            if not _apply_candidate(trial, indexed[path], candidate):
                continue
            if recurse(index + 1, trial):
                return True
        return False

    return recurse(0, deep_copy(original))
