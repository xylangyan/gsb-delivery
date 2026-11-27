"""Owner-ID rewriting and the safe-assignment search.

The builder chooses one replacement value per owner-ID group and decides which
individual permits to use so that the issued cassette:

* carries no original owner ID,
* introduces no new request match under any group's specified matchers -
  including matches between surviving original pages and newly recorded pages
  after a withdrawal, and
* keeps the original handover order, so every occurrence is still fetchable in
  the same order.

A pair that already matched before rewriting is not a new collision: the two
occurrences were already distinguishable only by play order, which the issued
cassette preserves. Assignments that satisfy playback and retention are ranked
by fewest individual permits, with the receipt-manifest order breaking ties.
"""

import copy
import json
from dataclasses import dataclass
from itertools import product

from vcr.matchers import requests_match

from .config import OwnerIdRules, replace_query_parameter


class RewriteError(ValueError):
    """Raised when no safe owner-ID assignment exists."""


@dataclass(frozen=True)
class IssuedInteraction:
    """An interaction destined for the issued cassette, with its provenance."""

    request: object
    response: dict
    source: tuple

    @property
    def label(self):
        if self.source[0] == "original":
            return f"original occurrence #{self.source[1]}"
        shift_id, sequence = self.source[1], self.source[2]
        return f"recorded interaction from shift {shift_id!r}, sequence {sequence}"


# -- low level location access ------------------------------------------------


def _body_bytes(body):
    if body is None:
        return None
    if hasattr(body, "read"):
        body = body.read()
    if isinstance(body, str):
        return body.encode("utf-8")
    return body


def _json_node(body, path):
    """Return (parsed_json, node) for a dotted path, or (None, None)."""
    raw = _body_bytes(body)
    if raw is None:
        return None, None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None, None
    node = data
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return None, None
        node = node[part]
    return data, node


def _set_body_path(body, path, value):
    data = json.loads(_body_bytes(body))
    node = data
    for part in path[:-1]:
        node = node[part]
    node[path[-1]] = value
    return json.dumps(data).encode("utf-8")


def _as_str(value):
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


def get_location_value(interaction, location):
    """Return the current string value at a location, or None."""
    request, response = interaction
    if location.kind == "request":
        if location.part == "uri":
            return request.uri
        if location.part == "header":
            return request.headers.get(location.name)
        _, value = _json_node(request.body, location.name.split("."))
        return _as_str(value)
    if location.kind == "response":
        if location.part == "header":
            values = response["headers"].get(location.name)
            return values[0] if values else None
        _, value = _json_node(response["body"]["string"], location.name.split("."))
        return _as_str(value)
    if location.part == "query":
        from .config import query_parameter

        return query_parameter(request.uri, location.name)
    _, value = _json_node(response["body"]["string"], location.name.split("."))
    return _as_str(value)


def set_location_value(interaction, location, value):
    """Write a replacement at a location, mutating the interaction's copies."""
    request, response = interaction
    value = str(value)
    if location.kind == "request":
        if location.part == "uri":
            request.uri = request.uri.replace(request.uri, value)
            return
        if location.part == "header":
            request.headers[location.name] = value
            return
        request.body = _set_body_path(request.body, location.name.split("."), value)
        return
    if location.kind == "response":
        if location.part == "header":
            response["headers"][location.name] = [value]
            return
        response["body"]["string"] = _set_body_path(
            response["body"]["string"], location.name.split("."), value
        )
        return
    if location.part == "query":
        request.uri = replace_query_parameter(request.uri, location.name, value)
        return
    response["body"]["string"] = _set_body_path(
        response["body"]["string"], location.name.split("."), value
    )


def replace_within_location(interaction, location, old, new):
    """Substitute ``old`` with ``new`` inside a location's current value.

    Group rewrites must only swap the owner ID itself: a header value, JSON
    field or query parameter that embeds the ID among other text keeps the
    rest of its content and order.
    """
    request, response = interaction
    if location.kind == "request":
        if location.part == "uri":
            request.uri = request.uri.replace(old, new)
            return
        if location.part == "header":
            current = request.headers.get(location.name)
            if current is not None:
                request.headers[location.name] = current.replace(old, new)
            return
        request.body = _replace_body_path(request.body, location.name.split("."), old, new)
        return
    if location.kind == "response":
        if location.part == "header":
            values = response["headers"].get(location.name)
            if values:
                response["headers"][location.name] = [value.replace(old, new) for value in values]
            return
        response["body"]["string"] = _replace_body_path(
            response["body"]["string"], location.name.split("."), old, new
        )
        return
    if location.part == "query":
        from .config import query_parameter

        current = query_parameter(request.uri, location.name)
        if current is not None:
            request.uri = replace_query_parameter(request.uri, location.name, current.replace(old, new))
        return
    response["body"]["string"] = _replace_body_path(
        response["body"]["string"], location.name.split("."), old, new
    )


def _replace_body_path(body, path, old, new):
    data = json.loads(_body_bytes(body))
    node = data
    for part in path[:-1]:
        node = node[part]
    leaf = node[path[-1]]
    if isinstance(leaf, str):
        node[path[-1]] = leaf.replace(old, new)
    return json.dumps(data).encode("utf-8")


# -- assignment search --------------------------------------------------------


@dataclass(frozen=True)
class Assignment:
    group_values: dict
    permits_used: tuple
    permit_count: int
    manifest_key: tuple


def _group_locations(rules):
    grouped = {}
    for location, original in rules.locations.items():
        grouped.setdefault(original, []).append(location)
    return grouped


def _present_groups(rules, interactions):
    grouped = _group_locations(rules)
    present = []
    for original in rules.group_order:
        for interaction in interactions:
            pair = (interaction.request, interaction.response)
            if any(get_location_value(pair, location) is not None for location in grouped.get(original, ())):
                present.append(original)
                break
    return present


def _applicable_permits(rules, interactions):
    applicable = []
    for permit in rules.permits:
        for interaction in interactions:
            value = get_location_value((interaction.request, interaction.response), permit.location)
            if value is not None and value == permit.original:
                applicable.append(permit)
                break
    return tuple(applicable)


def apply_group_assignment(interactions, rules, group_values):
    """Rewrite every group location in place with the group's chosen value."""
    for location, original in rules.locations.items():
        # A group that never appears has no chosen value and no non-None
        # location to rewrite anyway.
        if original not in group_values:
            continue
        replacement = group_values[original]
        for interaction in interactions:
            pair = (interaction.request, interaction.response)
            value = get_location_value(pair, location)
            if value is None:
                continue
            replace_within_location(pair, location, original, replacement)


def _apply_permits(interactions, permits):
    for permit in permits:
        for interaction in interactions:
            pair = (interaction.request, interaction.response)
            value = get_location_value(pair, permit.location)
            if value is not None and value == permit.original:
                set_location_value(pair, permit.location, permit.replacement)


def _all_group_matchers(rules):
    matchers = []
    for original in rules.group_order:
        for matcher in rules.group_match_on.get(original, ()):
            if matcher not in matchers:
                matchers.append(matcher)
    return matchers


def _match_matrix(interactions, matchers):
    """Map (i, j) to whether the pair matches under any group matcher."""
    matrix = {}
    for i, left in enumerate(interactions):
        for j in range(i + 1, len(interactions)):
            matrix[(i, j)] = any(
                requests_match(left.request, interactions[j].request, (matcher,)) for matcher in matchers
            )
    return matrix


def _permit_subsets_by_size(permits):
    for size in range(len(permits) + 1):
        yield from _combinations(permits, size)


def _combinations(items, size):
    if size == 0:
        return [()]
    results = []
    for index, item in enumerate(items[: len(items) - size + 1]):
        for rest in _combinations(items[index + 1 :], size - 1):
            results.append((item, *rest))
    return results


def _deep_copy_interactions(interactions):
    return [
        IssuedInteraction(
            request=copy.deepcopy(interaction.request),
            response=copy.deepcopy(interaction.response),
            source=interaction.source,
        )
        for interaction in interactions
    ]


def find_safe_assignment(rules, interactions, page_turn="<unknown>"):
    """Search group values and permits for a collision-free rewrite."""
    if not isinstance(rules, OwnerIdRules):
        raise RewriteError("handover rules are required to build an issued cassette")
    present = _present_groups(rules, interactions)
    candidates = {}
    for original in present:
        replacements = rules.groups[original]
        if not replacements:
            raise RewriteError(
                f"owner ID {original!r} has no allowed replacement but appears on a retained interaction; "
                f"cannot issue a cassette free of the original owner ID (page turn {page_turn!r})",
            )
        candidates[original] = replacements

    matchers = _all_group_matchers(rules)
    before = _match_matrix(interactions, matchers)
    applicable_permits = _applicable_permits(rules, interactions)
    viable = []
    for chosen in product(*(candidates[original] for original in present)):
        group_values = dict(zip(present, chosen, strict=True))
        manifest_groups = tuple(
            rules.groups[original].index(group_values[original])
            for original in rules.group_order
            if original in group_values
        )
        for permits in _permit_subsets_by_size(applicable_permits):
            candidate = _deep_copy_interactions(interactions)
            apply_group_assignment(candidate, rules, group_values)
            _apply_permits(candidate, permits)
            after = _match_matrix(candidate, matchers)
            if any(matched and not before[pair] for pair, matched in after.items()):
                continue
            # The issued cassette may carry no original owner ID, including at
            # locations only an individual permit can scrub. This is what makes
            # a permit necessary and drives the minimum-permit ranking.
            if _leaks_original_id(rules, candidate):
                continue
            manifest_permits = tuple(rules.permits.index(permit) for permit in permits)
            viable.append(
                Assignment(
                    group_values=group_values,
                    permits_used=permits,
                    permit_count=len(permits),
                    manifest_key=(manifest_groups, manifest_permits),
                ),
            )
            break  # smallest permit subset for this group assignment
    if not viable:
        raise RewriteError(
            "every allowed owner-ID assignment either keeps an original owner ID or creates a new "
            "request collision under a group's matchers (including retained pages vs. newly recorded "
            f"ones) at page turn {page_turn!r}",
        )
    viable.sort(key=lambda assignment: (assignment.permit_count, assignment.manifest_key))
    return viable[0]


def rewrite_for_issuance(rules, interactions, page_turn="<unknown>"):
    """Return deep-copied interactions rewritten with the safest assignment."""
    assignment = find_safe_assignment(rules, interactions, page_turn=page_turn)
    issued = _deep_copy_interactions(interactions)
    apply_group_assignment(issued, rules, assignment.group_values)
    _apply_permits(issued, assignment.permits_used)
    _assert_no_original_id(rules, issued)
    return issued, assignment


def _leaks_original_id(rules, interactions):
    """True if any group location still holds its original ID, or a permit
    location still holds the value its permit is allowed to replace."""
    for location, original in rules.locations.items():
        for interaction in interactions:
            value = get_location_value((interaction.request, interaction.response), location)
            if value is not None and original in str(value):
                return True
    for permit in rules.permits:
        for interaction in interactions:
            value = get_location_value((interaction.request, interaction.response), permit.location)
            if value is not None and value == permit.original:
                return True
    return False


def _assert_no_original_id(rules, interactions):
    if not _leaks_original_id(rules, interactions):
        return
    for location, original in rules.locations.items():
        for interaction in interactions:
            value = get_location_value((interaction.request, interaction.response), location)
            if value is not None and original in str(value):
                raise RewriteError(
                    f"issued cassette would still carry original owner ID {original!r} at {location} "
                    f"on {interaction.label}",
                )
    for permit in rules.permits:
        for interaction in interactions:
            value = get_location_value((interaction.request, interaction.response), permit.location)
            if value is not None and value == permit.original:
                raise RewriteError(
                    f"issued cassette would still carry the original value {permit.original!r} "
                    f"at {permit.location} on {interaction.label}",
                )
