"""Offline vendor-report preview selection.

The entry point :func:`build_preview` picks one fully renderable report body
from a vendor MIME message and the original images its ``cid:`` references
need.  Selection is deliberately scoped to the report's own
``multipart/related`` tree: images attached to or forwarded outside that
tree can never fill a missing reference.

The wire structures handled here are defined by RFC 2045 (media types and
dispositions), RFC 2046 (the ``multipart/related``, ``multipart/alternative``
and ``multipart/mixed`` containers) and RFC 2392 (``cid:`` URLs).  Only
``img`` ``src`` references are inspected, after URL-unescaping, and matched
against normalized ``Content-ID`` values.
"""

from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import unquote

from mailparser.utils import ported_string

# Metadata lookups stay defensive: a malformed parameter must not abort the
# whole preview, it only removes the offending part from selection.
_METADATA_ERRORS = (LookupError, UnicodeError, ValueError, TypeError)


def _safe_content_type(part):
    """Return a part's lowercased media type, or an empty string on failure."""
    try:
        return ported_string(part.get_content_type()).lower()
    except _METADATA_ERRORS:
        return ""


def _safe_disposition(part):
    """Return a part's lowercased disposition, or None when unset/invalid."""
    try:
        disposition = part.get_content_disposition()
    except _METADATA_ERRORS:
        return None
    return ported_string(disposition).lower() if disposition else None


def _safe_charset(part, failobj):
    """Return the declared charset, falling back on malformed parameters."""
    try:
        return part.get_content_charset(failobj)
    except _METADATA_ERRORS:
        return failobj


def normalize_cid(value):
    """Normalize a Content-ID or cid: reference for comparison.

    RFC 2392 uses the RFC 2045 msg-id form with angle brackets; obsolete or
    malformed messages may omit them.  Surrounding whitespace and a single
    angle-bracket pair are stripped.  Comparison stays case-sensitive, as
    msg-ids are.

    Args:
        value (str): raw Content-ID header value or cid URL tail.

    Returns:
        str: the normalized content identifier.
    """
    cid = ported_string(value).strip()
    if len(cid) >= 2 and cid.startswith("<") and cid.endswith(">"):
        cid = cid[1:-1].strip()
    return cid


@dataclass
class _Node:
    """One MIME part with its selection-relevant metadata and children."""

    part: object
    path: tuple
    content_type: str
    disposition: str
    children: list = field(default_factory=list)
    cid: str = None
    payload: object = None


def _build_tree(part, path):
    """Wrap a Message and its descendants in :class:`_Node` instances.

    The walk is iterative even though MIME nesting can be deep: a crafted
    multipart chain must not turn preview selection into a recursion crash.
    Leaf payloads are transfer-decoded once, here, so selection never pays
    decoding cost for parts it does not return.

    Args:
        part (email.message.Message): MIME part to wrap.
        path (tuple): zero-based child positions from the root message.

    Returns:
        _Node: the wrapped subtree.
    """
    root = _Node(
        part=part,
        path=path,
        content_type=_safe_content_type(part),
        disposition=_safe_disposition(part),
    )
    pending = [root]
    while pending:
        node = pending.pop()
        if not node.part.is_multipart():
            raw_cid = node.part.get("Content-ID")
            if raw_cid is not None:
                node.cid = normalize_cid(raw_cid)
            payload = node.part.get_payload(decode=True)
            if isinstance(payload, str):
                # A string payload means the message was fed as text with
                # a 7bit CTE: latin-1 recovers the exact wire octets for
                # image bytes.
                payload = payload.encode("latin-1")
            node.payload = payload if payload is not None else b""
            continue
        for position, child_part in enumerate(node.part.get_payload()):
            child = _Node(
                part=child_part,
                path=node.path + (position,),
                content_type=_safe_content_type(child_part),
                disposition=_safe_disposition(child_part),
            )
            node.children.append(child)
            pending.append(child)
    return root


def _find_report_related(root):
    """Locate the report's multipart/related container.

    Two vendor layouts are accepted: the outermost part is itself
    ``multipart/related``, or an outer ``multipart/mixed`` has exactly one
    direct ``multipart/related`` child that is not marked as an attachment.

    Args:
        root (_Node): wrapped root message.

    Returns:
        _Node: the related container, or None when the layout does not match.
    """
    if root.content_type == "multipart/related":
        return root
    if root.content_type != "multipart/mixed":
        return None
    candidates = [
        child
        for child in root.children
        if child.content_type == "multipart/related"
        and child.disposition != "attachment"
    ]
    return candidates[0] if len(candidates) == 1 else None


def _select_related_root(related):
    """Resolve the related root per its ``start`` parameter.

    A ``start`` parameter points at a direct child by Content-ID; without
    one the first direct child is the root (RFC 2387 section 3.1).

    Args:
        related (_Node): the related container.

    Returns:
        _Node: the root child, or None when start names no direct child.
    """
    if not related.children:
        return None
    try:
        start = related.part.get_param("start")
    except _METADATA_ERRORS:
        start = None
    if not start:
        return related.children[0]
    target = normalize_cid(start)
    for child in related.children:
        raw_cid = child.part.get("Content-ID")
        if raw_cid is not None and normalize_cid(raw_cid) == target:
            return child
    return None


class _CidCollector(HTMLParser):
    """Collect ``cid:`` URLs from img src attributes, in document order."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._references = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "img":
            return
        for name, value in attrs:
            if name.lower() == "src" and value:
                self._collect(value)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def _collect(self, src):
        if src[:4].lower() != "cid:":
            return
        # RFC 2392 allows percent-escaped octets in the cid URL; undo URL
        # escaping before comparing with the Content-ID header.
        self._references.append(normalize_cid(unquote(src[4:])))

    @property
    def references(self):
        """Return the deduplicated cid references in first-seen order."""
        ordered = []
        seen = set()
        for cid in self._references:
            if cid not in seen:
                seen.add(cid)
                ordered.append(cid)
        return ordered


def _extract_html_cids(part):
    """Extract the distinct img cid references of an HTML part.

    Args:
        part (email.message.Message): text/html MIME part.

    Returns:
        list[str]: deduplicated cid identifiers in document order.
    """
    payload = part.get_payload(decode=True)
    if payload is None:
        return []
    if isinstance(payload, bytes):
        charset = _safe_charset(part, "utf-8")
        try:
            text = payload.decode(charset)
        except (LookupError, UnicodeError, ValueError):
            text = payload.decode("utf-8", "replace")
    else:
        text = payload
    collector = _CidCollector()
    collector.feed(text)
    collector.close()
    return collector.references


@dataclass
class _Body:
    """A selectable body candidate."""

    kind: str
    node: _Node
    cids: list


def _body_candidates(node):
    """Flatten the root's HTML and plain text alternatives.

    The walk is iterative so a deep alternative chain cannot exhaust the
    call stack.

    Args:
        node (_Node): the related root (HTML, plain or multipart/alternative).

    Returns:
        list[_Body]: body candidates in wire order.
    """
    candidates = []
    pending = [node]
    while pending:
        current = pending.pop()
        if current.content_type == "text/html":
            candidates.append(_Body("html", current, _extract_html_cids(current.part)))
        elif current.content_type == "text/plain":
            candidates.append(_Body("plain", current, []))
        elif current.content_type == "multipart/alternative":
            # Reverse push keeps wire order in the output list.
            pending.extend(reversed(current.children))
    return candidates


@dataclass
class _Image:
    """One selectable image occurrence."""

    cid: str
    node: _Node


@dataclass
class _Branch:
    """One alternative branch: a single image or a pack used together."""

    images: list


@dataclass
class _Option:
    """One section's resource alternatives, in wire order.

    Each ``multipart/alternative`` inside a section is one alternative;
    exactly one of its branches is selected.  Branches also keep wire
    order, which drives the preference rules.
    """

    alternatives: list


def _collect_branch_images(node):
    """Gather the cid-bearing image leaves inside one branch.

    Branches are either a single image part or a multipart/mixed pack of
    images.  A branch that contains no referenceable image is unusable.
    The walk is iterative to survive deeply nested packs.

    Args:
        node (_Node): branch root.

    Returns:
        list[_Image]: cid-bearing images in wire order, or None if unusable.
    """
    images = []
    pending = [node]
    while pending:
        current = pending.pop()
        if not current.part.is_multipart():
            if current.content_type.startswith("image/") and current.cid:
                images.append(_Image(current.cid, current))
                continue
            return None
        if current.content_type != "multipart/mixed":
            return None
        # Reverse push keeps wire order when popping from the stack.
        pending.extend(reversed(current.children))
    return images or None


def _extract_resources(related):
    """Index direct images and the per-section resource alternatives.

    Args:
        related (_Node): the report's related container.

    Returns:
        tuple: (direct images cid -> list[_Node], options, index cid ->
        list of (section, media type))
    """
    direct = {}
    options = []
    index = {}
    section = 0
    for child in related.children:
        if not child.part.is_multipart():
            if child.content_type.startswith("image/") and child.cid:
                direct.setdefault(child.cid, []).append(child)
                index.setdefault(child.cid, []).append((None, child.content_type))
            continue
        if child.content_type != "multipart/mixed":
            continue
        # Section numbers follow wire position, even when a section holds
        # no usable alternative, so diagnostic indices stay stable.
        wire_section = section
        section += 1
        alternatives = []
        for alternative in child.children:
            if alternative.content_type != "multipart/alternative":
                continue
            branches = []
            for branch_node in alternative.children:
                images = _collect_branch_images(branch_node)
                if images is not None:
                    branches.append(_Branch(images))
            if branches:
                alternatives.append(branches)
        if not alternatives:
            continue
        for branches in alternatives:
            for branch in branches:
                for image in branch.images:
                    index.setdefault(image.cid, []).append(
                        (wire_section, image.node.content_type)
                    )
        options.append(_Option(alternatives))
    return direct, options, index


def _intrinsic_branch(branch, required, allowed):
    """Validate a branch independently of choices made in other sections.

    Referenced cids must have an allowed media type and appear at most once
    inside the branch pack.

    Args:
        branch (_Branch): candidate branch.
        required (frozenset): cids referenced by the body.
        allowed (frozenset): permitted image media types.

    Returns:
        dict: cid -> image for referenced images, or None when invalid.
    """
    selected = {}
    for image in branch.images:
        if image.cid not in required:
            continue
        if image.node.content_type not in allowed:
            return None
        if image.cid in selected:
            return None
        selected[image.cid] = image.node
    return selected


def _solve(required, allowed, direct, options):
    """Search branch assignments covering every referenced cid exactly once.

    Each section contributes one branch from each of its resource
    alternatives (an alternative without a valid branch simply supplies
    none).  Earlier sections dominate the preference order; within one
    alternative its last wire branch is preferred.  Depth-first search in
    that order returns the lexicographically preferred assignment, and
    suffix coverage prunes branches whose remaining requirements no later
    section can satisfy.

    Args:
        required (list[str]): body cid references, first-seen order.
        allowed (frozenset): permitted image media types.
        direct (dict): cid -> direct image nodes inside the related.
        options (list[_Option]): per-section resource alternatives.

    Returns:
        tuple: (cid -> image node mapping, deepest missing cids) on success
        the second element is None; otherwise (None, missing cids).
    """
    environment = {}
    for cid in required:
        copies = direct.get(cid, [])
        if len(copies) > 1:
            # Two direct images share one cid: nothing defines which one
            # the body means, so exact-once selection cannot be decided.
            return None, list(required)
        if len(copies) == 1:
            node = copies[0]
            if node.content_type in allowed:
                environment[cid] = node
            # A disallowed direct image does not poison the cid: a section
            # branch may still offer an allowed copy.  When none does, the
            # diagnostics builder reports the blocked media type.

    # Per section, one validated branch per alternative, in wire order.
    sections = []
    for option in options:
        validated = []
        for alternative in option.alternatives:
            branches = []
            for branch in alternative:
                intrinsic = _intrinsic_branch(branch, frozenset(required), allowed)
                if intrinsic is not None:
                    branches.append(intrinsic)
            if branches:
                validated.append(branches)
        sections.append(validated)

    coverage = [set() for _ in range(len(sections) + 1)]
    for position in range(len(sections) - 1, -1, -1):
        coverage[position] = set(coverage[position + 1])
        for alternative in sections[position]:
            for branch in alternative:
                coverage[position].update(branch)

    work = dict(environment)
    deepest = {"depth": -1, "blocked": []}

    def record_block(position, uncoverable):
        """Keep the blocker set found at the deepest search point."""
        if position > deepest["depth"]:
            deepest["depth"] = position
            deepest["blocked"] = list(uncoverable)

    def search(position):
        missing = [cid for cid in required if cid not in work]
        if position == len(sections):
            if missing:
                record_block(position, missing)
            return None if missing else True
        uncoverable = [cid for cid in missing if cid not in coverage[position]]
        if uncoverable:
            record_block(position, uncoverable)
            return None
        alternatives = sections[position]

        def choose_alternative(index):
            if index == len(alternatives):
                return search(position + 1)
            # Last wire branch first: each alternative prefers its later
            # branch.  Earlier alternatives get their preferred branch
            # before later alternatives are even tried.
            for branch in reversed(alternatives[index]):
                if any(cid in work for cid in branch):
                    continue
                work.update(branch)
                result = choose_alternative(index + 1)
                if result is not None:
                    return result
                for cid in branch:
                    work.pop(cid, None)
            return None

        result = choose_alternative(0)
        if result is None and deepest["depth"] < position:
            # Every branch combination failed here without a coverage
            # prune: the still-missing cids pin down the conflict.
            record_block(
                position,
                [cid for cid in required if cid not in work],
            )
        return result

    if search(0) is not None:
        return work, None
    return None, deepest["blocked"]


def _sections_for(index, cids):
    """Return sorted section indices that offer any of the cids."""
    sections = set()
    for cid in cids:
        for section, _ in index.get(cid, []):
            if section is not None:
                sections.add(section)
    return sorted(sections)


def _failure_diagnostics(required, allowed, index, dead_end):
    """Explain why a body cannot be rendered completely.

    Args:
        required (list[str]): referenced cids.
        allowed (frozenset): permitted image media types.
        index (dict): cid -> (section, media type) occurrences.
        dead_end (list[str]): cids unsatisfied at the deepest search point.

    Returns:
        list[dict]: structured missing/conflict diagnostics.
    """
    missing = []
    unsupported = []
    media_types = set()
    for cid in required:
        occurrences = index.get(cid)
        if not occurrences:
            missing.append(cid)
            continue
        if all(media_type not in allowed for _, media_type in occurrences):
            unsupported.append(cid)
            media_types.update(media_type for _, media_type in occurrences)
    diagnostics = []
    if missing:
        diagnostics.append({"reason": "missing-image", "cids": missing, "sections": []})
    if unsupported:
        diagnostics.append(
            {
                "reason": "media-type-not-allowed",
                "cids": unsupported,
                "sections": _sections_for(index, unsupported),
                "media_types": sorted(media_types),
            }
        )
    blocked = [cid for cid in dead_end if cid not in missing and cid not in unsupported]
    if blocked:
        diagnostics.append(
            {
                "reason": "unsatisfiable-cids",
                "cids": blocked,
                "sections": _sections_for(index, blocked),
            }
        )
    return diagnostics


def _image_result(cid, node):
    """Build the caller-facing image entry with original bytes."""
    return {
        "cid": cid,
        "part_path": node.path,
        "media_type": node.content_type,
        "payload": node.payload,
    }


def build_preview(message, allowed_image_types):
    """Select one fully renderable report body and its original images.

    The report is the outermost ``multipart/related`` part, or the unique
    non-attachment direct ``multipart/related`` child of an outer
    ``multipart/mixed``.  Its root follows the related ``start`` parameter
    or defaults to the first child, and may be HTML or a
    ``multipart/alternative`` of HTML and plain text.

    HTML candidates are tried from the last to the first; the first one
    whose every img ``cid:`` reference resolves to exactly one allowed image
    inside the related wins.  Resource alternatives inside per-section
    ``multipart/mixed`` containers follow wire order, preferring the last
    branch of each alternative.  Plain text is used only when no HTML body
    can be completed.  Existing ``body`` and ``attachments`` parsing is
    untouched.

    Args:
        message (email.message.Message): parsed MIME message.
        allowed_image_types (iterable[str]): permitted image media types,
            for example ``("image/png", "image/svg+xml")``.

    Returns:
        dict: ``available`` flags whether a body was selected.  On success
        ``body`` gives the selected part's media type, Content-ID and MIME
        position and ``images`` maps each referenced cid to its original
        part position, media type and transfer-decoded bytes, ordered by
        first reference.  On failure ``body`` and ``images`` are empty and
        ``unavailable`` explains the missing or conflicting cids, including
        the sections involved.

    Raises:
        TypeError: if ``allowed_image_types`` is a string rather than a
            collection of media types.
    """
    if isinstance(allowed_image_types, str):
        raise TypeError(
            "allowed_image_types must be a collection of media type strings"
        )
    allowed = frozenset(media_type.lower() for media_type in allowed_image_types)

    failure = {
        "available": False,
        "body": None,
        "images": [],
        "unavailable": [],
    }
    if message is None:
        failure["unavailable"].append({"reason": "report-structure-not-found"})
        return failure

    related = _find_report_related(_build_tree(message, ()))
    if related is None:
        failure["unavailable"].append({"reason": "report-structure-not-found"})
        return failure
    root = _select_related_root(related)
    if root is None:
        failure["unavailable"].append({"reason": "related-root-not-found"})
        return failure

    candidates = _body_candidates(root)
    html_candidates = [body for body in candidates if body.kind == "html"]
    plain_candidates = [body for body in candidates if body.kind == "plain"]
    if not html_candidates and not plain_candidates:
        failure["unavailable"].append({"reason": "report-body-not-found"})
        return failure
    direct, options, index = _extract_resources(related)

    diagnostics = []
    for body in reversed(html_candidates):
        selected, dead_end = _solve(body.cids, allowed, direct, options)
        if selected is not None:
            images = [
                _image_result(cid, selected[cid])
                for cid in body.cids
                if cid in selected
            ]
            return {
                "available": True,
                "body": {
                    "media_type": "text/html",
                    "part_path": body.node.path,
                    "content_id": body.node.cid,
                },
                "images": images,
                "unavailable": [],
            }
        diagnostics.extend(_failure_diagnostics(body.cids, allowed, index, dead_end))

    if plain_candidates:
        body = plain_candidates[-1]
        return {
            "available": True,
            "body": {
                "media_type": "text/plain",
                "part_path": body.node.path,
                "content_id": body.node.cid,
            },
            "images": [],
            "unavailable": _merge_diagnostics(diagnostics),
        }

    failure["unavailable"] = _merge_diagnostics(diagnostics)
    if not failure["unavailable"]:
        failure["unavailable"].append({"reason": "report-body-not-found"})
    return failure


def _merge_diagnostics(diagnostics):
    """Merge per-body diagnostics into one entry per reason."""
    merged = {}
    for entry in diagnostics:
        reason = entry["reason"]
        target = merged.setdefault(
            reason,
            {
                "reason": reason,
                "cids": [],
                "sections": [],
            },
        )
        for cid in entry.get("cids", []):
            if cid not in target["cids"]:
                target["cids"].append(cid)
        for section in entry.get("sections", []):
            if section not in target["sections"]:
                target["sections"].append(section)
        if "media_types" in entry:
            target["media_types"] = sorted(
                set(target.get("media_types", [])) | set(entry["media_types"])
            )
    return list(merged.values())
