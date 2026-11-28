# mypy: disallow_untyped_defs=False
"""
Local release verification for inventory-domain migrations.

Manifest model
---------------

A manifest is a JSON document with three sections:

``pages``
    One entry per fully rendered page: ``id``, final page ``url``, the
    ``syntaxes`` actually enabled on that page, the baseline ``template``
    (complete HTML as currently deployed) and optional named ``slots`` mapping
    a slot name to the exact baseline fragment that appears once in the
    template.

``fields``
    The migration registry, one entry per site/page/SKU. ``kind`` is either
    ``migrating`` (``old_value`` -> ``new_value``) or ``retained``
    (``old_value`` forever). ``property`` names the inventory reference
    property (``url`` by default). Only registered references are resolved and
    compared.

``units``
    Publish units, each with approved ``versions``. A version lists, per page
    it touches, either full ``content`` (the unit alone renders the page) or
    ``fragments`` mapping slot names to replacement fragments. All fragments of
    one unit switch atomically on one cache copy.

A unit switches at most once. Units inside one batch may switch in any order,
including simultaneously, so every subset of a batch (on top of the units
already switched) must render safely; the next batch only starts after every
old cache copy of the current batch has expired.

Extraction always runs through :func:`extruct.extract` on complete rendered
HTML, so existing ``extract`` calls are unaffected, and each distinct
(html, final url, syntaxes) combination is extracted at most once.

JSON-LD keeps relative references verbatim; flagged references (a string value
or the ``@id`` of an object value) are resolved against the nearest in-scope
inline ``@base``, falling back to the page document base when no local
declaration applies. The document base is the final page URL combined with the
first valid ``<base href>``; it is also the base URL used for microdata and
RDFa extraction. RDFa checks use the native resource identifiers (absolute
``@id`` values); identical strings inside literal text stay untouched.
"""
from __future__ import annotations

import argparse
import json
from itertools import combinations
from typing import Any, Iterable
from urllib.parse import urljoin

from extruct import extract as _extract
from extruct.utils import parse_html

KNOWN_SYNTAXES = ("microdata", "json-ld", "rdfa")
DEFAULT_PROPERTY = "url"
SEARCH_NODE_CAP = 20000


# ---------------------------------------------------------------------------
# Base handling
# ---------------------------------------------------------------------------

def document_base(final_url: str, html: str | bytes, encoding: str = "UTF-8") -> str:
    """Resolve the document base from the final URL and first valid base href.

    A ``<base>`` element without an href is ignored. The result is the base URL
    handed to the microdata and RDFa extraction calls.
    """
    tree = parse_html(html, encoding)
    for node in tree.xpath("//base[@href]"):
        href = (node.get("href") or "").strip()
        if href:
            return urljoin(final_url, href)
    return final_url


def _resolve_iri(ref: str, base: str) -> str:
    if not isinstance(ref, str):
        return ref
    return urljoin(base, ref)


# ---------------------------------------------------------------------------
# JSON-LD @base tracking and reference resolution
# ---------------------------------------------------------------------------

def _context_base(context: Any) -> str | None:
    """Return the inline @base declared by a JSON-LD context, if any."""
    if isinstance(context, dict):
        base = context.get("@base")
        return base if isinstance(base, str) else None
    if isinstance(context, list):
        bases = [
            base
            for entry in context
            if (base := _context_base(entry)) is not None
        ]
        return bases[-1] if bases else None
    return None


def _iter_jsonld_nodes(
    node: Any, base: str | None, sku: str | None
) -> Iterable[tuple[Any, str | None, str | None]]:
    """Yield (sub-node, nearest enclosing inline @base, nearest enclosing SKU).

    An @base or sku declared on an object applies to that object and nested
    content until a deeper object declares its own.
    """
    if isinstance(node, dict):
        current_base = base
        context_base = _context_base(node.get("@context"))
        if context_base is not None:
            current_base = context_base
        local_sku = node.get("sku")
        current_sku = local_sku if isinstance(local_sku, str) else sku
        yield node, current_base, current_sku
        for key, value in node.items():
            if key == "@context":
                continue
            yield from _iter_jsonld_nodes(value, current_base, current_sku)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_jsonld_nodes(item, base, sku)


def _reference_values(node: dict, keys: Iterable[str]) -> list[str]:
    """Reference strings (values or object @ids) carried under the keys."""
    refs: list[str] = []
    for key in keys:
        if key not in node:
            continue
        value = node[key]
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            if isinstance(candidate, str):
                refs.append(candidate)
            elif isinstance(candidate, dict) and isinstance(
                candidate.get("@id"), str
            ):
                refs.append(candidate["@id"])
    return refs


def _rdfa_term(term: str) -> str:
    return "http://schema.org/" + term


# ---------------------------------------------------------------------------
# Rendered page states
# ---------------------------------------------------------------------------

class PageState:
    def __init__(
        self,
        page_id: str,
        final_url: str,
        doc_base: str,
        syntaxes: tuple[str, ...],
        readings: dict[tuple[str, str, str], list[str]],
    ):
        self.page_id = page_id
        self.final_url = final_url
        self.doc_base = doc_base
        self.syntaxes = syntaxes
        # (syntax, sku, property) -> sorted absolute values read
        self.readings = readings

    def values_for(self, sku: str, prop: str) -> dict[str, list[str]]:
        return {
            syntax: values
            for syntax in self.syntaxes
            if (values := self.readings.get((syntax, sku, prop)))
        }


class ExtractionBudgetExceeded(RuntimeError):
    def __init__(self, limit: int):
        super().__init__(
            "full-page extraction budget of {} renders exceeded".format(limit)
        )
        self.limit = limit


class PageRenderer:
    """Extracts each distinct (content, final url, syntax) combination once.

    Pages and units that never share a publish unit therefore never cause
    extractions for one another.
    """

    def __init__(self, encoding: str = "UTF-8", limit: int | None = 2000):
        self.encoding = encoding
        self.limit = limit
        self.extraction_count = 0
        self._cache: dict[tuple[str, str, tuple[str, ...]], dict] = {}

    def render(
        self,
        page_id: str,
        html: str | bytes,
        final_url: str,
        syntaxes: Iterable[str],
    ) -> PageState:
        if isinstance(html, bytes):
            html = html.decode(self.encoding)
        syntax_tuple = tuple(dict.fromkeys(syntaxes))
        key = (html, final_url, syntax_tuple)
        cached = self._cache.get(key)
        if cached is not None:
            return PageState(
                page_id,
                final_url,
                cached["doc_base"],
                syntax_tuple,
                dict(cached["readings"]),
            )
        doc_base = document_base(final_url, html, self.encoding)
        self.extraction_count += 1
        if self.limit is not None and self.extraction_count > self.limit:
            raise ExtractionBudgetExceeded(self.limit)
        extracted = _extract(
            html,
            base_url=doc_base,
            encoding=self.encoding,
            syntaxes=list(syntax_tuple),
        )
        readings: dict[tuple[str, str, str], list[str]] = {}
        if "json-ld" in syntax_tuple:
            for item in extracted.get("json-ld", []):
                for node, inline_base, sku in _iter_jsonld_nodes(
                    item, None, None
                ):
                    if not isinstance(node, dict) or sku is None:
                        continue
                    base = (
                        _resolve_iri(inline_base, doc_base)
                        if inline_base is not None
                        else doc_base
                    )
                    for prop in _jsonld_properties(node):
                        refs = _reference_values(node, [prop])
                        if refs:
                            readings.setdefault(
                                ("json-ld", sku, prop), []
                            ).extend(
                                _resolve_iri(ref, base) for ref in refs
                            )
        if "rdfa" in syntax_tuple:
            for item in extracted.get("rdfa", []):
                if not isinstance(item, dict):
                    continue
                skus = [
                    entry["@value"]
                    for entry in item.get(_rdfa_term("sku"), [])
                    if isinstance(entry, dict)
                    and isinstance(entry.get("@value"), str)
                ]
                for term in _rdfa_properties(item):
                    refs = _reference_values(item, [_rdfa_term(term)])
                    if refs:
                        for sku in skus:
                            readings.setdefault(
                                ("rdfa", sku, term), []
                            ).extend(refs)
        if "microdata" in syntax_tuple:
            for item in extracted.get("microdata", []):
                for sku, prop, refs in _microdata_readings(item, set()):
                    readings.setdefault(
                        ("microdata", sku, prop), []
                    ).extend(refs)
        readings = {key: sorted(set(values)) for key, values in readings.items()}
        self._cache[key] = {"doc_base": doc_base, "readings": readings}
        return PageState(page_id, final_url, doc_base, syntax_tuple, readings)


def _jsonld_properties(node: dict) -> list[str]:
    return [key for key in node if not key.startswith("@") and key != "sku"]


def _rdfa_properties(item: dict) -> list[str]:
    prefix = _rdfa_term("")
    return [
        key[len(prefix):]
        for key in item
        if key.startswith(prefix) and key != _rdfa_term("sku")
    ]


def _microdata_readings(
    item: Any, inherited_skus: set[str]
) -> Iterable[tuple[str, str, list[str]]]:
    if not isinstance(item, dict):
        return
    properties = item.get("properties")
    if not isinstance(properties, dict):
        return
    sku_values = properties.get("sku")
    own_skus = (
        sku_values if isinstance(sku_values, list) else [sku_values]
    )
    active_skus = set(inherited_skus)
    active_skus.update(sku for sku in own_skus if isinstance(sku, str))
    for prop, value in properties.items():
        if prop == "sku":
            continue
        candidates = value if isinstance(value, list) else [value]
        refs: list[str] = []
        nested: list[Any] = []
        for candidate in candidates:
            if isinstance(candidate, str):
                refs.append(candidate)
            elif isinstance(candidate, dict):
                nested.append(candidate)
        if refs:
            for sku in active_skus:
                yield sku, prop, refs
        for nested_item in nested:
            yield from _microdata_readings(nested_item, active_skus)


# ---------------------------------------------------------------------------
# Manifest model and page assembly
# ---------------------------------------------------------------------------

class FieldSpec:
    def __init__(self, entry: dict[str, Any]):
        self.site = entry["site"]
        self.page_id = entry["page"]
        self.sku = entry["sku"]
        self.kind = entry["kind"]
        self.prop = entry.get("property", DEFAULT_PROPERTY)
        self.old_value = entry.get("old_value")
        self.new_value = entry.get("new_value")

    def allowed(self, final: bool) -> list[str]:
        if self.kind == "migrating":
            return [self.new_value] if final else [self.old_value, self.new_value]
        return [self.old_value]


class PageSpec:
    def __init__(self, entry: dict[str, Any]):
        self.page_id = entry["id"]
        self.final_url = entry["url"]
        self.syntaxes = tuple(entry.get("syntaxes", KNOWN_SYNTAXES))
        self.template = entry["template"]
        self.slots: dict[str, str] = dict(entry.get("slots", {}))
        unknown = [
            syntax for syntax in self.syntaxes if syntax not in KNOWN_SYNTAXES
        ]
        if unknown:
            raise ValueError(
                "page {!r} uses unsupported syntaxes {}".format(
                    self.page_id, unknown
                )
            )
        for name, fragment in self.slots.items():
            if self.template.count(fragment) != 1:
                raise ValueError(
                    "slot {!r} of page {!r} must appear exactly once in the "
                    "baseline template".format(name, self.page_id)
                )


class UnitVersion:
    def __init__(self, unit_id: str, entry: dict[str, Any]):
        self.unit_id = unit_id
        self.version_id = entry["id"]
        self.full_content: dict[str, str] = {}
        self.fragments: dict[str, dict[str, str]] = {}
        for page_entry in entry["pages"]:
            page_id = page_entry["page"]
            if "content" in page_entry:
                self.full_content[page_id] = page_entry["content"]
            elif "fragments" in page_entry:
                self.fragments[page_id] = dict(page_entry["fragments"])
            else:
                raise ValueError(
                    "version {}/{} needs content or fragments for page {!r}".format(
                        unit_id, self.version_id, page_id
                    )
                )

    @property
    def page_ids(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.full_content) | set(self.fragments)))


class AssemblyConflict(Exception):
    def __init__(self, page_id: str, reason: str):
        super().__init__("page {}: {}".format(page_id, reason))
        self.page_id = page_id
        self.reason = reason


class ReleaseManifest:
    def __init__(self, data: dict[str, Any]):
        self.pages: dict[str, PageSpec] = {
            entry["id"]: PageSpec(entry) for entry in data.get("pages", [])
        }
        self.fields = [FieldSpec(entry) for entry in data.get("fields", [])]
        self.units: dict[str, list[UnitVersion]] = {}
        for entry in data.get("units", []):
            unit_id = entry["id"]
            self.units[unit_id] = [
                UnitVersion(unit_id, version) for version in entry.get("versions", [])
            ]
        self.extraction_limit = data.get("extraction_limit", 2000)
        self.validate()

    def validate(self) -> None:
        for spec in self.fields:
            if spec.page_id not in self.pages:
                raise ValueError(
                    "field references unknown page {!r}".format(spec.page_id)
                )
            if spec.kind not in ("migrating", "retained"):
                raise ValueError(
                    "field {}/{} has unknown kind {!r}".format(
                        spec.page_id, spec.sku, spec.kind
                    )
                )
            if spec.kind == "migrating" and not (
                spec.old_value and spec.new_value
            ):
                raise ValueError(
                    "migrating field {}/{} needs old_value and new_value".format(
                        spec.page_id, spec.sku
                    )
                )
            if spec.kind == "retained" and not spec.old_value:
                raise ValueError(
                    "retained field {}/{} needs old_value".format(
                        spec.page_id, spec.sku
                    )
                )
        for unit_id, versions in self.units.items():
            if not versions:
                raise ValueError(
                    "unit {!r} has no approved versions".format(unit_id)
                )
            for version in versions:
                for page_id in version.page_ids:
                    if page_id not in self.pages:
                        raise ValueError(
                            "version {}/{} references unknown page {!r}".format(
                                unit_id, version.version_id, page_id
                            )
                        )
                    page = self.pages[page_id]
                    for slot in version.fragments.get(page_id, {}):
                        if slot not in page.slots:
                            raise ValueError(
                                "version {}/{} replaces unknown slot {!r} on "
                                "page {!r}".format(
                                    unit_id, version.version_id, slot, page_id
                                )
                            )

    def fields_for_page(self, page_id: str) -> list[FieldSpec]:
        return [spec for spec in self.fields if spec.page_id == page_id]

    def units_for_page(self) -> dict[str, set[str]]:
        owners: dict[str, set[str]] = {page_id: set() for page_id in self.pages}
        for unit_id, versions in self.units.items():
            for version in versions:
                for page_id in version.page_ids:
                    owners[page_id].add(unit_id)
        return owners

    def assemble(
        self, page_id: str, selections: dict[str, UnitVersion]
    ) -> str:
        """Render a page with the given unit versions switched on."""
        page = self.pages[page_id]
        active = [
            (unit_id, version)
            for unit_id, version in sorted(selections.items())
            if page_id in version.page_ids
        ]
        full = [
            unit_id
            for unit_id, version in active
            if page_id in version.full_content
        ]
        if len(full) > 1:
            raise AssemblyConflict(
                page_id,
                "units {} all supply full content for the same page".format(
                    sorted(full)
                ),
            )
        if full and len(active) > 1:
            raise AssemblyConflict(
                page_id,
                "full-content unit {} cannot be combined with fragment units "
                "on the same page".format(full[0]),
            )
        if full:
            return selections[full[0]].full_content[page_id]
        html = page.template
        replaced: dict[str, str] = {}
        for unit_id, version in active:
            for slot, fragment in version.fragments.get(page_id, {}).items():
                if slot in replaced:
                    raise AssemblyConflict(
                        page_id,
                        "slot {!r} is replaced by both unit {} and unit {}".format(
                            slot, replaced[slot], unit_id
                        ),
                    )
                baseline = page.slots[slot]
                if html.count(baseline) != 1:
                    raise AssemblyConflict(
                        page_id,
                        "slot {!r} baseline fragment is no longer unique when "
                        "assembling units".format(slot),
                    )
                html = html.replace(baseline, fragment, 1)
                replaced[slot] = unit_id
        return html


# ---------------------------------------------------------------------------
# Safety checks
# ---------------------------------------------------------------------------

def verify_state(
    state: PageState, manifest: ReleaseManifest, final: bool
) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    for spec in manifest.fields_for_page(state.page_id):
        per_syntax = state.values_for(spec.sku, spec.prop)
        allowed = spec.allowed(final)
        seen: set[str] = set()
        for syntax, values in per_syntax.items():
            bad = [value for value in values if value not in allowed]
            if bad:
                violations.append(
                    {
                        "page": state.page_id,
                        "sku": spec.sku,
                        "syntax": syntax,
                        "read_values": values,
                        "allowed_values": allowed,
                        "reason": (
                            "retained SKU changed value"
                            if spec.kind == "retained"
                            else "value outside the old/new addresses"
                        ),
                    }
                )
            seen.update(values)
        enabled = list(per_syntax)
        if len(enabled) >= 2 and len(seen) > 1:
            violations.append(
                {
                    "page": state.page_id,
                    "sku": spec.sku,
                    "syntax": "*",
                    "readings": per_syntax,
                    "reason": "enabled syntaxes point at different addresses "
                    "while cache copies expire",
                }
            )
        if not enabled:
            violations.append(
                {
                    "page": state.page_id,
                    "sku": spec.sku,
                    "syntax": "*",
                    "read_values": [],
                    "allowed_values": allowed,
                    "reason": "no enabled syntax exposes the registered SKU",
                }
            )
    return violations


# ---------------------------------------------------------------------------
# Release planning
# ---------------------------------------------------------------------------


class PlannedBatch:
    def __init__(self, units: dict[str, str], pages: set[str]):
        self.units = units
        self.pages = pages

    def to_dict(self) -> dict[str, Any]:
        return {
            "units": dict(self.units),
            "republished_pages": sorted(self.pages),
        }


class ReleasePlanner:
    def __init__(self, manifest: ReleaseManifest, encoding: str = "UTF-8"):
        self.manifest = manifest
        self.encoding = encoding
        self.renderer = PageRenderer(
            encoding=encoding, limit=manifest.extraction_limit
        )
        self.owners = manifest.units_for_page()
        self._state_cache: dict[tuple[str, frozenset], PageState] = {}
        self.blockers: list[dict[str, Any]] = []

    def render_selected(
        self, page_id: str, selections: dict[str, UnitVersion]
    ) -> PageState:
        key = (
            page_id,
            frozenset(
                (unit_id, version.version_id)
                for unit_id, version in selections.items()
                if page_id in version.page_ids
            ),
        )
        cached = self._state_cache.get(key)
        if cached is not None:
            return cached
        html = self.manifest.assemble(page_id, selections)
        page = self.manifest.pages[page_id]
        state = self.renderer.render(
            page_id, html, page.final_url, page.syntaxes
        )
        self._state_cache[key] = state
        return state

    def plan(self) -> dict[str, Any]:
        manifest = self.manifest
        for page_id, page in manifest.pages.items():
            state = self.renderer.render(
                page_id, page.template, page.final_url, page.syntaxes
            )
            self._state_cache[(page_id, frozenset())] = state
            for violation in verify_state(state, manifest, final=False):
                violation["active_versions"] = {}
                self.blockers.append(violation)
        if self.blockers:
            return self._verdict(False, [])

        selections, exhaustive = self._choose_selections()
        if selections is None:
            return self._verdict(False, [])

        batches = self._build_batches(selections)
        if batches is None:
            return self._verdict(False, [])

        for page_id in manifest.pages:
            state = self.render_selected(page_id, selections)
            for violation in verify_state(state, manifest, final=True):
                violation["active_versions"] = {
                    unit_id: version.version_id
                    for unit_id, version in selections.items()
                }
                self.blockers.append(violation)
        if self.blockers:
            return self._verdict(False, [])

        republished: set[str] = set()
        planned: list[PlannedBatch] = []
        for batch_units in batches:
            pages: set[str] = set()
            unit_versions: dict[str, str] = {}
            for unit_id in batch_units:
                version = selections[unit_id]
                unit_versions[unit_id] = version.version_id
                pages.update(version.page_ids)
            republished.update(pages)
            planned.append(PlannedBatch(unit_versions, pages))

        verdict = self._verdict(True, planned)
        verdict["republished_pages"] = sorted(republished)
        verdict["republished_page_count"] = len(republished)
        verdict["exhaustive_search"] = exhaustive
        return verdict

    # -- component decomposition -------------------------------------------

    def _components(self) -> list[set[str]]:
        """Units connected through shared pages; isolated sites stay apart."""
        adjacency: dict[str, set[str]] = {
            unit_id: set() for unit_id in self.manifest.units
        }
        for page_id, owner_set in self.owners.items():
            owners = list(owner_set)
            for unit_id in owners:
                adjacency[unit_id].update(owners)
        seen: set[str] = set()
        components: list[set[str]] = []
        for unit_id in sorted(adjacency):
            if unit_id in seen:
                continue
            stack = [unit_id]
            component: set[str] = set()
            while stack:
                current = stack.pop()
                if current in seen:
                    continue
                seen.add(current)
                component.add(current)
                stack.extend(adjacency[current] - seen)
            components.append(component)
        return components

    # -- version selection --------------------------------------------------

    def _choose_selections(
        self,
    ) -> tuple[dict[str, UnitVersion] | None, bool]:
        selections: dict[str, UnitVersion] = {}
        exhaustive = True
        for component in self._components():
            chosen, component_exhaustive = self._solve_component(component)
            if chosen is None:
                return None, exhaustive
            selections.update(chosen)
            exhaustive = exhaustive and component_exhaustive
        return selections, exhaustive

    def _solve_component(
        self, units: set[str]
    ) -> tuple[dict[str, UnitVersion] | None, bool]:
        manifest = self.manifest
        ordered = sorted(units)
        component_pages = {
            page_id
            for unit_id in ordered
            for version in manifest.units[unit_id]
            for page_id in version.page_ids
        }
        options: dict[str, list[UnitVersion | None]] = {
            unit_id: [None, *manifest.units[unit_id]] for unit_id in ordered
        }
        best: dict[str, UnitVersion] | None = None
        best_pages: set[str] | None = None
        node_count = 0
        exhaustive = True

        def final_ok(chosen: dict[str, UnitVersion]) -> bool:
            for page_id in component_pages:
                state = self.render_selected(page_id, chosen)
                if verify_state(state, manifest, final=True):
                    return False
            return True

        def batchable(chosen: dict[str, UnitVersion]) -> bool:
            return self._batch_order(chosen) is not None

        def search(
            index: int,
            chosen: dict[str, UnitVersion],
            used_pages: set[str],
        ) -> None:
            nonlocal best, best_pages, node_count, exhaustive
            node_count += 1
            if node_count > SEARCH_NODE_CAP:
                exhaustive = False
                return
            if best_pages is not None and len(used_pages) >= len(best_pages):
                return
            if index == len(ordered):
                try:
                    if not final_ok(chosen):
                        return
                    if not batchable(chosen):
                        return
                except AssemblyConflict:
                    return
                best = dict(chosen)
                best_pages = set(used_pages)
                return
            unit_id = ordered[index]
            candidates = sorted(
                options[unit_id],
                key=lambda version: (
                    version is not None,
                    version.version_id if version is not None else "",
                ),
            )
            for version in candidates:
                if version is None:
                    search(index + 1, dict(chosen), set(used_pages))
                    continue
                trial = dict(chosen)
                trial[unit_id] = version
                search(
                    index + 1,
                    trial,
                    used_pages | set(version.page_ids),
                )

        search(0, {}, set())
        if best is not None:
            return best, exhaustive

        greedy = self._greedy_component(ordered, component_pages)
        if greedy is None:
            return None, False
        return greedy, False

    def _greedy_component(
        self, ordered: list[str], component_pages: set[str]
    ) -> dict[str, UnitVersion] | None:
        manifest = self.manifest
        chosen: dict[str, UnitVersion] = {}
        while True:
            pending = self._pending_fields(chosen, component_pages)
            if not pending:
                final_violations: list[dict[str, Any]] = []
                for page_id in component_pages:
                    state = self.render_selected(page_id, chosen)
                    for violation in verify_state(state, manifest, final=True):
                        violation["active_versions"] = {
                            unit_id: version.version_id
                            for unit_id, version in chosen.items()
                        }
                        final_violations.append(violation)
                if not final_violations and self._batch_order(chosen) is not None:
                    return chosen
                if not any(unit_id not in chosen for unit_id in ordered):
                    self.blockers.extend(final_violations)
                    if not final_violations:
                        self.blockers.append(
                            {
                                "page": sorted(component_pages),
                                "sku": "*",
                                "syntax": "*",
                                "active_versions": {
                                    unit_id: version.version_id
                                    for unit_id, version in chosen.items()
                                },
                                "reason": "every selected unit mixes a page at "
                                "some point, so no safe batch order exists",
                            }
                        )
                    return None
            best_candidate: tuple | None = None
            for unit_id in ordered:
                if unit_id in chosen:
                    continue
                for version in manifest.units[unit_id]:
                    trial = dict(chosen)
                    trial[unit_id] = version
                    improvement = 0
                    violations = 0
                    for spec in pending:
                        if spec.page_id not in version.page_ids:
                            continue
                        state = self.render_selected(spec.page_id, trial)
                        values = state.values_for(spec.sku, spec.prop)
                        seen = {
                            value for per in values.values() for value in per
                        }
                        target = (
                            {spec.new_value}
                            if spec.kind == "migrating"
                            else {spec.old_value}
                        )
                        if seen == target:
                            improvement += 1
                        violations += len(
                            verify_state(state, manifest, final=False)
                        )
                    added = set(version.page_ids) - {
                        page
                        for selected in chosen.values()
                        for page in selected.page_ids
                    }
                    score = (len(added), -improvement, violations, unit_id,
                             version.version_id)
                    if best_candidate is None or score < best_candidate[0]:
                        best_candidate = (score, unit_id, version, improvement)
            if best_candidate is None or best_candidate[3] <= 0:
                for unit_id in ordered:
                    if unit_id in chosen:
                        continue
                    for version in manifest.units[unit_id]:
                        trial = dict(chosen)
                        trial[unit_id] = version
                        for page_id in version.page_ids:
                            state = self.render_selected(page_id, trial)
                            for violation in verify_state(
                                state, manifest, final=False
                            ):
                                violation["active_versions"] = {
                                    chosen_id: chosen_version.version_id
                                    for chosen_id, chosen_version in trial.items()
                                }
                                self.blockers.append(violation)
                for spec in self._pending_fields(chosen, component_pages):
                    state = self.render_selected(spec.page_id, chosen)
                    self.blockers.append(
                        {
                            "page": spec.page_id,
                            "sku": spec.sku,
                            "syntax": "*",
                            "readings": state.values_for(spec.sku, spec.prop),
                            "active_versions": {
                                unit_id: version.version_id
                                for unit_id, version in chosen.items()
                            },
                            "reason": "no approved unit version can drive this "
                            "field to its required value safely",
                        }
                    )
                return None
            _, unit_id, version, _ = best_candidate
            chosen[unit_id] = version

    def _pending_fields(
        self, chosen: dict[str, UnitVersion], component_pages: set[str]
    ) -> list[FieldSpec]:
        manifest = self.manifest
        pending: list[FieldSpec] = []
        for spec in manifest.fields:
            if spec.page_id not in component_pages:
                continue
            state = self.render_selected(spec.page_id, chosen)
            values = state.values_for(spec.sku, spec.prop)
            seen = {value for per in values.values() for value in per}
            if spec.kind == "migrating":
                if seen != {spec.new_value}:
                    pending.append(spec)
            elif seen and seen != {spec.old_value}:
                pending.append(spec)
        return pending

    # -- batching -----------------------------------------------------------

    def _build_batches(
        self, selections: dict[str, UnitVersion]
    ) -> list[list[str]] | None:
        order = self._batch_order(selections)
        if order is None:
            return None
        remaining = list(order)
        switched: set[str] = set()
        batches: list[list[str]] = []
        while remaining:
            batch: list[str] = []
            for unit_id in remaining:
                trial = set(batch) | {unit_id}
                if not self._subset_violations(
                    switched, trial, selections
                ):
                    batch.append(unit_id)
            if not batch:
                # The linear order guarantees the first remaining unit is
                # safe on its own, so fall back to a one-unit batch rather
                # than declaring the release impossible.
                batch = [remaining[0]]
            batches.append(batch)
            switched.update(batch)
            remaining = [unit_id for unit_id in remaining if unit_id not in batch]
        return batches

    def _batch_order(
        self, selections: dict[str, UnitVersion]
    ) -> list[str] | None:
        """Return a unit ordering in which every prefix renders safely."""
        remaining = set(selections)
        switched: set[str] = set()
        order: list[str] = []
        while remaining:
            progress = False
            for unit_id in sorted(remaining):
                if not self._subset_violations(
                    switched, {unit_id}, selections
                ):
                    order.append(unit_id)
                    switched.add(unit_id)
                    remaining.remove(unit_id)
                    progress = True
                    break
            if not progress:
                return None
        return order

    def _subset_violations(
        self,
        switched: set[str],
        batch: set[str],
        selections: dict[str, UnitVersion],
    ) -> list[dict[str, Any]]:
        manifest = self.manifest
        involved_pages = {
            page_id
            for unit_id in switched | batch
            for page_id in selections[unit_id].page_ids
        }
        violations: list[dict[str, Any]] = []
        for page_id in involved_pages:
            owners = {
                unit_id
                for unit_id in switched | batch
                if page_id in selections[unit_id].page_ids
            }
            prior = owners & switched
            newcomers = owners & batch
            for width in range(len(newcomers) + 1):
                for extra in combinations(sorted(newcomers), width):
                    active = prior | set(extra)
                    selected = {
                        unit_id: selections[unit_id] for unit_id in active
                    }
                    try:
                        state = self.render_selected(page_id, selected)
                    except AssemblyConflict as conflict:
                        violations.append(
                            {
                                "page": page_id,
                                "sku": "*",
                                "syntax": "*",
                                "active_versions": {
                                    unit_id: selections[unit_id].version_id
                                    for unit_id in active
                                },
                                "reason": conflict.reason,
                            }
                        )
                        continue
                    for violation in verify_state(state, manifest, False):
                        violation["active_versions"] = {
                            unit_id: selections[unit_id].version_id
                            for unit_id in active
                        }
                        violations.append(violation)
        return _dedupe(violations)

    # -- output -------------------------------------------------------------

    def _verdict(self, safe: bool, batches: list) -> dict[str, Any]:
        return {
            "safe": safe,
            "batches": [batch.to_dict() for batch in batches],
            "republished_pages": [],
            "republished_page_count": 0,
            "extraction_count": self.renderer.extraction_count,
            "blockers": _dedupe(self.blockers),
        }


def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in items:
        key = json.dumps(item, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def plan_release(
    manifest: ReleaseManifest, encoding: str = "UTF-8"
) -> dict[str, Any]:
    return ReleasePlanner(manifest, encoding=encoding).plan()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="extruct-release",
        description=(
            "Verify a local inventory-domain release from fully rendered page "
            "templates and approved publish-unit versions"
        ),
    )
    parser.add_argument("manifest", help="JSON release manifest")
    parser.add_argument(
        "--output",
        help="write the JSON verdict to this path instead of stdout",
    )
    parser.add_argument(
        "--encoding",
        default="UTF-8",
        help="encoding of inline HTML contents (default: UTF-8)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    with open(args.manifest, encoding=args.encoding) as handle:
        data = json.load(handle)
    try:
        manifest = ReleaseManifest(data)
        verdict = plan_release(manifest, encoding=args.encoding)
    except ExtractionBudgetExceeded as exc:
        verdict = {
            "safe": False,
            "batches": [],
            "republished_pages": [],
            "republished_page_count": 0,
            "extraction_count": exc.limit,
            "blockers": [
                {
                    "page": "*",
                    "sku": "*",
                    "reason": str(exc),
                }
            ],
        }
    except (AssemblyConflict, ValueError) as exc:
        verdict = {
            "safe": False,
            "batches": [],
            "republished_pages": [],
            "republished_page_count": 0,
            "extraction_count": 0,
            "blockers": [
                {
                    "page": getattr(exc, "page_id", "*"),
                    "sku": "*",
                    "reason": str(exc),
                }
            ],
        }
    rendered = json.dumps(verdict, indent=2, sort_keys=True, ensure_ascii=False)
    if args.output:
        with open(args.output, "w", encoding=args.encoding) as handle:
            handle.write(rendered)
            handle.write("\n")
    else:
        print(rendered)
    return 0 if verdict["safe"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
